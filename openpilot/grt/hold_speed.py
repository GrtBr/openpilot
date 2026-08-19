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
# RATE LIMIT on the correction. Added 2026-08-19 after the operator reported jerking on
# three uphill windows. Cause was mine: with GAIN 3.0 and DEAD 0.05 the correction saturates
# once u > 0.15, so the whole transition band is 0.10 wide in a NOISY signal -- the servo was
# effectively bang-bang, slamming 0.000 <-> 0.300 every ~150 ms while the model's own output
# sat flat at ~0.00 and the plan source never changed. Hook 6 has always rate-limited its
# floor; this had nothing. Symmetric, same value as hook 6's floor, so a full-scale swing
# takes 1.0 s instead of one frame.
_HS_CORR_JERK = 0.30      # m/s^3
_HS_MIN_SPEED = 8.33      # m/s == 30 km/h, same floor as hook 6
_HS_MIN_HEADROOM = 0.28   # m/s == 1 km/h. Never push past the set speed; cruise owns that.

# LOGGING. Unlike hook 6 this servo is continuous, not event-based -- it corrects on ~40% of
# frames, so a per-transition line would flood swaglog (the `lead.status` incident put 38,300
# lines in one drive). Instead: summarise each correcting BURST when it ends, skip trivial
# ones, and rate-limit. A burst still running past _HS_LOG_PROGRESS_T also reports, so a long
# sustained correction is not invisible until it finishes.
_HS_LOG_MIN_T = 1.0       # s, do not log bursts shorter than this
_HS_LOG_EVERY = 5.0       # s, at most one burst summary per this interval
_HS_LOG_PROGRESS_T = 10.0  # s, report a burst that is still going

_LAG_N = max(1, int(round(_HS_LAG / DT_MDL)))
_SMOOTH_N = max(1, int(round(_HS_SMOOTH / DT_MDL)))


class HoldSpeed:
  """One instance, owned by grt.hooks. See the module docstring."""

  def __init__(self):
    self.cmd_hist = deque(maxlen=_LAG_N + 1)
    self.u_hist = deque(maxlen=_SMOOTH_N)
    self.corr = 0.0           # rate-limited correction actually applied
    self.stats = {"frames_correcting": 0, "frames_capped_at_zero": 0,
                  "frames_at_cap": 0, "inactive": 0, "bursts": 0}
    # burst accounting for the throttled log
    self.b_t = 0.0            # s this burst has been correcting
    self.b_sum = 0.0          # integral of the correction, for a mean
    self.b_peak = 0.0         # peak correction
    self.b_peak_u = 0.0       # peak under-delivery seen
    self.b_zero_capped = 0    # frames the zero-cap bit
    self.b_reported = 0.0     # s of this burst already reported
    self.since_log = _HS_LOG_EVERY   # start ready to log

  def _log(self, msg: str):
    try:
      from openpilot.common.swaglog import cloudlog
      cloudlog.warning(f"grt hold_speed: {msg}")
    except Exception:
      pass

  def _burst_line(self, tag: str, v_ego: float) -> str:
    mean = self.b_sum / max(self.b_t / DT_MDL, 1.0)
    return (f"{tag} {self.b_t:.1f}s v={v_ego * 3.6:.0f}km/h "
            f"corr mean={mean:+.3f} peak={self.b_peak:+.3f} "
            f"peak_u={self.b_peak_u:+.3f}"
            + (f" zero_capped={self.b_zero_capped}" if self.b_zero_capped else ""))

  def _end_burst(self, v_ego: float):
    if self.b_t >= _HS_LOG_MIN_T and self.since_log >= _HS_LOG_EVERY:
      self.stats["bursts"] += 1
      self._log(self._burst_line("corrected", v_ego))
      self.since_log = 0.0
    self.b_t = 0.0
    self.b_sum = 0.0
    self.b_peak = 0.0
    self.b_peak_u = 0.0
    self.b_zero_capped = 0
    self.b_reported = 0.0

  def _ramp(self, target: float) -> float:
    """Move the applied correction toward `target` at _HS_CORR_JERK. Symmetric: the way OUT
    must be rate-limited too, or releasing the correction is itself a step."""
    step = _HS_CORR_JERK * DT_MDL
    self.corr = min(target, self.corr + step) if target > self.corr \
        else max(target, self.corr - step)
    return self.corr

  def reset(self):
    self.cmd_hist.clear()
    self.u_hist.clear()
    self.corr = 0.0

  def update(self, a_e2e: float, a_commanded: float, a_ego: float, v_ego: float,
             v_cruise: float, aggressive: bool, long_pid: bool, driver_input: bool,
             experimental: bool) -> float:
    """Return the e2e candidate, raised by what the plant is failing to deliver."""
    self.since_log += DT_MDL
    if not (experimental and aggressive and long_pid and not driver_input
            and v_ego >= _HS_MIN_SPEED):
      self.stats["inactive"] += 1
      self._end_burst(v_ego)
      self.reset()
      return a_e2e                       # preconditions gone: hard release is correct here

    self.cmd_hist.append(a_commanded)
    if len(self.cmd_hist) < self.cmd_hist.maxlen:
      return a_e2e                       # not enough history to measure a lag yet

    # u > 0: the car is doing LESS than it was told to, LAG seconds ago.
    self.u_hist.append(self.cmd_hist[0] - a_ego)
    if len(self.u_hist) < self.u_hist.maxlen:
      return a_e2e
    u = sum(self.u_hist) / len(self.u_hist)

    if v_cruise - v_ego < _HS_MIN_HEADROOM:
      self._end_burst(v_ego)
      corr = self._ramp(0.0)             # ramp out, do not step out
      return a_e2e + corr if corr > 0.0 else a_e2e

    target = min(_HS_CAP, max(0.0, _HS_GAIN * (u - _HS_DEAD)))

    # THE ZERO CAP IS FOLDED INTO THE TARGET, NOT CLAMPED ONTO THE OUTPUT.
    # Clamping the output was the cause of the 2026-08-19 jerking, and it is the SAME defect
    # as hook 6's 08-16 zero-crossing bug -- a hard clamp on a signal that crosses zero. The
    # model wanders across zero continuously, so `out = min(0, a_e2e + corr)` toggled between
    # 0.00 and ~0.31 frame to frame and squared the command. Folding the limit into the target
    # lets the rate limiter smooth the transition instead.
    if a_e2e < 0.0:
      # Undo over-braking down to coasting, never accelerate against a deceleration request.
      capped_target = max(0.0, -a_e2e)
      if capped_target < target:
        self.b_zero_capped += 1
        self.stats["frames_capped_at_zero"] += 1
      target = min(target, capped_target)

    corr = self._ramp(target)
    if corr <= 0.0:
      self._end_burst(v_ego)
      return a_e2e

    self.stats["frames_correcting"] += 1
    if corr >= _HS_CAP - 1e-9:
      self.stats["frames_at_cap"] += 1

    self.b_t += DT_MDL
    self.b_sum += corr
    self.b_peak = max(self.b_peak, corr)
    self.b_peak_u = max(self.b_peak_u, u)
    if self.b_t - self.b_reported >= _HS_LOG_PROGRESS_T:
      # still going -- report progress so a long correction is not invisible until it ends
      self.b_reported = self.b_t
      self.stats["bursts"] += 1
      self._log(self._burst_line("correcting", v_ego))
      self.since_log = 0.0
    return a_e2e + corr
