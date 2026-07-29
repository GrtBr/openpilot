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
(adopting a higher limit raises the set speed). It is behind its own flag,
`SmartCruiseControlSetSpeed`, default OFF, separate from `SmartCruiseControlMap`.

Agreed behaviour (user spec, 2026-07-29)
----------------------------------------
1. AT ENGAGE the set speed is seeded from the current posted limit, or 60 km/h if there is no
   map data. This replaces upstream's fixed `V_CRUISE_INITIAL*` constants (40, or 105 in
   experimental mode) — including on a RES/resume engage, which the user chose deliberately.
2. THEREAFTER a limit change is adopted automatically only if ALL of:
     a. the feature still OWNS the set speed — it equals the limit in force, or the value we
        ourselves last wrote (so a driver who dials in their own number is never overridden);
     b. the set speed is a multiple of 10 (60, 70, 80 ... 120) — a non-round value is a
        hand-tuned one;
     c. the change is within ±20 km/h.
   Otherwise the new limit is offered as a PENDING confirmation for 10 s and adopted only if
   the driver taps RES/+ in that window.

The >20 km/h rule is an absolute safety floor: it applies even while the feature owns the set
speed. On these roads limits of 20 and 40 exist, so 120 -> 80 and 60 -> 20 both prompt.

Timing note: this runs in `card`, a 100 Hz loop (DT_CTRL), NOT the 20 Hz model rate the rest of
grt/ uses. All frame counts here are in DT_CTRL units.
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

# Auto-adopt band, symmetric. Beyond this the driver is asked, always.
AUTO_ADOPT_BAND_KPH = 20.0

# "a factor of 10 (60,70,80...100,110,120)" — a set speed that is not a round number is taken as
# hand-tuned, so it is never overwritten silently. Verified against 11 MB of on-device
# mapd_debug.log that the limits on these roads are only ever 20/40/60/80/120, so this test
# never latches the feature into permanent-prompt mode by itself.
ROUND_STEP_KPH = 10.0

# Set speed at engage when there is genuinely no map data.
NO_MAP_DEFAULT_KPH = 60.0

# How long to wait after engage for a trusted limit before falling back to NO_MAP_DEFAULT_KPH.
# Engaging from standstill reports waySelectionType=fail (vEgo=0, bearing=0), so without this
# window every drive would start on 60 and then immediately prompt to move to the real limit.
# Upstream's own initial value stands during the window.
SEED_TIMEOUT_S = 10.0

PENDING_TIMEOUT_S = 10.0

# Confirmation prompts are live: the alert reaches the driver via grtSetSpeedState ->
# selfdrived -> AlertManager. Set False to fall back to ignoring out-of-band changes.
PENDING_ENABLED = True

# A new limit must hold this long before it is acted on. mapdOut arrives at 20 Hz, so card sees
# each value repeated ~5 frames regardless; this is about way-selection flapping at junctions.
LIMIT_STABLE_S = 1.0

# Floats here have been through round(), += and clip(). Never compare them with ==: one
# 99.99999 would silently end tracking forever, and the symptom would look like "the feature
# got annoying" rather than a bug.
SPEED_EPS_KPH = 0.5

# Plausibility band for a posted limit, in km/h. Anything outside is ignored outright rather
# than clamped — clamping would silently turn a garbage value into a legal set speed. This also
# traps a units error: mapd publishes m/s, so a km/h value leaking through reads ~3.6x high.
MIN_LIMIT_KPH = 20.0
MAX_LIMIT_KPH = float(V_CRUISE_MAX)

# waySelectionType values that mean mapd actually knows which way we are on. `fail` is what it
# reports while parked. The second tier is a deliberate asymmetry: `predicted` is mapd GUESSING
# which way we will take at a junction. Acting on a guess to slow down is conservative; acting
# on it to speed up is not, so an upward auto-adopt waits for `current`/`extended`.
GOOD_WAY_SELECTION = ("current", "predicted", "extended")
GOOD_WAY_SELECTION_UP = ("current", "extended")

_DEBUG_LOG = os.path.join(GRT_CONFIG_DIR, "set_speed.log") if platform.system() != "Darwin" else None

# Heartbeat: how often to log WHY nothing happened. Without this a road test where the feature
# never fires produces an empty log and no way to tell which gate rejected — and mapdOut is not
# in rlog on a prebuilt branch, so there is no retrospective path either.
HEARTBEAT_S = 2.0


def _near(a, b) -> bool:
  return a is not None and b is not None and abs(a - b) < SPEED_EPS_KPH


def _is_round(v: float) -> bool:
  return abs(v - round(v / ROUND_STEP_KPH) * ROUND_STEP_KPH) < SPEED_EPS_KPH


class SetSpeedLimitTracker:
  def __init__(self):
    self.params = Params()
    self.enabled = get_bool_safe(self.params, ENABLE_KEY)
    self.frame = -1

    # --- ownership -------------------------------------------------------------------------
    self._owned_kph: float | None = None      # the set speed WE last established
    self._prev_limit_kph: float | None = None  # the posted limit currently in force

    # --- engage seeding --------------------------------------------------------------------
    self._seed_pending = False
    self._seed_frames = 0

    # --- stability gate --------------------------------------------------------------------
    self._acted_limit_kph: float | None = None
    self._cand_limit_kph: float | None = None
    self._cand_frames = 0

    # --- confirmation ----------------------------------------------------------------------
    self.pending_limit_kph: float | None = None
    self._pending_frames = 0

    self.tracking = False
    self.last_action = ""
    self._way = ""
    self._raw_limit = 0.0
    self._hb_frame = -1

  # ---------------------------------------------------------------------------------------

  @property
  def pending_seconds_left(self) -> float:
    if self.pending_limit_kph is None:
      return 0.0
    return max(0.0, PENDING_TIMEOUT_S - self._pending_frames * DT_CTRL)

  def update_params(self) -> None:
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_CTRL) == 0:
      self.enabled = get_bool_safe(self.params, ENABLE_KEY)

  def _reset(self) -> None:
    self._owned_kph = None
    self._prev_limit_kph = None
    self._seed_pending = False
    self._seed_frames = 0
    self._acted_limit_kph = None
    self._cand_limit_kph = None
    self._cand_frames = 0
    self.pending_limit_kph = None
    self._pending_frames = 0
    self.tracking = False

  @staticmethod
  def _clamped(limit_kph: float) -> float:
    # Same result as upstream's `np.clip(round(v, 1), V_CRUISE_MIN, V_CRUISE_MAX)` for a scalar.
    # Plain min/max so this module has no numpy dependency and stays testable off-device.
    return float(min(max(round(limit_kph, 1), V_CRUISE_MIN), V_CRUISE_MAX))

  @staticmethod
  def _button(CS, which) -> bool:
    """`which` button released this frame.

    Release, not press, on purpose: upstream's `_update_v_cruise_non_pcm` acts on the same edge
    and adjusts the set speed. Our hook runs AFTER it in card.py and assigns an absolute value,
    so matching its edge means an adopted limit wins cleanly instead of landing on limit±1. On
    the Staria RES/+ maps to `accelCruise` (opendbc/car/hyundai/carstate.py BUTTONS_DICT).
    """
    from openpilot.selfdrive.car.cruise import ButtonType
    want = getattr(ButtonType, which)
    return any(b.type.raw == want and not b.pressed for b in CS.buttonEvents)

  @classmethod
  def _cruise_button_event(cls, CS) -> bool:
    """Any set-speed button activity this frame — we stay out of the driver's way on those."""
    from openpilot.selfdrive.car.cruise import ButtonType
    return any(b.type.raw in (ButtonType.accelCruise, ButtonType.decelCruise)
               for b in CS.buttonEvents)

  def _read_limit(self, sm) -> tuple[float | None, str]:
    """Posted limit in km/h from mapdOut, plus WHY it was rejected.

    The reason string is the whole point of the heartbeat log: on the road, "nothing happened"
    has several possible causes and they are indistinguishable without it.
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

  def _owns(self, v_set: float) -> bool:
    """Does the FEATURE still own the set speed, or has the driver dialled in their own?

    Two ways to own it, both from the user's spec: the set speed equals the limit in force
    ("auto change should only happen if the set max-speed = OSM map speed limit"), or it equals
    the value we ourselves last wrote (which covers the no-map 60 seed, and an adopted limit
    that has since been superseded).
    """
    return _near(v_set, self._prev_limit_kph) or _near(v_set, self._owned_kph)

  # ---------------------------------------------------------------------------------------

  def update(self, sm, CS, v_cruise_kph: float, enabled: bool, engage_edge: bool = False) -> float:
    """Return the set speed for this frame: `v_cruise_kph` unchanged, a seed, or an adopted limit.

    `enabled` is openpilot's engaged state (carControl.enabled), not the feature flag.
    `engage_edge` is True on the frame upstream ran `initialize_v_cruise`.
    """
    self.frame += 1
    self.update_params()

    if not self.enabled or not enabled:
      self._reset()
      return v_cruise_kph

    limit_kph, reason = self._read_limit(sm)

    # --- engage seeding ---------------------------------------------------------------------
    if engage_edge:
      self._reset()
      self._seed_pending = True

    if self._seed_pending:
      self._seed_frames += 1
      if limit_kph is not None:
        return self._seed(limit_kph, "seed_from_map", v_cruise_kph)
      if self._seed_frames >= int(SEED_TIMEOUT_S / DT_CTRL):
        return self._seed(NO_MAP_DEFAULT_KPH, "seed_no_map", v_cruise_kph)
      self._heartbeat(f"seeding:{reason}", v_cruise_kph, limit_kph)
      return v_cruise_kph

    if v_cruise_kph == V_CRUISE_UNSET or v_cruise_kph <= 0:
      return v_cruise_kph

    self.tracking = self._owns(v_cruise_kph)

    # --- a pending limit is awaiting confirmation --------------------------------------------
    if self.pending_limit_kph is not None:
      self._pending_frames += 1
      if self._button(CS, "accelCruise"):
        adopted = self._clamped(self.pending_limit_kph)
        self._owned_kph = adopted
        self._prev_limit_kph = self.pending_limit_kph
        self.pending_limit_kph = None
        self._log("confirm", limit_kph, v_cruise_kph, adopted)
        return adopted
      if self._button(CS, "decelCruise"):
        # Driver asserting their own speed instead — take that as a decline.
        self._log("decline", limit_kph, v_cruise_kph, None)
        self._retire_pending()
        return v_cruise_kph
      if limit_kph is not None and not _near(limit_kph, self.pending_limit_kph):
        # The road changed under us; the offer is stale. Let the gate re-decide for the new one.
        self._log("stale", limit_kph, v_cruise_kph, None)
        self._retire_pending()
        return v_cruise_kph
      if self._pending_frames > int(PENDING_TIMEOUT_S / DT_CTRL):
        self._log("expire", limit_kph, v_cruise_kph, None)
        self._retire_pending()
      return v_cruise_kph

    # --- stability gate ---------------------------------------------------------------------
    # A limit that is absent/untrusted does NOT clear `_acted_limit_kph`: losing a limit must
    # never trigger anything, and a road that flickers in and out must not re-adopt each time.
    #
    # Note the failure mode this creates and which the heartbeat exists to expose: the candidate
    # counter resets here, so a waySelectionType flickering between `current` and `fail` faster
    # than LIMIT_STABLE_S means a limit NEVER clears the gate, and nothing is logged as a
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

    # The limit is stable. It is now the one in force, whatever we decide to do about it.
    if _near(limit_kph, v_cruise_kph):
      # Already at the limit: nothing to change, and this re-establishes tracking for a driver
      # who dialled the posted number in by hand.
      self._prev_limit_kph = limit_kph
      self._acted_limit_kph = limit_kph
      self.tracking = True
      self._heartbeat("at_limit", v_cruise_kph, limit_kph)
      return v_cruise_kph

    if limit_kph == self._acted_limit_kph:
      self._heartbeat("already_handled", v_cruise_kph, limit_kph)
      return v_cruise_kph

    # --- one-shot decision for this limit value -----------------------------------------------
    # Never decide on a frame the driver is working the buttons: upstream is mid-adjustment and
    # an absolute assignment here would eat the press. `_cand_frames` uses `>=` above precisely
    # so this is a DEFERRAL to the next clear frame — with an exact `==` the decision would be
    # dropped permanently and silently. `_acted_limit_kph` is what makes it one-shot.
    if self._cruise_button_event(CS):
      self._heartbeat("driver_busy", v_cruise_kph, limit_kph)
      return v_cruise_kph

    delta = limit_kph - v_cruise_kph

    # Raising the set speed on a merely PREDICTED way is acting on a guess in the accelerating
    # direction. A DEFERRAL, not a decision: returns before `_acted_limit_kph` is set, so the
    # same limit is reconsidered once mapd settles on `current`.
    if delta > 0 and self._way not in GOOD_WAY_SELECTION_UP:
      self._heartbeat("defer_up_on_predicted", v_cruise_kph, limit_kph)
      return v_cruise_kph

    self._acted_limit_kph = limit_kph
    prev_limit = self._prev_limit_kph
    self._prev_limit_kph = limit_kph

    auto = self.tracking and _is_round(v_cruise_kph) and abs(delta) <= AUTO_ADOPT_BAND_KPH
    if auto:
      adopted = self._clamped(limit_kph)
      self._owned_kph = adopted
      self._log("adopt", limit_kph, v_cruise_kph, adopted)
      return adopted

    if PENDING_ENABLED:
      self.pending_limit_kph = limit_kph
      self._prev_limit_kph = prev_limit   # not in force until the driver decides
      self._pending_frames = 0
      self._log("pending", limit_kph, v_cruise_kph, None,
                why=self._why_not_auto(v_cruise_kph, delta))
    else:
      self._log("ignore", limit_kph, v_cruise_kph, None,
                why=self._why_not_auto(v_cruise_kph, delta))
    return v_cruise_kph

  def _why_not_auto(self, v_set: float, delta: float) -> str:
    if not self.tracking:
      return "driver_owns_set_speed"
    if not _is_round(v_set):
      return "set_speed_not_multiple_of_10"
    return f"delta_{abs(delta):.0f}_over_{AUTO_ADOPT_BAND_KPH:.0f}"

  def _retire_pending(self) -> None:
    self._acted_limit_kph = self.pending_limit_kph
    self.pending_limit_kph = None
    self._pending_frames = 0

  def _seed(self, limit_kph: float, action: str, v_cruise_kph: float) -> float:
    self._seed_pending = False
    self._seed_frames = 0
    seeded = self._clamped(limit_kph)
    self._owned_kph = seeded
    self._prev_limit_kph = limit_kph if action == "seed_from_map" else None
    self._acted_limit_kph = limit_kph if action == "seed_from_map" else None
    self.tracking = True
    self._log(action, limit_kph, v_cruise_kph, seeded)
    return seeded

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
      "tracking": self.tracking,
      "owned_kph": self._owned_kph,
      "prev_limit_kph": self._prev_limit_kph,
      "cand_frames": self._cand_frames,
    })

  def _log(self, action: str, limit_kph, v_cruise_kph: float, adopted, why: str = "") -> None:
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
      "why": why,
      "limit_kph": limit_kph,
      "v_cruise_kph": v_cruise_kph,
      "adopted_kph": adopted,
      "raw_limit_ms": round(self._raw_limit, 3),
      "way": self._way,
      "tracking": self.tracking,
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
