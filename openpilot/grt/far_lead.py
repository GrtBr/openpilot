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

A FOURTH BUG, FOUND ON A REAL DRIVE (2026-08-28), THE OPPOSITE FAILURE DIRECTION: the
2026-08-27 fix made the hook arm when it should have; this one made it hold the floor long
after it should have let go. Operator reported the car keeping an oversized, oscillating gap on
the highway -- braking on approach, then braking again on every subsequent gentle re-approach,
never settling. Measured on a real 54-minute highway drive (route 00000143): the hook won
`min()` for 300.6 s of 3233.9 s total (9.3% of the ENTIRE drive), 60.5 s of that at highway
speed (>22 m/s) across 11 separate arm events.

Root cause is NOT filter noise producing a false average -- checked directly, frame by frame,
against several of the offending runs. The filter is working as designed: `_RangeRateFilter`
correctly detects real, short-lived closing transients that the model's own per-frame `vRel`
head does not report (one instrumented example: dRel fell 111.4 -> 104.9 m in ~0.5 s while raw
`vRel` read only -0.57 to -0.93 m/s; `v_filt` correctly integrated this into -4.1 m/s). The
actual defect: once armed, the ONLY way to release (short of the lead being lost, `dRel < 20`,
or stock itself reaching `<= FLOOR`) was `eff_vRel_range >= 0` -- the pessimistic pairing
`min(vRel_model, v_filt)` reaching fully non-negative. On ordinary noisy highway data this is a
much stricter bar than "the transient that triggered arming has resolved": `v_filt` has slow
dynamics by design (that is what makes it noise-resistant) and can take many seconds to
decay back through zero after even a brief closing pulse, holding the floor the entire time even
though the real gap has been flat or oscillating for seconds already. One instrumented example:
armed for 9.35 s while dRel oscillated 82-92 m the whole time (never trending), because `v_filt`
lingered in a shallow -0.4 to -1.8 m/s band and never crossed back to >= 0.

Fix: gate BOTH arming and continued-armed status on the same closing-rate floor,
`HOT_CLOSING_RATE` (2.78 m/s, ~10 km/h -- operator's proposed number, validated against real
data before adopting). Arming additionally requires `v_filt <= -HOT_CLOSING_RATE`, not just
`a_req_filt > HOT_A_REQ` alone (a_req's distance-scaling means small closing rates at long range
can already clear 0.30 on their own -- validated: this alone only cut highway false-arm time
60.5 -> 47.8 s, most of the problem survived). Release additionally fires once
`eff_vRel_range > -HOT_CLOSING_RATE`, not only once it reaches fully non-negative -- this is
what does most of the work (60.5 -> 19.3 s of highway time, and a chunk of that remaining 19.3 s
is a verified GENUINE hard approach, dRel 113 -> 20 m in ~8 s, correctly kept armed, not a
defect). Checked for re-arm chatter from releasing sooner (each re-arm calls `_reset()`, wiping
the filter to `v=0`): 3 short re-arm clusters (<5 s apart) out of 17 total events across the
54-minute drive -- present but not frequent enough to justify asymmetric arm/release thresholds
(hysteresis) over the single shared constant. Costs ~0.25-0.6 s of armed time on both prior
validated incidents (2026-08-27: 4.0 -> 3.75 s; 2026-08-25: 6.3 -> 5.69 s) -- same floor
severity while armed, released slightly sooner. Operator explicitly signed off on this tradeoff
after seeing both numbers, since the arming-gate-only fix left ~80% of the reported problem
unaddressed.

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
`[-2.0, -0.40]` (widened from `-1.2` 2026-08-31, attempt 5 -- see that section above; `-2.0` is
still well inside the real vehicle-level clamp `ACCEL_MIN=-3.5` in `opendbc/car/interfaces.py`,
and hook 2's own `HAZARD_ACCEL_MIN=-1.5` in `grt/scc_map.py` is existing fork precedent for a
harder-than-old-CAP bound) and only ever competes inside the planner's `min()`, so it can never
make braking weaker than stock. `FLOOR` is -0.40, not something softer, because hook 10 layer C
(`ABANDON = -0.20` in `grt/throttle_hold.py`) would otherwise eat a milder request. Every gate
(personality, `longActive`, driver input) is re-checked every frame and any exception drops
straight to `[]`, so a wedged state cannot outlive one bad frame's inputs.

FLOOR EXPERIMENT, 2026-08-31: TRIED 0.00, REVERTED TO -0.40 -- A SIXTH BUG, FOUND ON A REAL DRIVE
---------------------------------------------------------------------------------------------------
Motivated by a fifth finding, distinct from the four bugs above: a real 4-pulse cluster
(2026-08-28 drive, ~18 s, vEgo 54->39 km/h) where hook 11 armed on top of a lead-following
approach STOCK WAS ALREADY HANDLING -- stock's own candidate was at -0.31 to -0.70 in the frames
immediately before each arm, nowhere near the ~0.04 coasting baseline seen in the genuinely-
needed events. Hook 11 exists to cover stock being ASLEEP at long range with an understated
`vRel` -- here stock wasn't asleep. The precise, targeted fix this points to is an ARM-TIME gate
on `stock_min` (already passed into `step()`, currently used only for release) -- don't arm at
all if stock is already braking meaningfully. STILL NOT IMPLEMENTED (see below for why the
priority order changed). At the operator's request, `FLOOR` alone was lowered to 0.00 first, for
one real test drive, to observe the effect directly before committing to a gate design.

The disclosed, PREDICTED consequence (hook 10 layer C's `ABANDON = -0.20` erasing the first 3
frames / 0.15 s of every arm in cruise-headroom conditions) was real but turned out to be the
SMALLER problem. The actual failure, found on the test drive (2026-08-31, ~10:13, a genuine
~119 km/h approach with dRel collapsing toward 70 m and closing rate reaching -13 to -16 m/s):
hook 11 armed correctly and tracked its own predicted ramp exactly for 4 frames (confirmed via
side-by-side replay against the real published `aTarget`: -0.225, -0.300, -0.375, -0.450, both
sequences matching to the millivolt), then SELF-RELEASED and stayed inert while the real,
serious approach continued to develop for another full second, handled from then on by stock's
own (slower, independently-arrived-at) response.

Root cause: the release condition below, `stock_min <= FLOOR`, is a fixed-threshold check by
design (see "WHY A FIXED THRESHOLD, NOT HOOK 11's OWN LIVE VALUE" below) -- and that threshold
is `FLOOR` itself. At `FLOOR = -0.40`, "stock caught up" meant stock was genuinely braking
meaningfully before handoff was considered safe. At `FLOOR = 0.00`, the exact same check became
`stock_min <= 0.00` -- true almost constantly in ordinary driving (any coast, any mild lead
response, anything not actively accelerating) -- so the hook released almost immediately after
every arm, regardless of whether the danger had actually resolved. Confirmed directly: the
published value the frame after the real self-release was -0.062, which clears the OLD
threshold (`<= 0.00`, releases) but would NOT clear a `-0.40` threshold (stays armed) --
consistent with the fix described below.

Lowering `FLOOR` softened the arm-frame severity as intended, but silently broke a SECOND,
unrelated meaning the same constant carried: the bar for "stock has genuinely woken up and it's
safe to hand back." That coupling is not a coincidence to patch around quietly -- it is why this
file no longer overloads `FLOOR` for both purposes going forward (see the arm-time `stock_min`
gate still pending above, which was always the more targeted fix for the original fifth
finding, once the sixth finding made clear that touching `FLOOR` reopens more than the one
interaction that was disclosed up front).

REVERTED. `FLOOR` restored to -0.40. The fifth finding (arming on top of a stock-handled
approach) remains open and still points to the `stock_min` arm-time gate, decoupled from
`FLOOR`'s value, as the next real fix to design and validate.

WHY A FIXED THRESHOLD, NOT HOOK 11's OWN LIVE VALUE
----------------------------------------------------
The release check compares `stock_min` against the constant `FLOOR`, not against whatever hook
11 itself is currently computing (`self.last_emitted` / `target`, which climbs toward `CAP` as
the approach develops). Comparing against the live value was considered and rejected: it turns
release into a chase where stock must out-escalate a number that is itself still climbing,
making the hook stickier than intended in exactly the fast-developing approaches where handoff
should be easiest to earn. It also ties the release decision to hook 11's own filtered internal
state (`v_filt`, `eff_dRel`), which is noisier than a fixed reference. `FLOOR` as a fixed
threshold means "has stock met the MINIMUM guarantee hook 11 promised on arming" -- a stable
trust bar, not a moving target -- which is the right design as long as `FLOOR` itself still
means "stock is genuinely braking," per the bug above.

`a_req` IS WRONG FOR A MOVING LEAD -- DO NOT "FIX" IT WITHOUT READING captains_log.md 2026-08-31
--------------------------------------------------------------------------------------------------
`a_req = (v_ego**2 - v_lead**2) / (2*d)` was, until attempt 5 below, used at both the arming gate
and the severity formula; it is only exact when the lead is stationary, and the physically correct
relative-motion form for a moving lead is `v_filt**2 / (2*d)`. This is a real, confirmed bug, not a
matter of opinion. FOUR independent, differently-shaped attempts to fix it were made the same day
(2026-08-31, `captains_log.md` has the full numbers for all four); the first three were reverted:
(1) correcting the formula everywhere, including the arming gate -- fails because no `HOT_A_REQ`
recovers the deployed arming envelope; (2) correcting only the post-arm severity formula, leaving
arming untouched -- fails because it closes 2.5-10.8 m tighter (less speed bled by handoff) on the
two founding incidents than the deployed formula; (3) correct kinematics plus an explicit,
separately-tuned speed-scaled margin term -- fails because any margin strong enough to recover
incident (1)'s lost handoff distance produces MORE gratuitous full-CAP braking on ordinary highway
following (65% of armed time) than the "wrong" formula it would replace (29%).

CORRECTION, attempt 5 (below): the guidance this paragraph used to end on -- "measure any attempt
against a route143 CAP-time-fraction bound (<=13.6%) from the start" -- is NOT a sufficient test on
its own. Attempt 5 passed that bound (10.8%) and still failed the `gap_at_release` test that sank
attempt 2, worse than attempt 2 did. Any future attempt on this formula must clear BOTH the
CAP-time-fraction bound AND `gap_at_release` against the git-blob-pinned deployed baseline
(methodology in captains_log.md 2026-08-31) before being considered validated -- not just deployed,
which is a lower bar (see next section).

ATTEMPT 5, DEPLOYED DESPITE FAILING VALIDATION -- OPERATOR OVERRIDE, 2026-08-31
--------------------------------------------------------------------------------
The formula and constants below (`a_req` now `v_filt**2/(2d)` at BOTH the arming gate and the
active-command severity, `HOT_A_REQ=0.10`, `CAP=-2.0`) are attempt 5 from the section above --
`captains_log.md` 2026-08-31 has the full sweep. Validated BEFORE deployment and found to FAIL:
`gap_at_release` regresses 6.61-16.65 m vs the true deployed baseline on all three genuine founding
incidents (route139's -16.65 m is 6.7x attempt 2's -2.5 m on the SAME route -- strictly worse, not
a milder version of the same problem), and a real arm the pre-attempt-5 formula caught (route14f
t+127.39s, dRel=83.6 m) is missed entirely under the corrected formula + `HOT_A_REQ=0.10` (a_req
there peaks at 0.087, never crosses 0.10). Both advisor consultations this session recommended
against deploying this. The operator reviewed those numbers and explicitly chose to deploy anyway,
for a real-world test drive (day after logging), overriding that recommendation.

This is documented here, not silently, so a future reader (including a future session) does not
mistake "this is what's running" for "this was found to be correct" -- it was found to be WORSE
than what it replaced on the exact test that matters, and shipped anyway as a deliberate,
informed field experiment, not a validated fix. If the test drive reproduces a handoff-margin
problem -- braking that resolves with LESS distance/speed shed to the lead than the prior
(`a69672e67`/`2d4473136`) formula would have on a comparable encounter -- that is the PREDICTED
failure mode from the simulation above, not a surprise requiring fresh diagnosis.

REVERT PATH: this is one self-contained commit (the two formula sites, `HOT_A_REQ`, `CAP`, and
this docstring/the SAFETY section's bound text -- no other file's runtime behavior changed).
`git revert` it to restore the `2d4473136` state (old formula, `HOT_A_REQ=0.30`, `CAP=-1.2`)
that all four prior attempts' validation converged on as the one that actually held up.
"""
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalPlanSource
from openpilot.selfdrive.controls.lib.drive_helpers import should_stop

# ---- filter tuning (see module docstring for how these were chosen) ----
ALPHA = 0.10
BETA = 0.003

# ---- arming (spec section 4, amended -- see module docstring, "THIRD BUG") ----
ARM_MIN_DIST = 70.0          # m -- dRel_at_hot_start must exceed this (anchor semantics changed,
                             # see docstring -- this is NOT "distance at first sight" anymore).
                             #
                             # 80 -> 70 m on 2026-09-04, OPERATOR DECISION under acknowledged
                             # measurement uncertainty. The honest position at the time: the
                             # evidence that had argued for keeping 80 m was contaminated (the
                             # old REAL/FALSE classifier scored a correctly-braked approach as a
                             # false positive, because a handled approach does not collapse the
                             # gap), and the uncontaminated replacement could only classify ~35%
                             # of arms -- the rest unknowable without per-object tracking, since
                             # leadOne is an anonymous slot. On the measurable subset 70 m scored
                             # 10 justified / 1 unjustified. Operator elected to settle it on the
                             # road and report ghost braking if it appears. REVERT TO 80.0 if it
                             # does; that is the whole rollback.
THRESH_SCALE_DIST = 80.0     # m -- reference distance for the distance-neutral threshold below.
                             # DELIBERATELY NOT ARM_MIN_DIST, though they were equal until
                             # 2026-09-04. Tying them means moving the arming floor also relaxes
                             # the far-field threshold (at 110 m, 70/110 instead of 80/110), i.e.
                             # two changes at once and an unreadable road test. Keep this pinned
                             # to the distance the scaling was VALIDATED at.
PRESENCE_PERSIST_S = 0.30    # s -- continuous presence required before the arming gate evaluates
HOT_A_REQ = 0.10             # m/s^2 -- a_req(v_filt) must clear this... LOWERED from 0.30,
                             # 2026-08-31, attempt 5 (deployed despite failing validation -- see
                             # module docstring "ATTEMPT 5, DEPLOYED DESPITE FAILING VALIDATION")
# DISTANCE-NEUTRAL ARMING THRESHOLD, 2026-09-04. Arming requires a_req = v^2/(2*(d-STOP_MARGIN))
# to clear HOT_A_REQ, which means the CLOSING RATE it demands is v_req = sqrt(2*HOT_A_REQ*(d-6)) --
# a rate that GROWS with distance: 3.85 m/s at 80 m but 4.56 m/s at 110 m. A far-lead feature was
# therefore hardest to trigger exactly where it is meant to work, and measurement showed this is
# the dominant cost: 76% of arming latency on a real drive sat on this one condition
# (captains_log 2026-09-04 (b)).
#
# Scaling the threshold by ARM_MIN_DIST/dRel almost exactly cancels the (d-6) growth inside the
# square root, making v_req flat at ~3.85-3.90 m/s at every distance -- which is what the feature
# always meant. Measured over 11 drives: 12 shared arms fired earlier (median +0.21 s, max
# +3.05 s), arms beyond 100 m went from 1 to 5, ZERO real arms lost, false arms unchanged at 3.
#
# SAFETY: min(1.0, ...) makes this EXACTLY 1.0 for dRel <= ARM_MIN_DIST, so the threshold is
# bit-identical to the old constant at and below 80 m, and ARM_MIN_DIST already requires the
# anchor above 80 m. It can only ever RELAX, never tighten, so it cannot cause a missed arm that
# the previous criterion would have caught.
HOT_A_REQ_MIN_SCALE = 0.60   # floor on that scaling, so the relaxation is BOUNDED rather than
                             # open-ended. It binds only beyond ~133 m; the model's own hard
                             # ceiling is 139.11 m and the published signal effectively stops near
                             # 120 m, so on measured data this floor never activates -- it is a
                             # guard against an unseen regime, not a tuning knob.
HOT_CLOSING_RATE = 2.78      # m/s (~10 km/h) -- ...AND v_filt must be closing at least this fast
                             # ("FOURTH BUG" in module docstring -- a_req alone can clear 0.30 on
                             # tiny closing rates at long range, which is correct for a genuine
                             # slow-pack approach but also fires on highway measurement noise)
HOT_PERSIST_S = 0.5          # ...continuously for this long before arming (kills noise)
STOP_MARGIN = 6.0            # m -- same STOP_DISTANCE long_mpc.py uses

# ---- command while latched (spec section 6) ----
FLOOR = -0.40                # m/s^2 -- softest command once armed; see hook 10 C (ABANDON).
                             # Reverted here 2026-08-31 after a 0.00 experiment caused a real
                             # self-release failure on a live drive -- see module docstring
                             # "FLOOR EXPERIMENT" for the full story before changing this again
CAP = -2.0                   # m/s^2 -- hardest command this hook may ever issue. WIDENED from
                             # -1.2, 2026-08-31, attempt 5 (deployed despite failing validation --
                             # see module docstring). Bounded well inside the real vehicle-level
                             # clamp ACCEL_MIN=-3.5 (opendbc/car/interfaces.py); hook 2's own
                             # HAZARD_ACCEL_MIN=-1.5 (grt/scc_map.py) already exceeds the old -1.2.
JERK_ARM = 1.5               # m/s^3 -- rate limit on the FALLING edge only, first armed frames

# ---- release (spec section 6, amended -- see module docstring) ----
RELEASE_DIST = 20.0          # m -- absolute backstop regardless of stock
LEAD_LOST_S = 1.0            # s -- release if the lead itself is lost this long


def hot_a_req_for(dRel: float) -> float:
  """Effective arming threshold at this distance. See HOT_A_REQ_MIN_SCALE for the full rationale.

  Returns exactly HOT_A_REQ at or below THRESH_SCALE_DIST, and a bounded relaxation beyond it, so
  the CLOSING RATE required to arm is roughly constant with distance instead of growing.

  Keyed on THRESH_SCALE_DIST, not ARM_MIN_DIST: the two are independent knobs and were only
  briefly equal. See THRESH_SCALE_DIST for why coupling them would be a mistake.
  """
  if dRel <= THRESH_SCALE_DIST:
    return HOT_A_REQ
  return HOT_A_REQ * max(HOT_A_REQ_MIN_SCALE, THRESH_SCALE_DIST / dRel)


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

      # CORRECTED relative-motion kinematics, 2026-08-31, attempt 5 (deployed despite failing
      # validation -- see module docstring "ATTEMPT 5, DEPLOYED DESPITE FAILING VALIDATION").
      # `v_filt` IS the closing rate; the old `(v_ego**2 - v_lead**2)/(2d)` form (exact only for
      # a stationary lead) is gone from this site.
      a_req_filt = (v_filt ** 2) / (2.0 * max(dRel - STOP_MARGIN, 1.0))
      # a_req alone can clear HOT_A_REQ on a tiny closing rate at long range -- correct for a
      # genuine slow-pack approach, but also fires on ordinary highway measurement noise (see
      # module docstring, "FOURTH BUG"). Require a real closing rate too.
      # Distance-neutral threshold (2026-09-04): identical to HOT_A_REQ at/below ARM_MIN_DIST,
      # bounded relaxation beyond it. Applied HERE ONLY -- the armed branch's severity formula
      # must stay physically exact, since it decides how hard to brake, not whether to.
      if a_req_filt > hot_a_req_for(dRel) and v_filt <= -HOT_CLOSING_RATE:
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
    # Release once no longer closing FAST, not only once fully non-negative -- see module
    # docstring, "FOURTH BUG". v_filt's slow dynamics mean "fully >= 0" is a much stricter bar
    # than "the transient that triggered arming has resolved" on noisy real data.
    if present and eff_vRel_range >= -HOT_CLOSING_RATE:
      self._reset()
      return []

    # CORRECTED relative-motion kinematics -- see arming-gate comment above and module docstring
    # "ATTEMPT 5, DEPLOYED DESPITE FAILING VALIDATION".
    a_req = (eff_vRel_range ** 2) / (2.0 * max(eff_dRel - STOP_MARGIN, 1.0))
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
