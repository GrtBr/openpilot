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

ANCHOR ON FILTERED RANGE, 2026-09-14
------------------------------------
`dRel_at_hot_start` was a single RAW `dRel` sample. Measured across a 29-route corpus, that one
sample decides arming far more often than it should: ~58% of hot streaks that had ALREADY cleared
every other gate (closing rate, a_req, persistence) died on the ARM_MIN_DIST test alone, and ~12%
of them sat within +-5 m of the line -- inside dRel's own 4-6 m frame-to-frame noise. It now
anchors on `self.filt.x`, the same alpha-beta filter's position state.

BE CLEAR ABOUT WHAT THIS BUYS -- it is an accuracy fix, and it is NOT bias-free. Measured at 139
hot-start frames against a non-causal centred +-0.5 s fit of dRel (evaluation reference only):

    raw dRel   median error -2.14 m, 12% above reference, median |err| 2.28 m
    filt.x     median error +0.93 m, 73% above reference, median |err| 1.26 m

Two effects, both pushing the same way. The raw sample is biased LOW by ~2.1 m because hot-start
is not a random frame: the streak begins exactly when a_req and the closing rate cross threshold,
which happens preferentially on frames where the sample dipped below trend. Removing that is a
genuine correction. But `filt.x` then overshoots HIGH by ~0.9 m of filter lag -- the filter is
reset on every presence re-lock, so it is seldom in steady state. Net, absolute anchor error
roughly halves (2.28 -> 1.26 m) while the effective gate relaxes by ~3 m of true range.

ARM_MIN_DIST 70.0 -> 73.0 COMPENSATES FOR THAT ~3 m. Keeping 70.0 would have silently relaxed the
gate on top of the deliberate 80 -> 70 move of 2026-09-04, and the corpus shows that relaxation
buys the WRONG arms: at 70.0 the 39 added arms clear the "lead genuinely slower" bar only 29% of
the time, against a 45-46% baseline; at 73.0 the 20 added arms clear it at 44%, i.e. they are as
good as the arms already being taken.

Corpus effect, 29 routes, anchor + 73.0 vs the pre-change baseline: arms 171 -> 190, arms reaching
CAP 17 -> 26, all 17 of the baseline's CAP events still matched, 1 non-CAP baseline arm lost.

OPEN CONCERN, deliberately not papered over: of the 9 NEW arms that reach CAP, only 2 are on a
genuinely slower lead (baseline CAP arms score 8/17). A false arm at FLOOR costs ~1.7 s of 0.04 g
and is cheap; a false arm at CAP is -2.0 m/s^2 and is not. Watch for unexplained hard braking on
the road and report it. ROLLBACK is two lines: restore `self.dRel_at_hot_start = dRel` and
ARM_MIN_DIST to 70.0.

DOES NOT ADDRESS the KNOWN LIMITATION above: the anchor is still captured once and frozen, so an
encounter whose hot streak begins inside ARM_MIN_DIST still fails forever. Separate change.

OBJECT-SWITCH GUARDS + ARM_MIN_DIST 73 -> 65, 2026-09-15
--------------------------------------------------------
THE DEFECT. `leadOne` is an anonymous slot. When radard's lead switches to a different, nearer
object, dRel steps (single frames of 4-26 m, level shifts of 7-43 m within 0.25 s on the corpus),
and the alpha-beta filter DIFFERENTIATED that step: a 40 m step becomes ~-24 m/s of closing
(beta/dt * sum of the decaying residuals = 0.06 * 40/alpha), which `a_req = v^2/(2d)` then squares.
Every one of the 26 arms that reached CAP on the 29-route corpus under the 2026-09-14 code followed
such a switch within ~1.5 s. By channels independent of hook 11 (post-switch step-excluded closing,
lead0_v - vEgo, stock's own demand with the hook's frames removed), 10 of those 26 were traffic
pulling AWAY -- a -2.0 m/s^2 brake on nothing -- and 8 of the 10 were admitted by the 09-14
filtered anchor, whose `filt.x` lagged ABOVE ARM_MIN_DIST after the step while the raw range was
already 34-54 m. That is the "OPEN CONCERN" of the section above, confirmed. The deployed code
caught the genuinely real ones by the same accident: phantom velocity plus a lagging anchor.

THE FIX. A range change that motion cannot produce is a NEW OBJECT, not a velocity. On such a frame
`_RangeRateFilter` re-initialises on the new range (`stepped` = True) and the hook restarts presence
persistence, the hot streak and the anchor, so the new object must earn an arm on its own evidence.
STEP GUARD REMOVED 2026-09-24, OPERATOR DECISION -- "it needs refinement and I'll work on it
later". Only test 2 (the physical bound) is live. Restore from `7997662e0` (far_lead.py, the STEP block
in `_RangeRateFilter.update` and the STEP_GATE_M / STEP_FRAMES constants). What removing it does,
replayed on c7+c8+cf+d7 (FINDINGS 33): arming unchanged (56 arms either way, since the band-slope
gate does not read v_filt); time at -1.0 or harder 16.6 -> 23.6 s, at CAP 4.1 -> 5.8 s. On d7 every
arm that got harder followed a camera range jump that preceded a REAL approach (bookmark 3's first
1.5 s -0.42 -> -0.80). The exposure is the case this was built for: a jump to a nearer, DIFFERENT
object is differentiated into phantom closing and squared by a_req, up to CAP. On this car there
is no radar -- every jump is a camera range jump.
Originally two tests, in this order:
  1. STEP (REMOVED): median of the last 3 raw samples vs the 3 before differs by > STEP_GATE_M (12 m). 30 m/s
     of real closing moves 4.5 m across those frames. One such frame is COASTED (not fed to the
     filter); STEP_FRAMES (2) consecutive frames re-initialise.
  2. PHYSICAL BOUND, for switches that slide over several frames instead of stepping: over
     PHYS_SPAN_S (0.8 s then, 0.45 s since 2026-09-24; two 5-sample medians) the range may not close faster than ego speed (a
     lead cannot reverse) or open faster than PHYS_OPEN_MPS (15 m/s), each + PHYS_MARGIN_M (8 m).
A single-frame gate was measured and rejected: at 73-100 m ordinary |frame-to-frame| dRel changes
have sd 3.8 m and p99 12.4 m, so "> 8 m in one frame" fires on several percent of normal frames.
Clamping v_filt with the model's velocity head was also rejected: against non-causal truth its gain
is 0.10 at 60-80 m and 0.05 at 80-100 m, and it reads "not closing" on 52-69% of genuinely fast
closes beyond 60 m -- it can confirm a close, never refute one.
Holding x/v through short absences was tested and dropped (neutral on the corpus).

ARM_MIN_DIST 73 -> 65, OPERATOR DECISION, PAIRED WITH THE GUARDS. With honest velocity, a real
object that appears through a switch is first seen confidently at 55-72 m, i.e. inside 73 m, and
hook 11 could not arm on it at all. 65 m widens hook 11's scope to part of that band. Corpus replay
(31 TSVs, relaxed only, stock_min = 0 so hardest commands and releases are upper bounds; the
guards-only row at 73 m for comparison):

                          spurious CAP  real CAP  arms  real closes caught  added arms: slower lead / not closing
    09-14 code (73 m)        10/10        7/7      190        49/87          (own arms: 43% not closing)
    guards, 73 m              0/10        0/7      161        46/87           3/7  /  4/7
    guards, 65 m  <- THIS     0/10        1/7      204        56/87          17/25  /  8/25
    no guards, 65 m          10/10        7/7      233        58/87          13/23  / 10/23

Expect: no hard braking on traffic pulling away after a lead switch; softer arms (about -0.5 to
-1.0) on real objects that appear at 55-70 m, where stock was already braking on 4 of 7; about +7%
arms overall, the added ones mostly FLOOR arms (median -0.40) at 65-73 m, two thirds of them on a
genuinely slower lead -- better than the 09-14 code's own arms. One spurious event still reaches
-1.97 in replay (not CAP). The two CAP events kept are the
two whose lead was genuinely near-stopped (1.1 and 3.2 m/s against ego 14-16 m/s).
"no guards, 65 m" is the row that must never ship: the guards are what make 65 m acceptable.

COST ON ORDINARY DRIVING, measured over all 31 TSVs (not just CAP encounters): the guards fire ~135
times per hour of eligible lead time (once per ~12 s at 73-100 m, once per ~48 s at 40-73 m). The
STEP test is the noisy one: after ~37% of its resets the raw range is back within 4 m a second later
(noise, or a sub-second switch and back); the physical bound is ~62% clean switches. A reset during
a genuine approach restarts presence + hot persistence, so it delays that arm by >= 0.8 s. Of the 43
arms the 09-14 code made and this code does not, 28 follow a clean switch (including the 10 spurious
CAP arms) and 6 follow a noise-like reset. Arms both versions make are unchanged: 112/134 non-CAP
shared arms have identical timing (p90 +0.05 s) and 104/134 an identical hardest command.

ROLLBACK: ARM_MIN_DIST = 73.0 alone gives the "guards, 73 m" row. `git revert` of this commit
restores the 2026-09-14 code. Evidence: analysis/lead_filter/tracker/FINDINGS.md §20;
captains_log.md 2026-09-15.
BAND-SLOPE GATE, 2026-09-22 -- THE ARMING GATE REPLACED
-------------------------------------------------------
The presence/hot-streak/ARM_MIN_DIST gate is no longer what arms this hook. It is replaced by a
gate on the SLOPE OF A LOWER BOLLINGER BAND over the model's range series:

    band  = mean(BAND_N) - BAND_K * stdev(BAND_N)   of dRel_model
    slope = OLS fit over the last SLOPE_N band samples

ARM when that slope CROSSES below ARM_SLOPE, on a frame with a radar lead present and
dRel > HANDOFF_DIST. RELEASE when it crosses back above RELEASE_SLOPE, or eff_dRel falls under
HANDOFF_DIST, or stock reaches FLOOR, or the lead is lost. No persistence timer on anything: the
5.0 s mean and the 2.0 s slope fit ARE the persistence.

WHY THE BAND AND NOT THE MEAN. For steady closing the band's slope settles at EXACTLY the true
closing rate, but the -BAND_K*stdev term makes it get there ~2.5 s sooner, because stdev grows
while range is falling. Removing the stdev term is NOT a trade of opening speed against release
speed -- measured, it costs ~4 s at BOTH ends. After closing stops, a plain mean approaches zero
slope asymptotically FROM BELOW and never crosses it; the stdev collapsing is the only thing that
lifts the band slope back through zero. Alternatives measured and rejected on the same corpus:
the UPPER band (mean + K*stdev) and a raw 40-frame slope on dRel both produced 6 premature
releases each out of 17-18 gates. See FINDINGS.md 21.

WHY A RANGE BAR. Across 14 logged hook-11 spans, stock reached FLOOR exactly once, at 46.7 m; on
the other 13 its best command over the whole span stayed within [-0.165, +0.192] while this hook
was armed at 60-87 m. Above ~50 m stock is not acting; below it stock owns the approach. Adding
the bar raised median closure rate over the gate from +1.57 to +2.09 m/s and cut dead time at the
end of the gate from 0.70 s to ~0.2 s. See FINDINGS.md 22.

IT ARMS ON A CROSSING, WHICH HAS A REAL EDGE. A series that is ALREADY closing faster than
ARM_SLOPE when the slope first becomes defined never crosses the bar, and never arms -- the gate
needs to have seen the slope above ARM_SLOPE first. Real approaches begin from a steadier gap, so
this did not cost an arm on the corpus, but it is the reason every arming test warms the band on
a steady range first. A level test instead of a crossing was rejected: it re-arms on every frame
the slope sits past the bar.

THE v_filt RELEASE HAD TO GO, AND THIS IS NOT OPTIONAL. The old release
`eff_vRel_range >= -HOT_CLOSING_RATE` cannot coexist with this gate. The whole point of the band
slope is that it fires BEFORE the range-rate filter has converged; on the frame after arming,
`eff_vRel_range` is still near zero, so that test would fire immediately and every span would
collapse to one or two frames. The two are mutually exclusive by construction.

RESIDUAL RISK, AND WHAT THE FIELD TEST IS WATCHING. Dropping that release means a lead still
genuinely closing SLOWLY no longer releases on rate: it holds at FLOOR until closing stops or the
range falls under HANDOFF_DIST. That is the FOURTH BUG's shape, and it is the main thing this
change could get wrong. On the c7+c8 replay the spans did not run long -- median 3.10 s, max
11.20 s, 14 arms in 0.62 h (22.6/h, against the old gate's 16 arms / 25.8/h on the same data) --
but that is 0.62 h of evidence on two routes.

THE OLD CONSTANTS ARE RETAINED ON PURPOSE. HOT_A_REQ, HOT_PERSIST_S, PRESENCE_PERSIST_S,
ARM_MIN_DIST, THRESH_SCALE_DIST and hot_a_req_for() are all still defined and still tested, but
NOTHING in this file calls them any more. They are read by hook 11b's `_ArmMirror` (grt/hooks.py),
which shadows what the OLD gate would have armed on -- the comparator for this field test. Do not
delete them without retiring 11b, and do not reintroduce them into the arming path: a test asserts
hot_a_req_for() appears exactly once (its definition).

ROLLBACK is `git revert` of this commit; 108e5850e is the last pre-change tip.

HAND-OFF BAR DECOUPLED FROM FLOOR, 2026-09-22
---------------------------------------------
`HANDOFF_ACCEL` is now its own constant and the release reads it instead of `FLOOR`. It is
deliberately equal to `FLOOR` (-0.40), so this change is behaviour-neutral: replayed over c7+c8+cf
it produces the same 25 arms with the same durations and the same commands.

The two are different quantities and were only ever accidentally equal. `FLOOR` is an AUTHORITY
bar -- the softest command this hook may issue. `HANDOFF_ACCEL` is a TRUST bar -- how much braking
from someone else counts as "handled". Overloading them is what caused the 2026-08-31 sixth bug
(see "FLOOR EXPERIMENT"): lowering `FLOOR` to soften the command silently moved the release bar,
so "stock is genuinely braking" degenerated into "stock isn't accelerating" and the hook released
within frames of every arm. Tuning the authority bar must never drag the trust bar.

NOTE the replay cannot test this: the tracker harness feeds `stock_min = 0.0`, so the hand-off
never fires there. The change is covered by unit tests, one of which reproduces the 2026-08-31
setup exactly (FLOOR = 0.00, stock at -0.10) and asserts the hook now stays armed.

Hook 11c reads `HANDOFF_ACCEL` too, so the recorder measures the bar the hook actually releases on.
FINDINGS.md 25-26.

ARMING IS A LEVEL TEST, 2026-09-22
-----------------------------------
The gate arms when the band slope is AT OR BELOW `ARM_SLOPE` with a radar lead present and the
range above `HANDOFF_DIST`. Beyond `ARM_CONFIRM_DIST` that is enough; inside it, the conditions
must hold for `ARM_CONFIRM_FRAMES` consecutive frames. It previously required a CROSSING -- a transition from
at-or-above ARM_SLOPE to below it.

WHY. The crossing test had a real blind spot, documented when it shipped: a range series ALREADY
closing faster than ARM_SLOPE at the moment the slope first becomes defined produces no transition
at all, so the gate could never arm on it however hard the approach was. That is the state after
every gap in the model lead, and at route start. A level test cannot miss it.

WHAT IT COST, AND THE TWO GUARDS IT NEEDED. A level test re-arms the instant its condition is true
again, and the crossing was masking two sources of that:

  * ARM_CONFIRM_DIST / ARM_CONFIRM_FRAMES -- above 55 m the gate arms on the first qualifying
    frame; between HANDOFF_DIST and 55 m the condition must hold for 3 consecutive frames. Without
    some guard there
    the hook armed just over 50 m and released a frame or two later as the range dipped back
    under: 21 sub-2-frame spans on c7+c8+cf, 20 of them within 8 m of the bar, median arm range
    51.0 m. A 10 m hysteresis margin was tried first and fixed the chatter by refusing to arm
    between 50 and 60 m at all; the confirmation rejects the same chatter while keeping that band
    (5 arms recovered there, chatter equal). It costs 0.15 s on every arm, which is not free for a
    hook whose value is earliness.
  * RE_ARM_HOLD_S -- after a stock hand-off the slope is usually still past ARM_SLOPE, so without
    a hold the hook would re-arm next frame and oscillate for as long as stock kept braking.

RELEASE STAYS A CROSSING. A level release would re-fire every frame the slope sat above
RELEASE_SLOPE; there is no equivalent blind spot on that side, since the release condition is
reached by the approach resolving rather than by the buffers warming up.

MEASURED, c7+c8+cf (0.86 h): 25 arms both ways, same timestamps, median span 3.40 s both, armed
time 107.9 -> 110.9 s, sub-2-frame spans 2 -> 1. So on this corpus the change is behaviour-neutral;
its value is the blind spot it closes, which these three drives happen not to exercise.
FINDINGS.md 29.

"""
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalPlanSource
from openpilot.selfdrive.controls.lib.drive_helpers import should_stop

# ---- filter tuning (see module docstring for how these were chosen) ----
ALPHA = 0.10
BETA = 0.003

# ---- object-switch guards (module docstring, "OBJECT-SWITCH GUARDS", 2026-09-15) ----
PHYS_WINDOW = 14             # samples -- two 5-sample medians at either end of this window...
PHYS_SPAN_S = 0.45           # s -- ...whose centres are this far apart: (PHYS_WINDOW - 5) * DT_MDL.
                             # Was 21 / 0.8 s until 2026-09-24; shortened on operator decision to the
                             # smallest span that still clears the genuine arms on route 000001d7
                             # (FINDINGS 35). 0.40 s trips on bookmark 3's 119 -> 102 m jump 0.3 s
                             # before the arm and would undo the STEP-guard removal. Keep the two in
                             # step: the bound is v_ego * PHYS_SPAN_S, so a mismatch misjudges speed.
PHYS_MARGIN_M = 8.0          # m -- noise margin on that median change
PHYS_OPEN_MPS = 15.0         # m/s -- a lead does not pull away faster than this; faster = farther object

# ---- arming (spec section 4, amended -- see module docstring, "THIRD BUG") ----
ARM_MIN_DIST = 65.0          # m -- dRel_at_hot_start must exceed this (anchor semantics changed,
                             # see docstring -- this is NOT "distance at first sight" anymore, and
                             # since 2026-09-14 it is the FILTERED range, not a raw dRel sample).
                             #
                             # 73 -> 65 m on 2026-09-15, OPERATOR DECISION, PAIRED WITH the
                             # object-switch guards and NOT safe without them: on the corpus, 65 m
                             # without the guards re-admits all 10 spurious CAP arms. See docstring,
                             # "OBJECT-SWITCH GUARDS". Rollback to the guards-only row: 73.0.
                             #
                             # 70 -> 73 m on 2026-09-14, PAIRED WITH the filtered-anchor change
                             # and not independent of it: the filtered anchor reads ~3 m further
                             # out than the raw sample it replaced, so 73.0 holds the effective
                             # gate where 70.0 held it before. See docstring, "ANCHOR ON FILTERED
                             # RANGE", for why the uncompensated 70.0 was measurably worse.
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
STOP_MARGIN = 6.0            # m -- same STOP_DISTANCE long_mpc.py uses. Still read by the OLD
                             # arming gate's math (hot_a_req_for, and hook 11b's _ArmMirror in
                             # hooks.py, which shadows what that gate would have armed on) -- it
                             # must NOT follow STOP_MARGIN_FRAC or the comparator stops comparing.
STOP_MARGIN_FRAC = 0.5       # PROPORTIONAL stopping target for the ARMED command, 2026-09-22.
                             # The severity question is "how hard to brake to bleed off the
                             # closing rate", and the answer scales with the room available, not
                             # with a fixed 6 m taken off it. Reaching the lead's speed within
                             # HALF the present gap is self-similar: the same demand at 100 m as
                             # at 60 m. With FRAC = 0.5 the denominator is exactly dRel, so
                             # a_req = v^2/dRel -- about 1.9x the old value in the far field,
                             # converging on it near 12 m where the old margin dominates.

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
HANDOFF_ACCEL = -0.40        # m/s^2 -- THE HAND-OFF BAR: hook 11 lets go once the planner's own
                             # best candidate has reached this, i.e. once stock is genuinely
                             # braking. DECOUPLED from FLOOR 2026-09-22 and deliberately equal to
                             # it, so this commit changes no behaviour.
                             #
                             # They were the same constant until now, and that overloading caused
                             # the 2026-08-31 "sixth bug": lowering FLOOR to soften the command
                             # silently moved this bar too, turning "stock is genuinely braking"
                             # into "stock isn't accelerating" -- trivially true, so the hook
                             # self-released almost immediately after every arm. See the module
                             # docstring, "FLOOR EXPERIMENT".
                             #
                             # THE TWO MUST NOW MOVE INDEPENDENTLY. This one is a TRUST bar: how
                             # much braking from someone else counts as "handled". FLOOR is an
                             # AUTHORITY bar: the softest command this hook may issue. Tuning the
                             # second must never drag the first. See FINDINGS.md 25.
ARM_CONFIRM_DIST = 60.0      # m -- boundary between "arm immediately" and "confirm first".
                             # Above this the gate arms on the first qualifying frame; between
                             # HANDOFF_DIST and this it must hold for ARM_CONFIRM_FRAMES frames.
                             # Below HANDOFF_DIST it does not arm at all.
                             #
                             # Operator 2026-09-22. The guard is only needed near the hand-off
                             # bar -- that is where the level test chattered, 20 of 21 degenerate
                             # spans arming within 8 m of it. Applying it everywhere cost 0.15 s
                             # on EVERY arm, including the far-field arms this hook exists for,
                             # to fix a near-field problem. Scoping it by distance keeps the far
                             # field as early as it can be and guards only where the noise is.
                             #
                             # 55 was tried first and is too low: with the boundary there, 4 of 5
                             # remaining degenerate spans armed in 55-60 m, the band that arms
                             # immediately. 60 matches the original measurement that 20 of 21
                             # degenerate spans sat within 8 m of HANDOFF_DIST.
ARM_CONFIRM_FRAMES = 3       # frames the whole arm condition must hold, INSIDE ARM_CONFIRM_DIST.
                             # 2 was tested and is too few (8 sub-2-frame spans, 7 re-arms over
                             # the corpus); 5 removes all chatter but costs 0.25 s per arm.
RE_ARM_HOLD_S = 1.0          # s -- after a hand-off to stock, do not re-arm for this long.
                             # Only needed since arming became a LEVEL test (2026-09-22): the
                             # slope is often still past ARM_SLOPE at the moment stock takes over,
                             # so without this the hook would re-arm on the very next frame and
                             # oscillate arm/hand-off for as long as stock kept braking.
RELEASE_DIST = 20.0          # m -- absolute backstop regardless of stock
LEAD_LOST_S = 1.0            # s -- release if the lead itself is lost this long

# ---- band-slope arming gate (2026-09-22) -- see module docstring "BAND-SLOPE GATE" ----
BAND_N = 100                 # samples (5.0 s) in the mean/stdev window on dRel_model
SLOPE_N = 40                 # samples (2.0 s) of band history the slope is fitted over
BAND_K = 2.0                 # stdev multiplier; the band is mean - BAND_K*stdev
ARM_SLOPE = -5.0             # m/s -- arm when the band slope CROSSES below this
RELEASE_SLOPE = 0.0          # m/s -- release when it CROSSES back above this
HANDOFF_DIST = 50.0          # m -- do not arm below this, and hand off on falling under it.
                             # Measured: across 14 logged spans stock reached FLOOR once, at
                             # 46.7 m; in the other 13 its best command stayed within
                             # [-0.165, +0.192] while this hook was armed at 60-87 m. Above
                             # ~50 m stock is not acting, below it stock owns the approach.
PROB_GATE = 0.5              # the FILTERED model lead prob a lead must exceed to count as believed
PROB_ALPHA = 0.2             # asymmetric filter on that prob: instant rise, this alpha on decay
MODEL_RANGE_OFFSET = 1.52    # m -- modelV2 leadsV3 x[0] is measured from the camera; radar
                             # range is ~1.5 m further forward. Mirrors RADAR_TO_CAMERA
                             # (selfdrive/controls/radard.py:26), which radard applies at :139 to
                             # produce leadOne.dRel. Verified bit-identical to radar dRel over
                             # 4,640 frames where both were present (max |diff| 0.0001 m).
_SLOPE_XB = (SLOPE_N - 1) / 2.0
_SLOPE_SXX = sum((i - _SLOPE_XB) ** 2 for i in range(SLOPE_N))


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


def _median(a: list) -> float:
  return sorted(a)[len(a) // 2]


class _RangeRateFilter:
  """[x, v] filter on `leadOne.dRel`. Position measurement -- NOT radard.py's KF1D, which
  measures a Doppler velocity into a [SPEED, ACCEL] state. See module docstring."""

  def __init__(self, alpha: float, beta: float, dt: float = DT_MDL):
    self.alpha = alpha
    self.beta = beta
    self.dt = dt
    self.x = None
    self.v = 0.0
    self.window = []        # last PHYS_WINDOW raw samples, physical-bound test
    self.stepped = False    # True on the frame the filter re-initialised on a new object

  def reset(self, x0: float) -> None:
    self.x = x0
    self.v = 0.0
    self.window = [x0]

  def _switch(self, z: float) -> float:
    self.reset(z)
    self.stepped = True
    return self.v

  def update(self, z: float, v_ego: float = 0.0) -> float:
    """Feed one dRel measurement, return the filtered closing rate (m/s, negative = closing).

    A range change that motion cannot produce is a new object, not a velocity: the filter
    re-initialises on it instead of differentiating it. See module docstring, "OBJECT-SWITCH GUARDS"."""
    self.stepped = False
    if self.x is None:
      self.reset(z)
      return self.v
    x_pred = self.x + self.v * self.dt

    # STEP guard REMOVED 2026-09-24 (operator): see module docstring, "OBJECT-SWITCH GUARDS". Only
    # the physical bound below re-initialises the filter now.
    self.window.append(z)
    if len(self.window) > PHYS_WINDOW:
      self.window.pop(0)
    if len(self.window) == PHYS_WINDOW:
      dz = _median(self.window[-5:]) - _median(self.window[:5])
      if dz < -(v_ego * PHYS_SPAN_S + PHYS_MARGIN_M) or dz > PHYS_OPEN_MPS * PHYS_SPAN_S + PHYS_MARGIN_M:
        return self._switch(z)

    residual = z - x_pred
    self.x = x_pred + self.alpha * residual
    self.v = self.v + (self.beta / self.dt) * residual
    return self.v


class _BandSlope:
  """Lower Bollinger band on the MODEL range, and the slope of that band.

    band  = mean(BAND_N) - BAND_K * stdev(BAND_N)   of dRel_model
    slope = OLS fit over the last SLOPE_N band samples, m/s

  Fed on EVERY frame the model publishes a lead (2026-09-24; between 09-22 and 09-24 only while
  its filtered prob exceeded PROB_GATE). That includes frames where no lead is present and frames
  where hook 11 is not eligible at all: the buffers describe the road, not this hook's state, so
  `FarLeadPreBrake._reset()` must never clear them. A reset costs BAND_N + SLOPE_N frames (7.0 s)
  of warm-up, and re-warming on every personality or engagement flicker would leave the gate
  blind exactly when it is needed.

  Model confidence no longer gates anything here (`pf` / `confident` are still computed, for
  information). Gating the input (09-22) kept acquisition transients out but made the slope
  undefined after every prob dip, including while armed, which blocked release (FINDINGS 31a).
  Operator decision 2026-09-24: sample always, arm as before. Accepted cost: low-confidence model
  ranges reach the band, so acquisition steps can arm again (FINDINGS 30, 36).

  Why the band and not the plain mean: for steady closing the band's slope settles at EXACTLY the
  true closing rate, but the -BAND_K*stdev term makes it arrive there ~2.5 s sooner, because the
  stdev grows while range is falling. Dropping the stdev term does not trade opening speed for
  release speed -- measured, it costs ~4 s at BOTH ends, because after closing stops a plain mean
  approaches zero slope asymptotically FROM BELOW and never crosses. The stdev collapsing is the
  only thing that lifts the band slope back through zero. See FINDINGS.md 21.
  """

  def __init__(self):
    self.d = []                 # dRel_model samples, most recent last
    self.b = []                 # band samples, most recent last
    self.slope = None           # current band slope, m/s (None while warming up)
    self.prev = None            # last non-None slope, for crossing detection
    self.crossed_arm = False
    self.crossed_release = False
    self.pf = 0.0               # asymmetric-filtered model lead prob, mirroring radard
    self.confident = False      # latched: is pf currently above PROB_GATE

  def update(self, z, prob=None) -> None:
    """Feed one model range sample, or None on a frame with no model lead.

    A frame with no model lead contributes nothing and clears the crossing flags, matching the
    offline gate this was validated against (which skipped those frames outright) -- NOT a gap
    filled by interpolation, which would invent closing that was never measured.

    prob=None means "caller supplied no probability" and reads as CONFIDENT, so callers that pass
    no prob (the unit tests, pre-2026-09-22 scripts) can still arm. grt.hooks always passes it.
    """
    # Asymmetric prob filter, matching radard.py's lead_prob_filters: rise instantly, decay at
    # PROB_ALPHA. radard's FirstOrderFilter(0.0, 0.2, DT_MDL) takes 0.2 as an RC time constant,
    # not an alpha -- alpha = dt/(rc+dt) = 0.05/0.25 = 0.20, so the two coincide numerically ONLY
    # at DT_MDL = 0.05. If DT_MDL ever changes, recompute PROB_ALPHA rather than assuming 0.2. Below PROB_GATE the model's range head is a regression with no target -- at
    # prob 0.006 x[0] wanders tens of metres -- and differentiating that produces pure phantom
    # closing. radard refuses to PUBLISH a lead until this same filtered prob clears 0.5; before
    # 2026-09-22 the band was the only consumer of leadsV3 that read x[0] unconditioned.
    if prob is not None:
      p_in = float(prob)
      self.pf = p_in if p_in > self.pf else self.pf + PROB_ALPHA * (p_in - self.pf)

    self.confident = (prob is None) or (self.pf > PROB_GATE)

    # SAMPLE EVERY FRAME (operator, 2026-09-24). From 2026-09-22 to 09-24 this returned early while
    # unconfident and re-seeded the window when confidence came back. That kept acquisition
    # transients out of the band, but a prob flicker WHILE ARMED re-seeded it too, leaving the slope
    # undefined -- and release needs the slope to cross back above 0, so the hook could not release
    # (route 000001d7: 9.3 s and 8.7 s at FLOOR on opening gaps, both ended by the driver's gas).
    # Now the band always has a slope once warm, so release always works. ARMING is unchanged:
    # slope <= ARM_SLOPE with a lead present (plus the HANDOFF_DIST / near-field confirmation /
    # re-arm-hold guards). The cost is back: low-confidence model ranges reach the band again, and
    # an acquisition step can arm (the 2026-09-22 13:44:41 case) -- measured, FINDINGS 36.
    if z is None:
      self.crossed_arm = self.crossed_release = False
      return

    self.d.append(float(z))
    if len(self.d) > BAND_N:
      self.d.pop(0)

    s = None
    if len(self.d) == BAND_N:
      m = sum(self.d) / BAND_N
      var = sum((x - m) ** 2 for x in self.d) / BAND_N
      self.b.append(m - BAND_K * (var ** 0.5))
      if len(self.b) > SLOPE_N:
        self.b.pop(0)
      if len(self.b) == SLOPE_N:
        bm = sum(self.b) / SLOPE_N
        s = (sum((i - _SLOPE_XB) * (v - bm) for i, v in enumerate(self.b)) / _SLOPE_SXX) / DT_MDL

    p = self.prev
    # CROSSINGS, not levels: a level test re-arms every frame the slope stays past the bar, and
    # would re-arm immediately after a release while the approach is still resolving.
    self.crossed_arm = s is not None and p is not None and p >= ARM_SLOPE and s < ARM_SLOPE
    self.crossed_release = s is not None and p is not None and p <= RELEASE_SLOPE and s > RELEASE_SLOPE
    self.slope = s
    if s is not None:
      self.prev = s


class FarLeadPreBrake:
  """One instance, owned by grt.hooks. See the module docstring for the full design."""

  def __init__(self):
    # Created HERE, not in _reset(): the band is continuous history of the road and must survive
    # every release, dropout and eligibility change. See _BandSlope.
    self.band = _BandSlope()
    # Also HERE, not in _reset(), since 2026-09-24 (operator): the range-rate filter now runs on
    # EVERY frame, so v_filt always has a reading. See the top of step().
    self.filt = _RangeRateFilter(ALPHA, BETA)
    self.v_filt = 0.0
    self._reset()

  def _reset(self) -> None:
    self.present_s = 0.0
    self.absent_s = 0.0
    self.armed = False
    self.last_emitted = None
    self.rearm_hold_s = 0.0        # counts down after a hand-off; see RE_ARM_HOLD_S
    self.arm_confirm = 0           # consecutive frames the arm condition has held
    self.last_known = None         # (dRel, vRel_range) held across a brief dropout while armed

  def step(self, present: bool, dRel: float, vRel_model: float, v_ego: float,
           relaxed: bool, long_active: bool, driver_input: bool, stock_min: float,
           dRel_model=None, prob_model=None) -> list:
    # FIRST, and before every early return: the band is a property of the road, not of this
    # hook's eligibility. Feeding it only while relaxed+engaged would re-warm it for 7.0 s after
    # each flicker. `dRel_model` is None only if the caller could not read modelV2, in which case
    # the gate simply never reaches a crossing and this hook stays inert.
    self.band.update(dRel_model, prob_model)
    # The range-rate filter, likewise on EVERY frame and before every early return (operator,
    # 2026-09-24: "v_filt ... have a reading not only if lead is present"). It is fed the lead's
    # dRel while one is present, and the model's own range otherwise -- NEVER the absent frame's
    # dRel, which is the struct default 0.0 and would read as a 100 m collapse. On this car the two
    # are the same camera estimate (leadOne is radard's vision fallback; there is no radar), so the
    # series is continuous across presence changes. No model range either: coast, keep v.
    z = dRel if present else dRel_model
    if z is not None:
      self.v_filt = self.filt.update(z, v_ego)
    if self.rearm_hold_s > 0.0:
      self.rearm_hold_s = max(0.0, self.rearm_hold_s - DT_MDL)

    if not relaxed or not long_active or driver_input:
      self._reset()
      return []

    v_filt = self.v_filt             # always a reading now; see the top of step()
    if present:
      # No fresh-lock reset any more: the filter has been running on the model range the whole
      # time, and resetting it here would throw exactly that history away.
      self.present_s += DT_MDL
      self.absent_s = 0.0
      if self.filt.stepped:
        # the lead slot switched objects: nothing learned about the old one applies to the new one.
        # The BAND is deliberately not touched -- it tracks the model's range series, which is
        # continuous across a radar slot switch, and re-warming it here would blind the gate for
        # 7.0 s at exactly the moment a new object appeared.
        self.present_s = DT_MDL
    else:
      self.absent_s += DT_MDL
      self.present_s = 0.0

    if not self.armed:
      # ---- ARM: the band-slope gate. 2026-09-22, replacing the presence/hot/ARM_MIN_DIST gate.
      # See module docstring "BAND-SLOPE GATE" and FINDINGS.md 21-22a.
      #
      # Three conditions, no persistence timers on any of them -- the 5.0 s mean and the 2.0 s
      # slope fit ARE the persistence, and the crossing test cannot be re-triggered by noise
      # while the slope sits past the bar.
      if not present:
        self.arm_confirm = 0
        return []                                  # the band may run on model-only frames, but
                                                   # arming needs a radar lead to command against
      # LEVEL, not crossing, 2026-09-22. This used to require `crossed_arm` -- a transition from
      # at-or-above ARM_SLOPE to below it. That had a real blind spot: a range series ALREADY
      # closing faster than ARM_SLOPE at the moment the slope first becomes defined (route start,
      # or after any gap in the model lead) never produces a transition, so the gate never armed
      # on it no matter how hard the approach was. A level test cannot miss that case.
      #
      # It cannot re-trigger while armed -- this whole branch is `if not self.armed`. The exposure
      # a level test does add is re-arming immediately after a release that leaves the slope still
      # past the bar, which the slope release itself cannot do (it fires at slope > RELEASE_SLOPE)
      # but a `stock_min` hand-off can. See RE_ARM_HOLD_S.
      if self.band.slope is None or self.band.slope > ARM_SLOPE:
        self.arm_confirm = 0
        return []
      if self.rearm_hold_s > 0.0:
        self.arm_confirm = 0
        return []
      if dRel <= HANDOFF_DIST:
        self.arm_confirm = 0
        return []                                  # stock owns the near field; see HANDOFF_DIST
      # CONFIRMATION, NEAR FIELD ONLY. Beyond ARM_CONFIRM_DIST the gate arms on the first
      # qualifying frame -- that is the far-field case this hook exists for and every frame of
      # delay is a frame of lost warning. Inside it, the same conditions must hold for
      # ARM_CONFIRM_FRAMES consecutive frames, because that is where the range noise sits close
      # enough to HANDOFF_DIST to arm and release a frame or two later.
      if dRel <= ARM_CONFIRM_DIST:
        self.arm_confirm += 1
        if self.arm_confirm < ARM_CONFIRM_FRAMES:
          return []
      else:
        self.arm_confirm = ARM_CONFIRM_FRAMES      # far field: no delay, and do not carry a
                                                   # part-built count down into the near field

      # ---- ARM. First frame emits the floor, never the full formula -- see module docstring
      # on JERK_ARM: the point is that a noisy lock cannot step straight to -1.2.
      #
      # Expect FLOOR to be held for ~2 s after arming: this gate fires BEFORE `v_filt` has
      # converged (that earliness is the whole point), so the severity formula below sees a small
      # closing rate and asks for little. It hardens as the filter catches up.
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

    # `eff_dRel`, never the raw `dRel`: radarState.leadOne.dRel is 0.0 when the lead is absent
    # (struct default), so a single dropped frame would read as 0 m and hand off instantly.
    # eff_dRel falls back to last_known for exactly this reason.
    if eff_dRel < HANDOFF_DIST:
      self._reset()          # hand off to stock, which owns the approach inside HANDOFF_DIST
      return []
    if eff_dRel < RELEASE_DIST:
      self._reset()          # backstop; unreachable while RELEASE_DIST < HANDOFF_DIST, kept so
      return []              # lowering HANDOFF_DIST cannot silently remove the absolute floor
    # RELEASE on the band slope crossing back above RELEASE_SLOPE.
    #
    # The old release here was `eff_vRel_range >= -HOT_CLOSING_RATE`, and it CANNOT coexist with
    # this gate: the band-slope gate arms ~2.5 s before `v_filt` has converged, so on the frame
    # after arming `eff_vRel_range` is still near zero and that test fires immediately. The hook
    # would arm and release within a frame or two, commanding nothing. The two are mutually
    # exclusive by construction; this one replaces it.
    if self.band.crossed_release:
      self._reset()
      return []

    # CORRECTED relative-motion kinematics -- see arming-gate comment above and module docstring
    # "ATTEMPT 5, DEPLOYED DESPITE FAILING VALIDATION".
    a_req = (eff_vRel_range ** 2) / (2.0 * max(eff_dRel * (1.0 - STOP_MARGIN_FRAC), 1.0))
    target = max(CAP, min(-a_req, FLOOR))
    if target >= self.last_emitted:
      out = target                                          # rising (softer) -- immediate
    else:
      out = max(target, self.last_emitted - JERK_ARM * DT_MDL)   # falling -- rate-limited
    self.last_emitted = out
    cand = [(out, LongitudinalPlanSource.lead0, should_stop(v_ego, out))]

    if stock_min <= HANDOFF_ACCEL:
      self._reset()          # stock has caught up -- hand off starting next frame.
                             # HANDOFF_ACCEL, never FLOOR: see that constant for why they are
                             # separate even while they hold the same value.
      self.rearm_hold_s = RE_ARM_HOLD_S   # set AFTER _reset, which clears it
    return cand
