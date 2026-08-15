"""Hook 7 state: a rising-edge JERK CAP on the final accel command, relaxed personality only.

WHAT THIS IS FOR
----------------
The operator's report: the car "hits the throttle" rather than swelling into it, and asked
for a gentler rise in relaxed mode. The original proposal was "ramp up over ~3 seconds".

WHY IT IS A JERK CAP AND NOT A TIME CONSTANT
--------------------------------------------
Measured over 7.9 h of this car's logs (2026-08-14 fleet scan): the plan's rising updates
are extremely bottom-heavy -- median 0.011 m/s^2 per 50 ms planner tick (0.2 m/s^3), p90
0.036 (0.7 m/s^3), max 1.634 (33 m/s^3). Almost all rises are trim; a handful are steps.

A time constant applies to EVERY update equally, including the thousands of small ones. A
symmetric tau = 3 s was measured to cut PEAK command 1.96 -> 1.28 uphill (-35%) and
0.67 -> 0.16 on the highway (-76%) while leaving mean effort untouched: because the
commands are short transients, a slow filter never reaches the target. That is an
amplitude cut wearing a slope-change costume, and it is the opposite of what was asked
for ("keep the amplitude, gentle the rise").

A jerk cap sits far out in the tail instead, so it ignores normal driving and catches only
the steps. At 1.5 m/s^3 over the same 7.9 h: peak preserved exactly (1.96), 1.2% of
positive command area lost, binds on 0.5% of frames.

Also considered and REJECTED, both on measurement:
  * a deficit gate (bypass when far below set speed) -- it would have been bypassed during
    the entire event that motivated this, since the car was 12 km/h down at the time.
  * a deadband before the limiter -- +/-0.05 m/s^2 costs 16% of highway positive area for
    no peak benefit.

BRAKE RELEASE IS NOT RATE-LIMITED
---------------------------------
Rising edges are capped, but a rise that is merely the RELEASE of braking runs to zero
instantly: the ramp restarts from max(prev, 0). Limiting that too (variant A in
`ramp_final.py`) was measured to leave the command up to 2.93 m/s^2 behind the plan --
i.e. dragging the brakes long after the plan wanted them off. Variant B caps the worst
instantaneous shortfall at 1.22 m/s^2 and halves the area cost.

SAFETY
------
UNLIKE hook 6, this hook DOES carry the standard claim: it can never make braking weaker.
The output is `min(plan, ...)` on a rise and exactly `plan` on a fall, so the commanded
acceleration is never greater than the planner asked for, in any state. A sudden demand
for hard braking passes through in the same frame, unfiltered.

The cost is lag, and it is real: up to ~1.2 m/s^2 of instantaneous shortfall, decaying at
the cap. That is the trade "relaxed" is asking for. It composes with the wire-level clip in
hyundaicanfd.py (~5 m/s^3); the tighter of the two dominates.
"""
from openpilot.common.realtime import DT_MDL

# m/s^3. 3x gentler than the wire clip, inside ISO 15622 comfort bounds, and measured to
# preserve peak while binding on 0.5% of frames.
JERK_RELAXED = 1.5


class RelaxedAccelRamp:
  """One instance, owned by grt.hooks. Holds a single float of state."""

  def __init__(self):
    self.prev = None

  def reset(self):
    self.prev = None

  def update(self, a_target: float, active: bool) -> float:
    """Return the rate-limited command. `active` = relaxed personality AND engaged.

    When inactive the state is dropped, so re-entering relaxed never ramps up from a
    stale value left over from an earlier drive.
    """
    if not active:
      self.prev = None
      return a_target

    if self.prev is None or a_target <= self.prev:
      # First frame, or any decrease: pass straight through. Decreases are never delayed.
      self.prev = a_target
      return a_target

    # Rising. Restart the ramp from at-least-zero so releasing the brakes is instant and
    # only the throttle portion is gentled.
    out = min(a_target, max(self.prev, 0.0) + JERK_RELAXED * DT_MDL)
    self.prev = out
    return out
