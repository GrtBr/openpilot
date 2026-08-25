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

  B  DEADBAND THE CRUISE CANDIDATE at or below the set speed. At the set speed
     `a_cruise = 0`, so `min(e2e_raised, 0) = 0` and hooks 6 and 8 are structurally vetoed --
     whatever they add is thrown away. Coast on a grade -> speed droops -> headroom opens ->
     hook 8 works again -> ~5 s pump. 16:40 is the clean picture: 60.0 -> 50.9 -> 60.0 against
     a 60 set. At 17:34-17:40 the hooks raised on only 7% of frames and cruise won 77% of the
     `min()` -- they almost never reached the wheels.

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

  * B stops the cruise branch vetoing a positive e2e+hook candidate at or below the set speed.
    That is the entire point of hooks 6 and 8 (operator design, 2026-08-14). Overspeed is
    still capped -- above the set speed the branch is the ordinary P-term, hook-5 softened.
    A lead or the MPC still wins the `min()` when lower. Map curves, speed limits and hazards
    still work, because hook 1 LOWERS `v_cruise`, which puts us in the `v_ego > v_cruise`
    branch where cruise goes negative as designed.
  * A can hold a pre-glitch POSITIVE command through a dip shorter than T_HOLD (0.30 s).
  * C can refuse a mild coast while at least MIN_HEADROOM (5 km/h) of set-speed headroom is
    unused. It does NOT add acceleration -- unlike hook 6 it only declines to cut throttle.

In all three, `a <= ABANDON` is accepted immediately and unmodified.

UNTESTED DIRECTION -- READ BEFORE TUNING
----------------------------------------
**B removes the cruise approach taper.** Below the set speed the stock candidate is
`clip(v_cruise - v_ego, ...)`, i.e. +0.28 m/s^2 at 1 km/h below set, and that P-term is what
eases the car in. Returning `ACCEL_MAX` deletes it: the approach is now shaped by whatever
e2e and hooks 6/8 ask for, right up to the set speed, and the frame `v_ego` crosses
`v_cruise` the branch snaps back to its negative P-term. Overshoot-then-snap at the set speed
is a plausible NEW oscillation and **no replay gate in the 2026-08-24 spec covers it** -- all
five gates test the droop side. If the car starts hunting AT the set speed rather than below
it, this is the first place to look.

A second bound worth stating: while `last_sign` is positive, layer A never emits 0, it emits
EPSILON (0.04 m/s^2). Nothing times that out. It ends when the model asks for less than zero
for T_HOLD, or when `v_ego` crosses `v_cruise` and cruise goes negative. Sustained EPSILON is
~8.6 km/h per minute of speed gain, so in RELAXED -- where hooks 6/8 do not exist -- layers
A+C become the mechanism that walks the car up to the set speed. That is what C is for, but
it is behaviour relaxed did not have before this hook.
"""
from opendbc.car.interfaces import ACCEL_MAX

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

    At or below the set speed the cruise branch stops competing, so hooks 6 and 8 can
    actually reach the SCC. Above it, the branch is untouched.
    """
    if v_cruise < MIN_V_CRUISE:
      return a_cruise                      # forceDecel demands a stop; do not touch it
    if v_ego <= v_cruise:
      return ACCEL_MAX                     # let e2e + hooks 6/8, or the MPC, choose
    # Overshoot only. Keep the existing P-term (possibly hook-5 softened), but do not let a
    # ripple of a few tenths of a km/h click the throttle off and on.
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
