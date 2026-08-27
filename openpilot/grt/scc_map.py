"""SmartCruiseControlMap — consumes mapdOut and produces longitudinal targets.

Ported from sunnypilot's
`sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py`.

The constants and comments here encode real tuning history from on-car testing. Values that
look arbitrary usually are not — read the comment before changing one.

Two outputs, consumed by openpilot/grt/hooks.py:
  * `output_v_target`      — speed ceiling (V_CRUISE_UNSET when inactive)
  * `output_hazard_accel`  — firm hazard decel, or None. (sunnypilot calls this
                             `output_a_min_override`; see hooks.py for why the injection
                             mechanism differs in this openpilot version.)

Differences from the sunnypilot original, all deliberate:
  * `MapState` is a local IntEnum (sunnypilot's lives in the LongitudinalPlanSP capnp schema,
    which this fork does not port).
  * The vestigial `LastGPSPosition` / `MapTargetVelocities` param reads are gone. Nothing
    downstream used them (mapd computes the curve speed itself and publishes mapCurveSpeed),
    and they would raise UnknownKeyName here.
  * Speed limits come from `mapdOut.speedLimitSuggestedSpeed` rather than a second python-side
    pre-braking integrator — see `update_calculations`.
"""
import json
import math
import platform
import time
from enum import IntEnum

import openpilot.cereal.messaging as messaging
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET

_DEBUG_LOG = "/data/media/0/mapd_debug.log" if platform.system() != "Darwin" else None

# Inlined from sunnypilot (openpilot.sunnypilot.PARAMS_UPDATE_PERIOD / smart_cruise_control.MIN_V)
PARAMS_UPDATE_PERIOD = 3  # seconds
MIN_V = 20 * CV.KPH_TO_MS  # do not operate under 20 km/h


def get_bool_safe(params, key: str) -> bool:
  """Read a fork bool setting: Params first, then a plain file, else False.

  openpilot's Params raises UnknownKeyName for any key not in the COMPILED params_keys.h
  table. Our keys live in grt_params_keys.inc, which only takes effect after a C++ rebuild —
  and `nightly-dev` is a PREBUILT branch that runs committed binaries and must not be built
  (see captains_log). So on device these keys always raise.

  Fallback: a plain file at <GRT_CONFIG_DIR>/<key> containing 1/true/on/yes. That directory
  lives outside /data/params, so Params::clear_all() can never delete it. Params is still
  preferred when available, so this keeps working unchanged if the keys are ever compiled in.

  Any failure at all yields False: a fork feature must never break the base system, so the
  failure mode is "disabled", not "crash".
  """
  try:
    return bool(params.get_bool(key))
  except Exception:
    pass
  try:
    import os
    from openpilot.grt.registry import GRT_CONFIG_DIR
    path = os.path.join(GRT_CONFIG_DIR, key)
    if os.path.isfile(path):
      with open(path) as f:
        return f.read().strip().lower() in ("1", "true", "on", "yes")
  except Exception:
    pass
  return False


class MapState(IntEnum):
  disabled = 0
  enabled = 1
  turning = 2
  overriding = 3


ACTIVE_STATES = (MapState.turning, )
ENABLED_STATES = (MapState.enabled, MapState.overriding, *ACTIVE_STATES)

# Jerk-limited pre-braking profile. Currently used only by the optional python-side
# speed-limit pre-braking path, which is DISABLED in favour of mapd's own internal lookahead
# (see update_calculations). Retained because it is the documented tuning baseline.
TARGET_JERK = -0.6   # m/s^3
TARGET_ACCEL = -1.2  # m/s^2 should match up with the long planner limit
TARGET_OFFSET = 3.0  # seconds before the sign at which the target velocity should be reached

# ---------------------------------------------------------------------------------------
# Approach shaping. THE key tuning knob for how a slow-down FEELS.
#
# Rather than commanding the final target speed the instant a hazard/limit comes into range
# (which makes the planner's P-controller saturate at A_CRUISE_MIN = -1.2 m/s^2 and reach the
# target far too early), we command the speed the car should be at RIGHT NOW in order to
# arrive at the target exactly AT the hazard:
#
#     v_now = sqrt(v_target^2 + 2 * APPROACH_DECEL * distance)
#
# Measured on the first working drive: approaches used -1.64 m/s^2 where only -0.17..-0.28 was
# needed (5.8x-10x too hard), reaching target up to 365 m early. This profile fixes both: the
# deceleration IS APPROACH_DECEL, and it lands on target at distance 0.
#
# Lower = gentler and starts braking earlier. 0.5 m/s^2 is ~3.3x gentler than what the first
# drive used. If a hazard appears late the formula self-escalates (small d -> low v_now ->
# harder braking), so safety is preserved.
APPROACH_DECEL = 0.5  # m/s^2

HAZARD_HOLD_DISTANCE = 10.0  # metres to hold target speed after passing hazard waypoint

# Hazard-specific deceleration constants. Kept separate from TARGET_ACCEL/TARGET_JERK so
# changing hazard pre-braking aggression doesn't affect speed-limit pre-braking or the
# in-flight curve braking.
HAZARD_TARGET_ACCEL = -0.6     # m/s² baseline hazard decel
HAZARD_REACH_BUFFER_M = 1.0    # minimal margin; engagement ≈ the pure kinematic decel distance
HAZARD_ACCEL_MIN = -1.5        # m/s² — hardest decel the adaptive loop may command
HAZARD_ACCEL_MAX = -0.3        # m/s² — softest decel; never command less than this floor
                               # (was -0.1; reverted because the looser floor let the planner
                               # coast in the final 50 m and the car couldn't reach target —
                               # 5/6 stop-sign approaches required driver brake)
HAZARD_ACCEL_RATE = 0.05       # m/s² per 50 ms frame — rate limit (1 m/s² per second)
LEAD_PAST_HAZARD_MARGIN_M = 10.0  # lead must be this far past the hazard to be ignored

# ---------------------------------------------------------------------------------------
# Upcoming-T-junction validation from mapd's OWN path geometry.
#
# The T-Junction flag in the tiles is computed by topology alone — road-name/ref grouping
# plus termination counts, with no geometry anywhere in the decision. Two false positives
# measured on the SA extract:
#
#   n36316730   (-33.8306696, 20.0808104)  R60-primary and R62 form a straight crossbar
#               (176.3 deg apart) and R60-secondary is the real stem. The detector groups
#               the two "R60" ways together and names R62 the stem, so the hazard is
#               INVERTED onto the through road: driving straight through fires 20 km/h.
#   n5999476430 (-34.0309952, 20.4399907)  the bidirectional N2 meeting its two one-way
#               NR2/4 carriageways, which diverge by 8.6 deg. Called a T with the N2 trunk
#               as the stem — 20 km/h on a national route.
#
# Both are rejected by asking what the tile cannot answer: does OUR path actually turn at
# that node? mapdExtendedOut.path is CurrentWay nodes + NextWays nodes in travel order
# (mapd_source/extended_state.go setPath), so the turn angle is computable on-device with
# no tile rebuild. A genuine stem->crossbar movement turns ~90 deg; going straight through
# a crossbar, or taking either leg of a carriageway split, does not.
#
# FAILS OPEN: no path, no GPS fix, a stale/short path, or any exception leaves the hazard
# honoured exactly as before. Suppression requires positive evidence that we go straight.
T_JUNCTION_HAZARD = "T-Junction"
TURN_HONOUR_DEG   = 45.0   # honour only if the path turns at least this much at the node
TURN_BASELINE_M   = 25.0   # bearing baseline either side; adjacent OSM nodes can be metres
                           # apart and their pairwise bearings are noise
TURN_MIN_PATH_PTS = 5
TURN_WINDOW_M     = 20.0   # search this far either side of the announced distance for
                           # the actual corner; nextHazardDistance does not land on a node
TURN_MAX_OFFPATH_M = 100.0 # if the nearest path point is further than this, we are not on
                           # the published path — do not trust the geometry


def _wrap180(deg: float) -> float:
  return (deg + 180.0) % 360.0 - 180.0


def _bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
  """Initial bearing a->b in degrees (0=N, 90=E)."""
  lat1, lon1 = math.radians(a[0]), math.radians(a[1])
  lat2, lon2 = math.radians(b[0]), math.radians(b[1])
  dlon = lon2 - lon1
  y = math.sin(dlon) * math.cos(lat2)
  x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
  return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _flat_dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
  """Local flat-earth distance. Exact enough over the tens of metres used here."""
  dlat = (b[0] - a[0]) * 111320.0
  dlon = (b[1] - a[1]) * 111320.0 * math.cos(math.radians((a[0] + b[0]) * 0.5))
  return math.hypot(dlat, dlon)


def path_turn_deg(coords: list[tuple[float, float]], car: tuple[float, float],
                  distance_m: float) -> float | None:
  """Sharpest heading change of `coords` near `distance_m` ahead of `car`.

  Pure geometry so it can be unit-tested against real node bearings. Returns None
  whenever the answer cannot be trusted — every such case means "honour the hazard".

  Why a WINDOW rather than the single point at `distance_m`: nextHazardDistance is a
  path distance that will not land exactly on a node, and floating-point accumulation
  can put the naive "first point at or beyond d" one node PAST the junction — which
  measures a chord ACROSS the corner instead of the turn AT it, and understates a 103.5
  deg stem turn as 74.7. Scanning a window and taking the sharpest corner is immune to
  both, and answers the question we actually care about: does the path turn near the
  announced hazard?
  """
  n = len(coords)
  if n < TURN_MIN_PATH_PTS or distance_m <= 0.0:
    return None

  best_i, best_d = 0, float("inf")
  for i, c in enumerate(coords):
    d = _flat_dist_m(car, c)
    if d < best_d:
      best_d, best_i = d, i
  if best_d > TURN_MAX_OFFPATH_M:
    return None

  # cumulative path distance ahead of the car
  cum = [0.0] * n
  for i in range(best_i + 1, n):
    cum[i] = cum[i - 1] + _flat_dist_m(coords[i - 1], coords[i])
  if cum[n - 1] < distance_m - TURN_WINDOW_M:
    return None                       # hazard lies beyond the published path

  lo, hi = distance_m - TURN_WINDOW_M, distance_m + TURN_WINDOW_M
  best_turn = None
  for j in range(best_i + 1, n - 1):
    if cum[j] < lo:
      continue
    if cum[j] > hi:
      break
    # step back / forward by the baseline so bearings are not node-spacing noise
    a, back = j, 0.0
    while a > 0 and back < TURN_BASELINE_M:
      back += _flat_dist_m(coords[a - 1], coords[a])
      a -= 1
    c, fwd = j, 0.0
    while c + 1 < n and fwd < TURN_BASELINE_M:
      fwd += _flat_dist_m(coords[c], coords[c + 1])
      c += 1
    if a == j or c == j:
      continue
    turn = abs(_wrap180(_bearing_deg(coords[j], coords[c]) - _bearing_deg(coords[a], coords[j])))
    if best_turn is None or turn > best_turn:
      best_turn = turn

  return best_turn


# OSM hazard string -> target speed (m/s). 5.55 m/s = 20 km/h, 4.16 m/s = 15 km/h.
_HAZARD_SPEED_TARGETS = {
  "stop": 5.55, "give_way": 5.55, "roundabout": 5.55,
  "mini_roundabout": 5.55, "turning_circle": 5.55,
  "T-Junction": 5.55,
  "toll_booth": 5.55, "level_crossing": 5.55, "railway_crossing": 5.55,
  "traffic_calming": 4.16,
}


def approach_speed(v_target: float, distance: float, decel: float = APPROACH_DECEL) -> float:
  """Speed to hold NOW so that decelerating at `decel` reaches `v_target` exactly at `distance`.

  At distance 0 this returns v_target exactly; far away it returns a high value (no effect).
  """
  return math.sqrt(max(0.0, v_target * v_target + 2.0 * max(0.0, decel) * max(0.0, distance)))


def calculate_accel(t, target_jerk, a_ego):
  return a_ego + target_jerk * t


def calculate_velocity(t, target_jerk, a_ego, v_ego):
  return v_ego + a_ego * t + target_jerk / 2 * (t ** 2)


def calculate_distance(t, target_jerk, a_ego, v_ego):
  return t * v_ego + a_ego / 2 * (t ** 2) + target_jerk / 6 * (t ** 3)


class SmartCruiseControlMap:
  def __init__(self):
    self.params = Params()
    self.enabled = get_bool_safe(self.params, "SmartCruiseControlMap")
    self.long_enabled = False
    self.long_override = False
    self.is_enabled = False
    self.is_active = False
    self.state = MapState.disabled
    self.v_cruise = 0.0
    self.frame = -1

    self.v_target = 0.0
    self.v_ego = 0.0
    self.a_ego = 0.0
    self.output_v_target = V_CRUISE_UNSET
    self.output_hazard_accel = None

    self._curve_ceiling = 0.0     # rate-limited map-curve ceiling (m/s); 0 = no curve

    # T-junction turn test, latched per announcement (see path_turn_deg)
    self._tj_active = False
    self._tj_turn_deg = None
    self._tj_suppressed = False
    self._tj_errors = 0
    self._tj_next_try = 0

    self.hazard_speed_target = 0.0
    self.hazard_hold_m = 0.0
    self.hazard_active = False
    self._hazard_engaged = False                          # sticky latch — prevents gate oscillation
    self._adaptive_hazard_accel = HAZARD_TARGET_ACCEL
    self._prev_hazard_distance = 0.0
    self._has_lead = False

    # debug-only snapshots
    self._dbg = {}

  def get_v_target_from_control(self) -> float:
    if self.is_active:
      return max(self.v_target, MIN_V)
    return V_CRUISE_UNSET

  def update_params(self) -> None:
    if self.frame % int(PARAMS_UPDATE_PERIOD / DT_MDL) == 0:
      self.enabled = get_bool_safe(self.params, "SmartCruiseControlMap")

  def _update_t_junction_gate(self, sm, hazard_str, distance_m):
    """Latch a suppress/honour decision for the CURRENT T-Junction announcement.

    Returns the measured turn angle in degrees, or None while it is still unknown.

    Decided once per announcement rather than every frame: mapdExtendedOut refreshes at
    1 Hz, so re-deciding at 20 Hz would only add jitter, and the junction geometry does
    not change as we approach it.

    Every failure path leaves `_tj_suppressed` False, i.e. the hazard is honoured exactly
    as before. Suppression requires a positive measurement that our path goes straight.
    """
    if str(hazard_str) != T_JUNCTION_HAZARD:
      self._tj_active = False
      self._tj_turn_deg = None
      self._tj_suppressed = False
      return None

    if not self._tj_active:                 # rising edge of a new announcement
      self._tj_active = True
      self._tj_turn_deg = None
      self._tj_suppressed = False
      self._tj_next_try = 0                 # evaluate immediately on the rising edge

    if self._tj_turn_deg is not None:
      return self._tj_turn_deg              # already decided for this announcement

    # Retry no faster than mapdExtendedOut actually refreshes (1 Hz). path_turn_deg scans
    # the whole published path, and this runs inside plannerd's 20 Hz realtime loop — an
    # unresolvable announcement must not rescan a multi-hundred-point path every frame.
    if self.frame < self._tj_next_try:
      return None
    self._tj_next_try = self.frame + int(1.0 / DT_MDL)

    try:
      gps = sm['gpsLocationExternal']
      car = (float(gps.latitude), float(gps.longitude))
      if abs(car[0]) < 0.01 and abs(car[1]) < 0.01:
        return None                         # no fix -> honour
      coords = [(float(pt.latitude), float(pt.longitude))
                for pt in sm['mapdExtendedOut'].path]
      turn = path_turn_deg(coords, car, distance_m)
    except Exception:
      # Never let a fork geometry check take down longitudinal planning. Counted so a
      # silently-dead gate is visible in mapd_debug.log rather than looking like "no
      # T-junctions today" — the failure silhouette that cost a whole drive before.
      self._tj_errors += 1
      return None

    if turn is None:
      return None                           # not resolvable yet -> honour, retry next frame
    self._tj_turn_deg = turn
    self._tj_suppressed = turn < TURN_HONOUR_DEG
    return turn

  def _ramp_curve_ceiling(self, map_curve_speed: float) -> float:
    """Rate-limit how fast the map-curve ceiling may DESCEND, to APPROACH_DECEL.

    Returns the ceiling to command this frame (m/s), or 0.0 when there is no curve.

    WHY. mapd publishes mapCurveSpeed as a step. Measured on route 00000072
    (2026-08-05 11:43:18, no lead, no hazard): the ceiling fell 85.2 -> 57.2 km/h in a single
    frame at 72.7 km/h, and the planner's P-controller
    a_cruise = clip(v_cruise - v_ego, A_CRUISE_MIN=-1.2, ...) saturated on the spot —
    -1.34 m/s^2 with -2.33 m/s^3 of jerk, where mapd's own trigger distance had been sized for
    -0.26 m/s^2. Across that day's drives 70% of curve steps hit A_CRUISE_MIN. This is the same
    pathology approach_speed() fixed for hazards and speed limits on 2026-07-29.

    WHY NOT approach_speed(). That profile needs a DISTANCE, and mapdOut carries none for
    curves — only nextSpeedLimitDistance / nextHazardDistance / nextAdvisorySpeedDistance.
    Shaping in the time domain instead needs no distance and gives the same result: the ceiling
    descends at APPROACH_DECEL, so the planner sees a small error each frame instead of one big
    one.

    Properties, each asserted by test:
      * Rises are NOT limited. When the curve ends the ceiling lifts immediately, so this can
        never hold the car back.
      * The ramp is anchored at v_ego, never at the previous ceiling. A ceiling above the
        current speed cannot brake, so ramping down from a stale high value (85 km/h above)
        would burn seconds doing nothing before braking started.
      * Self-escalating, so authority is never reduced. The ceiling descends on its own clock
        whether or not the car keeps up; a curve closer than the ramp assumes simply grows the
        v_cruise - v_ego error and the planner brakes harder. Only the onset transient is
        softened — the floor is still A_CRUISE_MIN.
      * It still lands in time. mapd's trigger distance includes a
        target_speed_time_offset = 4 s margin; the ramp needs Δv / APPROACH_DECEL plus ~1 s to
        build the tracking error (5.8 s for the measured median Δv of 8.7 km/h) against the
        5.5 s mapd planned, so the ceiling reaches target inside that margin.
    """
    if map_curve_speed <= 0.0:
      self._curve_ceiling = 0.0
      return 0.0

    # Never carry a ceiling higher than what could actually bind this frame.
    cap = max(self.v_ego, map_curve_speed)
    prev = self._curve_ceiling if self._curve_ceiling > 0.0 else cap
    prev = min(prev, cap)

    self._curve_ceiling = max(map_curve_speed, prev - APPROACH_DECEL * DT_MDL)
    return self._curve_ceiling

  def update_calculations(self, sm: messaging.SubMaster) -> None:
    # Reset each frame; the hazard / hold branches below set it if the hazard logic ends up
    # owning v_target. Gates the firm hazard decel output.
    self.hazard_active = False

    mapd = sm['mapdOut']

    # mapd's UpdateCurveSpeed() handles path distance, the 3-phase jerk profile,
    # TriggerDistance hysteresis and CURVE_CALC_OFFSET — but it publishes the result as a STEP,
    # and the step is what the driver feels. See _ramp_curve_ceiling().
    map_curve_speed = float(mapd.mapCurveSpeed)
    self.v_target = self._ramp_curve_ceiling(map_curve_speed)

    # --- speed limits (Phase 6) ---
    # mapd already folds the posted limit, speed_limit_offset, hold-last-seen AND the
    # next-limit lookahead + jerk profile (speed_limit.go SuggestNewSpeedLimit) into
    # speedLimitSuggestedSpeed, which state.go sets unconditionally. Take it as one more
    # candidate. Deliberately NOT re-implementing sunnypilot's nextSpeedLimit /
    # nextSpeedLimitDistance pre-braking block: it duplicates that lookahead, and two
    # integrators fight over the same slow-down. If mapd's pre-braking feels too gentle,
    # tune MapdSettings — do not add a second python-side integrator here.
    # Note: mapdOut.suggestedSpeed is deliberately NOT used; it already folds in vCruise and
    # the curve speeds with mapd's own priority rules, which would fight this arbitration.
    # AUTHORISATION GATE (2026-07-30, on drive evidence). The set-speed feature asks the driver
    # before changing the set speed for an out-of-band limit — but this controller was obeying the
    # posted limit physically regardless, so the car slowed for a sign while the display still
    # showed the old set speed awaiting confirmation. Measured: 1,069 frames where the posted-limit
    # ceiling was the binding source, plus 95 frames of pre-sign approach ramp.
    #
    # So when the set-speed feature is ACTIVE, obey only the limit it has authorised. When it is
    # not active (flag off, not engaged, or the message is missing/stale) FAIL OPEN to mapd's own
    # value — infrastructure failure must not silently stop the car obeying speed limits.
    authorised_ms, gated, authorised_next_ms = self._authorised_limit(sm)

    speed_limit_suggested = float(mapd.speedLimitSuggestedSpeed)
    if gated:
      # THE DRIVER'S SET SPEED IS THE FINAL AUTHORITY (operator, 2026-08-07).
      #
      # The steady-state posted-limit ceiling is REMOVED while the set-speed feature is active.
      # Reported: the map wrongly said 60, the driver set cruise to 100, the MAX display followed
      # — and the car still dropped back to 60 the moment they lifted off. Cause: `authorised_ms`
      # held the last authorised limit (60) and nothing revoked it when the driver raised the set
      # speed, so this branch pinned v_target at 60 forever.
      #
      # The deeper point: a ceiling only ever DOES anything when it is below the set speed, so
      # "the map may never hold the car below the set speed" and "there is no ceiling" are the
      # same statement. And it is redundant anyway — with the set-speed feature the posted limit
      # already reaches the car THROUGH the set speed (auto-adopt / confirm). One authority, not
      # two. Map data is often wrong or stale; the driver must always be able to overrule it.
      #
      # Still gated, still un-removed elsewhere: curve braking and hazard braking are NOT speed
      # limits and are untouched, and the pre-sign approach ramp below still runs for a
      # PRE-AUTHORISED upcoming limit.
      speed_limit_suggested = 0.0
    if speed_limit_suggested > 0 and (self.v_target == 0 or speed_limit_suggested < self.v_target):
      self.v_target = speed_limit_suggested

    # Upcoming LOWER limit: shape the approach ourselves so it lands on the limit AT the sign.
    # mapd's own next-limit lookahead is disabled (slow_down_for_next_speed_limit: false in
    # MapdSettings) precisely so it cannot step speedLimitSuggestedSpeed straight down to the
    # new limit while the sign is still hundreds of metres away - that was measured causing
    # ~10x harder braking than needed. Revises the earlier "let mapd do the lookahead"
    # decision, on drive evidence.
    next_speed_limit = float(mapd.nextSpeedLimit)
    next_speed_limit_distance = float(mapd.nextSpeedLimitDistance)
    # The approach ramp is what the driver felt as "it slowed down BEFORE the sign", so while the
    # set-speed feature is active it is switched OFF entirely.
    #
    # KNOWN TRADE-OFF, deliberate and documented: the ramp acts on the UPCOMING limit, which by
    # definition cannot have been authorised yet — the driver is only asked once that limit
    # becomes current, at the sign. So there is no way to keep the ramp AND honour authorisation
    # without also pre-authorising upcoming limits (a two-stage design: authorise early, move the
    # set speed at the sign). That is the follow-up if braking at signs now feels abrupt.
    # Consequence today: the slow-down happens AT the sign via the ceiling, giving the planner's
    # a_cruise floor of -1.2 m/s² rather than the shaped 0.5 m/s² ramp.
    # ...unless the upcoming limit has been PRE-AUTHORISED because it would auto-adopt anyway
    # (no confirmation needed). Then the ramp is exactly what we want: it shapes the run-up at
    # APPROACH_DECEL instead of stepping at the sign.
    if gated and not (authorised_next_ms > 0 and abs(next_speed_limit - authorised_next_ms) < 0.3):
      next_speed_limit = 0.0
    if 0 < next_speed_limit < self.v_ego and next_speed_limit_distance > 0:
      v_sl_now = approach_speed(next_speed_limit, next_speed_limit_distance)
      if self.v_target == 0 or v_sl_now < self.v_target:
        self.v_target = v_sl_now

    # --- hazards: stop signs, give-way, level crossings, T-junctions, ... ---
    next_hazard_str = mapd.nextHazard
    next_hazard_speed_target = _HAZARD_SPEED_TARGETS.get(next_hazard_str, 0.0)
    next_hazard_distance = float(mapd.nextHazardDistance)

    # Validate an upcoming T-Junction against mapd's own path geometry before acting on it.
    # The tile flag is topological only and is measurably inverted at n36316730 and plain
    # wrong at the n5999476430 carriageway split. Only T-Junction is gated — real OSM
    # hazards (stop, give_way, level_crossing, ...) are surveyed features and are trusted.
    tj_turn_deg = self._update_t_junction_gate(sm, next_hazard_str, next_hazard_distance)
    if self._tj_suppressed:
      next_hazard_str = ""
      next_hazard_speed_target = 0.0
    d_frame = self.v_ego * DT_MDL

    # Lead vehicle gate: when following a lead, skip hazard pre-braking — the lead-following
    # MPC encodes the right brake authority. Cached for the firm-decel gate below.
    lead1 = sm['radarState'].leadOne
    lead2 = sm['radarState'].leadTwo
    # NOTE: openpilot's radarState.LeadData uses `present` ("true if a lead is present").
    # sunnypilot's schema called this `status`; porting that name verbatim made this raise
    # AttributeError on EVERY frame on the car. Verified by tests/test_schema_conformance.py.
    raw_has_lead = lead1.present or lead2.present
    self._has_lead = raw_has_lead

    # Lead-past-hazard refinement: a lead already beyond the hazard line (plus margin) is not
    # between us and the hazard and should not block engagement. Used ONLY at the rising edge.
    has_lead_for_engage = raw_has_lead
    if raw_has_lead and next_hazard_distance > 0:
      blocking = False
      if lead1.present and lead1.dRel <= next_hazard_distance + LEAD_PAST_HAZARD_MARGIN_M:
        blocking = True
      if lead2.present and lead2.dRel <= next_hazard_distance + LEAD_PAST_HAZARD_MARGIN_M:
        blocking = True
      if not blocking:
        has_lead_for_engage = False

    # Clear the sticky latch when there is no hazard, we've passed it, mapd switched to a new
    # hazard far ahead, or openpilot disengaged. A lead does NOT clear an engaged latch —
    # leads only gate the rising edge, so transient cross-traffic can't cancel a slow-down
    # already underway.
    if (not self.long_enabled or
        next_hazard_speed_target == 0 or
        next_hazard_distance == 0 or
        next_hazard_distance > self._prev_hazard_distance + 100):
      self._hazard_engaged = False

    # Decay hold distance (maintain target speed briefly after passing the hazard point)
    if self.hazard_hold_m > 0:
      self.hazard_hold_m = max(0.0, self.hazard_hold_m - d_frame)
      if self.hazard_hold_m > 0 and (self.v_target == 0 or self.hazard_speed_target < self.v_target):
        self.v_target = self.hazard_speed_target
        self.hazard_active = True

    if next_hazard_speed_target > 0 and next_hazard_distance > 0:
      if next_hazard_speed_target < self.v_ego:
        # Kinematic brake distance at constant decel. Stable across a_ego variation, unlike
        # the cubic jerk-limited formula, which oscillates the engagement gate during approach.
        decel_dist = (self.v_ego ** 2 - next_hazard_speed_target ** 2) / (2.0 * APPROACH_DECEL)
        brake_dist = decel_dist + HAZARD_REACH_BUFFER_M
        # Latch on the rising edge, gated on `not has_lead` HERE only.
        if brake_dist > 0 and next_hazard_distance <= brake_dist and not has_lead_for_engage:
          self._hazard_engaged = True
      # Sticky: while engaged, keep applying the hazard target every frame.
      if self._hazard_engaged:
        # Command the PROFILE speed, not the raw target: this decelerates at APPROACH_DECEL and
        # lands on the hazard speed exactly at the hazard rather than long before it.
        v_hazard_now = approach_speed(next_hazard_speed_target, next_hazard_distance)
        if self.v_target == 0 or v_hazard_now < self.v_target:
          self.v_target = v_hazard_now
          self.hazard_speed_target = next_hazard_speed_target
          self.hazard_active = True
      if next_hazard_distance <= d_frame * 2:
        self.hazard_hold_m = HAZARD_HOLD_DISTANCE
        self.hazard_speed_target = next_hazard_speed_target

    # Adaptive decel: while the hazard branch owns v_target and we have a valid positive
    # distance, compute the decel actually needed to hit the hazard target at the line.
    # Rate-limit and clamp. Compensates for road grade and other unmodelled forces.
    #
    # Gated on hazard_active, NOT _hazard_engaged: hazard_active also covers the 10 m hold
    # past the line. During the hold, next_hazard_distance resets to 0 (or jumps to the next
    # hazard), so the inner guard skips the recompute and we keep the last adapted value.
    if self.hazard_active:
      if next_hazard_distance > 5.0 and self.v_ego > next_hazard_speed_target:
        needed = -(self.v_ego ** 2 - next_hazard_speed_target ** 2) / (2.0 * next_hazard_distance)
        delta = needed - self._adaptive_hazard_accel
        delta = max(-HAZARD_ACCEL_RATE, min(HAZARD_ACCEL_RATE, delta))
        self._adaptive_hazard_accel += delta
        self._adaptive_hazard_accel = max(HAZARD_ACCEL_MIN,
                                          min(HAZARD_ACCEL_MAX, self._adaptive_hazard_accel))
      # else (in hold, or already at/below target): keep the current adapted value
    else:
      # Reset to baseline so the next approach starts fresh and no stale value leaks across.
      self._adaptive_hazard_accel = HAZARD_TARGET_ACCEL

    self._prev_hazard_distance = next_hazard_distance

    self._dbg = {
      "map_curve_speed": map_curve_speed,          # RAW from mapd (the step)
      "curve_ceiling": self._curve_ceiling,        # after the APPROACH_DECEL descent limit
      "speed_limit_suggested": speed_limit_suggested,
      "next_speed_limit": float(mapd.nextSpeedLimit),
      "next_speed_limit_distance": float(mapd.nextSpeedLimitDistance),
      "next_hazard_str": next_hazard_str,
      "tj_turn_deg": tj_turn_deg,
      "tj_suppressed": self._tj_suppressed,
      "tj_errors": self._tj_errors,
      "next_hazard_speed_target": next_hazard_speed_target,
      "next_hazard_distance": next_hazard_distance,
      "lead1_d_rel": lead1.dRel if lead1.present else 0.0,
      "lead2_d_rel": lead2.dRel if lead2.present else 0.0,
    }

  def _authorised_limit(self, sm) -> tuple[float, bool, float]:
    """(authorised limit in m/s, gated?) from the set-speed feature.

Third element is an UPCOMING limit pre-authorised for the approach ramp ONLY.

    `gated=False` means **fail open**: obey mapd exactly as before this gate existed. That is the
    right failure mode for infrastructure problems — a dropped message must never silently stop
    the car obeying speed limits. `gated=True` with 0.0 means the opposite and is deliberate:
    the feature IS running and has authorised nothing, so no limit is obeyed.
    """
    try:
      if not sm.alive.get('grtSetSpeedState', False):
        return 0.0, False, 0.0
      s = sm['grtSetSpeedState']
      if not s.active:
        return 0.0, False, 0.0
      return (float(s.authorisedLimit) * CV.KPH_TO_MS, True,
              float(s.authorisedNextLimit) * CV.KPH_TO_MS)
    except Exception:
      return 0.0, False, 0.0

  def _update_state_machine(self) -> tuple[bool, bool]:
    if self.state != MapState.disabled:
      if not self.long_enabled or not self.enabled:
        self.state = MapState.disabled
      elif self.long_override:
        self.state = MapState.overriding
      else:
        # ENABLED
        if self.state == MapState.enabled:
          if self.v_cruise > self.v_target != 0:
            self.state = MapState.turning
        # TURNING
        elif self.state == MapState.turning:
          if self.v_cruise <= self.v_target or self.v_target == 0:
            self.state = MapState.enabled
        # OVERRIDING
        elif self.state == MapState.overriding:
          if not self.long_override:
            if self.v_cruise > self.v_target != 0:
              self.state = MapState.turning
            else:
              self.state = MapState.enabled
    # DISABLED
    elif self.state == MapState.disabled:
      if self.long_enabled and self.enabled:
        self.state = MapState.overriding if self.long_override else MapState.enabled

    return self.state in ENABLED_STATES, self.state in ACTIVE_STATES

  def update(self, sm: messaging.SubMaster, long_enabled: bool, long_override: bool,
             v_ego: float, a_ego: float, v_cruise: float) -> None:
    self.long_enabled = long_enabled
    self.long_override = long_override
    self.v_ego = v_ego
    self.a_ego = a_ego
    self.v_cruise = v_cruise

    self.update_params()
    self.update_calculations(sm)

    self.is_enabled, self.is_active = self._update_state_machine()

    self.output_v_target = self.get_v_target_from_control()

    # Firm hazard decel — triple-gated for safety:
    #   1. is_active     → state machine in `turning` (controller actively in charge)
    #   2. hazard_active → v_target was set by the hazard branch, not passthrough
    #   3. not _has_lead → no lead car (lead-following keeps full authority)
    # Any one False → None → no extra decel authority.
    if self.is_active and self.hazard_active and not self._has_lead:
      # Use the ADAPTIVE value. Hardcoding HAZARD_TARGET_ACCEL here was the historical bug:
      # the adaptive loop's output never reached the planner and a_ego plateaued near
      # -0.9 m/s² regardless of tuning.
      self.output_hazard_accel = self._adaptive_hazard_accel
    else:
      self.output_hazard_accel = None

    self._write_debug(sm)
    self.frame += 1

  def _write_debug(self, sm: messaging.SubMaster) -> None:
    if _DEBUG_LOG is None or self.frame % 10 != 0:
      return
    line = json.dumps({
      "t": time.monotonic(),
      "v_ego_kmh": self.v_ego * 3.6,
      "a_ego_mps": self.a_ego,
      "v_cruise_kmh": self.v_cruise * 3.6,
      "map_curve_speed_kmh": self._dbg.get("map_curve_speed", 0.0) * 3.6,
      "curve_ceiling_kmh": self._dbg.get("curve_ceiling", 0.0) * 3.6,
      "speed_limit_suggested_kmh": self._dbg.get("speed_limit_suggested", 0.0) * 3.6,
      "next_sl_kmh": self._dbg.get("next_speed_limit", 0.0) * 3.6,
      "next_sl_dist_m": self._dbg.get("next_speed_limit_distance", 0.0),
      "next_hazard_kmh": self._dbg.get("next_hazard_speed_target", 0.0) * 3.6,
      "next_hazard_dist_m": self._dbg.get("next_hazard_distance", 0.0),
      "next_hazard_str": self._dbg.get("next_hazard_str", ""),
      "tj_turn_deg": self._dbg.get("tj_turn_deg"),
      "tj_suppressed": self._dbg.get("tj_suppressed", False),
      "tj_errors": self._dbg.get("tj_errors", 0),
      "hazard_hold_m": self.hazard_hold_m,
      "v_target_kmh": self.v_target * 3.6,
      "output_v_target_kmh": self.output_v_target * 3.6,
      "is_active": self.is_active,
      "is_enabled": self.is_enabled,
      "state": int(self.state),
      "long_enabled": self.long_enabled,
      "long_override": self.long_override,
      "hazard_active": self.hazard_active,
      "has_lead": self._has_lead,
      "output_hazard_accel": self.output_hazard_accel,
      "adaptive_hazard_accel": self._adaptive_hazard_accel,
      "lead1_d_rel": self._dbg.get("lead1_d_rel", 0.0),
      "lead2_d_rel": self._dbg.get("lead2_d_rel", 0.0),
    })
    try:
      with open(_DEBUG_LOG, "a") as f:
        f.write(line + "\n")
    except OSError:
      pass
