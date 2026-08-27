"""Hook 11: far-lead pre-brake, RELAXED personality only.

See FAR_LEAD_PREBRAKE_PROMPT.md (repo root) for the full spec and the 2026-08-25 10:49
incident this closes. Summary: on this car (radarUnavailable=True, vision-only lead), the
model's per-frame `radarState.leadOne.vRel` is unreliable at range and high closing speed.
Measured on that log (route 00000128--201591a1fc, 10:49:43-52): `vRel` averaged -1.56 m/s
while the true, position-derived closing rate averaged -8.16 m/s over the same 8 s. The
planner's own MPC/e2e candidates trust `vRel` and stayed near 0 m/s^2 until dRel had already
collapsed under 65 m. This hook fills exactly that hole with one more `min()` candidate in
`longitudinal_planner.py`, built from a filter on `leadOne.dRel` (position) instead of the
model's velocity head.

WHY NOT radard.py's KF1D
-------------------------
`radard.py`'s `Track.kf` is a `[SPEED, ACCEL]` filter fed a velocity MEASUREMENT
(`self.kf.update(self.vLead)`) -- built for radar Doppler, which this car does not have. This
hook needs the opposite: an `[x, v]` filter fed a POSITION measurement (`dRel`). `_RangeRateFilter`
below is the same idea in spirit -- a small recursive two-state estimator -- but a different,
purpose-built implementation with its own gains, no shared instance with radard.py.

THE ARMING GATE, AND WHY IT DOES NOT USE `min(lead.vRel, v_filt)` (measured -- do not
"simplify" this back)
-------------------------------------------------------------------------------------
A first cut gated arming on `a_req` computed from `min(lead.vRel, v_filt)` -- i.e. it let the
model's raw, single-frame `vRel` help decide WHETHER to arm. Replayed against the 10:49 log,
that gate fired at t=0.35 s on one noisy `vRel` sample (-1.18 m/s, `a_req` cleared the 0.30
threshold by 0.005) while the filter itself still read ~0. It ALSO false-armed on an unrelated
noisy pre-episode blip earlier in the same recording: 4.8 s of flickering, non-closing
detections at 111-114 m, prob 0.5-0.7, essentially flat dRel. Both fire for the same reason --
`lead.vRel` is exactly as noisy as `dRel` (single-sample `d(dRel)/dt` on this log spikes -55 to
+53 m/s), and letting it decide the arming gate reintroduces the noise the filter exists to
reject.

Fix: the SLOW signal (the filter) decides WHETHER to arm; the pessimistic pairing
(`min(lead.vRel, v_filt)`) is used only for HOW HARD to brake once already armed, where it is
safely bounded by the `[-1.2, -0.40]` clip regardless. Arming additionally requires
`a_req(v_filt)` to clear `HOT_A_REQ` for `HOT_PERSIST_S` CONTINUOUSLY, not on one frame.

Retuned and replayed against the same log (d011-d014, `.venv` pycapnp, kinematic replay --
acados not run): `ALPHA=0.10, BETA=0.003, HOT_PERSIST_S=0.5` arms the real episode at t=2.3 s,
dRel=115.0 m (spec's target was "~118 m" -- close enough given the true closing rate at the
first persistence-satisfying instant, t=0.7 s / dRel=114 m, was actually only about -0.2 m/s;
the real danger did not exist yet at first lock, it developed over the next ~1.5 s), and
produces ZERO arms against the 4.8 s pre-episode blip. Faster tunings (alpha 0.15, 0.20) arm
the real episode a little earlier but still false-arm on the blip even with 1.0 s of hot
persistence -- rejected for that reason, not for missing the earlier arm point. This numeric
validation has NOT yet had a second advisor pass (unavailable at implementation time); flagged
here so a future reader knows the gap.

A SECOND BUG FOUND BY TESTING, BEFORE ANY REPLAY: the arming distance check (`dRel > 100 m`)
was first evaluated at the moment the 0.8 s persistence gate (0.30 s presence + 0.5 s hot)
completed, using the LIVE `dRel` at that instant. For a lead closing at the maximum possible
rate (fully stopped, `vRel = -v_ego`), dRel can shrink 20+ m during that 0.8 s -- a synthetic
"110 vs 0 at 120 m" test never armed at all, because by the time the gate cleared, dRel had
already crossed under 100. That is the single most dangerous case this hook exists for. v1
fixed this by capturing `dRel_at_lock` once, at the FIRST frame of the qualifying presence run.

A THIRD BUG, FOUND ON A REAL DRIVE (2026-08-27, ~07:55), NOT IN TESTING: v1 additionally
required, before any of the above, a rising edge -- the lead had to have been ABSENT for
`ABSENCE_S` (2.0 s) immediately before the presence run that triggers arming. On that drive, a
lead closing at up to ~3.0 m/s^2 of `a_req` (ten times `HOT_A_REQ`) for several sustained
seconds was preceded by only a 0.20 s gap, not 2.0 s -- so `qualifying_absence` latched `False`
for that entire presence run and the hook could never arm, no matter how hot the danger signal
became. Root-caused by direct instrumentation of `hot_elapsed`/`qualifying_absence`/
`dRel_at_lock` frame-by-frame; quantified counterfactually at ~3.15 s of denied armed time and
~3.6 m/s of speed shed not delivered. Driver had to intervene manually.

The absence gate was never load-bearing for the thing it looked like it was protecting --
flicker/noise rejection. That job is done entirely by `PRESENCE_PERSIST_S` (0.30 s continuous
presence) and `HOT_PERSIST_S` (0.5 s continuous hot signal) below; both were already required
before v1 would arm, independent of `qualifying_absence`. Replayed against the 2026-08-25
10:49 log's 4.8 s pre-episode noise blip with the absence gate removed entirely: still zero
false arms, because that blip's dRel is flat and never produces a sustained `a_req_filt` hot
streak. So the absence gate bought nothing but the false confidence that flicker rejection
needed it, while actively blocking exactly the kind of gap-then-danger sequence a real drive
produced. REMOVED. Arming now requires only: `PRESENCE_PERSIST_S` continuous presence, followed
by `HOT_PERSIST_S` of continuous `a_req_filt > HOT_A_REQ` -- no rising edge, no absence
precondition, evaluated fresh every frame the lead is present.

Removing the rising edge means the old "distance at first lock" anchor (`dRel_at_lock`) no
longer has a well-defined trigger point -- there is no "lock" event anymore, just continuous
presence. Replaced with `dRel_at_hot_start`: dRel captured once, at the first frame the HOT
STREAK begins (i.e. the first frame `a_req_filt` crosses above `HOT_A_REQ`, not the first frame
of presence). This is a different instant than v1's `dRel_at_lock` and a different semantic --
"distance when the danger became detectable", not "distance at first sight" -- do not conflate
the two if reading old test cases or the spec doc's earlier revisions. `ARM_MIN_DIST` dropped
from 100 to 80 m to compensate: anchoring later (at hot-start instead of first-lock) means the
anchor distance is naturally smaller for the same encounter, so the old 100 m threshold would
reject cases it used to accept. Validated against both real incident logs plus the canonical
"110 vs 0 at 120 m" stopped-lead synthetic (see `test_far_lead.py`): 2026-08-27 now arms at
t+52.77 s, dRel=93.6 m (previously: never armed); 2026-08-25 still arms at dRel=115.0 m
(unchanged, hot-start and first-lock coincide when the lead is genuinely fresh); the stopped-
lead synthetic still arms, `dRel_at_hot_start`=112.35 m at the 120 m starting condition.

KNOWN LIMITATION of the hot-start anchor, found while validating the above (not a regression --
v1 fails the same class of case for a different reason, see below): if a fully-stopped lead is
first detected already inside roughly 87 m (this car's radar/vision detection range, closing at
`v_ego`), the hot streak can begin with `dRel_at_hot_start` already at or under `ARM_MIN_DIST`,
and because that value is captured once and frozen, the arming check then fails FOREVER for
that encounter -- it never re-evaluates from a later, closer anchor. v1 has an analogous
failure mode (a lead first detected already inside 100 m never arms either, since
`dRel_at_lock` is captured at first sight). Neither design was built or tested for "stopped
object first visible already inside ~90 m while still doing 110 km/h" -- that is a sub-3-second
emergency-stop scenario outside this hook's declared envelope (correcting complacency at LONG
range); stock's own emergency-braking path, not this hook, is what should dominate there.
Documented rather than silently shipped; the tested worst case (120 m onset) is unaffected.

WHY THE RELEASE CONDITION IS NOT A BARE DISTANCE CUTOFF
--------------------------------------------------------
The original spec released the latch on `dRel < 50`. Checked against the 10:49 log: at
dRel=50.24 m, stock (MPC/e2e) `aTarget` was still -0.298 -- WEAKER than this hook's own -0.40
floor -- only crossing -0.40 at dRel~50.08 m. A hard release at 50 m lands inside that gap and
can step the commanded accel from -0.40 back up to -0.30 for a frame or two at the tightest
part of the approach -- the one failure mode where this hook would make things worse, not
merely unhelpful. Instead: release once the OTHER candidates already being built this frame
(`stock_min`, passed in by the caller) have themselves reached `<= FLOOR`, returning this
hook's own candidate ONE MORE TIME on that same frame so `min()` picks whichever is harder,
then dropping the latch for the next frame. `RELEASE_DIST` (20 m) is only an absolute backstop
in case stock never catches up.

This means the calling convention is `far_lead_candidates(sm, v_ego, stock_min)`, not the
`(sm, v_ego)` shape in the original spec doc -- `stock_min` cannot be recovered from `sm`
alone (the MPC/e2e candidates are local variables in `longitudinal_planner.update()`, not
published anywhere before this hook runs), and `carControl.actuators.accel` was considered and
rejected: it is the PREVIOUS frame's actual output, which after this hook has won once already
reflects this hook's own prior command -- using it as "has stock caught up" would self-release
one frame after arming.

SAFETY
------
Returns `[]` (inert) unless armed. Once armed, the candidate is clamped to `[CAP, FLOOR]` =
`[-1.2, -0.40]` and only ever competes inside the planner's `min()`, so it can never make
braking weaker than stock. `FLOOR` is -0.40, not something softer, because hook 10 layer C
(`ABANDON = -0.20` in `grt/throttle_hold.py`) would otherwise eat a milder request. Every gate
(personality, `longActive`, driver input) is re-checked every frame and any exception drops
straight to `[]`, so a wedged state cannot outlive one bad frame's inputs.
"""
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalPlanSource
from openpilot.selfdrive.controls.lib.drive_helpers import should_stop

# ---- filter tuning (see module docstring for how these were chosen) ----
ALPHA = 0.10
BETA = 0.003

# ---- arming (spec section 4, amended -- see module docstring, "THIRD BUG") ----
ARM_MIN_DIST = 80.0          # m -- dRel_at_hot_start must exceed this (anchor semantics changed,
                             # see docstring -- this is NOT "distance at first sight" anymore)
PRESENCE_PERSIST_S = 0.30    # s -- continuous presence required before the arming gate evaluates
HOT_A_REQ = 0.30             # m/s^2 -- a_req(v_filt) must clear this...
HOT_PERSIST_S = 0.5          # ...continuously for this long before arming (kills noise)
STOP_MARGIN = 6.0            # m -- same STOP_DISTANCE long_mpc.py uses

# ---- command while latched (spec section 6) ----
FLOOR = -0.40                # m/s^2 -- softest command once armed; see hook 10 C (ABANDON)
CAP = -1.2                   # m/s^2 -- hardest command this hook may ever issue (A_CRUISE_MIN)
JERK_ARM = 1.5               # m/s^3 -- rate limit on the FALLING edge only, first armed frames

# ---- release (spec section 6, amended -- see module docstring) ----
RELEASE_DIST = 20.0          # m -- absolute backstop regardless of stock
LEAD_LOST_S = 1.0            # s -- release if the lead itself is lost this long


class _RangeRateFilter:
  """[x, v] filter on `leadOne.dRel`. Position measurement -- NOT radard.py's KF1D, which
  measures a Doppler velocity into a [SPEED, ACCEL] state. See module docstring."""

  def __init__(self, alpha: float, beta: float, dt: float = DT_MDL):
    self.alpha = alpha
    self.beta = beta
    self.dt = dt
    self.x = None
    self.v = 0.0

  def reset(self, x0: float) -> None:
    self.x = x0
    self.v = 0.0

  def update(self, z: float) -> float:
    """Feed one dRel measurement, return the filtered closing rate (m/s, negative = closing)."""
    if self.x is None:
      self.reset(z)
      return self.v
    x_pred = self.x + self.v * self.dt
    residual = z - x_pred
    self.x = x_pred + self.alpha * residual
    self.v = self.v + (self.beta / self.dt) * residual
    return self.v


class FarLeadPreBrake:
  """One instance, owned by grt.hooks. See the module docstring for the full design."""

  def __init__(self):
    self._reset()

  def _reset(self) -> None:
    self.filt = _RangeRateFilter(ALPHA, BETA)
    self.present_s = 0.0
    self.absent_s = 0.0
    self.hot_elapsed = 0.0
    self.armed = False
    self.last_emitted = None
    self.last_known = None         # (dRel, vRel_range) held across a brief dropout while armed
    self.dRel_at_hot_start = None  # dRel at the first frame a_req_filt crossed HOT_A_REQ --
                                    # see step() and module docstring, "THIRD BUG"

  def step(self, present: bool, dRel: float, vRel_model: float, v_ego: float,
           relaxed: bool, long_active: bool, driver_input: bool, stock_min: float) -> list:
    if not relaxed or not long_active or driver_input:
      self._reset()
      return []

    if present:
      if self.present_s == 0.0 and not self.armed:
        self.filt.reset(dRel)          # fresh lock -- stale filter state would mislead
      self.present_s += DT_MDL
      self.absent_s = 0.0
      v_filt = self.filt.update(dRel)
    else:
      self.absent_s += DT_MDL
      self.present_s = 0.0
      self.hot_elapsed = 0.0
      self.dRel_at_hot_start = None
      v_filt = None

    if not self.armed:
      if not present or self.present_s < PRESENCE_PERSIST_S:
        self.hot_elapsed = 0.0
        self.dRel_at_hot_start = None
        return []

      v_lead_filt = v_ego + v_filt
      a_req_filt = (v_ego ** 2 - v_lead_filt ** 2) / (2.0 * max(dRel - STOP_MARGIN, 1.0))
      if a_req_filt > HOT_A_REQ:
        if self.hot_elapsed == 0.0:
          # anchor once, at the first frame the streak goes hot -- NOT at first presence
          # (removed with the absence gate) and not re-checked live every frame thereafter
          # (that reintroduces the v1 stopped-lead bug -- see module docstring)
          self.dRel_at_hot_start = dRel
        self.hot_elapsed += DT_MDL
      else:
        self.hot_elapsed = 0.0
        self.dRel_at_hot_start = None
        return []
      if self.hot_elapsed < HOT_PERSIST_S:
        return []
      if self.dRel_at_hot_start is None or self.dRel_at_hot_start <= ARM_MIN_DIST:
        return []

      # ---- ARM. First frame emits the floor, never the full formula -- see module docstring
      # on JERK_ARM: the point is that a noisy lock cannot step straight to -1.2.
      self.armed = True
      self.last_emitted = FLOOR
      self.last_known = (dRel, min(vRel_model, v_filt))
      return [(FLOOR, LongitudinalPlanSource.lead0, should_stop(v_ego, FLOOR))]

    # ---- already armed ----
    if present:
      vRel_range = min(vRel_model, v_filt)
      eff_dRel, eff_vRel_range = dRel, vRel_range
      self.last_known = (eff_dRel, eff_vRel_range)
    else:
      if self.absent_s > LEAD_LOST_S or self.last_known is None:
        self._reset()
        return []
      eff_dRel, eff_vRel_range = self.last_known

    if eff_dRel < RELEASE_DIST:
      self._reset()
      return []
    if present and eff_vRel_range >= 0:
      self._reset()
      return []

    v_lead_range = v_ego + eff_vRel_range
    a_req = (v_ego ** 2 - v_lead_range ** 2) / (2.0 * max(eff_dRel - STOP_MARGIN, 1.0))
    target = max(CAP, min(-a_req, FLOOR))
    if target >= self.last_emitted:
      out = target                                          # rising (softer) -- immediate
    else:
      out = max(target, self.last_emitted - JERK_ARM * DT_MDL)   # falling -- rate-limited
    self.last_emitted = out
    cand = [(out, LongitudinalPlanSource.lead0, should_stop(v_ego, out))]

    if stock_min <= FLOOR:
      self._reset()          # stock has caught up -- hand off starting next frame
    return cand
