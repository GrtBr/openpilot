"""Hook 8: correct the car when it decelerates FASTER than the model asked.

THE DEFECT
----------
`modeld` never receives the set speed as an input (its inputs are `desire_pulse`,
`traffic_convention`, `action_t`, plus frames), so the model CANNOT hold a speed -- it emits a
comfortable acceleration for the scene. The planner does contain a speed controller (the
cruise candidate) but `min()` discards it whenever the model is more conservative: over 298
sustained-headwind episodes that was 100% of frames, throwing away a mean of +1.781 m/s^2.

The only component that knows the target cannot win; the component that wins does not know
the target. Measured on the 08-18 14:06 incident, the plant under-delivers by a median of
**0.107 m/s^2** -- the droop in one number.

WHAT THIS DOES
--------------
Pure disturbance rejection on the model's OWN request:

    u    = a_commanded(t - LAG) - aEgo(t)        > 0 means the plant under-delivered
    corr = clip(GAIN * (u - DEAD), 0, CAP)
    out  = a_e2e + corr                          (capped at 0 while a_e2e < 0, see below)

It is a SERVO, not an override: whatever the model asked for, this makes the car actually
deliver it. That holds for ANY commanded value, so unlike an anchor-on-quiet design there is
no band and no ceiling -- of the frames where the model was actively asking to SLOW at 14:06,
82% were under-delivering, and correcting those is still honouring the request.

IT BACKS OFF BY ITSELF. The correction is proportional to the error, so as the car reaches
what was asked the error goes to zero and the correction with it. No flip-off is needed and
adding one would chatter at the threshold.

BUT P-ONLY LEAVES A RESIDUAL. Against a steady disturbance d the correction settles at
-GAIN*d/(1+GAIN), so it rejects GAIN/(1+GAIN) of it -- 75% at GAIN=3, not 100%. Removing the
rest needs integral action, which is deliberately NOT here (windup, and a much bigger step).

WHY THE REFERENCE IS THE ACTUAL COMMAND, NOT a_e2e
--------------------------------------------------
`aEgo` responds to whatever won the planner's `min()`. If a lead branch commanded -1.0 and the
car achieved -1.0, then measuring against a_e2e (~0) would read as a huge under-delivery and
demand a correction that is pure nonsense. So the lag reference is the ACTUAL commanded accel
from `carControl.actuators.accel`. When another branch is in charge, u correctly reads ~0.

SAFETY
------
This canNOT carry hook 7's "never makes braking weaker" claim, and that is inherent: to make
the car achieve -0.10 against a -0.25 disturbance you must COMMAND about +0.15. The command
and the outcome are different quantities once a disturbance exists.

Bounded three ways:
  * the correction is POSITIVE-ONLY and capped at CAP;
  * while the model asks for deceleration the output is capped at 0 -- so it can undo
    over-braking down to coasting, but never accelerates against a deceleration request.
    Measured cost of that cap: 8.08 -> closed-loop recovery, vs 11.35 uncapped;
  * it is applied to the e2e CANDIDATE, so `min()` still hands control to cruise or a lead
    branch whenever either wants less. It cannot override a lead.

CLOSED-LOOP EXPECTATION, not open-loop
--------------------------------------
Earlier estimates applied a correction to logged `aEgo` that the ORIGINAL command produced,
which overstates the result. Iterating the loop against the logged disturbance on the 14:06
incident (22.8 km/h of droop):

    GAIN 1.0, capped   4.94 km/h   correcting 49% of frames
    GAIN 3.0, capped   8.08 km/h   correcting 40%      <- shipped
    GAIN 5.0, capped   8.81 km/h   correcting 40%

GAIN 3 nearly doubles GAIN 1 while correcting LESS often, because it settles the error rather
than grinding against it. Above 3 the returns fall off, as the residual formula predicts.
"""
from collections import deque

from openpilot.common.realtime import DT_MDL

_HS_LAG = 0.70            # s, established command->response lag for this car
_HS_SMOOTH = 1.00         # s, the raw under-delivery estimate is noisy
_HS_GAIN = 3.0            # m/s^2 per m/s^2 of under-delivery. See the closed-loop table.
_HS_DEAD = 0.05           # m/s^2, ignore under-delivery smaller than this
_HS_CAP = 0.30            # m/s^2 cap on the correction
_HS_MIN_SPEED = 8.33      # m/s == 30 km/h, same floor as hook 6
_HS_MIN_HEADROOM = 0.28   # m/s == 1 km/h. Never push past the set speed; cruise owns that.

_LAG_N = max(1, int(round(_HS_LAG / DT_MDL)))
_SMOOTH_N = max(1, int(round(_HS_SMOOTH / DT_MDL)))


class HoldSpeed:
  """One instance, owned by grt.hooks. See the module docstring."""

  def __init__(self):
    self.cmd_hist = deque(maxlen=_LAG_N + 1)
    self.u_hist = deque(maxlen=_SMOOTH_N)
    self.stats = {"frames_correcting": 0, "frames_capped_at_zero": 0,
                  "frames_at_cap": 0, "inactive": 0}

  def reset(self):
    self.cmd_hist.clear()
    self.u_hist.clear()

  def update(self, a_e2e: float, a_commanded: float, a_ego: float, v_ego: float,
             v_cruise: float, aggressive: bool, long_pid: bool, driver_input: bool,
             experimental: bool) -> float:
    """Return the e2e candidate, raised by what the plant is failing to deliver."""
    if not (experimental and aggressive and long_pid and not driver_input
            and v_ego >= _HS_MIN_SPEED):
      self.stats["inactive"] += 1
      self.reset()
      return a_e2e

    self.cmd_hist.append(a_commanded)
    if len(self.cmd_hist) < self.cmd_hist.maxlen:
      return a_e2e                       # not enough history to measure a lag yet

    # u > 0: the car is doing LESS than it was told to, LAG seconds ago.
    self.u_hist.append(self.cmd_hist[0] - a_ego)
    if len(self.u_hist) < self.u_hist.maxlen:
      return a_e2e
    u = sum(self.u_hist) / len(self.u_hist)

    if v_cruise - v_ego < _HS_MIN_HEADROOM:
      return a_e2e                       # at the set speed; cruise owns it from here

    corr = min(_HS_CAP, max(0.0, _HS_GAIN * (u - _HS_DEAD)))
    if corr <= 0.0:
      return a_e2e

    out = a_e2e + corr
    if a_e2e < 0.0:
      # The model is asking to slow. Undo over-braking down to coasting, but never
      # accelerate against a deceleration request. This is the operator's constraint and it
      # costs ~3 km/h of recovery on the 14:06 incident; it is what keeps the feature from
      # ever commanding acceleration while the model wants the opposite.
      capped = min(0.0, out)
      if capped != out:
        self.stats["frames_capped_at_zero"] += 1
      out = capped
      if out <= a_e2e:
        return a_e2e

    self.stats["frames_correcting"] += 1
    if corr >= _HS_CAP - 1e-9:
      self.stats["frames_at_cap"] += 1
    return out
