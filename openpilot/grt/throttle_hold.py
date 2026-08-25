"""Hook 10: stop the SCC throttle from clicking off, and stop `cruise = 0` from vetoing hooks 6/8.

WHAT THIS IS FOR
----------------
On this car `aReq ~ 0` is the SCC throttle DEADBAND (`create_acc_control` in
`opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py`). Crossing zero is throttle OFF and then
ON again. That is a SIGN CHANGE, not a sharp step -- on 2026-08-22 at 14:35 the worst
single-frame step was only 0.058 m/s^2 and the CAN layer already clips jerk at 5 m/s^3.
Smoothing the slope cannot help, and was measured not to: an EMA on the logged command took
65 -> 41 crossings/min at tau 0.10 and was still at 37 at tau 0.20. Only tau ~ 1 s killed it,
and that is the time constant already rejected for hook 7. The fix is to hold the PRE-GLITCH
COMMAND, not to filter it and not to clip it to zero (clipping to zero still hits the
deadband).

Two distinct 2026-08-22 symptoms, three layers:

  A  SIGN DEBOUNCE on the final command. 14:35-14:36 at 110 vs 110 with no lead: cruise is
     `clip(v_cruise - v_ego, ...)`, which at equality is 0, so a +-0.4 km/h ripple (~0.11
     m/s^2 of P-term) goes slightly NEGATIVE, beats e2e's +0.07 in the `min()`, and dumps the
     throttle. 65 zero-crossings/min, 59 source flips; worst 8 s ran at 90/min.

  B  RIPPLE CLIP on the cruise candidate: a few tenths of a km/h of overspeed must not click
     the SCC throttle off and on. NOTE this layer used to do much more -- it released the
     cruise branch entirely at or below the set speed. That was reverted on the same day it
     shipped; see "WHAT LAYER B USED TO DO" below and the method docstring.

  C  BACKSTOP with unused headroom, mainly for RELAXED, where hooks 6/8/9 are all off. 15:28
     uphill at 62-80 vs 110, 100% e2e, `a_cmd == raw`: the model dipped through zero and the
     cut was instant because hook 7 only rate-limits RISES.

WHAT THIS IS NOT
----------------
Not a filter. There is no time constant anywhere in this file. A request at or beyond
ABANDON (-0.20) is passed through UNFILTERED on the same 50 ms planner tick, in every layer.

SAFETY
------
This hook can make the car LESS conservative, deliberately, in three bounded ways:

  * B now only clips a sub-BAND negative to zero, so it can withhold at most 0.08 m/s^2 of
    braking, and only while the car is within a few tenths of a km/h over the set speed.
  * A can hold a pre-glitch POSITIVE command through a dip shorter than T_HOLD (0.30 s).
  * C can refuse a mild coast while at least MIN_HEADROOM (5 km/h) of set-speed headroom is
    unused. It does NOT add acceleration -- unlike hook 6 it only declines to cut throttle.

In all three, `a <= ABANDON` is accepted immediately and unmodified.

WHAT LAYER B USED TO DO, AND WHY IT DOES NOT ANY MORE
-----------------------------------------------------
Layer B originally returned `ACCEL_MAX` at or below the set speed, to stop the cruise branch
vetoing hooks 6/8. That was shipped on 2026-08-25 with an explicit warning that it deleted the
cruise approach taper and that overshoot-then-snap was untested. The first drive produced
exactly that: time spent above the set speed went 23% -> 54% and reversals at a 0.10 ruler went
18.9 -> 22.7/min, a ~1 km/h bang-bang limit cycle across the set speed. It was also unnecessary
-- 5159 of 5182 measured veto frames were within 1 km/h of the set speed, where cruise easing
off is correct. The release is gone; only the SCC ripple clip remains. Layers A and C are
unchanged and are what actually fixed the chatter (zero-crossings 82 -> 25/min on the same
drive). Full reasoning in `deadband_cruise_accel`.

A second bound worth stating: while `last_sign` is positive, layer A never emits 0, it emits
EPSILON (0.04 m/s^2). Nothing times that out. It ends when the model asks for less than zero
for T_HOLD, or when `v_ego` crosses `v_cruise` and cruise goes negative. Sustained EPSILON is
~8.6 km/h per minute of speed gain, so in RELAXED -- where hooks 6/8 do not exist -- layers
A+C become the mechanism that walks the car up to the set speed. That is what C is for, but
it is behaviour relaxed did not have before this hook.
"""
from openpilot.common.realtime import DT_MDL

# Stay off the SCC deadband. Never emit a positive command smaller than this while holding
# throttle on -- a command of 0.00 IS the deadband, which is the thing being fixed.
EPSILON = 0.04            # m/s^2

# Tiny-negative clip on the overshoot branch, so a 0.4 km/h ripple just over the set speed
# does not click the SCC. Same magnitude as hook 6's _DECAY_DEADBAND.
BAND = 0.08               # m/s^2

# How long a sign disagreement must persist before it is believed. 6 * DT_MDL. Same value as
# hook 6's _DECAY_T / _ABANDON_T, deliberately -- it is the same "is the model actually
# asking, or is this a blip" question.
T_HOLD = 0.30             # s

# A request at or beyond this is a REAL brake (lead, curve, hazard, vision) and is never
# delayed, never clipped, never held. Same value as hook 6's _ABANDON_ACCEL; kept as its own
# constant here so this file reads standalone, but the two must not drift apart.
ABANDON = -0.20           # m/s^2

# Layer C only. 5 km/h of unused headroom before C refuses to cut throttle. Same value as
# hook 6's _MIN_HEADROOM.
MIN_HEADROOM = 1.39       # m/s == 5 km/h

# Below this v_cruise, `forceDecel` is demanding a stop and every layer stands down. Matches
# hook 5's _COAST_MIN_V_CRUISE.
MIN_V_CRUISE = 1.0        # m/s


class ThrottleHold:
  """One instance, owned by grt.hooks. See the module docstring."""

  def __init__(self):
    self.last_sign = 0      # 0 = no state yet, else +1 / -1
    self.last_a = 0.0       # last EMITTED command, not the last requested one
    self.pending_t = 0.0    # s the current sign disagreement has persisted
    self.stats = {"held": 0, "epsilon": 0, "layer_c": 0, "abandon": 0, "flips": 0}

  def reset(self):
    self.last_sign = 0
    self.last_a = 0.0
    self.pending_t = 0.0

  # ---------------------------------------------------------------- layer B ---------
  def deadband_cruise_accel(self, a_cruise: float, v_ego: float, v_cruise: float) -> float:
    """Run on the CRUISE candidate, after hook 5, before the `min()`.

    ONLY the SCC ripple clip. The set-speed RELEASE this used to do was reverted on
    2026-08-25 -- see below.

    WHY THE RELEASE WAS REMOVED (it caused exactly the failure it was warned about)
    ------------------------------------------------------------------------------
    From 2026-08-25 this returned `ACCEL_MAX` whenever `v_ego <= v_cruise`, to stop
    `min(raised_e2e, 0) = 0` discarding hooks 6 and 8 at the set speed. The docstring
    flagged that this deletes the cruise APPROACH TAPER and that overshoot-then-snap was
    an untested direction. It happened, on the first drive:

        near the set speed (+-2 km/h), engaged      before      with release
          zero-crossings/min                          82.1          25.0   <- layer A, kept
          reversals/min at a 0.10 ruler               18.9          22.7   <- WORSE
          share of time ABOVE the set speed            23%           54%   <- WORSE

    Measured mechanism, 12:33:54 at a 110 set: the command is a bang-bang limit cycle across
    the set speed. Just BELOW it the source is e2e on 95% of frames with a mean command of
    +0.064 (still accelerating 0.4 km/h from target, because nothing tapers any more); just
    ABOVE it the source is cruise on 96% with a mean of -0.008. Cross, snap, cross back:
    ~4-5 s period, ~1 km/h amplitude.

    AND THE RELEASE WAS NEVER NEEDED. `a_cruise` IS the headroom in m/s, so the cruise branch
    can only out-bid a hook candidate of ~0.3-0.8 while headroom is under ~3 km/h. Measured
    over the four 2026-08-22 windows where cruise won: of 5182 frames, **5159 were within
    1 km/h of the set speed and not one had more than 3 km/h of headroom**. Inside that last
    km/h the car is already at its target and cruise easing off is correct behaviour, not a
    veto to be defeated. The genuine droop case (16:40, 51 km/h against a 60 set) had cruise
    saturated at ACCEL_MAX with 9 km/h of headroom -- cruise was never what held it down.
    That one is fixed by hook 8's zero-cap seam, which is unrelated to this layer.

    So: keep the ripple clip, which is about the SCC deadband and is what this hook is for.
    Do not reintroduce the release. If hooks 6/8 ever do need authority at the set speed, the
    answer is a TAPER, never a step at `v_ego == v_cruise` -- the step is the oscillator.
    """
    if v_cruise < MIN_V_CRUISE:
      return a_cruise                      # forceDecel demands a stop; do not touch it
    # A ripple of a few tenths of a km/h around the set speed must not click the SCC throttle
    # off and on. This is the only thing this layer does now.
    if -BAND < a_cruise < 0.0:
      return 0.0
    return a_cruise

  # ------------------------------------------------------------- layers C + A -------
  def update(self, a: float, v_ego: float, v_cruise: float, long_active: bool) -> float:
    """Run on the FINAL command, after the `min()` and after hook 7. All personalities."""
    if not long_active:
      self.reset()
      return a

    # ---- layer C: refuse a mild coast while set-speed headroom is going unused --------
    # Runs BEFORE the debounce so that what A sees, and therefore what it latches a sign
    # from, is already the held value.
    if (v_cruise >= MIN_V_CRUISE and a > ABANDON
        and v_cruise - v_ego >= MIN_HEADROOM):
      floor = EPSILON if self.last_sign > 0 else 0.0
      if a < floor:
        self.stats["layer_c"] += 1
        a = floor

    # ---- layer A: sign debounce ------------------------------------------------------
    if self.last_sign == 0:
      # First active frame ADOPTS the current command. Seeding at 0 would invent a sign
      # change on frame 2 and hold a stale 0 through the debounce window.
      self.last_sign = 1 if a > 0.0 else -1
      self.last_a = a
      self.pending_t = 0.0
      return a

    if a <= ABANDON:
      # A real brake. Unfiltered, this frame, always.
      self.stats["abandon"] += 1
      self.last_sign = -1
      self.last_a = a
      self.pending_t = 0.0
      return a

    # A command of exactly 0.0 counts as NEGATIVE for sign purposes: while holding throttle
    # on, 0.0 is not a neutral request, it is the deadband -- the glitch being debounced.
    sign = 1 if a > 0.0 else -1

    if sign == self.last_sign:
      self.pending_t = 0.0
      out = a
      if self.last_sign > 0 and out < EPSILON:
        self.stats["epsilon"] += 1
        out = EPSILON
      self.last_a = out
      return out

    # Sign disagrees and it is not a real brake: hold the PRE-GLITCH command.
    self.pending_t += DT_MDL
    if self.pending_t < T_HOLD:
      self.stats["held"] += 1
      return self.last_a

    # Held long enough to be believed. `a == 0` arriving here means the model really has
    # settled into coast, so we take it.
    self.stats["flips"] += 1
    self.last_sign = sign
    self.pending_t = 0.0
    out = max(a, EPSILON) if sign > 0 else a
    self.last_a = out
    return out
