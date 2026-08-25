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
1. AT ENGAGE the set speed is seeded from the current posted limit, replacing upstream's fixed
   `V_CRUISE_INITIAL*` constants (40, or 105 in experimental mode) — including on a RES/resume
   engage, which the user chose deliberately. If there is NO map data the set speed is left
   exactly as upstream set it; the seed simply waits for a real limit. There is deliberately no
   fallback value (see the note at the seeding branch in `update`).
2. THEREAFTER a limit change is adopted automatically iff BOTH of:
     a. the set speed is a multiple of 10 (60, 70, 80 ... 120) — a non-round value is taken as
        hand-tuned and is never overwritten silently;
     b. the change is within ±20 km/h.
   Otherwise the new limit is offered as a PENDING confirmation for 10 s, accepted with the
   button that matches the direction of travel (RES/+ for a higher limit, SET/- for a lower).

   Ownership is deliberately NOT a condition (removed 2026-08-06): a driver-set 110 must
   auto-adopt a 120 limit, and rule 2a already protects a hand-tuned 103 or 116.

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

PENDING_TIMEOUT_S = 10.0

# How long before a prompt the driver never answered is offered again, while the mismatch
# persists. Without this, one ignored prompt kills the feature for the whole drive: the limit
# would stay `already_handled` forever and the set speed could sit far below the posted limit
# with the driver believing the feature was managing it. A deliberate SET/- decline is NOT
# re-offered — that was an answer, not a missed prompt.
REOFFER_S = 60.0

# Confirmation prompts are live: the alert reaches the driver via grtSetSpeedState ->
# selfdrived -> AlertManager. Set False to fall back to ignoring out-of-band changes.
PENDING_ENABLED = True

# A new limit must hold this long CONTINUOUSLY before it is acted on.
#
# Raised 1.0 -> 3.0 on drive evidence. At a spot where a freeway and a parallel service road
# overlap, mapd alternated `speedLimit` between 60 and 120 every 1-2 s while `waySelectionType`
# churned current -> possible -> extended. At 1.0 s BOTH values qualified as "stable", so the
# feature offered 120 (wrong road) and then retired the offer 0.2-0.3 s later when the reading
# flipped back — the prompt appeared and vanished before the driver could react. A stability
# requirement on the RETIREMENT cannot fix that, because the 60 is equally stable; the gate
# itself has to outlast the alternation.
#
# Cost: a genuine sign change is acted on ~3 s late (~50 m at 60 km/h). Acceptable because the
# APPROACH profile, not this gate, is what shapes the run-up to a sign.
LIMIT_STABLE_S = 3.0

# Floats here have been through round(), += and clip(). Never compare them with ==: one
# 99.99999 would silently end tracking forever, and the symptom would look like "the feature
# got annoying" rather than a bug.
SPEED_EPS_KPH = 0.5

# Plausibility band for a posted limit, in km/h. Anything outside is ignored outright rather
# than clamped — clamping would silently turn a garbage value into a legal set speed. This also
# traps a units error: mapd publishes m/s, so a km/h value leaking through reads ~3.6x high.
# The cap on what the FEATURE may command — NOT a cap on the driver (operator, 2026-08-19).
# V_CRUISE_MAX stays at upstream's 145 so the steering-wheel +/- buttons can go above this; the
# automatic path (seed, auto-adopt, confirm) never commands more than AUTO_MAX_KPH.
AUTO_MAX_KPH = 110.0

MIN_LIMIT_KPH = 20.0
# Deliberately a literal, NOT float(V_CRUISE_MAX). V_CRUISE_MAX is the operator's cap on how fast
# they will let the car travel (110); this is "is that a plausible POSTED LIMIT?". Tying them
# together would make a real 120 limit read as implausible and silently switch the feature off on
# exactly the roads it matters most, with the heartbeat reporting a reassuring `implausible_limit`.
# A 120 limit is recognised here and then clamped to AUTO_MAX_KPH by _clamped().
MAX_LIMIT_KPH = 145.0

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
#
# 2.0 -> 10.0 on 2026-08-25. At 2 s the heartbeat was 98% of the file (3906 of the last 4000
# lines were `action: idle`) and the log had reached 17.1 MB. The heartbeat exists to explain a
# road test where nothing fired, not to narrate ordinary driving; 10 s still catches which gate
# is rejecting while cutting the volume 5x. Decisions are logged separately and are unaffected.
HEARTBEAT_S = 10.0

# ROTATION. Added 2026-08-25 — until then `_write` was a bare append with NO cap of any kind,
# so this file grew without bound (measured ~0.44 MB per engaged hour).
#
# The sibling failure is instructive and is why rotation is preferred to a hard cap: the
# TEMPORARY `cruise_log.py` recorder had a 50 MB cap and, on reaching it, latched itself off
# SILENTLY. Every row it wrote afterwards was stale, and that stale data made a replay claim a
# 110 km/h set speed on a road where the car was on 60. A cap that stops writing without saying
# so is worse than no cap. So: roll over, keep exactly one previous file, and write a line
# recording that it happened.
#
# Worst case on disk is _MAX_BYTES * 2 (the live file plus one rolled), i.e. bounded and
# predictable, which a 90%-full /data/media needs.
_MAX_BYTES = 8 * 1024 * 1024      # 8 MB live, 16 MB total worst case
_ROTATED_SUFFIX = ".1"


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
    # Exactly two facts, each written in exactly one place:
    #   _owned_kph    — the set speed WE last established (seed, adopt, or driver matching the
    #                   limit by hand). Written only by _take_ownership().
    #   _in_force_kph — the posted limit currently in force. Written only where the stability
    #                   gate passes, as a fact about the road, independent of what we decide.
    # Keeping them separate and single-writer is deliberate: the first version maintained them
    # on some code paths and not others, and both bugs found in review lived in those seams.
    self._owned_kph: float | None = None
    self._in_force_kph: float | None = None
    # The debounced limit — the value that has actually held for LIMIT_STABLE_S. Written only by
    # _track_established(). Decisions are made on this, never on a raw frame reading.
    self._established_kph: float | None = None
    # The limit the driver (or the auto rules) has AUTHORISED. scc_map obeys only this, so an
    # unanswered or declined change means the car keeps the previous limit. Written only by
    # _authorise().
    self._authorised_kph: float | None = None
    # An UPCOMING limit pre-authorised for the approach ramp only (never the ceiling).
    self._authorised_next_kph: float | None = None

    # --- engage seeding --------------------------------------------------------------------
    self._seed_pending = False
    self._seed_frames = 0

    # --- stability gate --------------------------------------------------------------------
    self._acted_limit_kph: float | None = None   # limit value already decided on
    self._acted_frame = 0
    self._acted_declined = False                 # decided by the DRIVER, never re-offer
    self._cand_limit_kph: float | None = None
    self._cand_frames = 0

    # --- confirmation ----------------------------------------------------------------------
    self.pending_limit_kph: float | None = None
    self._pending_frames = 0
    # Direction of the offer, captured when it was MADE. Decides which button accepts and which
    # declines, so a set-speed nudge mid-window cannot invert the buttons under the driver.
    self._pending_is_increase = False

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
    self._in_force_kph = None
    self._established_kph = None
    self._authorised_kph = None
    self._authorised_next_kph = None
    self._seed_pending = False
    self._seed_frames = 0
    self._acted_limit_kph = None
    self._acted_frame = 0
    self._acted_declined = False
    self._cand_limit_kph = None
    self._cand_frames = 0
    self.pending_limit_kph = None
    self._pending_frames = 0
    self._pending_is_increase = False
    self.tracking = False

  def _preauthorise_upcoming(self, sm, v_cruise_kph: float) -> None:
    """Pre-authorise an UPCOMING limit when it would be auto-adopted anyway.

    Why this exists (operator, 2026-07-30): gating physical compliance on authorisation switched
    the pre-sign approach ramp off, because an upcoming limit cannot be authorised before it
    becomes current. But when the auto rules already say "no confirmation needed", there is
    nothing to wait for — so authorise it early and let `scc_map` shape the run-up at
    APPROACH_DECEL instead of stepping at the sign.

    Deliberately published in its OWN field: this must unlock the ramp only, never the ceiling.
    If the ceiling saw it, the target would drop to the new limit while the sign was still far
    away — exactly the harsh step the ramp exists to prevent.

    The rules are the same three as a normal auto-adopt, so a change that WOULD prompt is not
    silently pre-authorised here.
    """
    self._authorised_next_kph = None
    # INVARIANT: nothing is pre-authorised while a prompt is open. The operator must be able to
    # rely on "the car keeps doing what it was doing until I answer" — so this returns before it
    # can unlock the approach ramp, whatever the upcoming limit is.
    if self.pending_limit_kph is not None:
      return
    try:
      mapd = sm['mapdOut']
      nl = round(float(mapd.nextSpeedLimit) * 3.6)
      nd = float(mapd.nextSpeedLimitDistance)
    except Exception:
      return
    if nd <= 0 or not (MIN_LIMIT_KPH <= nl <= MAX_LIMIT_KPH):
      return
    if self._way not in GOOD_WAY_SELECTION:
      return
    # Only while the set speed is still OURS. If the driver has dialled in their own number, the
    # map may not pull them below it even transiently (operator, 2026-08-07). Safe to use the
    # time-varying ownership term HERE — unlike the auto rule, a stale reading only means "no ramp
    # shaping", never "the car acted while the display waited".
    # Cost, stated plainly: after any manual set-speed change the ramp stays off until the feature
    # re-takes ownership (via `at_limit` or the next adopt).
    if not _near(v_cruise_kph, self._owned_kph):
      return
    delta = nl - v_cruise_kph
    if delta > 0 and self._way not in GOOD_WAY_SELECTION_UP:
      return
    if _is_round(v_cruise_kph) and abs(delta) <= AUTO_ADOPT_BAND_KPH:   # same rule as auto-adopt
      self._authorised_next_kph = float(nl)

  @property
  def authorised_next_limit_kph(self) -> float:
    """Upcoming limit pre-authorised for the approach ramp only. 0.0 = none."""
    return float(self._authorised_next_kph or 0.0)

  @property
  def authorised_limit_kph(self) -> float:
    """Published to plannerd. 0.0 means 'nothing authorised' — scc_map then FAILS OPEN and keeps
    obeying mapd directly, so a dropped message can never silently disable limit compliance."""
    return float(self._authorised_kph or 0.0)

  def _track_established(self, limit_kph) -> str:
    """Maintain the debounced 'established limit'. The ONLY writer of `_established_kph`.

    A value must hold CONTINUOUSLY for LIMIT_STABLE_S to become established. Losing the limit
    resets the candidate but deliberately does NOT clear `_established_kph`: a road that flickers
    in and out must not re-trigger anything.

    Returns a heartbeat reason: `no_limit` / `new_candidate` / `settling` / `established`.
    """
    if limit_kph is None:
      self._cand_limit_kph = None
      self._cand_frames = 0
      return "no_limit"

    if limit_kph != self._cand_limit_kph:
      self._cand_limit_kph = limit_kph
      self._cand_frames = 0
      return "new_candidate"

    self._cand_frames += 1
    if self._cand_frames < int(LIMIT_STABLE_S / DT_CTRL):
      return "settling"

    self._established_kph = limit_kph
    return "established"

  def _take_ownership(self, v_set: float) -> None:
    """Record that this set speed is ours, not the driver's. The ONLY writer of _owned_kph."""
    self._owned_kph = v_set

  def _authorise(self, limit_kph: float) -> None:
    """Record that the driver (or the auto rules) has AUTHORISED this limit.

    Published as `grtSetSpeedState.authorisedLimit` and consumed by `scc_map` in plannerd, which
    obeys only authorised limits. The ONLY writer of `_authorised_kph`.
    """
    self._authorised_kph = limit_kph

  def _mark_acted(self, limit_kph: float, declined: bool = False) -> None:
    """Record a decision about a limit VALUE so it is not re-decided every frame."""
    self._acted_limit_kph = limit_kph
    self._acted_frame = self.frame
    self._acted_declined = declined

  @staticmethod
  def _clamped(limit_kph: float, cap: bool = True) -> float:
    """`cap=True` (the automatic path: seed and auto-adopt) stops at AUTO_MAX_KPH — the feature
    never SILENTLY commands more than that. `cap=False` is for a limit the driver explicitly
    CONFIRMED with the +/- buttons: the prompt named a number, so accepting must deliver that
    number, and a button press is exactly the manual override the cap is not meant to block.
    Either way the result is still clamped to upstream's V_CRUISE_MAX.
    Plain min/max so this module has no numpy dependency and stays testable off-device."""
    ceiling = min(AUTO_MAX_KPH, V_CRUISE_MAX) if cap else V_CRUISE_MAX
    return float(min(max(round(limit_kph, 1), V_CRUISE_MIN), ceiling))

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
    return _near(v_set, self._in_force_kph) or _near(v_set, self._owned_kph)

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
      # NO FALLBACK VALUE. There used to be one (60 km/h after a 10 s timeout) and it was
      # removed on 2026-08-03 as problematic: with no map fix — which is the normal state when
      # engaging from standstill, and the persistent state anywhere off-tile — it forced the set
      # speed to 60 regardless of the road, so engaging at highway speed dropped it to 60 and the
      # car braked for a limit that was never posted. The feature must only ever set the set
      # speed to a limit that actually exists.
      #
      # So: keep waiting. Upstream's own V_CRUISE_INITIAL* stands until a real limit turns up,
      # and if none ever does the feature simply stays out of the way for the whole drive.
      if self._cruise_button_event(CS):
        # The driver dialled their own speed while we were waiting: it is theirs now. Stop
        # seeding, and let the normal ownership rules decide anything that follows.
        self._seed_pending = False
        self._log("seed_abandoned", limit_kph, v_cruise_kph, None, why="driver_set_own_speed")
        return v_cruise_kph
      self._heartbeat(f"seeding:{reason}", v_cruise_kph, limit_kph)
      return v_cruise_kph

    if v_cruise_kph == V_CRUISE_UNSET or v_cruise_kph <= 0:
      return v_cruise_kph

    self.tracking = self._owns(v_cruise_kph)

    # Stability tracking runs EVERY frame, including while a prompt is open. That is not
    # incidental: the stale test in the pending block needs to know when a DIFFERENT limit has
    # become established in its own right, and it cannot know that if the tracker stops while an
    # offer is outstanding.
    gate = self._track_established(limit_kph)

    # Pre-authorise an upcoming limit that needs no confirmation, so the approach ramp can shape
    # the run-up. Runs every frame, including while a prompt is open for the CURRENT limit.
    self._preauthorise_upcoming(sm, v_cruise_kph)

    # --- a pending limit is awaiting confirmation --------------------------------------------
    if self.pending_limit_kph is not None:
      self._pending_frames += 1

      # DIRECTION-MATCHED CONFIRMATION (operator's spec, 2026-07-30): you push the switch the way
      # the speed is going. A higher pending limit is accepted with RES/+ and declined with SET/-;
      # a lower one is accepted with SET/- and declined with RES/+. The direction is the one
      # captured when the offer was MADE — it is what the driver was shown — so a set-speed nudge
      # mid-window cannot invert the meaning of the buttons under them.
      accept_btn = "accelCruise" if self._pending_is_increase else "decelCruise"
      reject_btn = "decelCruise" if self._pending_is_increase else "accelCruise"

      if self._button(CS, accept_btn):
        adopted = self._clamped(self.pending_limit_kph, cap=False)
        self._take_ownership(adopted)
        self._authorise(self.pending_limit_kph)
        self.pending_limit_kph = None
        self._log("confirm", limit_kph, v_cruise_kph, adopted)
        return adopted
      if self._button(CS, reject_btn):
        # An explicit answer, so it is never re-offered.
        self._mark_acted(self.pending_limit_kph, declined=True)
        self._log("decline", limit_kph, v_cruise_kph, None)
        self.pending_limit_kph = None
        self._pending_frames = 0
        return v_cruise_kph

      # An offer is retired as stale ONLY once a DIFFERENT limit has become established in its
      # own right — i.e. has passed the full stability gate. Retiring on a single differing frame
      # is what killed prompts in 0.2-0.3 s at a flip-flopping way selection; the offer is a
      # question about a value that WAS stable, and a transient reading does not answer it.
      if (self._established_kph is not None and
          not _near(self._established_kph, self.pending_limit_kph)):
        self._log("stale", limit_kph, v_cruise_kph, None)
        self.pending_limit_kph = None
        self._pending_frames = 0
        return v_cruise_kph

      if self._pending_frames > int(PENDING_TIMEOUT_S / DT_CTRL):
        # Unanswered, not declined: re-offered after REOFFER_S while the mismatch persists.
        self._mark_acted(self.pending_limit_kph)
        self._log("expire", limit_kph, v_cruise_kph, None)
        self.pending_limit_kph = None
        self._pending_frames = 0
      return v_cruise_kph

    # --- act on the ESTABLISHED limit --------------------------------------------------------
    if limit_kph is None:
      self._heartbeat(reason, v_cruise_kph)
      return v_cruise_kph
    if gate != "established":
      self._heartbeat(gate, v_cruise_kph, limit_kph)
      return v_cruise_kph

    # Decide on the value that PASSED the gate, never on the raw frame value.
    limit_kph = self._established_kph

    # The established limit IS the one in force — a fact about the road, independent of what we
    # go on to decide. This is the single place that fact is recorded; `tracking` above was
    # computed against the PREVIOUS one, which is what the ownership test needs.
    self._in_force_kph = limit_kph

    # Compare the CLAMPED limit: on a 120 road with V_CRUISE_MAX = 110 the set speed can never
    # equal the raw limit, and without this the tracker would re-decide every REOFFER_S forever.
    if _near(self._clamped(limit_kph), v_cruise_kph):
      # Already at the limit (or at the cap): nothing to change, and this re-establishes tracking
      # for a driver who dialled the posted number in by hand — so the NEXT change auto-adopts.
      self._take_ownership(v_cruise_kph)
      self._mark_acted(limit_kph)
      # The set speed already equals the posted limit, so it is accepted by construction.
      self._authorise(limit_kph)
      self.tracking = True
      self._heartbeat("at_limit", v_cruise_kph, limit_kph)
      return v_cruise_kph

    if limit_kph == self._acted_limit_kph:
      # Re-offer a prompt the driver never answered, once the cooldown has passed. Without this
      # a single missed prompt would leave the set speed stranded — 60 in a 100 zone — with the
      # heartbeat reporting a benign-looking `already_handled` for the rest of the drive.
      stale_decision = (not self._acted_declined and
                        self.frame - self._acted_frame >= int(REOFFER_S / DT_CTRL))
      if not stale_decision:
        self._heartbeat("already_handled", v_cruise_kph, limit_kph)
        return v_cruise_kph
      self._acted_limit_kph = None

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

    # THE CAP MUST NOT CLAW BACK A DELIBERATE MANUAL CHOICE (operator, 2026-08-19).
    # If the driver has pushed the set speed above AUTO_MAX_KPH and the posted limit is ALSO
    # above it, then nothing about the road is asking them to slow — only our own cap would, and
    # they overrode it on purpose. Without this, a driver at 130 on a 120 road gets dragged to
    # 110, pushes + back to 130, and gets dragged again every REOFFER_S. Forever.
    #
    # Note the asymmetry: if the posted limit is BELOW the cap (a 60 zone), the ROAD is asking
    # for the reduction, so the normal rules apply and they get the usual prompt.
    # `limit_kph <= v_cruise_kph` matters: if the road OFFERS more than the driver is doing
    # (116 with a 120 limit) there is a real decision to put to them, and confirming it bypasses
    # the cap. Only suppress when the road is not offering an increase and our cap alone would
    # pull them down.
    if (v_cruise_kph > AUTO_MAX_KPH and limit_kph > AUTO_MAX_KPH
        and limit_kph <= v_cruise_kph):
      self._mark_acted(limit_kph)
      self._log("above_auto_cap", limit_kph, v_cruise_kph, None,
                why="driver_chose_above_cap_and_road_does_not_ask")
      return v_cruise_kph

    self._mark_acted(limit_kph)

    # AUTO-ADOPT RULE (operator, 2026-08-06). Exactly two conditions:
    #   * the set speed is a multiple of 10 — that IS the "did the driver hand-tune this?" test;
    #   * the change is within +/-20 km/h.
    # The ownership test (`self.tracking`) was a THIRD condition I had added and the operator had
    # not asked for. It made a driver-set 110 prompt for a 120 limit, which is the reported bug.
    # Removing it also closes a real seam: `tracking` was the only TIME-VARYING term, and it was
    # evaluated once for the upcoming limit (unlocking the ramp) and again when that limit became
    # current (deciding prompt vs auto). If it flipped in between, the car slowed while the
    # display waited. Roundness and delta cannot disagree that way.
    auto = _is_round(v_cruise_kph) and abs(delta) <= AUTO_ADOPT_BAND_KPH
    if auto:
      adopted = self._clamped(limit_kph)
      self._take_ownership(adopted)
      self._authorise(limit_kph)
      self._log("adopt", limit_kph, v_cruise_kph, adopted)
      return adopted

    why = self._why_not_auto(v_cruise_kph, delta)
    if PENDING_ENABLED:
      self.pending_limit_kph = limit_kph
      self._pending_is_increase = delta > 0
      self._pending_frames = 0
      # _preauthorise_upcoming() already ran THIS frame, before the prompt existed. Revoke it now
      # rather than waiting for the next frame to notice.
      self._authorised_next_kph = None
      self._log("pending", limit_kph, v_cruise_kph, None, why=why)
    else:
      self._log("ignore", limit_kph, v_cruise_kph, None, why=why)
    return v_cruise_kph

  def _why_not_auto(self, v_set: float, delta: float) -> str:
    # NB: ownership is no longer a reason to prompt (2026-08-06). `tracking` is still published
    # and logged, but only as instrumentation — do not reintroduce it here.
    if not _is_round(v_set):
      return "set_speed_not_multiple_of_10"
    return f"delta_{abs(delta):.0f}_over_{AUTO_ADOPT_BAND_KPH:.0f}"

  def _seed(self, limit_kph: float, action: str, v_cruise_kph: float) -> float:
    """Establish the set speed at engage, from a real posted limit.

    Only ever called with a limit that passed `_read_limit`, so seeding IS an adoption: the
    limit is in force, decided, and authorised for `scc_map` from the first frame.
    """
    self._seed_pending = False
    self._seed_frames = 0
    seeded = self._clamped(limit_kph)
    self._take_ownership(seeded)
    self._in_force_kph = limit_kph
    self._established_kph = limit_kph
    self._mark_acted(limit_kph)
    self._authorise(limit_kph)
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
      "in_force_kph": self._in_force_kph,
      "acted_kph": self._acted_limit_kph,
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
    """Append one JSON line, rolling the file over at _MAX_BYTES.

    The size check costs a stat per write, which is free here: `_write` is reached at most
    once per HEARTBEAT_S plus once per decision, NOT once per frame — this is a 100 Hz loop
    and writing on every frame is exactly what the throttling upstream exists to prevent.

    A rollover is RECORDED as its own line rather than happening silently. See the comment on
    _MAX_BYTES for why that matters.
    """
    if _DEBUG_LOG is None:
      return
    try:
      rolled = False
      try:
        if os.path.getsize(_DEBUG_LOG) >= _MAX_BYTES:
          os.replace(_DEBUG_LOG, _DEBUG_LOG + _ROTATED_SUFFIX)
          rolled = True
      except FileNotFoundError:
        pass                                   # first write, or someone removed it underneath
      with open(_DEBUG_LOG, "a") as f:
        if rolled:
          f.write(json.dumps({"t": time.monotonic(), "action": "rotated",
                              "why": f"reached {_MAX_BYTES} bytes; previous kept as "
                                     f"{os.path.basename(_DEBUG_LOG) + _ROTATED_SUFFIX}"}) + "\n")
        f.write(json.dumps(record) + "\n")
    except OSError:
      pass
