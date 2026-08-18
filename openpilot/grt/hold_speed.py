"""Hook 8 state: hold the speed the model asked for, when the model asks for nothing.

THE DEFECT THIS ADDRESSES
-------------------------
Measured over 6.4 h of this car's logs plus the 08-18 14:06 incident: the planner contains a
speed controller (the cruise candidate, `clip(v_cruise - v_ego, -1.2, 2.0)`) but `min()`
discards it whenever the model is more conservative -- during sustained droop episodes that
was 100% of frames, throwing away a mean of +1.781 m/s^2 of speed-error correction. The
model itself CANNOT hold a speed: `modeld` never receives the set speed as an input. It emits
a comfortable acceleration for the scene it sees.

So: the only component that knows the target cannot win, and the component that wins does not
know the target.

WHAT THIS DOES, AND WHY IT IS A SERVO AND NOT AN OVERRIDE
---------------------------------------------------------
`a_e2e ~ 0` is a REQUEST -- "hold this state" -- not an absence of one. If the car then
decelerates on grade, the model's request is being violated by the plant, not by us.
Correcting it is making the car deliver what the model asked for.

That claim only holds inside a narrow band. Below it the model is genuinely asking to slow,
and holding speed would FIGHT it. Calibrated on the 14:06 incident (speed fell 114.4 -> 91.5
km/h with the model near zero), over the 28.3 s where speed was actively falling:

  model asking to slow (< -0.05)   12.4 s   44%   <- OUT OF SCOPE BY DESIGN
  no real opinion (-0.05..+0.05)   14.6 s   52%   <- what this addresses
  asking to go (> +0.05)            1.3 s    5%

Wider bands buy "coverage" only by swallowing frames where the model asked to slow: at
(-0.15, +0.20) coverage looks like 82% but 69% of it is negative commands we would be
fighting. Hence _HS_BAND = 0.05, and an honest ceiling of ~52% of that incident.

SAFETY
------
UNLIKE hook 6, this hook CAN carry the standard claim: it can never make braking weaker.
The correction is POSITIVE-ONLY and is applied to the e2e CANDIDATE, before the planner's
`min()`. So cruise and the MPC lead branches still bind exactly as they did -- if either wants
less, it wins. A reader who has absorbed hook 6's disclaimer should note this one is
different.

Overspeed needs no handling here: above set speed the cruise candidate goes negative and wins
`min()` on its own. The uncovered direction was only losing speed.
"""
from openpilot.common.realtime import DT_MDL

# "Quiet" = the model has no real opinion. Its output wanders +/-0.05 continuously, so this is
# the noise band, not a threshold on intent. See the 44/52/5 split above.
_HS_BAND = 0.05           # m/s^2

# Confirm the band entry before latching, to reject a single-frame crossing. Measured on the
# 14:06 band entries: the car does NOT settle after entry -- it keeps drifting at -0.17 to
# -0.23 km/h/s at every delay tested -- so waiting only gives speed away:
#   0.3 s -> 0.07 km/h already lost (p10 0.30);  1.2 s -> 0.23 km/h (p10 1.00)
# Latch as early as confirmation allows.
_HS_LATCH_T = 0.30        # s

_HS_GAIN = 0.30           # m/s^2 per m/s of speed lost. Fleet p99 correction at this gain was
                          # 0.324 m/s^2; mean 0.042. Gentle by construction.
_HS_MAX = 0.30            # m/s^2 cap on the correction.

# Anti-staleness, NOT an authority limit (_HS_MAX does that). If the anchor is this far from
# the current speed, something other than droop is happening and we stand down rather than
# chase it. Deliberately set ABOVE the observed maximum error (6.12 km/h over 6.4 h) so it
# fires only on a genuinely stale anchor, not on real droop.
_HS_MAX_ERR = 2.22        # m/s == 8 km/h

_HS_MIN_SPEED = 8.33      # m/s == 30 km/h, same floor as hook 6
_HS_MIN_HEADROOM = 0.28   # m/s == 1 km/h. Never push past the set speed; cruise owns that.


class HoldSpeed:
  """One instance, owned by grt.hooks. See the module docstring."""

  def __init__(self):
    self.anchor = None        # m/s, latched speed the model asked us to hold
    self.quiet_t = 0.0
    self.stats = {"latched": 0, "released_slow": 0, "released_stale": 0,
                  "released_precond": 0, "frames_correcting": 0}

  def reset(self, why: str = ""):
    if self.anchor is not None and why:
      self.stats[why] = self.stats.get(why, 0) + 1
    self.anchor = None
    self.quiet_t = 0.0

  def update(self, a_e2e: float, v_ego: float, v_cruise: float, aggressive: bool,
             long_pid: bool, driver_input: bool, experimental: bool) -> float:
    """Return the e2e candidate, raised only by what is needed to hold the anchor."""
    if not (experimental and aggressive and long_pid and not driver_input
            and v_ego >= _HS_MIN_SPEED):
      self.reset("released_precond")
      return a_e2e

    # ASYMMETRIC anchor hygiene. Dropping on EVERY band exit was measured to gut the feature:
    # over the 14:06 incident the model left the band 37 times in 50 s, so the anchor kept
    # resetting to an already-lower speed and only 2.23 km/h of a 22.8 km/h droop was held
    # back. Keeping it across UPWARD exits recovers 10.43 km/h -- ~46%, at the 52% ceiling
    # set by how often the model is genuinely asking to slow.
    if a_e2e < -_HS_BAND:
      # The model wants to SLOW. Holding speed would fight it. Drop at once -- this is the
      # safety constraint and is not negotiable; 52% of band exits in that incident were
      # this case, and they are exactly the half of the droop this feature cannot address.
      self.reset("released_slow")
      return a_e2e
    if a_e2e > _HS_BAND:
      # The model wants to GO. That does not conflict with holding a speed, so the anchor
      # survives -- and ratchets UP to whatever the model deliberately achieved, so once it
      # settles we hold the new speed rather than an older, lower one. Never ratchets down.
      self.quiet_t = 0.0
      if self.anchor is not None and v_ego > self.anchor:
        self.anchor = v_ego
      return a_e2e
    self.quiet_t += DT_MDL

    if self.anchor is None:
      if self.quiet_t < _HS_LATCH_T:
        return a_e2e
      self.anchor = v_ego
      self.stats["latched"] += 1
      return a_e2e

    err = self.anchor - v_ego            # positive == we have LOST speed
    if err > _HS_MAX_ERR:
      self.reset("released_stale")
      return a_e2e
    if v_cruise - v_ego < _HS_MIN_HEADROOM:
      return a_e2e                       # at the set speed; cruise owns it from here

    corr = min(_HS_MAX, max(0.0, _HS_GAIN * err))
    if corr <= 0.0:
      return a_e2e
    self.stats["frames_correcting"] += 1
    # ADDITIVE, not a floor: the model's request PLUS what the plant needs to deliver it.
    # Bounded by construction to |a_e2e| + _HS_MAX < 0.35 m/s^2.
    return a_e2e + corr
