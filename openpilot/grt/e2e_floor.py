"""Hook 6 state machine: a temporary acceleration FLOOR on the e2e planner candidate.

WHAT THIS IS FOR
----------------
Measured over 7.9 h of this car's own logs (fleet scan 2026-08-14, 11 routes, 1208
segments, 565,565 engaged frames):

  * With the set speed at the posted limit (60/80/100/120 in 6710 of 7032 contented
    seconds), the model settles a median 4.5-12.4 km/h BELOW it.
  * 64.8% of lead-free highway time spent >3 km/h below set speed has
    |desiredAcceleration| < 0.1 -- the model is asking for nothing at all.

So the car has usable headroom UNDER the limit and does not take it. This hook offers
that headroom back, and only in a state where the model has just demonstrated it was
willing to accelerate.

WHY THIS HOOK IS DIFFERENT FROM EVERY OTHER GRT HOOK
----------------------------------------------------
Hooks 1/2/5 each carry the claim "this can never make braking weaker."
THIS HOOK CANNOT MAKE THAT CLAIM, and the difference is deliberate, not an oversight.

Raising the e2e candidate is precisely how it stops winning the planner's `min()`. In
experimental mode the e2e candidate IS the vision-based caution layer:
`get_cruise_accel()` skips BOTH the lateral-accel limit and the `allow_throttle` coast
limit when `experimentalMode` is set (longitudinal_planner.py:39-53), and the
longitudinal MPC has no curvature input at all -- it sees only radar leads and the
cruise state.

Still covered while this hook is active:
  * mapped curves / speed limits / map hazards  -> hook 1 lowers v_cruise, cruise wins min()
  * radar-locked leads                          -> MPC lead branches win min()
  * the set speed itself                        -> cruise candidate binds at the top
NOT covered while this hook is active:
  * unmapped curves, roadworks, stopped traffic the radar has not locked, pedestrians,
    debris, poor visibility. Those are e2e-only, and this hook is overriding e2e.

The mitigation is NOT a second veto -- measurement showed no usable one exists.
`gasPressProbs` sits at 0.926 with only 0.1% below its 0.4 threshold in exactly the
contented state this hook arms in, so it cannot carry the gate (it is still applied
below, as cheap belt-and-braces, but nothing rests on it).

The mitigation is the ARM CONDITION. The hook refuses to arm on "the model is quiet",
because quiet conflates "finished accelerating, happy here" with "withholding
acceleration because something ahead concerns me". It arms only after watching the model
ACCELERATE for real and then taper off -- positive evidence of willingness seconds ago.

That condition also turned out to self-select straight road: across the 23 usable
accelerate-and-settle events found in the fleet scan, every single one had
|desiredCurvature| <= 0.0021, most <= 0.001. Acceleration followed by a settle does not
happen on curves. That is an emergent property of the gate, not a designed guard, and it
is the single strongest reason to prefer this arm condition over "model is content".

SECOND ARM TRIGGER: the driver switching INTO aggressive
--------------------------------------------------------
Rising edge of personality -> aggressive opens a 3 s window in which the hook will arm as
soon as the situational gates allow. Retried across the window rather than tested on one
frame, so a lead that is just clearing or a bend that is just straightening does not
silently swallow the request.

BE CLEAR ABOUT WHAT THIS TRADES. The taper trigger is justified by MODEL willingness — the
model accelerated for real seconds ago, so it was not withholding out of caution. The
personality trigger has no such evidence. It is justified by DRIVER authority: the driver
has just pressed a button, is demonstrably attentive, and is asking for the headroom.
Those are different arguments and only the first says anything about the road ahead.

Consequence: the curvature gate (_MAX_CURV_ARM) becomes LOAD-BEARING on this path. On the
taper path it is near-redundant — accelerate-then-settle does not happen on curves, which
is why all 23 usable events found offline measured <= 0.0021. On the personality path
nothing else stops a driver requesting the floor mid-bend, so do not weaken that gate
without replacing it. The lead / headroom / throttle_prob gates are identical on both
paths (`_gates_ok`), deliberately: one gate set, one place to change it.

RELEASE
-------
Total, and it STAYS out: re-arming requires a fresh strong-acceleration episode (or a
fresh deliberate personality selection), so the hook cannot re-apply during the same quiet
period. That gives the "hand control back to e2e" guarantee with no lockout timer to tune.

The objection must be SUSTAINED (_ABANDON_ACCEL for _ABANDON_T), not instantaneous — see
the recalibration note on those constants. A negative raw value is passed through unlifted
the whole time regardless, so the debounce delays the LATCH-OUT, never the deference.

WHAT THIS DOES NOT ADDRESS
--------------------------
The uphill droop (car sitting ~7 km/h under set speed for 100 s on grade with the ECU
holding torque headroom, root-caused to kp = ki = 0 and no grade input to either planner
branch). That is the larger defect and it is untouched by this hook. Uphill at >10 km/h
deficit the model is ACTIVE, not contented (16% content, 23% braking), so this hook will
rarely even arm there.
"""
from collections import deque

from openpilot.common.realtime import DT_MDL

# --- tuning, all derived from the 2026-08-14 fleet scan unless noted -----------------

# "STRONG" = that speed band's p85 of positive desiredAcceleration, i.e. an acceleration
# this model genuinely regards as a push rather than trim. Measured p50/p85 by band:
#   <30 km/h   0.490 / 1.102      30-60 km/h  0.223 / 0.534
#   60-90 km/h 0.124 / 0.336      90+  km/h   0.124 / 0.325
# Breakpoints are BAND MIDPOINTS converted to m/s. UNITS: v_ego is m/s in the planner.
_STRONG_BP = [4.17, 12.50, 20.83, 29.17]   # m/s  == 15, 45, 75, 105 km/h
_STRONG_V = [1.10, 0.53, 0.34, 0.33]       # m/s^2

_QUIET_FRAC = 0.40          # "settled" band, as a fraction of STRONG (matches the offline detector)
_QUIET_MIN = 0.08           # m/s^2, floor on the settled band so it never becomes unmeetable
_QUIET_MAX = 0.25           # m/s^2

_T_STRONG = 0.5             # s, how long STRONG must hold to count as a real push
_T_TAPER = 2.0              # s, max time allowed for the taper itself
_T_QUIET = 1.0              # s, how long it must sit settled before we call it a settle

# Arm gates
_MIN_SPEED = 8.33           # m/s == 30 km/h. Stop-go produced 0 usable events in 7.9 h.
_MIN_HEADROOM = 1.39        # m/s == 5 km/h below set speed
_MIN_THROTTLE_PROB = 0.40   # same threshold openpilot uses for allow_throttle
_MAX_CURV_ARM = 0.0020      # 1/m. All 23 usable events measured <= 0.0021.

# Release gates (deliberately looser than the arm gates; we latch out either way)
_MAX_CURV_RELEASE = 0.0030  # 1/m
# RECALIBRATED 2026-08-15 from the first on-road drive. The original -0.05 / no-debounce /
# 0.15-drop settings closed the gate almost instantly: sessions lasted 2-11 s and every logged
# "model objected" release tripped at raw_e2e of -0.050 to -0.057, i.e. by 0-7 THOUSANDTHS.
# Measured on that drive (300 s, highway, set 110): dAccel is negative 37% of the time, p10 is
# -0.098, and it dips below -0.05 forty-seven times (~9/min) with a median excursion depth of
# only -0.075. -0.05 sat at roughly the 15th percentile — it was reading noise as objection.
# Every short (<0.4 s) excursion bottomed out at -0.096 or shallower, so -0.20 plus a debounce
# ignores all of them while still catching the real ones (-0.357, -0.754, -1.467).
# NOTE this threshold governs whether we LATCH OUT, not whether we override: a negative raw is
# passed straight through regardless (see the active block), so widening it does not command
# acceleration against a deceleration request.
_ABANDON_ACCEL = -0.20      # m/s^2, sustained for _ABANDON_T
_ABANDON_T = 0.30           # s, how long the objection must hold. Kills the single-frame blips.
# The drop detector had to move too: at 0.15 it fired 3.0x/min on this drive and would simply
# have become the new dominant releaser once the threshold above was widened.
_ABANDON_DROP = 0.35        # m/s^2 drop in raw e2e over _ABANDON_DROP_T -> model withdrawing
_ABANDON_DROP_T = 0.5       # s
_RELEASE_HEADROOM = 0.28    # m/s == 1 km/h; we have arrived, let cruise take it

# The floor itself
_FLOOR_JERK = 0.30          # m/s^3. Discretionary acceleration: gentler than the 5 m/s^3 wire clip.
_FLOOR_MAX = 0.40           # m/s^2 cap. NOTE the planner's cruise candidate is
                            # clip(v_cruise - v_ego, -1.2, ACCEL_MAX=2.0), so it only opposes
                            # this floor once the deficit is under 0.40 m/s (1.4 km/h).
                            # min() does NOT bound us in between -- this cap is the real limit.
_MAX_ACTIVE_T = 20.0        # s. Conditions drift away from the evidence that armed us.

# Second arm trigger: the driver switching personality INTO aggressive. See "SECOND ARM
# TRIGGER" in the module docstring for why this one is justified differently from the taper.
# A short window rather than a single frame, so the request is not silently swallowed when a
# gate happens to be closed on the exact frame the button is pressed.
_PERSONALITY_WINDOW = 3.0   # s
# The personality must be STABLE on aggressive before it counts as a request. On 2026-08-15 the
# driver cycled relaxed->standard->aggressive with the wheel button; the intermediate values are
# published 9-160 ms apart, so the hook armed on values merely being passed THROUGH and then
# released 39 ms and 90 ms later with reason "preconditions". 0.40 s clears that comfortably and
# is imperceptible once the driver has actually settled.
_PERSONALITY_STABLE_T = 0.40  # s

_WAIT, _ACTIVE = 0, 1


def _interp(x, xs, ys):
  """Local linear interp; avoids importing numpy into a hot 20 Hz path for 4 points."""
  if x <= xs[0]:
    return ys[0]
  if x >= xs[-1]:
    return ys[-1]
  for i in range(1, len(xs)):
    if x < xs[i]:
      f = (x - xs[i - 1]) / (xs[i] - xs[i - 1])
      return ys[i - 1] + f * (ys[i] - ys[i - 1])
  return ys[-1]


def _enum_is(v, name: str) -> bool:
  """capnp enum readers stringify to their name. Never raise from a gate check."""
  try:
    return str(v) == name
  except Exception:
    return False


class E2EAccelFloor:
  """See module docstring. One instance, owned by grt.hooks."""

  def __init__(self):
    self.state = _WAIT
    self.floor = 0.0
    self.raw_hist = deque(maxlen=max(2, int(_ABANDON_DROP_T / DT_MDL) + 1))
    self.strong_t = 0.0        # how long STRONG has held
    self.taper_t = 0.0         # time since STRONG ended, while tapering
    self.quiet_t = 0.0         # how long we have been settled
    self.tapering = False      # saw a STRONG run, now watching it settle
    self.active_t = 0.0
    self.frame = 0
    self.last_reason = ""
    # Funnel counters. Cheap, and the only way to tell on-car WHY the hook is not arming
    # (offline replay says it should fire ~1-2x/hour; if the car disagrees, this says which
    # gate is eating the events). Same spirit as scc_map's debug dict.
    self.stats = {"strong": 0, "settled": 0, "armed": 0,
                  "gate_lead": 0, "gate_headroom": 0, "gate_tp": 0, "gate_curv": 0,
                  "personality_edge": 0, "armed_personality": 0, "personality_expired": 0}
    # None until we have seen one frame, so a car that boots already in aggressive does NOT
    # count as the driver having just asked for it.
    self.saw_non_aggressive = False   # set once a non-aggressive personality is observed
    self.aggr_t = 0.0          # s aggressive has been held continuously
    self.object_t = 0.0        # s raw e2e has been below _ABANDON_ACCEL continuously
    self.pending_t = 0.0       # s remaining in the personality-request window

  # -- helpers ------------------------------------------------------------------------

  def _reset_detector(self):
    self.strong_t = 0.0
    self.taper_t = 0.0
    self.quiet_t = 0.0
    self.tapering = False

  def _release(self, reason: str, a_e2e: float):
    if self.state == _ACTIVE:
      self._log(f"released ({reason}) floor={self.floor:+.3f} raw_e2e={a_e2e:+.3f}")
    self.state = _WAIT
    self.floor = 0.0
    self.active_t = 0.0
    self.object_t = 0.0
    # Requiring a fresh STRONG episode is what gives "it stays out". Do not shortcut this.
    self._reset_detector()
    self.last_reason = reason

  def _log(self, msg: str):
    try:
      from openpilot.common.swaglog import cloudlog
      cloudlog.warning(f"grt e2e_floor: {msg}")
    except Exception:
      pass

  def _gates_ok(self, lead, headroom, throttle_prob, curvature, count: bool) -> bool:
    """Situational gates, identical for BOTH arm triggers. `count` so the funnel only
    records a rejection once per settle, not once per frame of a pending request."""
    ok = True
    if lead:
      if count:
        self.stats["gate_lead"] += 1
      ok = False
    if headroom < _MIN_HEADROOM:
      if count:
        self.stats["gate_headroom"] += 1
      ok = False
    if throttle_prob < _MIN_THROTTLE_PROB:
      if count:
        self.stats["gate_tp"] += 1
      ok = False
    if abs(curvature) >= _MAX_CURV_ARM:
      if count:
        self.stats["gate_curv"] += 1
      ok = False
    return ok

  def _arm(self, via: str, a_e2e, v_ego, headroom, throttle_prob, curvature):
    self.stats["armed"] += 1
    self.state = _ACTIVE
    self.floor = a_e2e          # start from where the model actually is
    self.active_t = 0.0
    self.pending_t = 0.0
    self._log(f"armed via {via} v={v_ego * 3.6:.0f}km/h headroom={headroom * 3.6:.1f}km/h "
              f"raw_e2e={a_e2e:+.3f} tp={throttle_prob:.2f} curv={abs(curvature):.4f}")

  # -- main ---------------------------------------------------------------------------

  def update(self, a_e2e: float, v_ego: float, v_cruise: float, lead: bool,
             throttle_prob: float, curvature: float, aggressive: bool,
             long_pid: bool, driver_input: bool, experimental: bool) -> float:
    """Return the (possibly raised) e2e acceleration candidate.

    ORDERING IS SAFETY-CRITICAL: the release test is evaluated against THIS frame's raw
    a_e2e before any floor is applied, so the frame on which the model first objects
    never receives a stale floor.
    """
    self.frame += 1
    self.raw_hist.append(a_e2e)

    # --- trigger 2: the driver SETTLED on aggressive ----------------------------------
    # Requires (a) having actually observed a non-aggressive state, so booting or engaging
    # in aggressive is not a request, and (b) aggressive held for _PERSONALITY_STABLE_T, so
    # values merely passed THROUGH while cycling the wheel button do not arm. Fires once per
    # selection: the flag is only re-armed by leaving aggressive again.
    if aggressive:
      self.aggr_t += DT_MDL
    else:
      self.aggr_t = 0.0
      self.saw_non_aggressive = True
    if self.saw_non_aggressive and self.aggr_t >= _PERSONALITY_STABLE_T:
      self.saw_non_aggressive = False
      self.stats["personality_edge"] += 1
      self.pending_t = _PERSONALITY_WINDOW
      self._log(f"personality settled on aggressive; arm request open for "
                f"{_PERSONALITY_WINDOW:.0f}s")

    if self.pending_t > 0.0:
      self.pending_t = max(0.0, self.pending_t - DT_MDL)
      if self.pending_t == 0.0 and self.state == _WAIT:
        self.stats["personality_expired"] += 1

    strong = _interp(v_ego, _STRONG_BP, _STRONG_V)
    quiet = min(max(_QUIET_FRAC * strong, _QUIET_MIN), _QUIET_MAX)
    headroom = v_cruise - v_ego

    # Hard preconditions. Named individually so a release says WHICH one went, rather than
    # a bare "preconditions" that costs a log-diving session to interpret.
    missing = ""
    if not experimental:
      missing = "not experimental"
    elif not aggressive:
      missing = "not aggressive"
    elif not long_pid:
      missing = "not pid"
    elif driver_input:
      missing = "driver input"
    elif v_ego < _MIN_SPEED:
      missing = "below min speed"
    basics_ok = not missing

    # Sustained-objection accumulator. Updated every frame regardless of state so it can
    # never carry stale credit into a fresh arm.
    if a_e2e < _ABANDON_ACCEL:
      self.object_t += DT_MDL
    else:
      self.object_t = 0.0

    # ---------------- release path (checked first, against raw a_e2e) ----------------
    if self.state == _ACTIVE:
      reason = ""
      if missing:
        reason = f"precondition: {missing}"
      elif self.object_t >= _ABANDON_T:
        reason = f"model objected ({self.object_t:.2f}s)"
      elif len(self.raw_hist) == self.raw_hist.maxlen and \
              (self.raw_hist[0] - a_e2e) > _ABANDON_DROP:
        reason = "model withdrawing"
      elif lead:
        reason = "lead appeared"
      elif throttle_prob < _MIN_THROTTLE_PROB:
        reason = "throttle prob"
      elif abs(curvature) >= _MAX_CURV_RELEASE:
        reason = "curvature"
      elif headroom < _RELEASE_HEADROOM:
        reason = "reached set speed"
      elif self.active_t >= _MAX_ACTIVE_T:
        reason = "max duration"
      if reason:
        self._release(reason, a_e2e)
        return a_e2e

    # ---------------- arm detector (runs in WAIT only) -------------------------------
    if self.state == _WAIT:
      if not basics_ok:
        self._reset_detector()
        return a_e2e

      # Driver-requested arm. Tried every frame of the window so a momentarily-closed gate
      # (a lead just clearing, a bend straightening) does not swallow the request.
      if self.pending_t > 0.0 and \
              self._gates_ok(lead, headroom, throttle_prob, curvature, count=False):
        self.stats["armed_personality"] += 1
        self._arm("personality", a_e2e, v_ego, headroom, throttle_prob, curvature)

      if self.state == _WAIT and not self.tapering:
        if a_e2e > strong:
          self.strong_t += DT_MDL
        else:
          # STRONG run ended. Long enough to count as a real push?
          if self.strong_t >= _T_STRONG:
            self.stats["strong"] += 1
            self.tapering = True
            self.taper_t = 0.0
            self.quiet_t = 0.0
          self.strong_t = 0.0
      else:
        self.taper_t += DT_MDL
        if a_e2e < -quiet:
          self._reset_detector()            # it objected on the way down; not a taper
        elif abs(a_e2e) < quiet:
          self.quiet_t += DT_MDL
          if self.quiet_t >= _T_QUIET:
            # Settled. Situational gates, counted individually so the funnel is visible
            # rather than inferred.
            self.stats["settled"] += 1
            if self._gates_ok(lead, headroom, throttle_prob, curvature, count=True):
              self._arm("taper", a_e2e, v_ego, headroom, throttle_prob, curvature)
            self._reset_detector()
        else:
          self.quiet_t = 0.0
          if self.taper_t > _T_TAPER:
            self._reset_detector()          # never settled inside the taper window

    # ---------------- active: raise the floor ----------------------------------------
    if self.state == _ACTIVE:
      self.active_t += DT_MDL
      if a_e2e < 0.0:
        # NEVER lift a command the model wants negative. Values in (_ABANDON_ACCEL, 0) do not
        # release the hook — they are trivially small — but turning a deceleration request into
        # an acceleration is the one thing the safety argument does not cover, so we simply pass
        # it through. Bleed the floor off at the same jerk meanwhile, so that re-applying it
        # ramps rather than stepping back up.
        self.floor = max(0.0, self.floor - _FLOOR_JERK * DT_MDL)
        return a_e2e
      self.floor = min(_FLOOR_MAX, self.floor + _FLOOR_JERK * DT_MDL)
      if self.floor > a_e2e:
        return self.floor

    return a_e2e
