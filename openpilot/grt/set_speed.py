"""SetSpeedLimitTracker — make the cruise SET SPEED follow the posted limit from mapd.

This is a different thing from `scc_map.py`, and the difference matters:

  * `scc_map` lowers `v_cruise` INSIDE `longitudinal_planner`. That is a local planning
    variable. It shapes how the car drives but never reaches `carState.vCruise`, so neither
    the comma UI nor the Staria cluster ever changes. It is a ceiling, and it is transient
    (the approach profile deliberately commands intermediate speeds).
  * This module moves the DRIVER-FACING set speed — `VCruiseHelper.v_cruise_kph`, which becomes
    `carState.vCruise` -> the comma UI MAX readout, and `carState.vCruiseCluster` ->
    `hudControl.setSpeed` -> `hyundai/carcontroller.py set_speed_in_units` -> the car's own
    dash. So the number the driver reads changes.

Because of that this is the FIRST fork feature that can make the car ACCELERATE on OSM data
(adopting a higher limit raises the set speed). It is therefore:

  * behind its own flag, `SmartCruiseControlSetSpeed`, default OFF, separate from
    `SmartCruiseControlMap`;
  * edge-triggered on a CHANGE of posted limit, never continuous, so it cannot fight a driver
    who overrides the adopted value;
  * clamped to upstream's own `[V_CRUISE_MIN, V_CRUISE_MAX]` and to a plausible-limit band.

Agreed behaviour
----------------
  |new limit - current set speed| <= AUTO_ADOPT_BAND : adopt automatically, up AND down.
  |difference| >  AUTO_ADOPT_BAND                    : do NOT adopt. Hold the limit PENDING for
                                                       PENDING_TIMEOUT_S and adopt only if the
                                                       driver taps RES/+ within that window.

The pending window is implemented here and confirmed by RES/+, but the AUDIBLE/VISUAL alert for
it is NOT part of this module — see `pending_limit_kph` and the note in captains_log. Without a
notification a pending limit is invisible to the driver, so `PENDING_ENABLED` is False until an
alert mechanism is verified on device. With it False, a >band change is simply ignored, which is
the current (safe) behaviour of the car.

Timing note: this runs in `card`, which is a 100 Hz loop (DT_CTRL), NOT the 20 Hz model rate the
rest of grt/ uses. All frame counts here are in DT_CTRL units.
"""
import json
import os
import platform
import time

from openpilot.common.params import Params
from openpilot.common.realtime import DT_CTRL
from openpilot.grt.registry import GRT_CONFIG_DIR
from openpilot.grt.scc_map import PARAMS_UPDATE_PERIOD, get_bool_safe
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_MIN, V_CRUISE_UNSET

ENABLE_KEY = "SmartCruiseControlSetSpeed"

# Both directions get the same band, per the agreed behaviour. Kept as two constants so the
# upward (accelerating) direction can be tightened independently without touching the logic.
AUTO_ADOPT_BAND_UP_KPH = 20.0
AUTO_ADOPT_BAND_DOWN_KPH = 20.0

# >band changes: hold PENDING and wait for a RES/+ tap. OFF until an alert mechanism exists —
# a pending state the driver cannot perceive is worse than not offering one.
PENDING_ENABLED = False
PENDING_TIMEOUT_S = 10.0

# A new limit must hold this long before it is acted on. mapdOut arrives at 20 Hz, so card sees
# each value repeated ~5 frames regardless; this is about way-selection flapping at junctions,
# not about message rate.
LIMIT_STABLE_S = 1.0

# Plausibility band for a posted limit, in km/h. Anything outside is ignored outright rather
# than clamped — clamping would silently turn a garbage value into a legal set speed. This also
# traps a units error: mapd publishes m/s, so a km/h value leaking through reads as ~3.6x high.
MIN_LIMIT_KPH = 20.0
MAX_LIMIT_KPH = float(V_CRUISE_MAX)

# waySelectionType values that mean mapd actually knows which way we are on. `fail` (4) is what
# it reports while parked (vEgo=0, bearing=0), and adopting a limit off a failed selection would
# be adopting a limit for a road we may not be on.
#
# The two tiers are a deliberate asymmetry, not an oversight. `predicted` is mapd GUESSING which
# way we will take at a junction. Acting on a guess to SLOW DOWN is conservative; acting on it to
# SPEED UP is not — a mispredicted off-ramp would raise the set speed on a road we are not on.
# So upward adoption requires the way we are demonstrably on (`current`) or a continuation of it
# (`extended`). The ±20 km/h band bounds the damage either way; this bounds it further.
GOOD_WAY_SELECTION = ("current", "predicted", "extended")
GOOD_WAY_SELECTION_UP = ("current", "extended")

_DEBUG_LOG = os.path.join(GRT_CONFIG_DIR, "set_speed.log") if platform.system() != "Darwin" else None

# Heartbeat: how often to log WHY nothing happened. Without this a road test where the feature
# never fires produces an empty log and no way to tell which gate rejected — and mapdOut is not
# in rlog on a prebuilt branch, so there is no retrospective path either.
HEARTBEAT_S = 2.0


class SetSpeedLimitTracker:
  def __init__(self):
    self.params = Params()
    self.enabled = get_bool_safe(self.params, ENABLE_KEY)
    self.frame = -1

    self._acted_limit_kph: float | None = None   # limit value already handled; blocks re-trigger
    self._cand_limit_kph: float | None = None    # limit currently being held for stability
    self._cand_frames = 0

    self.pending_limit_kph: float | None = None  # >band limit awaiting a RES/+ confirmation
    self._pending_frames = 0

    self.last_action = ""                        # debug only: adopt / pending / confirm / expire
    self._way = ""                               # last waySelectionType seen, for the heartbeat
    self._raw_limit = 0.0                        # last mapdOut.speedLimit seen, in m/s
    self._hb_frame = -1

  # ---------------------------------------------------------------------------------------

  def update_params(self) -> None:
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_CTRL) == 0:
      self.enabled = get_bool_safe(self.params, ENABLE_KEY)

  def _reset(self) -> None:
    """Forget all state. Called whenever the feature cannot act (disengaged, disabled, ...).

    Deliberately clears `_acted_limit_kph` too: after a disengage/re-engage the driver's set
    speed may be anything, so the current limit deserves a fresh look.
    """
    self._acted_limit_kph = None
    self._cand_limit_kph = None
    self._cand_frames = 0
    self.pending_limit_kph = None
    self._pending_frames = 0

  @staticmethod
  def _clamped(limit_kph: float) -> float:
    # Same result as upstream's `np.clip(round(v, 1), V_CRUISE_MIN, V_CRUISE_MAX)` for a scalar.
    # Plain min/max so this module has no numpy dependency and stays testable off-device.
    return float(min(max(round(limit_kph, 1), V_CRUISE_MIN), V_CRUISE_MAX))

  @staticmethod
  def _accel_released(CS) -> bool:
    """RES/+ released this frame.

    Release, not press, on purpose: upstream's `_update_v_cruise_non_pcm` acts on the same edge
    and bumps the set speed by +1. Our hook runs AFTER it in card.py and assigns an absolute
    value, so matching its edge means our adopted limit wins cleanly instead of landing on
    limit+1. On the Staria RES/+ maps to `accelCruise`
    (opendbc/car/hyundai/carstate.py BUTTONS_DICT).
    """
    from openpilot.selfdrive.car.cruise import ButtonType
    return any(b.type.raw == ButtonType.accelCruise and not b.pressed for b in CS.buttonEvents)

  @staticmethod
  def _cruise_button_event(CS) -> bool:
    """Any set-speed button activity this frame — we stay out of the driver's way on those."""
    from openpilot.selfdrive.car.cruise import ButtonType
    return any(b.type.raw in (ButtonType.accelCruise, ButtonType.decelCruise)
               for b in CS.buttonEvents)

  def _read_limit(self, sm) -> tuple[float | None, str]:
    """Posted limit in km/h from mapdOut, plus WHY it was rejected.

    The reason string is the whole point of the heartbeat log: on the road, "nothing happened"
    has five possible causes and they are indistinguishable without it.
    """
    if not sm.alive.get('mapdOut', False):
      return None, "mapd_not_alive"
    if not sm.valid.get('mapdOut', True):
      return None, "mapd_not_valid"

    mapd = sm['mapdOut']
    self._way = str(mapd.waySelectionType)
    self._raw_limit = float(mapd.speedLimit)

    if not mapd.tileLoaded:
      return None, "no_tiles"
    if self._way not in GOOD_WAY_SELECTION:
      return None, f"way_{self._way}"

    limit_kph = round(self._raw_limit * 3.6)
    if limit_kph == 0:
      return None, "no_limit_posted"
    if not (MIN_LIMIT_KPH <= limit_kph <= MAX_LIMIT_KPH):
      return None, "implausible_limit"
    return float(limit_kph), "ok"

  # ---------------------------------------------------------------------------------------

  def update(self, sm, CS, v_cruise_kph: float, enabled: bool) -> float:
    """Return the set speed for this frame — either `v_cruise_kph` unchanged, or an adopted limit.

    `enabled` is openpilot's engaged state (carControl.enabled), not the feature flag.
    """
    self.frame += 1
    self.update_params()

    if not self.enabled or not enabled or v_cruise_kph == V_CRUISE_UNSET or v_cruise_kph <= 0:
      self._reset()
      return v_cruise_kph

    limit_kph, reason = self._read_limit(sm)

    # --- a pending limit is awaiting confirmation -------------------------------------------
    if self.pending_limit_kph is not None:
      self._pending_frames += 1
      if self._accel_released(CS):
        adopted = self._clamped(self.pending_limit_kph)
        self._log("confirm", limit_kph, v_cruise_kph, adopted)
        self.pending_limit_kph = None
        return adopted
      if self._pending_frames > int(PENDING_TIMEOUT_S / DT_CTRL):
        self._log("expire", limit_kph, v_cruise_kph, None)
        self.pending_limit_kph = None
      return v_cruise_kph

    # --- stability gate ---------------------------------------------------------------------
    # A limit that is absent/untrusted does NOT clear `_acted_limit_kph`: losing a limit must
    # never trigger anything, and a road that flickers in and out must not re-adopt each time.
    #
    # Note the failure mode this creates and which the heartbeat exists to expose: the candidate
    # counter resets here, so a waySelectionType flickering between `current` and `fail` faster
    # than LIMIT_STABLE_S means a limit NEVER clears the gate, and nothing is ever logged as a
    # decision.
    if limit_kph is None:
      self._cand_limit_kph = None
      self._cand_frames = 0
      self._heartbeat(reason, v_cruise_kph)
      return v_cruise_kph

    if limit_kph != self._cand_limit_kph:
      self._cand_limit_kph = limit_kph
      self._cand_frames = 0
      self._heartbeat("new_candidate", v_cruise_kph, limit_kph)
      return v_cruise_kph

    self._cand_frames += 1
    if self._cand_frames < int(LIMIT_STABLE_S / DT_CTRL):
      self._heartbeat("settling", v_cruise_kph, limit_kph)
      return v_cruise_kph
    if limit_kph == self._acted_limit_kph:
      self._heartbeat("already_handled", v_cruise_kph, limit_kph)
      return v_cruise_kph

    # --- one-shot decision for this limit value ----------------------------------------------
    # Never act on a frame the driver is working the buttons: upstream is mid-adjustment and an
    # absolute assignment here would eat the press. `_cand_frames` is compared with `>=` above
    # precisely so this is a DEFERRAL to the next clear frame — with an exact `==` the decision
    # would be dropped permanently and silently. `_acted_limit_kph` is what makes it one-shot.
    if self._cruise_button_event(CS):
      self._heartbeat("driver_busy", v_cruise_kph, limit_kph)
      return v_cruise_kph

    delta = limit_kph - v_cruise_kph
    band = AUTO_ADOPT_BAND_UP_KPH if delta > 0 else AUTO_ADOPT_BAND_DOWN_KPH

    # Raising the set speed on a merely PREDICTED way is acting on a guess in the accelerating
    # direction — see GOOD_WAY_SELECTION_UP. A DEFERRAL, like the button-busy case: this returns
    # before `_acted_limit_kph` is set, so the same limit is reconsidered once mapd settles on
    # `current`.
    if delta > 0 and self._way not in GOOD_WAY_SELECTION_UP:
      self._heartbeat("defer_up_on_predicted", v_cruise_kph, limit_kph)
      return v_cruise_kph

    self._acted_limit_kph = limit_kph

    if abs(delta) <= band:
      adopted = self._clamped(limit_kph)
      self._log("adopt", limit_kph, v_cruise_kph, adopted)
      return adopted

    if PENDING_ENABLED:
      self.pending_limit_kph = limit_kph
      self._pending_frames = 0
      self._log("pending", limit_kph, v_cruise_kph, None)
    else:
      self._log("ignore", limit_kph, v_cruise_kph, None)
    return v_cruise_kph

  # ---------------------------------------------------------------------------------------

  def _heartbeat(self, reason: str, v_cruise_kph: float, limit_kph=None) -> None:
    """Periodic 'why nothing happened' line. Throttled to HEARTBEAT_S; this is a 100 Hz loop.

    Only runs when the feature is enabled AND openpilot is engaged (the caller returns before
    this otherwise), so it cannot fill the disk while parked.
    """
    if _DEBUG_LOG is None or self.frame - self._hb_frame < int(HEARTBEAT_S / DT_CTRL):
      return
    self._hb_frame = self.frame
    self._write({
      "t": time.monotonic(),
      "action": "idle",
      "reason": reason,
      "v_cruise_kph": v_cruise_kph,
      "limit_kph": limit_kph,
      "raw_limit_ms": round(self._raw_limit, 3),
      "way": self._way,
      "cand_kph": self._cand_limit_kph,
      "cand_frames": self._cand_frames,
      "acted_kph": self._acted_limit_kph,
    })

  def _log(self, action: str, limit_kph, v_cruise_kph: float, adopted) -> None:
    """One line per DECISION, not per frame — this loop is 100 Hz.

    `mapdOut` is never written to rlog on a prebuilt branch (loggerd uses the stale compiled
    services.h), so a file is the only record of what the feature saw. The outcome, however,
    IS in rlog: carState.vCruise / vCruiseCluster.
    """
    self.last_action = action
    self._hb_frame = self.frame          # a decision counts as a heartbeat
    self._write({
      "t": time.monotonic(),
      "action": action,
      "limit_kph": limit_kph,
      "v_cruise_kph": v_cruise_kph,
      "adopted_kph": adopted,
      "raw_limit_ms": round(self._raw_limit, 3),
      "way": self._way,
    })

  @staticmethod
  def _write(record: dict) -> None:
    if _DEBUG_LOG is None:
      return
    try:
      with open(_DEBUG_LOG, "a") as f:
        f.write(json.dumps(record) + "\n")
    except OSError:
      pass
