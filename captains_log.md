# Captain's Log — `nightly-dev`

Running record of code changes to **this checkout only** (`~/Comma/openpilot/nightly-dev`, branch
`nightly-dev`). Newest entry first. Each entry: what changed, why, how it was verified, and current
deploy status.

The sibling `~/Comma/openpilot/release-mici-staging/` checkout keeps its **own** `captains_log.md`.
The two branches diverge — changes logged here are not present there unless cherry-picked.

---

---

## CURRENT STATE — longitudinal personality hooks (living section, update on change)

Last verified on car: **2026-08-17**, plannerd clean, 0 grt exceptions.
**Lead presence is NOT a gate** — `min()` covers a binding lead (measured 100% under 25 m).

### What each personality does (all fork-specific; upstream only ever varied T_FOLLOW)

| personality | behaviour | can it weaken braking? |
|---|---|---|
| **relaxed** | hook 7: rising-edge jerk cap on the final command | **No.** On a rise the output is `min(plan, ...)`, on a fall exactly `plan`. |
| **standard** | upstream behaviour, untouched | n/a |
| **aggressive** | hook 6: temporary accel floor on the e2e candidate | **Yes** — deliberate, see the hook docstring. |

Hook 6 additionally requires **experimental mode**; without it the e2e candidate is not in
the planner's `min()` at all and the hook is a no-op. There is NO feature param — the
personality IS the switch, by operator decision 2026-08-14. Do not reintroduce a param gate
without asking.

### Live constants (`openpilot/grt/e2e_floor.py`, `openpilot/grt/accel_ramp.py`)

| constant | value | set by |
|---|---|---|
| `_FLOOR_JERK` | 0.30 m/s^3 | original design |
| `_FLOOR_MAX` | 0.40 m/s^2 | original design |
| `_ABANDON_ACCEL` / `_ABANDON_T` | −0.20 / 0.30 s | 08-15 recalibration |
| `_ABANDON_DROP` | 0.35 m/s^2 over 0.5 s | 08-15 recalibration |
| `_DECAY_DEADBAND` / `_DECAY_T` | −0.08 / 0.30 s | 08-18 slow-stutter retune |
| `_FLOOR_FALL_JERK` | 0.30 m/s³ (symmetric with rise) | 08-18 slow-stutter retune |
| `_PERSONALITY_STABLE_T` | 0.40 s | 08-15, button-cycling fix |
| `_MAX_ACTIVE_T` | **120 s** | 08-18, operator request |
| `_PERSONALITY_EXIT_T` | 0.30 s | 08-18, button-transit fix |
| `_MIN_SPEED` / `_MIN_HEADROOM` | 30 km/h / 5 km/h | fleet scan |
| `_MAX_CURV_ARM` / `_MAX_CURV_RELEASE` | 0.0020 / 0.0030 1/m | fleet scan |
| `JERK_RELAXED` (hook 7) | 1.5 m/s^3 | fleet scan |

### Diagnosing it on the car

The hook logs every arm and release with a named reason. One command:

    python3 /tmp/parse_swag.py        # copy from analysis/parse_swag.py first; /tmp is wiped on reboot

Release reasons: `model objected (Ns)`, `model withdrawing`, `lead appeared`, `throttle prob`,
`curvature`, `reached set speed`, `max duration`, `precondition: <which>`.

### Open questions

1. **Release-reason distribution.** In the 08-15 replay 3 of 4 sessions ended on the 20 s
   `max duration` cap, which would mean the recalibrated release logic is barely exercised.
   The 08-16 drive ended on `reached set speed` and `lead appeared` — better, but two
   samples is not a distribution. Read this before tuning anything.
2. **relaxed → aggressive DURING a ramp** is still untested on road. Bounded at +0.550 m/s^2
   by the synthetic test; the real 08-16 transitions peaked at 0.008 because hook 7 never
   bound. To provoke: select relaxed, get a real acceleration going, switch mid-ramp.
3. **Set-speed oscillation** — the cruise branch running as a raw P term at near-zero error.
   Mechanism confirmed 08-19; recorder deployed to capture the internal `v_cruise` so the
   filter can be fitted to measured data. Awaiting a drive.

CLOSED: mapd `v_cruise` clamping (operator: working as intended, 08-19). The uphill droop is
addressed by hook 8 as of 08-19 — operator reports speed-up ramp and drooping both solved.

### Where the analysis lives

Full diagnosis, all measurement scripts, and the fleet-scan results:
`~/Comma/openpilot/analysis/` — deliberately OUTSIDE either checkout. It contains route
timestamps, GPS-derived grade and driving history, and `nightly-dev` pushes to a GitHub
fork, so it is not committed. Move it in only if that is an explicit decision.

---

## 2026-08-19 — mapd v_cruise clamping: CLOSED, not a defect

Operator's determination: the mapd speed-limit / curve clamping is working as intended and is
NOT relevant to any of the reported complaints. **Do not re-raise it.**

I flagged it three times as the most worthwhile thing to chase — 08-17 09:16 (~52.8 km/h),
08-18 11:25 (~53 then ~82) — on the grounds that the model wanted +1.0 to +1.4 while `cruise`
held the car to +0.001, and that the dash showed 110 while the internal target was far lower.

That observation was correct; the **interpretation** was wrong. A large gap between the
driver-facing set speed and the internal target is what a map speed-limit layer is FOR. I read
"the car will not accelerate to the number on the dash" as a defect, when the operator had
deliberately tuned that layer (`map_curve_target_lat_a` = 2.025, 08-04) and it was doing its
job. Knowing the roads is domain knowledge I do not have and cannot infer from the logs.

Worth keeping as a general caution: three of the "why won't it accelerate" reports traced to a
subsystem behaving correctly. A component being the PROXIMATE cause of a behaviour is not
evidence that the behaviour is wrong.

## 2026-08-19 (evening) — A/B loop, and a TEMPORARY cruise recorder

Operator drove the same loop twice, aggressive then relaxed. Best evidence yet.

### The hooks are not the remaining pebble

| window | a_cmd sd | maxstep | steps>0.08 | rev/min | cmd 1-3Hz | hook-raised |
|---|---|---|---|---|---|---|
| 14:23-24 AGG "obvious hunting" | 0.046 | 0.061 | 0 | **70** | **8%** | **0%** |
| 14:48-50 AGG "hunting" | 0.104 | 0.045 | 0 | 1 | 2% | 35% |
| 14:55 RELAXED "stutter" | 0.086 | 0.548 | 3 | 3 | 6% | 1% |

The WORST-rated window had the hooks doing nothing. Like-for-like over the whole loop,
relaxed actually had MORE and LARGER discrete steps than aggressive (8 vs 2, maxstep 1.137 vs
0.536); aggressive has higher continuous variation (sd 0.173 vs 0.134) because the hooks work
20% of the time. Different textures -- "busy" versus "occasionally lumpy".

### 14:23-14:24 is the CRUISE branch

```
plan source: cruise 89%, e2e 11%
corr(a_cmd, plan.accels[0]) = +0.979
corr(a_cmd, model_raw)      = -0.292
```

Car pinned at 110 against a 110 set, mean headroom +0.1 km/h. `a_cmd` tracks the planner's own
output and is NEGATIVELY correlated with the model. The cruise candidate is a raw P term,
gain 1.0/s, on a `v_ego` that ripples +/-0.4 km/h -> +/-0.11 m/s^2 at ~1 Hz.

Root cause is the same architectural pattern found twice before: `get_cruise_accel` skips
`j_cruise` under `if not e2e:`, exactly as it skips the lateral-accel and coast limits, on the
assumption that e2e is the binding constraint. At set speed it is not.

**Restoring `j_cruise` will NOT fix it.** At 110 km/h it is 0.725 m/s^3; the oscillation needs
only ~0.37 m/s^3 to sustain and would pass straight through. Necessary hygiene, not a fix.

### The retraction, and why the recorder exists

I first "confirmed" the mechanism from a trace sampled 1.5 s apart -- that was aliasing. Then
I reconstructed `a_cruise = clip(carState.vCruise/3.6 - v_ego, ...)` and got a SMOOTH signal
(0 rev/min) that did not reproduce the 70 rev/min symptom, and retracted the mechanism.

Both were wrong. `carState.vCruise` is the DRIVER-FACING set speed, not the internal one --
`limit_v_cruise` lowers it for mapd, and on 08-18 the dash read 110 while the internal target
was ~53. The reconstruction failed on a missing input, not a wrong theory. The plan-source and
correlation evidence above then confirmed the original mechanism.

**NEW `openpilot/grt/cruise_log.py`**, wired into hook 5 -- the only place that sees
`v_cruise` AFTER `limit_v_cruise` together with `v_ego` and the raw cruise candidate. Writes
one CSV row per planner tick to `<GRT_CONFIG_DIR>/cruise_log.csv`, buffered every 100 rows so
the 20 Hz loop never waits on the filesystem, capped at 50 MB, latched off on any failure.
Changes NO behaviour -- the hook returns its input untouched.

**TEMPORARY. Remove once the cruise filter is fitted and shipped.**

Tests (`test_cruise_log.py`, 9/9) assert the properties that matter for something running
inside plannerd: never raises whatever the filesystem does, latches off rather than retrying
every frame, buffers, and stops at the cap.

### Why the filter is not being guessed at

Every previous change was replayed against real logs before going near the car. This one
cannot be -- the key signal is not recorded. Hence: log first, drive once, THEN fit. The
planned shape is a light low-pass (tau ~0.3-0.5 s) on the cruise term, faded out by magnitude
so genuine braking is never delayed, and blended rather than thresholded -- three separate
bugs this week came from hard thresholds on signals that cross zero.

## 2026-08-19 (later) — hook 8 JERKING: my bug, the SAME zero-crossing defect a third time

Operator: hook 8 "helped speed pretty well but jerking is still an issue", three uphill
windows 10:21 / 10:25 / 10:29.

**Not the ECU, not the model, not the plan source.** Attribution over those windows:

```
10:21:00.897  a_cmd +0.000 -> +0.303   raw +0.003 -> +0.003   src e2e->e2e
10:21:01.047        +0.301 -> +0.000   raw -0.009 -> -0.009
10:21:01.198        +0.000 -> +0.306   raw +0.006 -> +0.006
```

Model output flat, plan source unchanged, and the command slamming between 0.000 and 0.30 --
exactly hook 8's cap -- every ~150 ms. Of the steps >0.08: 0% at a source change, 0% where
the model also stepped. All mine.

**TWO defects, and the second is the important one.**

1. Hook 8 had NO rate limit on its correction, unlike hook 6's floor. With GAIN 3.0 and
   DEAD 0.05 the correction saturates once u > 0.15, so the transition band is 0.10 wide in a
   noisy signal -- effectively bang-bang. Added `_HS_CORR_JERK = 0.30` m/s^3, symmetric.

2. **That alone barely helped (0.306 -> 0.294).** The real cause was the ZERO CAP applied as a
   hard clamp on the OUTPUT:

   ```
   a_e2e = -0.02, corr = 0.30  ->  out = min(0, 0.28) = 0.00
   a_e2e = +0.01, corr = 0.30  ->  out = 0.31            <- 0.31 step
   ```

   The model wanders across zero continuously, so the cap toggled and squared the command.
   **This is the identical defect to hook 6's 08-16 zero-crossing bug** -- a hard clamp on a
   signal that crosses zero -- reintroduced in a new hook eight days later. Fixed by folding
   the cap into the TARGET so the rate limiter smooths it, rather than clamping the output.

**Result over the three windows:**

| window | max step old -> new | steps >0.08 old -> new |
|---|---|---|
| 10:21 | 0.306 -> **0.037** | 9 -> **0** |
| 10:25 | 0.433 -> 0.180 | 20 -> **4** |
| 10:29 | 0.300 -> **0.033** | 5 -> **0** |

10:25 is the 46->86 km/h acceleration window; the residual 4 steps are partly the model's own
(it steps 0.096 there). NOT fully resolved in that case.

**`_FLOOR_MAX` 0.60 -> 0.50** at the operator's request in the same change.

### Answering "is there a downstream block for jerk?"

Yes -- `opendbc/car/hyundai/hyundaicanfd.py: create_acc_control`:

```
jerk = 5;  jn = jerk / 50                      # 0.1 m/s^2 per CAN frame -> ~5 m/s^3 wire clip
a_val = np.clip(accel, accel_last - jn, accel_last + jn)
"JerkLowerLimit": jerk if enabled else 1       # 5.0, hardcoded
"JerkUpperLimit": 3.0                          # hardcoded
```

The two `Jerk*Limit` signals are sent TO the Hyundai SCC and tell the ECU how hard to act on a
given `aReqValue`; sunnypilot makes them speed-dependent (ISO 15622), we do not. That IS the
right place for "how harshly does the ECU apply a request".

But it was the WRONG place for this fault: the command itself was a square wave, and the wire
clip at ~5 m/s^3 could only smear a 6 m/s^3 demand, not remove it. Fixing it downstream would
have masked my defect. Worth revisiting later if the ECU's response to a *clean* command still
feels harsh.

## 2026-08-19 — hook 8 DEPLOYED, plus throttled burst logging

Hook 8 (under-delivery servo) is live on the car at `656937c`, verified after reboot:
`gain 3.0 dead 0.05 cap 0.3 minspd 8.33`, plannerd clean, 0 grt exceptions.

**Throttled logging added.** Unlike hook 6 this servo is continuous, not event-based -- it
corrects on ~40% of frames, so a per-transition line would flood swaglog (cf. the
`lead.status` incident: 38,300 lines in one drive). Instead it summarises each correcting
BURST:

  * bursts shorter than `_HS_LOG_MIN_T = 1.0 s` are not logged at all
  * at most one summary per `_HS_LOG_EVERY = 5.0 s`
  * a burst still running past `_HS_LOG_PROGRESS_T = 10.0 s` reports progress, so a long
    sustained correction is not invisible until it ends

Line format: `corrected 3.4s v=95km/h corr mean=+0.180 peak=+0.300 peak_u=+0.240 zero_capped=12`

Asserted by test, not assumed: 600 artificially chopped bursts over 10 min produce 59 lines
(bounded by the rate limit), and one unbroken 60 s correction produces 6 progress lines
carrying the diagnostics. 16/16 in `test_hold_speed.py`.

**What to read after the next drive:** `frames_at_cap` high means gain 3.0 is too low for the
disturbances actually met; `frames_capped_at_zero` dominating means the operator's zero-cap is
doing more work than expected and is worth revisiting. `peak_u` in the burst lines is the raw
under-delivery and is the honest measure of how bad the grade was.

## 2026-08-18 (evening) — HOOK 8 REDESIGNED: under-delivery servo, not a speed anchor

Operator proposed the better trigger: **act when the car decelerates faster than the model
asked**, rather than anchoring a speed when the model goes quiet. Measured, it wins on every
axis, so the anchor design (committed earlier today) is replaced.

### Why it is better

```
u = a_commanded(t - 0.7s) - aEgo(t)      > 0 means the plant under-delivered
```

On the 14:06 incident: median u = **+0.107 m/s^2**, under-delivering in **78%** of frames.
That single number IS the droop.

Crucially it removes the anchor design's ceiling. The anchor could only act inside a +/-0.05
band, which excluded the 44% of droop frames where the model was actively asking to slow. But
of those frames, **82% were also under-delivering** -- the model asked -0.10 and the car did
-0.35. Correcting that is still honouring the request, so there is no band and no ceiling.

It is also simpler: no anchor, no latch delay, no staleness cap, no band.

### Two corrections to what I had told the operator

**Open-loop overstatement.** Every replay until now applied a correction to logged `aEgo` that
the ORIGINAL command had produced. The real loop changes `aEgo`, which changes the error,
which changes the correction. Iterating properly against the logged disturbance:

| config | open-loop (what I quoted) | CLOSED-LOOP (real) |
|---|---|---|
| gain 1.0, capped at 0 | 8.56 km/h | **4.94** |
| gain 3.0, capped at 0 | — | **8.08** |
| gain 5.0, capped at 0 | — | 8.81 |

Shipped **gain 3.0**: nearly double gain 1.0 while correcting LESS often (40% vs 49% of
frames), because it settles the error rather than grinding against it.

**P-only leaves a residual, by construction.** corr settles at `-gain*d/(1+gain)`, so gain 3
rejects 75% of the disturbance, not 100%. Removing the rest needs integral action, which is
deliberately NOT here (windup, bigger step). The operator asked whether it should "flip off
once it achieves the request" -- it backs off automatically since the correction is
proportional; an explicit flip-off would chatter.

### The operator's cap, and why the literal version is a no-op

Operator asked whether the command should be capped at the model's request. Measured:
capping at `a_cmd` gives **0% of frames, 0.00 km/h** -- mathematically a no-op, because to
ACHIEVE -0.10 against a -0.25 disturbance you must COMMAND about +0.15. The command and the
outcome are different quantities once a disturbance exists.

Capping at **zero** is what that instinct was reaching for, and it is shipped: the output
never rises above 0 while the model asks for deceleration, so it can undo over-braking down
to coasting but never accelerates against a deceleration request. Costs ~3 km/h of recovery
(11.35 -> 8.08 closed-loop) and is worth it.

### The correctness detail that could have made this nonsense

The lag reference is the **ACTUAL commanded accel** (`carControl.actuators.accel`), not
`a_e2e`. `aEgo` responds to whatever won `min()`. If a lead branch commanded -1.0 and the car
achieved -1.0, measuring against `a_e2e` (~0) would read u = +1.0 and demand a large bogus
correction. Covered by a regression test.

### Safety, stated

This canNOT carry hook 7's "never makes braking weaker" claim, and that is inherent to
disturbance rejection. Bounded three ways: positive-only and capped at 0.30; output capped at
0 while the model asks to slow; applied to the e2e CANDIDATE so `min()` still hands control
to cruise or a lead branch whenever either wants less. It cannot override a lead.

**Expectation to set: ~8 km/h of a 22.8 km/h droop.** Not a fix, a substantial dent.

NOT DEPLOYED -- comma4 offline until tomorrow morning.

## 2026-08-18 — HOOK 8: hold-speed servo. The droop root cause, and a partial fix

### Root cause of the droop, finally

`modeld` never receives the set speed as an input (its inputs are `desire_pulse`,
`traffic_convention`, `action_t`, plus frames). It emits a comfortable acceleration for the
scene; it structurally CANNOT hold a speed. Meanwhile the planner DOES contain a speed
controller — the cruise candidate, `clip(v_cruise - v_ego, -1.2, 2.0)` — and `min()` discards
it whenever the model is more conservative.

Measured over 298 sustained-headwind episodes (24.7 min, 6.7 h of data):

```
cruise candidate would command:  mean +1.826   saturated at 2.0 in 80% of frames
what was actually commanded:     mean +0.045
cruise was higher than the command in 100% of frames
mean discarded by min():         +1.781 m/s^2
```

And the command does NOT build against a persistent error — binned by time into episode:
+0.064 / +0.016 / +0.068 / +0.047 / -0.017 while the deficit sat at ~21 km/h and the
disturbance at -0.21. Flat or falling in 208 of 298 episodes.

**The only component that knows the target cannot win; the component that wins does not know
the target.** `kp/ki = 0` is real but is NOT the cause — those sit downstream of `min()`.

### Hook 8, sandboxed in the e2e branch under aggressive

`a_e2e ~ 0` is a REQUEST ("hold this"), not an absence of one. If the car then slows on grade
the request is violated by the plant, so correcting it is a SERVO on the model's own intent,
not an override. That claim only holds in a narrow band — calibrated on the 14:06 incident,
over the 28.3 s where speed was actively falling:

| model's request | time | share |
|---|---|---|
| asking to slow (< -0.05) | 12.4 s | **44% — out of scope by design** |
| no real opinion (-0.05..+0.05) | 14.6 s | **52% — what this addresses** |
| asking to go (> +0.05) | 1.3 s | 5% |

Wider bands buy "coverage" only by swallowing frames where the model asked to slow: at
(-0.15,+0.20) coverage reads 82% but 69% of it is negative commands we would be fighting.

**Latch delay measured, not guessed.** Advisor pushed back on a round number. The car does
NOT settle after band entry — it keeps drifting at -0.17 to -0.23 km/h/s at every delay
tested — so waiting only gives speed away (0.3 s costs 0.07 km/h, 1.2 s costs 0.23). Latch as
early as confirmation allows: **0.3 s**.

**ASYMMETRIC anchor drop, and this was the difference between a feature and a no-op.**
First build dropped the anchor on every band exit; the model left the band 37 times in 50 s,
so it kept re-anchoring to an already-lower speed and recovered only **2.23 of 22.8 km/h**.
Keeping the anchor across UPWARD exits (no conflict with holding speed) and ratcheting it up
to whatever the model achieved recovers **10.04 km/h — 44%**, right at the 52% ceiling.
Downward exits still drop it at once; that is the safety constraint and is not negotiable.

**This hook CAN claim it never makes braking weaker** — unlike hook 6. The correction is
positive-only and applied to the e2e CANDIDATE, so cruise and the MPC lead branches still
bind through `min()`. Stated explicitly in the docstring because a reader who absorbed hook
6's disclaimer would assume otherwise.

Precedence with hook 6 is `max()`, written as one expression with one comment in the shim.
They CAN be active together — hook 6's taper settles into `quiet`, which is inside hook 8's
band — and the churn test covers exactly that overlap.

### Corrections made along the way

- **Retracted:** regressing `a_cmd` on recent speed change gave slope +0.038, r=+0.548. That
  is confounded — the command CAUSES the speed change. It measured its own effect.
- **Retracted:** engagement "27% of driving" omitted the headroom gate, without which the
  controller is a no-op by construction. Real figure at deadband 0.03 is **6%**.
- **Fixed, not caveated:** concatenating per-date CSVs let episodes span day boundaries (the
  `prev` reset per file gives the first row `gap=False`). This produced a bogus +70 km/h max
  and contaminated the p99. Synthetic gap now added at every file boundary; real max 6.12.
- **Over-generalised:** I called `kp/ki` a red herring. Right about the speed anchor (they are
  downstream of `min()`); wrong for "make the car deliver what was asked", where they are
  exactly the mechanism. Operator chose the sandboxed hook over enabling them.

### Honest ceiling

This addresses ~44-52% of ONE incident. The other ~44% is the model genuinely asking to slow
while the car loses speed, which is out of scope by design. It is NOT "the droop is fixed".

## 2026-08-18 (drive 2) — hook behaving; two of three complaints are OTHER layers. Cap -> 0.60

**Five armed sessions, and every change from earlier today did what it was meant to.** Zero
`max duration` releases (the 120 s cap never fired); 4 of 5 ended on `reached set speed`.
THREE armed via **taper** — the safe trigger — against one all week previously. Session 4
armed **with a lead present** and worked. The single objection release was genuine: raw
-0.208, past the -0.20 threshold.

| # | armed | via | v | headroom | released | held | reason |
|---|---|---|---|---|---|---|---|
| — | 11:19:48.996 | request only | — | — | never armed | — | gates closed |
| 1 | 11:20:50.094 | personality | 82 | 28.3 | 11:21:12.841 | 22.7 s | reached set speed |
| 2 | 11:22:59.737 | taper | 101 | 9.2 | 11:23:06.185 | 6.4 s | reached set speed |
| 3 | 11:25:15.619 | taper | 72 | 10.0 | 11:25:22.969 | 7.3 s | reached set speed |
| 4 | 11:26:36.516 | taper (lead present) | 92 | 17.6 | 11:26:48.564 | 12.0 s | model objected (0.30 s) |
| 5 | 11:30:23.502 | personality | 36 | 23.6 | 11:30:39.401 | 15.9 s | reached set speed |

### Operator report 1 — 11:25 "hesitant to accelerate to set speed" = HOOK 1, not hook 6

```
11:24:58  44.0 km/h  set 65   a_cmd +1.623  raw +1.623   src e2e
11:24:59  49.6       set 75   a_cmd +0.968  raw +1.390   src cruise
11:25:01  52.5       set 95   a_cmd +0.140  raw +1.144   src cruise
11:25:03  53.0       set 110  a_cmd +0.001  raw +1.075   src cruise
```

The dash ramped 60->110 while the CRUISE candidate held the car to +0.001. Back-solving
`a_cruise = v_cruise - v_ego` puts the INTERNAL target at ~53 km/h. The model wanted +1.0 to
+1.4 throughout. That is **mapd / limit_v_cruise** holding `v_cruise` far below the displayed
set speed. It released ~11:25:06, the car ran 53->72, and hook 6 then armed seeing only
**10.0 km/h** of headroom — because mapd was still capping around 82. It used all of it and
released at 82.

**THIRD occurrence** (08-17 09:16 was the same at 52.8 km/h). This is now the most
worthwhile thing to chase; `map_curve_target_lat_a` is 2.025 from the 08-04 tuning, but a
~53 km/h clamp on a highway on-ramp reads more like a speed-limit target than a curve one.

### Operator report 2 — 11:26 "lead gone, should have accelerated, not smooth"

Hook 6 delivered (floor applied 85% of frames). The roughness is UPSTREAM: lead0 braked to
-1.215 as dRel closed 68->47 m; the lead then pulled away and **the model's own command rose
to +0.630 and then DECAYED to +0.108 over 4 s** before hook 6 caught it at 11:26:36.5. The
un-smooth part is that fade, not the floor. Also the clearest case yet FOR the lead-gate
removal — session 4 armed with a lead present and that is what enabled the recovery.

### Operator report 3 — 11:27-11:28 "stutter on slight uphill" = NOT hook 6

```
a_cmd == raw in 89% of frames          floor applied: 1%   (hook 6 not armed)
a_cmd  maxstep 0.060, p2p 0.796        slow-band 23% (baseline-normal)
aEgo   p2p 1.62, min -1.11, max +0.51
```

The COMMAND was smooth and tiny. What moved was the CAR: `aEgo` swung 1.62 m/s^2 p2p against
a near-constant request while speed bled 108.6 -> 106.3. That is the **uphill droop** in mild
form — the model asks ~+0.05 on grade, the car cannot hold it, and the drivetrain hunts.
`kp = ki = 0`, no grade input. Outstanding since day one; nothing built this week touches it.

### `_FLOOR_MAX` 0.40 -> 0.60

At the operator's request. The cap had become the binding constraint on pace — across 10
armed windows the floor sat AT it **88%** of the time. Swept over those windows:

| cap | dips | mean dip | max step | mean cmd | time at cap |
|---|---|---|---|---|---|
| 0.40 | 5 | 0.225 | 0.541 | +0.372 | 88% |
| **0.60** | **5** | 0.265 | **0.741** | **+0.547** | 84% |

Dip COUNT unchanged, so this does not re-open the oscillation; delivery rises ~47%. What
grows is the worst single step, 0.541 -> 0.741 — that is the immediate-deference branch
(floor at cap, model asks past `_ABANDON_ACCEL`, floor drops to it in one frame). Downward,
wire-clipped to ~0.15 s, and it must stay instant.

Three tests had `0.40` hardcoded and now read `_FLOOR_MAX` / `_FLOOR_JERK` instead — the same
mistake as the earlier fall-jerk test. Tests should track constants, not copy them.

## 2026-08-18 (later) — max duration 20 -> 120 s, and an EXIT debounce on the personality

**`_MAX_ACTIVE_T` 20 -> 120 s** at the operator's request; the data supports it. 6 of 10
releases that day were `max duration`, so the cap was the binding constraint and every other
release gate was going untested. Replaying the five max-duration sessions with the cap
lifted, **all five reach set speed naturally at 27.7 / 28.7 / 30.1 / 41.7 / 87.5 s.** So
120 s does not make sessions two minutes long — it lets them end on `reached set speed`
instead of an arbitrary clock. Expect the release-reason distribution to become meaningful
for the first time.

What is given up, stated: the 20 s cap existed because the arm evidence goes stale. At 120 s
it is up to two minutes stale and the cap no longer bounds that in any useful way. The
per-frame gates — objection, withdrawal, throttle_prob, curvature, headroom, preconditions —
are what guard the session now, and they are unchanged.

**`_PERSONALITY_EXIT_T = 0.30 s`, new.** Leaving aggressive no longer releases instantly.
Three of ten sessions on 08-18 died to a mere TRANSIT through another personality while the
driver cycled the wheel button — session 4 lasted 1.1 s, session 7 lasted 0.05 s. Entry was
already debounced by `_PERSONALITY_STABLE_T = 0.40 s`; this applies the same logic to the
exit. Deliberately short so a real switch away still disables promptly, and ONLY the
`not aggressive` precondition is debounced — `not pid`, `driver input` and the rest stay
instant.

### Correction to the previous entry

I wrote that session 2 "burned its full 20 s with the floor never rising above +0.000" and
was wasted. **Wrong.** I read the `floor=+0.000` in the release log line — a snapshot at the
instant the cap expired — as if it described the whole session. What actually happened:

```
a_cmd  mean +0.260, max +0.400        floor applied in 92% of frames
vEgo   90.5 -> 106.8 km/h  (+16.3 km/h in 20 s)
raw    min -0.046, max +0.084, mean +0.007   <- the model asked for NOTHING
```

The hook worked exactly as intended: the model was content to sit at 90 km/h with 19.5 km/h
of headroom, and the floor took 16 km/h of it. The lesson is the recurring one this week —
a terminal value is not a trajectory. Same class as the detector artifacts.

Session 8 (armed at raw -0.092, ended at floor -0.144) WAS a real instance of the problem the
arm-sign gate addresses. Session 2 was not, and arming at -0.011 turned out to be fine — the
gate would not have blocked it either way.

## 2026-08-18 — LOW-FREQUENCY stutter found and retuned; arm-sign gate added

**Lead-gate removal worked.** 10 armed sessions today vs 1 yesterday, and every one of the
9 personality requests armed in the SAME frame (+0.0 s). On 08-17 one request never armed.

**Standing question answered: `max duration` dominates — 6 of 10 releases**, plus one
`reached set speed` and three `precondition: not aggressive` (button cycling). ZERO
`model objected`, `model withdrawing`, `throttle prob` or `curvature`. The 20 s cap is the
binding constraint and the -0.20/0.30 s recalibration is still barely exercised in the field.

### The slow stutter is real, and it is the fall/rise asymmetry

Per-frame steps were capped at 0.100 as designed — the FAST stutter is fixed. But across the
10 armed windows the command was making half-a-m/s^2 round trips every 2-5 s:

| | armed windows | baseline (engaged, not armed) |
|---|---|---|
| variance at 0.17-1.0 Hz (1-6 s period) | 38-95%, median ~78% | 10-49%, median ~24% |
| dips > 0.05 | 6-15 per session | — |
| dip depth | **-0.43 to -0.53** | — |

Cause: fall 2.0 m/s^3 vs rise 0.30. Every excursion past the deadband dropped the floor fast
and then took ~1.3 s to climb back.

**Correction to my own first analysis:** I initially blamed the immediate-deference branch
(raw < -0.20) for a 0.391 step. Wrong twice over — that step was an artifact of my sweep
harness force-re-arming with `floor = max(floor, a)`, and the real data has **ZERO**
excursions past -0.20 in any armed window (min raw -0.174). Hard deference never fired.
Every dip came from the withdrawal band. Simulate the mechanism alone, not through a
harness that mutates it.

Clean sweep over the real raw sequences:

| fall / deadband / hold | dips | mean dip | max dip | max step | mean cmd | held above a negative model |
|---|---|---|---|---|---|---|
| 2.00 / -0.02 / 0.10 (as driven) | 55 | 0.401 | 0.568 | 0.100 | +0.245 | 31.3 s (25%) |
| 0.30 / -0.02 / 0.10 | 32 | 0.212 | 0.491 | 0.015 | +0.336 | 46.6 s (37%) |
| 0.30 / -0.05 / 0.20 | 10 | 0.189 | 0.478 | 0.015 | +0.373 | 47.7 s (38%) |
| **0.30 / -0.08 / 0.30 (shipped)** | **4** | **0.154** | **0.195** | **0.015** | **+0.387** | 48.0 s (38%) |

Fall is now SYMMETRIC with the rise. **Cost, stated plainly:** time commanding above a
mildly-negative model rises 25% -> 38%, max divergence 0.440 -> 0.553 m/s^2. That band is
mild by construction — immediate deference past `_ABANDON_ACCEL` and every release gate are
unchanged, and the model never went past -0.174 all day.

### Arm-sign gate

Sessions 2 and 8 armed at raw **-0.011** and **-0.092** — the arm gates never checked the
sign. Session 2 burned its full 20 s with the floor never rising above +0.000; session 8
ended at -0.144 having tracked the model down. ~24 s of 132 s armed but useless. The hook
now refuses to arm while `raw < _DECAY_DEADBAND`; the 3 s personality window retries every
frame, so it waits for neutral instead of spending a session.

**Not yet confirmed on road** — the 55 -> 4 figure is a projection from replay.

## 2026-08-17 — two more stutters (noise-level zero touches) + LEAD PRESENCE GATE REMOVED

### 1. Stutter, again — the 08-16 fix was necessary but not sufficient

Driver reported two more stutters ~09:10 uphill. Exactly two, and both were zero touches:

```
09:10:56.898  a_cmd +0.400 -> +0.300   raw +0.001 -> -0.001
09:10:59.548  a_cmd +0.400 -> +0.300   raw +0.007 -> -0.000
```

The 08-16 change cut the step from 0.414 to 0.100, but fall (2.0 m/s^3) and rise
(0.30 m/s^3) are ASYMMETRIC, so a momentary touch of -0.001 — pure noise — still cost a
0.10 dip plus a ~0.4 s recovery. A sawtooth the driver can feel.

Fix: the decay now needs the model to be MEANINGFULLY and PERSISTENTLY negative —
`_DECAY_DEADBAND = -0.02` held for `_DECAY_T = 0.10 s`. Both of that day's touches are
ignored outright. Replay over the 08-17 drive: **max step 0.100 -> 0.036, steps>0.05: 3 -> 0.**

### 2. "Why did it not keep accelerating 0 -> 110?" (~09:16) — three causes, none a bug

1. **09:15:41-47 the CRUISE candidate clamped it at ~52.8 km/h** while the model wanted
   +0.8..+1.1. `a_cruise = clip(v_cruise - v_ego, -1.2, 2.0)` would be +2.0 at set 110 and
   52.8 km/h, so `v_cruise` must have been lowered by **hook 1 (mapd curve / speed limit)**.
   The fork's own map layer, working as designed.
2. It then accelerated properly to ~78 km/h on e2e commands of +0.3..+1.15.
3. From 78 -> ~96 km/h it crawled on +0.02..+0.3 for ~90 s and never reached 110 — the
   "contented below set speed" behaviour hook 6 exists to counter. **Hook 6 never armed**,
   because a lead sat 60-120 m ahead almost continuously. Which is item 3.

### 3. Lead PRESENCE is no longer a gate — operator was right

Operator asked whether the MPC controls the car when a lead is present even if e2e says
accelerate, and if so whether the gate could go. Measured over the 08-17 drive (542 s
engaged+pid):

| | |
|---|---|
| lead present | 173 s (32% of engaged) |
| of which the LEAD BRANCH actually wins `min()` | **19%** — so 81% of "lead present" controlled nothing |
| when it wins, command is below the model's own value | **99%** of the time |
| largest margin by which it overrides e2e | **-1.073 m/s^2** |

By distance the lead branch wins **100%** of frames under 25 m, 73% at 25-40 m, 11% at
60-90 m, **1%** beyond 90 m.

So `min()` already hands control to the MPC whenever a lead genuinely binds, and raising the
e2e candidate cannot override it — the gate was redundant exactly when it fired. And it was
expensive: **it blocked 31% of otherwise arm-eligible time (109 s of 349 s)** because a lead
sat 60-120 m ahead controlling nothing. That is item 2's complaint.

The gate keyed on RADAR presence, so it never protected against a vision-only lead anyway.
A distance-based guard was considered and REJECTED: `min()` covers the close range
completely (100% under 25 m), so a guard would only re-add false blocking. Lead presence is
now recorded at arm time (`lead_present_at_arm`) for observability, and gates nothing.

**Caveat on the validation:** the replay shows arms unchanged at 1, because removing the
gate does not itself create arms — arming still needs a taper or a personality selection.
The 31% is eligibility recovered, not arms realised.

**Tests:** both suites pass, with regressions added for the noise touch and for a distant
lead no longer blocking.

## 2026-08-16 — THROTTLE STUTTER root-caused: my "never lift a negative" fix was a step

**Symptom (operator):** "a very clear stuttering feeling on the throttle, sort of a go don't
go go in very short succession", 10:50-10:55 local.

**Root cause — mine, introduced 2026-08-14.** The "never lift a negative command" change was
implemented as a HARD BRANCH: when `a_e2e < 0`, return it straight through. The model's
output wanders across zero constantly (negative 22-23% of engaged frames), so the command
alternated between the floor and the raw value.

Evidence, from the exact armed window the hook itself logged (wall clock recovered via the
`clocks` message, so no inference about which frames were armed):

```
10:54:49.450   a_cmd +0.385   raw +0.009   <- floor applied
10:54:49.551         -0.014       -0.014   <- raw crosses zero: DROP 0.40 in ONE frame
   ... ~0.6 s of nothing / slight braking ...
10:54:50.150         +0.235       +0.018   <- raw back positive: JUMP 0.27 in one frame
10:54:50.750         +0.400       +0.263
```

Window totals: max step **0.414 m/s^2**, 2 steps >0.20, p2p 0.534. Push -> nothing -> push,
about 0.7 s apart. The FIRST armed window (10:51:22-10:51:31) was clean — max step 0.034,
1 reversal — because `raw` never went negative there (0.0%). That contrast is what confirms
the mechanism rather than merely fitting it.

**Correction to my own first pass:** I initially reported "8 flips in 1.1 s" from a crude
detector that inferred armed windows as `a_cmd - raw > 0.05`. That was an artifact; the real
figure is 2 large discontinuities in 8 s. Same defect, wrong magnitude. Use the logged arm
times, not an inferred proxy.

**Fix.** The floor now WITHDRAWS smoothly instead of switching off, and decays toward
`a_e2e` rather than toward zero (bottoming at zero would hold the command at 0.0 while the
model asked for -0.15, never deferring at all). Chosen by replaying the exact window:

| variant | max step | steps>0.10 | steps>0.20 |
|---|---|---|---|
| old hard branch (as driven) | **0.414** | 2 | 2 |
| fall jerk 3.0 | 0.150 | 3 | 0 |
| **fall jerk 2.0 (shipped)** | **0.100** | **0** | **0** |
| fall jerk 1.5 | 0.075 | 0 | 0 |

2.0 m/s^3 is 2.5x gentler than the ~5 m/s^3 wire clip, so the CAN layer passes it unchanged,
and it clears the 0.40 cap in 0.20 s.

**Regression the fix introduced, caught by the personality-churn test before deploy.** The
smooth decay would have held the command up to ~1.0 m/s^2 ABOVE a hard braking request for
the 0.30 s release debounce. `_ABANDON_T` governs whether we LATCH OUT — it must never
govern whether we OBEY. Anything beyond `_ABANDON_ACCEL` now sets the floor to `a_e2e` in
the same frame. Two regression tests added (a -0.25 request obeyed while still armed; a
-1.20 request obeyed and released via the drop detector).

**Tests:** both suites pass (test_e2e_floor now 20, test_accel_ramp 14).

## 2026-08-15 — hooks 6/7: tests moved into the repo, and the personality HANDOFF characterised

Both hooks' tests now live in `openpilot/grt/tests/` (`test_e2e_floor.py` 16,
`test_accel_ramp.py` 14) instead of a scratchpad, following the house style: stubbed deps,
standalone runnable, docstring listing the safety-relevant properties. Existing suites
(`test_hooks`, `test_scc_map`, `test_set_speed`) unaffected.

**The gap that prompted this:** hooks 6 and 7 are mutually exclusive by personality, so
neither single-hook suite covered the HANDOFF — and switching personality mid-drive is
exactly what the operator does when evaluating them. Now covered by a `Chain` harness that
mirrors the planner (e2e candidate -> hook 6 -> min() -> hook 7 -> output).

**Measured, and left UNFIXED deliberately:**

| transition | step | demand | after the ~5 m/s^3 wire clip |
|---|---|---|---|
| aggressive -> relaxed | **-0.380** m/s^2 | 7.6 m/s^3 | 0.076 s |
| relaxed -> aggressive | **+0.550** m/s^2 | 11.0 m/s^3 | 0.110 s |

Both are real. Neither is smoothed, and that is a decision rather than an oversight:
hook 6's release must stay instant (its core safety property) and hook 7 must not delay
falls (its core safety property). Fixing either would mean weakening the thing that makes
these hooks safe. Both steps are also SMALLER than the ~1.634 m/s^2 per-tick steps the plan
itself produces in normal driving, both are bounded by the wire clip, and the upward one
never exceeds the planner's own value. 4000 frames of rapid personality churn leave neither
hook stuck and never exceed the raw candidate by more than hook 6's 0.40 cap.

**Also replayed against REAL mid-drive switching** (`scratchpad/replay_switch.py`, today's
5 segments, 279 s engaged, lead present 0.2%). The driver's actual cycling produced 14
personality transitions; the worst output step at any of them was **0.008 m/s^2** — 70x
smaller than the synthetic worst case and imperceptible. Hook 6 active 78.2 s, consistent
with the recalibration replay.

Limitation worth keeping: **hook 7 never bound in that drive** (0.0 s holding the command
below the plan) because the relaxed periods were 1-5 s of steady highway cruise while
cycling toward aggressive. So real data confirms the TYPICAL handoff is negligible; the
worst case — hook 7 mid-ramp, holding 0.45 below a 1.0 plan, at the moment of the switch —
is bounded only by the synthetic test at +0.550 m/s^2 (0.11 s after the wire clip). To
exercise it on road: select relaxed, get a real acceleration going, and switch to
aggressive DURING the ramp.

(Extraction bug found and fixed while doing this: the first run indexed `ss[l][0]` — the
timestamp — as the personality, so every frame looked like a transition and both hooks were
inert. Symptom was 5582 "transitions" and 0.0 s of hook activity. Sanity-check totals, not
just per-row output.)

**Still open — read the NEXT drive's swaglog for RELEASE-REASON DISTRIBUTION.** In the
08-15 replay 3 of 4 sessions ended on `_MAX_ACTIVE_T = 20 s`, which means the recalibrated
release logic is largely untested in the field: a detector that fired constantly was
replaced by one that in this sample barely fires, and the duration cap masks it. If the
next drive is still mostly `max duration`, we know -0.20 / 0.30 s is not obviously wrong —
not that it is right.

## 2026-08-15 — hook 7: rising-edge JERK CAP on the accel command, relaxed personality

**What changed** (additive): NEW `openpilot/grt/accel_ramp.py`; hook 7 shim
`ramp_relaxed_accel()` in `grt/hooks.py`; ONE line in `longitudinal_planner.py` inside
GRT-MOD sentinels, placed AFTER the `min()` so it shapes delivery of whichever candidate
won rather than biasing the selection.

**Why a jerk cap and not the "ramp over ~3 s" originally proposed.** The plan's rising
updates are extremely bottom-heavy — median 0.011 m/s^2 per 50 ms tick (0.2 m/s^3), p90
0.036 (0.7 m/s^3), max 1.634 (33 m/s^3). A time constant applies to every update equally,
including the thousands of trim ones: a symmetric tau = 3 s was measured to cut PEAK
command 1.96 -> 1.28 uphill (-35%) and 0.67 -> 0.16 on the highway (-76%) while leaving
mean effort untouched. Because the commands are short transients a slow filter never
reaches the target — an amplitude cut wearing a slope-change costume, the opposite of the
request. A jerk cap sits out in the tail and catches only the steps.

**Brake release is deliberately NOT rate-limited.** Two variants measured over the full
7.9 h fleet:

| variant | jerk | peak | +area lost | binds | worst lag |
|---|---|---|---|---|---|
| A: limit all rises | 1.5 | 1.96 | 2.2% | 0.8% | **2.93** |
| **B: ramp restarts from max(prev,0)** | **1.5** | **1.96** | **1.2%** | **0.5%** | **1.22** |

Variant A left the command up to 2.93 m/s^2 behind the plan — dragging the brakes long
after the plan wanted them off. B halves the area cost and the worst shortfall. Shipped B
at **1.5 m/s^3** (3x gentler than the ~5 m/s^3 wire clip, inside ISO 15622).

Also considered and REJECTED on measurement: a deficit gate (would have been bypassed
during the entire event that motivated this — the car was 12 km/h down at the time) and a
deadband (+/-0.05 costs 16% of highway positive area for no peak benefit).

**Safety — this hook DOES carry the standard claim, unlike hook 6.** On a rise the output
is `min(plan, ...)`, on a fall it is exactly `plan`, so the command is never GREATER than
the planner asked for in any state; a sudden demand for hard braking passes through in the
same frame. Relaxed personality only; state dropped whenever inactive, so re-entering
relaxed never ramps from a stale value. First active frame ADOPTS the current command
rather than seeding at zero — seeding at zero would lurch if relaxed is selected
mid-acceleration.

**Cost, stated plainly:** lag. Up to ~1.2 m/s^2 of instantaneous shortfall, decaying at the
cap. That is the trade "relaxed" is asking for.

**Tests:** 10/10 new (`scratchpad/test_ramp.py`), hook 6's 16/16 still pass.

**Personality map is now fully fork-specific:** relaxed = gentler throttle rise, standard =
upstream behaviour, aggressive = hook 6 accel floor. Upstream only ever varied `T_FOLLOW`.

## 2026-08-15 — hook 6 RECALIBRATED after first road drive: gate was closing on noise

**Symptom (operator):** "closing gate seems too sensitive." Confirmed: sessions lasted
0.04-11 s. Two independent faults, both mine, both found in the hook's own swaglog output.

**Fault 1 — `_ABANDON_ACCEL = -0.05` was reading noise as objection.** Every logged
"model objected" release tripped at raw_e2e of **-0.050 / -0.051 / -0.051 / -0.057**, i.e.
by 0-7 THOUSANDTHS of a m/s^2. Measured on the drive (300 s, highway, set 110 km/h):
`desiredAcceleration` is negative **37%** of the time, p10 = -0.098, and it crosses -0.05
**47 times (~9/min)** with median excursion depth -0.075. -0.05 sat near the 15th
percentile. With a single-frame trip and a latched release the gate could never stay open.

Every SHORT (<0.4 s) excursion bottomed at -0.096 or shallower, so a threshold of -0.20
plus a debounce ignores all 21 of them while still catching the real ones (-0.357, -0.754,
-1.467). Now **-0.20 sustained for 0.30 s**.

Critically this threshold governs whether we LATCH OUT, not whether we override — a
negative raw is passed through unlifted regardless (the 08-14 fix), so widening it never
commands acceleration against a deceleration request.

**Checked before shipping:** would `_ABANDON_DROP` just become the new dominant releaser?
Yes — at 0.15 it fires **3.0x/min** on this drive. Raised to **0.35** (0.6x/min). Fixing
only the threshold would barely have helped.

**Fault 2 — the personality edge armed on values merely PASSED THROUGH.** Two releases
logged `preconditions` at +39 ms and +90 ms with raw_e2e POSITIVE (+0.144, +0.139), which
is not an objection at all. The rlog shows why: the driver was cycling the wheel button,
and intermediate personalities publish 9-160 ms apart —
`relaxed -> standard -> aggressive(20 ms) -> standard -> aggressive(120 ms) -> relaxed -> ...`
Each transit through aggressive fired an edge, armed, then released as it moved on.
Now requires aggressive **held 0.40 s** and fires once per selection (re-armed only by
leaving aggressive). Boot-in-aggressive still not a request.

**Fault 3 (diagnostic) — "preconditions" did not say WHICH.** Cost a whole log-diving
round. Releases now name it: `precondition: driver input`, `not aggressive`, `not pid`, etc.

**Verified.** Replayed the REAL state machine over today's 5 segments, old vs new. The old
config reproduces the drive faithfully (4 arms; 4.0 / 1.6 / 10.7 / 0.2 s, mean 4.1 s —
matches the swaglog's 3.9 / 2.0 / 11.1 s), which validates the replay:

| | sessions | mean | releases |
|---|---|---|---|
| as driven | 4.0, 1.6, 10.7, 0.2 s | 4.1 s | 4x false objection |
| recalibrated | 20.0, 20.0, 18.2, 20.0 s | **19.6 s** | 3x max-duration, 1x genuine |

Unit tests 16/16, including regression cases for both faults ("cycling THROUGH aggressive
does not arm", "a -0.055 blip does NOT release").

**WATCH NEXT DRIVE:** `_MAX_ACTIVE_T = 20 s` is now the BINDING constraint — 3 of 4
sessions ended on it rather than on anything the model did. That cap is a deliberate
safety bound (conditions drift away from the evidence that armed us), so it was left
alone, but it is the next thing to tune if 20 s feels short.

## 2026-08-14 — hook 6: temporary accel FLOOR on the e2e candidate — ALWAYS ON, gated by AGGRESSIVE personality

**What changed** (purely additive, 97 insertions, 0 deletions):
- NEW `openpilot/grt/e2e_floor.py` — `E2EAccelFloor` state machine.
- `openpilot/grt/hooks.py` — hook 6 shim `floor_e2e_accel()` + latched singleton. NO feature
  param: **the aggressive personality is the switch.**
- `openpilot/selfdrive/controls/lib/longitudinal_planner.py` — ONE line inside GRT-MOD sentinels,
  placed after `soften_cruise_decel` / before `candidates = [...]`. Ordering after
  `limit_v_cruise()` is load-bearing: a lowered `v_cruise` shrinks the headroom and stops it arming.

**Why.** Fleet scan of 7.9 h engaged driving on this car (11 routes, 1208 segments, 565,565
frames): with the set speed at the posted limit (60/80/100/120 in 6710 of 7032 contented
seconds) the model settles a median 4.5–12.4 km/h BELOW it and then asks for nothing —
64.8% of lead-free highway time >3 km/h under set speed has |desiredAcceleration| < 0.1.
Usable headroom under the limit, not taken. Reclaiming it does not mean speeding.

**The safety trade, stated plainly.** Unlike hooks 1/2/5 this hook CANNOT claim "it can never
make braking weaker." Raising the e2e candidate is exactly how it stops winning the planner
`min()`, and in experimental mode e2e is the only vision-based caution in the chain
(`get_cruise_accel` skips the lateral-accel and coast limits; the MPC has no curvature input).
Still covered: mapped curves/limits/hazards (hook 1), radar leads (MPC), set speed (cruise).
NOT covered while active: unmapped curves, roadworks, stopped traffic radar has not locked,
pedestrians, debris, poor visibility.

Safety rests on the ARM CONDITION, not a second veto — measurement showed no usable veto exists
(`gasPressProbs` sits at 0.926 with 0.1% below its 0.4 threshold in exactly the contented state).
The hook arms only after the model ACCELERATED for real (band p85 of positive dAccel) and then
tapered to settled — positive evidence of willingness seconds ago, which "model is quiet" does
not give. Emergent property: all 23 usable events found offline had |curvature| <= 0.0021 —
accelerate-then-settle does not happen on curves. Release is instant and LATCHED: re-arming
requires a fresh strong-acceleration episode, so no lockout timer and no chatter.

**Second arm trigger (added on request): driver switches personality INTO aggressive.**
Rising edge only, and only from an explicitly-observed non-aggressive state — booting or
engaging while already in aggressive is not a request. Opens a 3 s window in which the hook
arms as soon as the situational gates allow, retried every frame rather than tested once, so a
lead just clearing or a bend just straightening does not silently swallow the press.

This trigger is justified DIFFERENTLY and the difference matters. The taper trigger rests on
MODEL willingness (it accelerated for real seconds ago, so it was not withholding out of
caution). The personality trigger has no such evidence — it rests on DRIVER authority: the
driver just pressed a button, is demonstrably attentive, and is asking for the headroom. Only
the first argument says anything about the road ahead.

Consequence, written into the module docstring: **the curvature gate is now LOAD-BEARING on the
personality path.** On the taper path it is near-redundant (accelerate-then-settle does not
happen on curves — all 23 usable offline events measured <= 0.0021). On the personality path
nothing else stops a driver requesting the floor mid-bend. Do not weaken `_MAX_CURV_ARM`
without replacing it. Lead / headroom / throttle_prob gates are shared by both paths via a
single `_gates_ok()` — one gate set, one place to change it.

**Unit tests** (`scratchpad/test_floor.py`, 9/9 pass): boot-in-aggressive is not a request;
relaxed->aggressive arms; floor ramps jerk-limited (max step 0.0150 <= 0.0151) and caps at
0.400; a lead blocks then admits the request when it clears in-window; the request expires if
the gate never opens; a mid-curve request is refused; an objection releases instantly and stays
out through 10 s of quiet; a fresh strong->taper does re-arm afterwards; negative raw is never
lifted.

**Regression:** full replay after the refactor is byte-identical on the taper path —
`strong=482 settled=34 armed=10`, 49 s active, max lift +0.400.

**Verified offline.** Replayed the REAL state machine over all 565,565 logged frames
(`scratchpad/replay_floor.py`): 10 arms, 49 s active = 0.17% of engaged time; every arm on
straight road (curv 0.0001–0.0015) with 7.5–46.8 km/h headroom; releases spread across
reached-set-speed (3), model-objected (3), throttle-prob (3), lead-appeared (1) — all four gate
families live, none decorative. Mean lift +0.225 m/s², max +0.400. Both files parse.

Funnel (now counted in `self.stats`, so the same breakdown is available on-car):
`strong=482  settled=34  armed=10  gate_lead=14  gate_headroom=6  gate_tp=6  gate_curv=9`.

Why 10 arms and not the 23 "usable" events the offline scan predicted — named, not hand-waved:
(a) the offline count applied only the lead and headroom filters, whereas the state machine also
gates on throttle_prob and curvature, which reject 6 and 9 settles respectively; (b) the offline
detector partitioned rows by speed band with a band-constant STRONG threshold and a 3 s dedupe,
while the state machine runs continuously with a speed-interpolated threshold — 34 settles here
vs 51 events there. Checked and REJECTED: the consecutive-frame STRONG detector is not the
cause; it finds 585 qualifying pushes under strict consecutive-frame matching (482 after the
30 km/h and pid preconditions), so STRONG detection is nowhere near the binding constraint.

**Fix applied during review:** the floor no longer lifts a raw e2e that is negative at all. Any
value in (−0.05, 0) is passed straight through and the floor bleeds off at the same jerk, so
re-applying ramps instead of stepping. Before this, a model request of −0.044 could be turned
into +0.40 — a 0.44 m/s² reversal in the one direction the safety argument does not cover.
Max lift is now exactly the 0.40 cap.

**Also fixed during review:** the param read was constructing a fresh `Params()` every 60
frames inside a 20 Hz loop; it is now cached in `_e2e_floor_params`, matching how hook 2 reuses
`scc.params`.

**NOT verified, and cannot be offline.** The model's reaction to actually being pushed is in no
log. Replay is open-loop. The first drive is the test, and the release path is the thing to watch.

**Does NOT address the uphill droop** (car 7 km/h under set for 100 s on grade with ECU torque
headroom; root cause `kp = ki = 0` and no grade input to either branch). Uphill at >10 km/h
deficit the model is active, not contented, so this hook will rarely arm there.

**Operator decision on the switch (2026-08-14).** I recommended a separate feature param,
default OFF, on three grounds: it is the only practical kill switch on a prebuilt branch; the
hook has never been driven; and it overloads `aggressive` (which upstream means only
`T_FOLLOW` 1.25 vs 1.45) with a much larger meaning. **Operator overruled all three and chose
ALWAYS ON**, reasoning that selecting a different personality is itself the switch — it is on
the wheel, usable mid-drive without stopping, and needs no shell. Rollback path agreed: switch
out of aggressive, report back, then fix or revert. The param gate was removed accordingly.
Do not reintroduce it without asking.

**DEPLOYED to comma4 2026-08-14.** git bundle `b500214..nightly-dev` (18 KB) -> scp -> `git
fetch <bundle> && git merge --ff-only` -> reboot. Device was 2 docs-only commits behind at
`b500214` (verified an ancestor first, so a true fast-forward); now at `0b2b418`, clean tree.
`prebuilt` untouched, no scons, no cereal SCP. Bundle deleted after.

Verified ON DEVICE with the real openpilot deps (not test stubs): `grt.hooks`, `grt.e2e_floor`
and the planner all import; `floor_e2e_accel` present; the planner's `update()` source contains
the call; a synthetic relaxed->aggressive switch armed via the personality path and the floor
capped at exactly 0.400. Post-reboot: manager and plannerd running, tree clean.

**False alarm investigated and cleared:** swaglog showed 47,642 `grt: scc_map update failed`.
All of them date to **2026-07-29 08:05-08:06** and the traceback is the `lead.status` bug already
recorded as fixed (its line numbers no longer match current source). Zero occurrences in the
newest swaglog and none since that date; `scc_map` constructs cleanly on the current code and no
longer references that field. Hook 1 is healthy — which matters, because hook 6's safety argument
depends on hook 1 still covering mapped curves.

**Requires:** experimental mode ON (otherwise the e2e candidate is not in the `min()` at all)
and AGGRESSIVE personality. Any other personality = hook fully inert.

## 2026-08-05 — curve approach: rate-limit the ceiling descent to APPROACH_DECEL (fixes the harsh curve slow-down)

Operator: the slow-down *before* a curve is too rapid. Root-caused from the 11:28 drive, fixed
in `scc_map.py`, tests green, **DEPLOYED**.

### Getting the right drive out of the log (worth reusing)

`mapd_debug.log` `t` is `time.monotonic()` — boot-relative and reset every boot, so it cannot
locate a wall-clock time. **Device file mtimes are also useless**: the RTC is batteryless, so
segments carry pre-NTP stamps (`Jun 5 15:37`) corrected only later.

What works: each route's qlog `clocks` messages, whose *last* sample is post-sync. Route
`00000072` → boot_epoch `2026-08-05 09:27:06 UTC`, so the drive ran **11:27:42–11:51:38 SAST**
= mapd_debug session 56. Verified the session↔route pairing twice by matching qlog
`carState.vEgo` against `v_ego_kmh` over the same monotonic window (21.5 vs 21.5 km/h;
13.6 vs 13.5 on a second drive). Boot cycles map 1:1 to the day's routes by duration.

### The aggregate hid the defect

Over 11 curve episodes: mean a_ego **−0.35 m/s²**, median frames at/below −1.15 = **0%**.
Nothing looks wrong. The whole problem is the onset transient, visible only frame by frame:

```
t       v_ego  a_ego  jerk   mapCurve  v_target
970.0   71.2    0.07         75.5      75.5     <- ceiling above us, inert
972.0   72.7    0.21         57.2      57.2     <- STEP 85.2 -> 57.2 in ONE 0.5 s frame
972.5   72.2   -0.96  -2.33
975.0   61.4   -1.34                            <- saturated A_CRUISE_MIN
976.0   58.2   -0.42                            <- gentle tail
```

Ported `CalculateJerkLimitedDistance` from the Go source to get mapd's own planned decel:
**mapd sized the trigger for −0.26 m/s², the car used −1.24** — 4.5× overshoot, and 70% of
curve steps saturated `A_CRUISE_MIN`. Same pathology `approach_speed()` fixed for hazards and
limits on 2026-07-29; curves never got it.

### It was NOT caused by the 10% cut — but the cut is why it became noticeable

Clean before/after, because no driving happened between the 08-04 change and this drive. A
recurring corner pair reads **(52.9, 41.7) km/h** across 16 pre-change sessions and
**(47.7, 37.6)** today — both ratios 0.9017, exactly the √(2.025/2.5) applied. Same corners,
same κ, only `latA` moved.

| no-lead events | PRE (latA 2.5) | TODAY (latA 2.025) |
|---|---|---|
| median Δv to target | 2.9 km/h | **8.7 km/h** |
| **peak decel used** | **−1.23** | **−1.24 m/s²** |
| events saturating ≤−1.15 | 57% | **70%** |
| median time to target | 3.0 s | **5.0 s** |

Peak decel is identical, so the saturation is structural and pre-existing. What the cut did was
triple Δv per step, so the same hard braking lasts ~1.7× longer and fires more often. A −1.2
blip shedding 3 km/h is imperceptible; −1.2 for 5 s shedding 9 km/h is what the operator felt.

### The fix: rate-limit the DESCENT, not the distance

`_ramp_curve_ceiling()` in `scc_map.py`. `approach_speed()` cannot be reused because it needs a
distance and **mapdOut publishes none for curves** (only `nextSpeedLimitDistance`,
`nextHazardDistance`, `nextAdvisorySpeedDistance`). Shaping in the *time* domain needs no
distance and achieves the same thing: the commanded ceiling may fall no faster than
`APPROACH_DECEL = 0.5 m/s²`, so the planner sees a small error each frame instead of one big one.

Design points, each asserted by test:
- **Anchored at `v_ego`, never at the previous ceiling.** A ceiling above current speed is inert;
  ramping down from a stale 85 km/h would burn ~7 s doing nothing before braking began.
- **Rises are not limited** — curve ends, ceiling lifts immediately, can never hold the car back.
- **Self-escalating, so authority is never reduced.** The ceiling descends on its own clock
  whether or not the car keeps up; a closer-than-assumed curve grows the error and the planner
  brakes harder. Floor is still `A_CRUISE_MIN`.
- **Still lands in time.** mapd's trigger carries a `target_speed_time_offset = 4 s` margin;
  the ramp needs Δv/0.5 ≈ 4.8 s plus ~1 s to build tracking error, against mapd's planned 5.5 s.

`_dbg` now logs BOTH `map_curve_speed_kmh` (raw step) and `curve_ceiling_kmh` (ramped), so the
next drive shows the shaping directly.

### Replay of the 11 real measured events through the new limiter

| | before | after |
|---|---|---|
| commanded `a_cruise` peak (median) | −1.20 | **−0.50 m/s²** |
| onset jerk (median) | −2.81 | **−0.84 m/s³** |
| events saturating `A_CRUISE_MIN` | 100% | **0%** |

Every event lands on exactly −0.50, i.e. `APPROACH_DECEL`, which is the design intent.
Open-loop caveat: real `v_ego` would differ once braking changes, so this is first-order — but
the onset transient is precisely what the change targets.

### Tests

**42 scc_map** (13 new for the limiter), **44 hooks**, **62 set_speed**, **30/30 schema
conformance** against the real `log.capnp` including all four wire discriminants.

Two pre-existing scc_map tests asserted the curve target arrives *instantly* — the exact step
being removed. Rewritten to assert convergence rather than deleted, per §0.3: a test that
encodes the old behaviour is not evidence, it is the old behaviour.

### Deploy + on-device verification

Pi5 → GitHub (`ae8322a`) → comma4 `git pull --ff-only`, offroad, then reboot.

Ran the cheap gates ON THE DEVICE before rebooting: **42 scc_map / 44 hooks / 62 set_speed /
30 schema conformance**, all passing under `/usr/local/venv`. Plus the real-import gate the
stubs cannot cover: `grt.scc_map` imports with the actual openpilot deps, `APPROACH_DECEL = 0.5`,
`DT_MDL = 0.05`, `A_CRUISE_MIN` still −1.2, and `_ramp_curve_ceiling(15.9)` from `v_ego = 20.2`
returns 20.175 on the first frame and converges to exactly 15.9 within 10 s.

After the reboot: all processes up, `longitudinalPlan VALID=True`, zero `grt:` exceptions, and
`curve_ceiling_kmh` confirmed live in `mapd_debug.log`.

**`micd`/`soundd` were down again on the first reboot**, raising `processNotRunning` — which is
`ET.NO_ENTRY` and blocks engagement. Same boot-time audio-init transient as 2026-07-30; `mapd`
itself was running, so the fork is not implicated. A second reboot cleared it: `onroadEvents` is
now `seatbeltNotLatched` + `wrongGear` only, **no engagement blockers**. Worth noting this has
now happened on two consecutive deploys — if it ever fails to clear, engagement stays blocked.

**Do not read `tileLoaded=False` here as a tile problem.** The car is parked with **no GPS fix**
(`gpsLocation` not alive, `gpsLocationExternal` reporting lat/lon ≈ 0), so mapd has no position,
loads no tile, and `roadName` is empty. Today's drive produced 971 frames of curve data from
these same tiles, so they load fine when there is a fix.

**New gotcha:** `waySelectionType` reads **`current`** in this state, which looks like success but
is not — `current @0` is the enum's zero value, so with no fix the field is simply never set. The
2026-07-29 entry recorded `fail` when parked; that was with a stale position. Neither value proves
anything parked. Judge way selection only while moving.

### Still outstanding

`latA` stays at **2.025**, keeping both goals (slower corners AND a gentle approach). The next
drive judges whether the onset now feels right; instrument is `curve_ceiling_kmh` vs
`map_curve_speed_kmh` in `/data/media/0/mapd_debug.log` — the gap between them IS the shaping.
Expect onset decel ≈ −0.5 m/s² instead of −1.2. If braking now feels *late*, raise the descent
rate — but `APPROACH_DECEL` is shared with hazards and limits, both separately drive-validated,
so prefer a curve-specific constant over changing it.

## 2026-08-05 — lead-vehicle dash icon, Tier 2: ROAD TEST — plausible, good for now, one thing to watch

Operator drove and reports the distance/speed reading on the cluster "looks plausible — steady,
roughly matches what I expect for the gap." Closes the last open question from the offroad-only
verification (whether the lead-shown branch and the actual numbers render sanely — confirmed by
direct observation, same as Tier 1's road test).

**One thing flagged, not yet acted on:** "a little bit of jumping around" in the reading — operator
wants more drive time before drawing conclusions, and asked to leave it alone for now ("good for
now"). Recorded as a candidate follow-up, not a bug to fix reactively:

- Tier 2's design deliberately did NOT add hysteresis/smoothing to the numeric `dRel`/`vRel`
  values themselves (`PORT_LEAD_ICON_FROM_SUNNYPILOT.md` §4 point 3) — only the *presence* gate
  (`hud_control.leadVisible` AND `radarState.leadOne.present` agreeing) is debounced, via Tier 1's
  already-existing signal. The raw distance/speed numbers pass through radard's fused track
  unsmoothed on top of that gate.
- **CORRECTION (2026-08-11):** the candidate fix originally noted here — "the same pattern already
  proven elsewhere on this branch (the e2e acceleration filter, 2026-07-24 entry)" — pointed at
  code that no longer exists. That filter was DISCARDED in the 2026-07-28 hard reset to upstream
  (see that entry's "Discarded" table); confirmed directly against the current
  `longitudinal_planner.py:132`, which reads `output_a_target_e2e =
  sm['modelV2'].action.desiredAcceleration` raw, no filter. Do not cite it as available prior art.
- **Also corrected, from a deeper look at `radard.py` while chasing the above:** the Staria runs
  `radarUnavailable=True` (already noted in the 2026-07-24 entry above), so it has NO radar point
  tracks. `radard.get_lead()` therefore ALWAYS takes the vision-only branch
  (`get_RadarState_from_vision()`), where `vLeadK`/`aLeadK` are direct copies of the model's raw
  output — `"vLeadK": float(v_ego + lead_v_rel_pred)`, no Kalman filter applied. The `KF1D`/`Track`
  Kalman filter class exists in this file but only runs for radar-matched tracks, which never
  happen on this car. So "swap `vRel` for the already-Kalman-filtered `vLeadK`" (suggested in
  chat before this entry was corrected) is **not actually a free smoothing win on this vehicle** —
  `vLeadK` carries no more smoothing than `vRel` does here. If dRel/vRel jitter turns out to be a
  real problem, it needs an actual new filter (`FirstOrderFilter` or `_hysteresis_update`-style),
  not a field swap. Not built now — no evidence yet that it's needed (§0.3 of the mapd doc: don't
  fix a problem you haven't measured).

**Both tiers of this feature are now DONE and road-tested.** No code changes pending. Watching
item above is the only open thread, and it's explicitly deferred to more drive data at the
operator's instruction.

## 2026-08-05 — lead-vehicle dash icon, Tier 2: DEPLOYED to comma4, offroad checks PASS, road test outstanding

Deployed `00c38bd` (Tier 2). Note on advisor: Tier 1's plan got two successful advisor reviews;
Tier 2's design (both-sources-agree gate, `ACC_ObjRelSpd` omission) did **not** — both attempts
this session hit "temporarily overloaded". Operator made an informed call to deploy anyway, given
the offline verification already done (schema conformance 30/30, 5 behavioural cases against the
real packing logic). Recorded so it's clear this wasn't silently skipped.

**Deploy:** Pi5 → GitHub → comma4, same route as Tier 1. Fast-forward `405e1a8 → a6e183a`
(7 files, +344/−74 — includes `00c38bd` Tier 2 plus a docs-only commit and the micd diagnostic
entry). `prebuilt` marker untouched, tree clean before and after.

**Also resolved, incidentally:** `micd` (see the entry below, filed by the previous investigation)
was still down going into this deploy. This reboot cleared it — `running=True, exitCode=0` for
both `micd` and `soundd` post-reboot, matching the established precedent that this class of
boot-time audio-init race self-resolves on a subsequent boot. No separate action was taken on it.

**Post-reboot, offroad, all checks before the road test:**
- Device back in ~1 minute. `git log -1` = `a6e183a`, tree clean.
- `managerState`: nothing shouldBeRunning-but-not-running. All processes up including `micd`.
- `onroadEvents` = `[wrongGear, seatbeltNotLatched]` only — **the new `radarState` subscriber on
  `card` did not trip anything**, confirming the plan's prediction that card's checks being scoped
  to `all_checks(['carControl'])` makes this safe.
- swaglog since boot grepped for `card|hyundaicanfd|carcontroller|packer|SCC_Obj|ACC_Obj|
  KeyError|Traceback`: **empty.**
- `carState.cumLagMs` = **28.45 ms**, DOWN from the Tier-1 baseline of 36.86 ms — no lag
  regression from the new subscriber (the doc's required Tier-2-specific check).
- **Captured a real `SCC_CONTROL` frame off `sendcan` and decoded ALL three touched signals**
  (not just `SCC_ObjSta` this time):
  - `SCC_ObjSta=0` — Tier 1 unaffected.
  - `ACC_ObjDist=1.0m` — exactly the pre-existing no-lead constant, correctly preserved when
    `radarState.leadOne.present=False` (confirmed via the same `sendcan` capture:
    `(dRel=0.0, vRel=0.0, present=False)`).
  - `ACC_ObjRelSpd=-16.4 m/s` — this is the packer's default for an UNSET signal (raw 0 → physical
    `0×0.1 + (-16.4)`), confirming the field really was omitted from the packed dict as designed,
    not explicitly zeroed. This was the design decision under the most scrutiny (§4 of the plan
    doc) and it's now verified on the real compiled packer, not just the offline stub test.

**NOT YET PROVEN: the lead-shown branch with real numbers, or how the cluster renders a moving
distance/speed.** Everything above is reachable parked and disengaged with no lead present. Road
test outstanding — operator driving next.

## 2026-08-05 — DIAGNOSTIC ONLY: `micd` down after operator reboot — NOT fork-related, unfixed, handed off

Operator rebooted comma4 (unrelated to any deploy from this session — device came back online on
its own) and hit `processNotRunning` blocking engagement, reported as "micd soundd process not
running". Investigated; **no fix applied**, handed off to a separate agent per operator's
instruction. Recorded here so the next investigation doesn't repeat the same steps.

**Ruled out as fork-related:**
- Device is on `405e1a8` (Tier 1 only — `SCC_ObjSta`). Tier 2 (`00c38bddb5`) has **not** been
  pulled to the device; nothing from this session's card.py/carcontroller.py/hyundaicanfd.py work
  is on the car.
- `micd`/`soundd` are the microphone/audio subsystem — no code path shared with the CAN/dash-icon
  changes in either tier.
- Tier 1 already passed a full road test earlier in this same session, before this reboot.

**State found:**
- `soundd`: running fine (confirmed via `pgrep`).
- `micd`: `managerState` reports `shouldBeRunning=True, running=False, exitCode=1`, stale
  `pid=18707`. **Not self-recovering** — polled 4× over ~80s with `managerState`, identical
  `pid`/`exitCode` every time, so manager is not retrying it (differs from the closest documented
  precedent, the 2026-07-30 `soundd` boot crash, which self-recovered via manager's own restart).
- `onroadEvents` includes `processNotRunning` — confirmed this blocks engagement, matching the
  documented behaviour of that event type.
- Manually running `python3 -m openpilot.system.micd` on the device **succeeds cleanly right now**
  — "micd stream started" logged immediately, no error, blocks normally on the audio stream. Not a
  persistent/reproducible fault as of this investigation.

**Root cause of the ORIGINAL crash (exitCode=1 at boot) could not be determined — the traceback is
structurally unrecoverable on this device**: `system/manager/process.py:221-222`
(`PythonProcess.start()`) redirects every Python-process's `stdout`/`stderr` to `/dev/null`. Once
manager launches a process, any traceback it prints on crash is gone. This is stock/upstream
manager code, not fork-owned, so not something to patch as part of this feature — but worth knowing
for any future investigation on this device: a crash-at-boot diagnosis needs either a live
`journalctl`/dmesg capture from the actual boot window, or `stdout`/`stderr` redirected to a file
temporarily (e.g. a manual foreground run at the moment of boot), not swaglog after the fact.

**Not attempted:** a reboot, which is the only thing precedent shows reliably clears this class of
issue (2026-07-30 entry). Deliberately left to the operator/next agent rather than done unasked,
since a device reboot is a real action on a physical, possibly-driven car.

## 2026-08-05 — lead-vehicle dash icon, Tier 2 (distance/speed): IMPLEMENTED, offline-verified, NOT DEPLOYED (comma4 offline)

Real `ACC_ObjDist`/`ACC_ObjRelSpd` numbers on top of Tier 1's `SCC_ObjSta` icon. `comma4` was
offline for this entire session — nothing below has touched the device.

**Design change from the original plan (`PORT_LEAD_ICON_FROM_SUNNYPILOT.md` §4), found while
re-grounding on the current code before writing anything:** the plan called for porting
sunnypilot's separate `LeadDataCarController` hysteresis class to debounce lead *presence*. Didn't
build it. Tier 1 already has a working, road-tested presence signal — `hud_control.leadVisible` —
which itself derives from `radarState.leadOne.present` one hop upstream
(`longitudinal_planner.py: longitudinalPlan.hasLead = sm['radarState'].leadOne.present`, re-checked
directly, not assumed). Porting a SECOND, independent hysteresis on `radarState.leadOne.present`
would risk `SCC_ObjSta` (gated on `hud_control.leadVisible`) and the new numeric fields (gated on
the new hysteresis) disagreeing frame-to-frame — icon on with no number, or vice versa. Instead:
gate the numeric fields on `hud_control.leadVisible` AND card's own `radarState.leadOne.present`
BOTH agreeing. When they don't (a real but narrow window — the two views update on independent
cycles), the fail-safe direction is "no number" — same rule as the mapd port's 60 km/h fallback
removal: never command a value the data didn't actually, currently supply.

**Changes:**
- `openpilot/selfdrive/car/card.py` — `'radarState'` added to the base `SubMaster` list (NOT
  `GRT_SUB_CARD` — it's a stock always-published service, none of `mapdOut`'s ignore-list
  treatment applies). One hook: `self.CI.CS._grt_lead = (dRel, vRel, present)` stashed onto the
  `CarStateBase` instance right before `self.CI.apply(CC, now_nanos)` — works because `apply()`
  forwards `self.CS` unchanged into `CarController.update()`, and `self.CI.CS` is already reached
  this exact way at the `secoc_key` line a few lines up. No `interfaces.py` signature change, no
  other car brand touched.
- `opendbc_repo/opendbc/car/hyundai/carcontroller.py` — the `create_acc_control()` call site
  passes `lead=getattr(CS, '_grt_lead', None)`. `CS` already reaches this call unchanged; `getattr`
  with a `None` default so any `CS` this branch hasn't touched (replay, tests) degrades to Tier 1's
  behaviour instead of raising.
- `opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py` — `create_acc_control()` gained an optional
  `lead=None` param. Packs `ACC_ObjDist` (clipped 0–204.7 m) and `ACC_ObjRelSpd` (clipped
  −16.4–34.7 m/s) only when the gate above is true. **`ACC_ObjRelSpd` is omitted from the values
  dict entirely (not set to 0) when no lead is shown** — mainline never packed it either, and its
  DBC receiver is unconfirmed (`XXX`, not `CLU`); writing an explicit `0` physical value would
  silently change existing behaviour on a signal that might feed something else. `ACC_ObjDist`
  stays at the constant `1` in that case, matching mainline exactly (it was already always set).
- `openpilot/grt/tests/test_schema_conformance.py` — added `radarState.leadOne.vRel` (`.dRel`/
  `.present` were already asserted from the mapd port).

**§2.3 trap, checked explicitly because it bit the mapd port exactly this way:** re-grepped
`log.capnp`'s `LeadData` struct directly before writing any code — `dRel @0`, `vRel @2`,
`present @11`. Confirmed on THIS repo's actual schema. (Earlier in this session, before any code
was written, a `.status` field name from an unrelated earlier grep against a different repo —
sunnypilot's — nearly got carried into a design note; caught and corrected by re-grepping before
committing to it, not after.)

**Offline verification (comma4 unreachable all session):**
- `ast.parse` on all 3 modified Python files: clean.
- `test_schema_conformance.py`: **30/30**, including the new `vRel` assertion.
- Real import of the `opendbc`-side files (`uv run --no-project --with numpy`): clean.
  `card.py`'s own import could not be exercised — needs the on-device path layout for `opendbc`/
  `cereal` resource resolution, which the Pi5 doesn't replicate. Pre-existing limitation from
  every prior port on this branch, not new here.
- **5 behavioural cases against the real `create_acc_control()` packing logic**, via a stub
  packer that captures the `values` dict instead of encoding real bytes (the compiled `CANPacker`
  needs a build the Pi5 can't do — same class of gate as the mapd port's Verification 1):
  1. no lead at all (`lead=None`) → `ACC_ObjDist=1`, `ACC_ObjRelSpd` absent — **exact parity with
     pre-Tier-2 behaviour**, the most important case to get right.
  2. lead present + visible, in range → both fields pack correctly.
  3. `hud_control.leadVisible=True` but `radarState.leadOne.present=False` → falls back to
     no-number, confirming the fail-safe-on-disagreement gate actually works.
  4. out-of-range `dRel`/`vRel` (500 m, 100 m/s) → clipped to 204/34.7, not passed through raw
     (which would make the real packer choke on the car).
  5. `gas_override=True` with a lead shown → `SCC_ObjSta=1` (uncontrollable), confirming Tier 1's
     logic and Tier 2's numeric fields don't fight each other.
  All 5 pass.

**Advisor could not review this round's design** (overloaded both times called this session,
same as earlier for Tier 1's initial DBC pull). Proceeded on the same grounding discipline as
Tier 1 — every claim re-checked against this repo's live code — but this is flagged explicitly in
`PORT_LEAD_ICON_FROM_SUNNYPILOT.md` §7 as outstanding: get advisor's read on the
gate-on-both-sources-agreeing design and the `ACC_ObjRelSpd` omission before deploying.

**NOT ON THE DEVICE.** `comma4` was offline for this entire session. Next step: deploy via the
same Pi5 → GitHub → device route that worked for Tier 1 (see the entry below for the exact runbook
and the `pkill -x manager.py` correction), then the on-device block — engagement check and a
`cumLagMs` comparison against the 36.86 ms Tier-1 baseline are the two items Tier 2 specifically
needs (new `radarState` subscriber, unlike Tier 1) — then a road test for the actual numbers.

## 2026-08-05 — lead-vehicle dash icon, Tier 1: ROAD TEST PASSED

Operator drove behind traffic and confirmed the lead icon appears on the Staria's cluster and
"looks sane" — steady, not flickering, no reported oddity. This closes the one open question from
the offroad-only verification below (the DBC confirms `SCC_ObjSta` is routed to the cluster; it
never proved what the cluster's firmware does with each value — now confirmed by direct observation).

**Tier 1 is DONE.** No further action needed on it. Proceeding to Tier 2 (real distance/speed via
`radarState`) — see the entry above this one for its full design; `comma4 is offline` at the start
of this work, so Tier 2 will be implemented and offline-verified only, not deployed, until the
device is reachable again.

## 2026-08-05 — lead-vehicle dash icon, Tier 1: DEPLOYED to comma4, offroad checks PASS, road test outstanding

Deployed the change from the entry below. Device confirmed parked/ignition-off by the operator
before touching anything (see the process-state note further down — worth recording as a real
gotcha, not just a formality).

**Deploy:** Pi5 → GitHub → comma4 (the normal-case route per PROGRESS.md's 2026-08-04 note).
Committed the 4 files below as `405e1a84e7`, `git push origin nightly-dev`, device fast-forwarded
`09a174b → 405e1a8` (picked up one prior docs commit it hadn't pulled yet, plus this one — 5 files,
+418/−7). `prebuilt` marker untouched. No scons, no cereal SCP (this change touches zero cereal
files, so that class of risk was structurally not in play — see the entry below).

**`pkill -x manager.py` matched nothing** — both `manager.py` processes' actual `/proc/PID/comm` is
`python3` (invoked as `python3 ./manager.py`), so an exact-name match never fires. Not a blocker:
per the 2026-08-04 SYNC entry's own finding, a plain `git merge --ff-only` is safe with the old
processes still running (they just keep the old module in memory until restarted); the reboot is
what actually applies the change, not the pkill. Recorded as a correction to the `pkill -x`
guidance in the deploy runbook — it doesn't reliably match on this device's process tree.

**Also noteworthy: `IsOffroad=0` while the operator reported the car parked/off.** Caught this before
doing anything: `controlsd`/`selfdrived`/`card` were all live pre-reboot, which is the *onroad*
process set. Paused and asked the operator directly rather than trusting either signal alone —
confirmed parked. Given the operator's direct confirmation, proceeded, but this is worth watching
on a future deploy: either the param is stale after a non-clean stop, or the device's onroad
detection doesn't match the operator's notion of "off". Not resolved here; just flagged.

**Post-reboot, offroad, all checks before any road test:**
- Device back in ~60s. `git log -1` = `405e1a8`, tree clean.
- `managerState`: **nothing** shouldBeRunning-but-not-running. mapd/modeld/dmonitoringmodeld/
  controlsd/selfdrived/card/plannerd all up.
- `onroadEvents` = `[wrongGear, seatbeltNotLatched]` only — the same benign parked-car signature
  recorded on 2026-07-30. No `commIssue`. **Engagement is not blocked.**
- swaglog since boot grepped for `card|hyundaicanfd|carcontroller|packer|SCC_ObjSta|KeyError`:
  **empty.** The packer accepted the new DBC key without throwing.
- **Captured the real `SCC_CONTROL` (address 416) frame off `sendcan`** and hand-decoded byte 13
  bits 4-6 (the DBC's `108|3@1+` layout) against the raw bytes: `SCC_ObjSta = 0`. Correct — the car
  is disengaged (`selfdriveState.enabled=False`), and the formula collapses to 0 whenever `enabled`
  is false, independent of lead state. This is the expected value here, not an inert result; it
  also cross-validates the packer's encoding against independent hand bit-math, both agreeing.
- `carState.cumLagMs` = 36.86 ms. No pre/post comparison needed (unlike the set-speed feature):
  this change adds no new `SubMaster`/`PubMaster` traffic to `card`, only edits an
  already-running message builder inside the existing car-control path.

**NOT YET PROVEN: the `1`/`2` branches, or what the cluster actually renders for any of them.**
Everything above is reachable while parked and disengaged; the DBC confirms `SCC_ObjSta` is routed
to the cluster (`CLU` receiver tag), not what the cluster's firmware does with each value. That
needs the driver engaged with a real lead vehicle present — the road test in
`PORT_LEAD_ICON_FROM_SUNNYPILOT.md` §6, still outstanding.

## 2026-08-05 — lead-vehicle dash icon, Tier 1 (`SCC_ObjSta`): CODED, NOT YET DEPLOYED

New feature, unrelated to mapd/set-speed. sunnypilot's Hyundai `CarController` fills the dash's
lead-vehicle icon from real lead data; mainline (this branch's base) sends the same `SCC_CONTROL`
CAN message with those fields hardcoded static, so the Staria's cluster shows no dynamic lead icon
today. Full plan: `PORT_LEAD_ICON_FROM_SUNNYPILOT.md`.

**Why this is smaller than the mapd port:** the data source (`radarState`) is already a stock,
compiled service — no `CustomReserved` slot, no `services.py`/`custom.capnp`/`log.capnp` edit. This
is the first fork feature in this repo's history to touch zero cereal files, so the repo `CLAUDE.md`
rule against SCP'ing cereal files to the device cannot be triggered by it.

**DBC finding (`opendbc/dbc/generator/hyundai/hyundai_canfd.dbc:374-407`, `SCC_CONTROL` id 416):**
`SCC_ObjSta` is the only signal in the message with a documented comment + `VAL_` table
(0=no object, 1=uncontrollable, 2=controllable:longitudinal) AND the only one receiver-tagged `CLU`
(cluster). Every other candidate signal (`ObjValid`, `OBJ_STATUS`, `ACC_ObjDist`) shows unconfirmed
`XXX` receivers. sunnypilot's own `ObjValid` polarity differs between its CANFD and non-CANFD paths
with no documented reason — rather than guess it, this change avoids that signal entirely and
targets `SCC_ObjSta`, whose semantics are unambiguous from the DBC alone.

**Confirmed the Staria's `SCC_CONTROL` is authored by openpilot, not a live factory ECU:**
`create_acc_control()` only fires `if self.CP.openpilotLongitudinalControl` (`carcontroller.py:196`),
already confirmed `True` on this car (line 310 of this log, 2026-07-24 entry). The ADAS Driving ECU
is disabled and openpilot is the sole author of this message.

**Advisor review, two checks before coding, both cleared:**
1. `hyundaicanfd.py` has a second `SCC_CONTROL` builder, `create_acc_cancel()` — checked it only
   fires when `openpilotLongitudinalControl` is `False` (`carcontroller.py:205-217`), the opposite
   condition to `create_acc_control()`. Exactly one builder is ever active for this car; no risk of
   two conflicting writers alternating the field.
2. `gas_override` (the argument driving the 1-vs-2 distinction) is `CC.cruiseControl.override` at
   the call site — the driver-pedal-override signal, matching the DBC's "uncontrollable" semantics.

**Change:** `opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py`, `create_acc_control()`, one dict
entry, GRT-MOD sentinel-wrapped:
```python
"SCC_ObjSta": 0 if not (enabled and hud_control.leadVisible) else (1 if gas_override else 2),
```
Uses `enabled`, `hud_control`, `gas_override` — all pre-existing function arguments. No new
SubMaster subscription, no signature change, no schema edit. `ObjValid`/`OBJ_STATUS` left at
mainline's hardcoded constants — their cluster relevance is unconfirmed by the DBC, so touching
them would add risk without a confirmed payoff.

**Deliberately NOT built in the same pass:** Tier 2 (real `ACC_ObjDist`/`ACC_ObjRelSpd` numbers via
`radarState`, hysteresis debounce) — advisor's instruction, followed: Tier 1 is an unproven probe
(no reference implementation ships icon-only; sunnypilot always drives `ObjValid`/`SCC_ObjSta`/
`ACC_ObjDist` together), so a road-test surprise with both changes present wouldn't be attributable
to either one. Tier 2's design is fully scoped in the plan doc but stays unstarted until Tier 1 is
driven.

**Verified so far (offline only — Pi5 cannot build the compiled `CANPacker`, same constraint as
every prior port on this branch):**
- `ast.parse` on the modified file: syntax OK.
- Real import: `from opendbc.car.hyundai import hyundaicanfd` succeeds (via `uv run --no-project
  --with numpy`, the same pattern used for the RHD fingerprint verification on 2026-07-28);
  `create_acc_control`'s signature unchanged.
- Packer-level round-trip (does the DBC-generated `CANPacker` actually accept `SCC_ObjSta` as a
  key) could NOT be run here — needs the compiled extension, which needs a build this branch's Pi5
  cannot do. Deferred to the device, same as the mapd port's Verification 1.

**NOT YET ON THE DEVICE.** Next step per the plan doc's §6: offroad on comma4, confirm engagement
still works (lowest risk here — no new SubMaster service, so none of the `all_checks()` blast-radius
concerns from the mapd port apply), then a `candump`-level check that `SCC_ObjSta` actually varies
with `hudControl.leadVisible`, then a road test to see what the cluster actually does with it — the
DBC confirms the signal is routed to the cluster, not what the cluster's firmware renders for each
of its three documented values. Do not run any of this unattended.

## 2026-08-04 — map curve speed cut 10% (`map_curve_target_lat_a` 2.5 → 2.025) + NATIONAL tile set, both DEPLOYED

Two changes, both on the car. Device on `09a174b`, offroad during the whole deploy, rebooted, back
in ~90 s.

### 1. Curve speed 10% slower — a setting, not a code path

Operator: curve entry slightly too fast.

mapd computes `v = sqrt(map_curve_target_lat_a / κ)` (`mapd_source/math.go` `GetTargetVelocities`),
and `UpdateCurveSpeed` takes the min over lookahead nodes. `scc_map.py:202` consumes
`mapdOut.mapCurveSpeed` **directly with no scaling** — and its own comment says to tune MapdSettings
rather than add a second python-side integrator. So the lever is the setting.

Speed scales with √latA, so a factor *f* on speed needs *f²* on latA: **2.5 × 0.9² = 2.025**.

**The key was ABSENT from `DEFAULT_MAPD_SETTINGS`**, so mapd was silently falling back to its own
embedded default of 2.5 (`mapd_source/settings/defaults.json`). Pinning it makes the tune explicit
and survives `clear_all()`, since `write_settings_file()` rewrites the blob before every mapd exec.

**Expect slightly MORE than 10%.** A lower target also lets more nodes pass `map_curve.go:58`'s
`tv.Velocity > VEgo + CURVE_CALC_OFFSET` filter and lengthens the jerk-limited trigger distance, so
braking starts marginally earlier too.

`APPROACH_DECEL = 0.5` deliberately **untouched** — it shapes the *approach*, latA sets the
*target*. The drive-3 validation stands; the complaint was corner speed, not braking feel.

**Verified:** ran `write_settings_file()` to a temp path **on device** → emits `2.025`; mapd's start
time is after the pull (uptime 7 min vs mapd elapsed 7:26, both post-reboot).

**Do NOT verify this by grepping `/data/params/d/MapdSettings`** — it is absent in steady state *by
design* (`clear_all()` deletes it; mapd holds the values in memory). Same trap recorded in the
2026-07-29 "one wrinkle understood and benign" entry. I hit it again and wasted a 5-minute poll on it.

Category A, fork-owned file → no `GRT_MODS.md` entry needed (checked).

### 2. National tile set replaces the two-band regional one

Tiles regenerated on another machine from the full South Africa PBF, using the reworked sunnypilot
generator (κ now suppressed at ≥3-way junctions and dead ends; chains held to a single highway tag).

Device: **376 files / 106 MB (bands -34, -36 only) → 2,046 files / 601 MB (bands -24 … -36).**

**The delivered tiles were verified before deploying, not taken on trust.**
`parity_check_ver2.py` gate 1a asserts *"≥3-way node ⇒ stored κ == 0.0"*. Run against the delivered
tiles with a Garden Route clip PBF: **4,468 junction nodes checked, 0 violations, PASS.** A stale
checkout on the build machine would have shown ~3,029 violations — that is measured, not assumed:
it is exactly what the pre-change generator produces on the same clip. So the build machine ran the
reviewed code.

Transfer: rsync → `offline.new`, verified 2,046 files + matching md5 on a spot tile, atomic swap.
`offline.old` kept briefly, then deleted at the operator's instruction — it was a *regional subset*,
not an equivalent fallback, so if the new tiles are ever wrong the fix is a rebuild, not a revert.

The Phase 1b band gotcha (`floor(lat/2)*2`) is now moot: the whole generated tree is deployed, so
every band is present rather than hand-picked.

### STILL OUTSTANDING — the drive

Every check confirms the **input** is 2.025. **Nothing confirms the output moved 10%.** Also, mapd
was never observed opening a tile: it has no open handles into `offline/` while parked and opens
tiles on demand near a fix, so that proves nothing either way.

Instrument is `/data/media/0/mapd_debug.log` → `map_curve_speed_kmh`, same corner before vs after.
`mapdOut` is still never in rlog on this prebuilt branch, so there is no retrospective path.

### Deploy method note

Used GitHub fetch → `git pull --ff-only` on device, matching the SYNC entry below. PROGRESS.md's
`DEPLOY RUNBOOK` still describes the older git-bundle route; marked superseded there rather than
deleted, since the bundle path is still correct when the device has no network.

## 2026-08-04 — SYNC: Pi5 → GitHub → comma4 (no code change; the 08-03 fallback removal is now ON the car)

Housekeeping entry. No source was modified today — this records where each copy of the code now
sits, and closes the "deploy tomorrow" left open by the 2026-08-03 entry below.

**GitHub (`GrtBr/openpilot`), both pushes fast-forward, no force:**

| Branch | Before | After | Note |
|---|---|---|---|
| `nightly-dev` | `dcb3550cac` | `005d003592` | 32 commits — the whole mapd + set-speed body of work |
| `release-mici-staging` | *(absent)* | `0af132822` | **new branch**; the RHD Staria FW commit was local-only until now |

Remote refs were a week stale (`FETCH_HEAD` dated Jul 28); re-fetched and re-checked
`rev-list --left-right` before pushing — `0 32`, a true fast-forward, not a divergence.

**comma4:** `git fetch origin nightly-dev && git merge --ff-only origin/nightly-dev`,
`19c3568 → 005d003` (2 commits: the fallback removal + one docs commit). `--ff-only` deliberately,
so it fails loudly rather than merging on the car.

**Verified on the device:** `HEAD == 005d003`, working tree clean and exactly level with
`origin/nightly-dev`, `prebuilt` sentinel still present (0 bytes, untouched — no scons was run and
none is implied: the diff is 3 markdown files + `set_speed.py` + its tests). `test_set_speed.py`
via `/usr/local/venv`: **42/42 passing on the car** — up from the 33 recorded on 2026-08-03,
which is the rewritten suite from `005d003` running green against the device's real fork-config.

**DEPLOY STATUS — read this before the next drive.** The car was *onroad* during the sync
(`IsOffroad=0`, 17 selfdrive processes up, not engaged). At the operator's instruction the pull was
done **without a reboot**: the new `set_speed.py` is on disk but the running processes still hold
the old module in memory. **The 60 km/h no-map fallback is therefore still live until the next
ignition cycle**, which will pick up the new code with no further action. First drive after that
cycle is the one that exercises the removal.

**Deliberately NOT committed** (local tooling, not code): `graphify-out/` (57 MB of generated
graph output), `CLAUDE.md`, `.graphifyignore`.

`CLAUDE.md` and `graphify-out/` were then added to `.gitignore` at the operator's instruction, in
the existing `# agents` block at the foot of the file. Both were already untracked, so the rules
hide nothing that was ever committed — verified with `git check-ignore -v` (both rules fire) and
`git ls-files | grep -cE 'CLAUDE\.md|graphify'` → `0`. This is a 2-line fork diff against upstream
in a file upstream rarely touches; if it ever conflicts on rebase, `.git/info/exclude` is the
zero-diff alternative. `.graphifyignore` was left untracked and un-ignored.

## 2026-07-28

### 3. Hard reset onto upstream `nightly-dev` — lockout and accel-filter changes DISCARDED

**This entry undoes entries under 2026-07-24. Read it before trusting anything below.**

At the user's instruction ("update nightly-dev from original github... overwrite all other changes"),
this checkout was hard-reset onto `upstream/nightly-dev` (`commaai/openpilot`) and only the Staria
fingerprint was re-applied.

- Before: `808a431b7d` (3 local commits on top of `80ac9b8adc`, openpilot v0.11.2 of 2026-07-07)
- After: `dcb3550cac` = Staria fingerprint on top of `111861914f` (openpilot v0.11.2, 2026-07-27)
- `git diff upstream/nightly-dev HEAD` is now **exactly the 2-line fingerprint** and nothing else.

**Discarded:**

| Commit | Change | Was it on the car? |
|---|---|---|
| `808a431b7d` | longitudinal: low-pass the e2e accel branch on relaxed personality | **Yes** — deployed, test-driven 2026-07-24 |
| `f23cdafed5` | monitoring: driver-distracted lockout 30 min → 30 s | **Yes** — deployed, confirmed after reboot |

**Preserved:** `b91340b` → re-applied as `dcb3550cac`.

**Recovery:** both discarded commits survive in three places — local branch
`nightly-dev-backup-2026-07-28`, `origin/nightly-dev` on the GrtBr fork (still at `808a431b7d`), and
the reflog. Nothing is lost.

**⚠️ The comma4 is now out of sync with this repo.** It is still running `808a431b7d`, i.e. the car
retains the 30-second lockout and the e2e accel filter. This repo no longer has them. Pulling this
branch onto the device will revert both behaviours.

**⚠️ The lockout patch can no longer be re-applied as-is.** Upstream reworked the design between
`80ac9b8adc` and `111861914f`: `_LOCKOUT_TIME` (a single constant) is gone, replaced by
`_LOCKOUT_TIMES = [int(60 * n_min / DT_DMON) for n_min in [1, 5, 15, 30]]` — an escalating ladder
indexed by `lockout_count` (`openpilot/selfdrive/monitoring/policy.py:44`). Reinstating a 30-second
lockout now means editing that ladder, not the old constant.

**Verification after the reset:**
- `opendbc/car/tests/test_fw_fingerprint.py`: **14 passed, 139 skipped, 2499 subtests passed**
- `match_fw_to_car_exact` on the RHD pair → `{CAR.HYUNDAI_STARIA_4TH_GEN}`

**⚠️ Disk:** the reset lazily fetched the v0.11.2 tree through the `blob:none` partial clone and took
`/` from ~4.0 GB free to **1.8 GB free (97% used)**. Untracked files (`captains_log.md`, `CLAUDE.md`,
`.graphifyignore`, `graphify-out/`, `PORT_MAPD_FROM_SUNNYPILOT.md`) were untouched by the reset.

**Deploy status:** ~~local only~~ — **DEPLOYED, see entry 4.**

### 4. Deployed to comma4 — required an AGNOS 18.4 → 18.7 OS upgrade

**Device is now running `dcb3550cac` on AGNOS 18.7. Verified healthy.**

Sequence:

1. Pushed the pre-reset tip to the fork as `pre-reset-2026-07-28` (`808a431b7d`) so the discarded
   accel filter and lockout survive on GitHub, then `git push --force-with-lease origin nightly-dev`
   (`808a431b7d` → `dcb3550cac`).
2. On device: stopped openpilot, `git fetch` + `git reset --hard origin/nightly-dev`, rebooted.

**The AGNOS jump was the real work.** `launch_env.sh` on the new tip sets `AGNOS_VERSION="18.7"`; the
old commit wanted `18.4`, which is what the device had. So `launch_chffrplus.sh` ran the AGNOS system
updater *before* manager, and openpilot did not start until the new OS image was flashed. The updater
waits for confirmation in the device UI before downloading — from ssh this looks exactly like a hang
(process sleeping, `write_bytes: 0`, no network throughput). It is not a hang. Confirm on the screen
and it downloads, flashes, and reboots itself.

**Two mistakes worth not repeating:**

- **Do not run scons on this branch.** It ships `prebuilt` (marker file at repo root) and
  `launch_chffrplus.sh:91` gates the build on `[ ! -f $DIR/prebuilt ]`, so the device runs committed
  binaries. `git reset` alone delivers everything. Attempting a build fails on a missing
  `driving_supercombo.onnx` (ONNX sources aren't shipped in a prebuilt branch) and dirties
  `panda/board/obj/{gitversion.h,version}`, which then need `git checkout --`.
- **`pkill -f <pattern>` over ssh matches the ssh session's own command line.** `pkill -f manager.py`
  killed the remote shell before it could do anything, twice, with silent empty output. Use
  `pkill -x <name>`, and keep the pattern string out of the rest of the command.

**Verification on device after the final reboot:**

- `/VERSION` = `18.7`, matching `AGNOS_VERSION` in `launch_env.sh`
- `git log -1` = `dcb3550`, working tree clean
- manager up; modeld, plannerd, controlsd, card, selfdrived, dmonitoringd all running; 18 selfdrive
  processes stable across a 30 s recheck, load settling (16.9 → 11.0)
- RHD Staria FW strings present in the device's `fingerprints.py`
- Newest swaglog: zero `ERROR`/`CRITICAL`/`Traceback`; no crash-looping processes

**Behaviour change on the car:** the e2e accel filter and the 30-second driver-distracted lockout are
gone, as intended. Lockout is now upstream's escalating ladder (1/5/15/30 min).

### 1. Pi5 checkout reorg: `openpilot/openpilot` → `openpilot/nightly-dev`

**Not a code change** — local directory layout only, no tracked files touched.

The working copy moved from `/home/pi5-ubuntu/Comma/openpilot/openpilot` to
`/home/pi5-ubuntu/Comma/openpilot/nightly-dev`, so a second checkout of a different branch can live
alongside it under the same parent. Verified nothing depended on the old absolute path: `.git/config`,
hooks and `core.worktree` are all path-free; `.venv` is a uv venv with no hardcoded paths;
`compile_commands.json` refers to `/data/openpilot` (device paths) and is unaffected.

Two things did carry the old path and were rewritten: `graphify-out/` (751 files — `.graphify_root`,
`manifest.json`, `graph.json`, AST cache) and `PORT_MAPD_FROM_SUNNYPILOT.md`. `graph.json` was
re-validated after the rewrite (11,879 nodes intact).

**Known pre-existing issue:** `graphify update .` refuses to write, because an AST-only pass produces
10,609 nodes against the stored 11,879 LLM-enriched ones. This predates the rename; the enriched graph
was left in place rather than force-rebuilding and losing 1,270 nodes.

**Note:** keep launching Claude from `/home/pi5-ubuntu/Comma/openpilot` (the parent). The session and
memory key derives from the cwd — starting inside `nightly-dev/` mints a fresh project dir and loses
the `MEMORY.md` index.

### 2. Second checkout: `release-mici-staging`, with the RHD Staria fingerprint

**Location:** `/home/pi5-ubuntu/Comma/openpilot/release-mici-staging`
**Files:** `opendbc_repo/opendbc/car/hyundai/fingerprints.py`

Fresh full clone of `GrtBr/openpilot` (origin, pushable), then `upstream` →
`commaai/openpilot` and the local `release-mici-staging` branch created from
`upstream/release-mici-staging` at `70e157462` (openpilot v0.11.1). The fork itself has no
`release-mici-staging` branch — only `master` and `nightly-dev` — so the branch had to come from
upstream. Local branch tracks `upstream/release-mici-staging`.

Commit `b91340b` (RHD Staria FW versions) was cherry-picked onto it as `0af132822`. It applied clean:
the staging branch's `fingerprints.py` was byte-identical to the pre-fingerprint version on
`nightly-dev`, so the cherry-pick was the entire delta. Adds to `HYUNDAI_STARIA_4TH_GEN`:

- `fwdCamera` `0x7c4`: `US4 MFC  AT GEN RHD 1.00 1.01 99211-CG200 250207`
- `fwdRadar` `0x7d0`: `US4_ RDR -----      1.00 1.01 99110-CG100`

**Verification** (on the Pi5, deps supplied via `uv run --no-project --with ...` since the repo `.venv`
is empty):

- `match_fw_to_car_exact` returns exactly `{CAR.HYUNDAI_STARIA_4TH_GEN}` for the RHD pair, for the
  pre-existing LHD pair, and for a mixed RHD-camera/LHD-radar pair.
- `opendbc/car/tests/test_fw_fingerprint.py`: **14 passed, 138 skipped, 2473 subtests passed** — no
  cross-model collisions introduced. Run with `-c ./pyproject.toml --confcutdir=.` from inside
  `opendbc_repo/`, which is required to bypass the parent openpilot `conftest.py` (it needs `zmq` and
  a built `params_pyx`).

**Deploy status:** local only. Nothing pushed to `origin`, nothing deployed to comma4.

---

## 2026-07-24

### 1. Driver-distracted lockout: 30 minutes → 30 seconds

**Files:** `selfdrive/monitoring/policy.py`, `selfdrive/selfdrived/events.py`,
`selfdrive/monitoring/test_monitoring.py`

`_LOCKOUT_TIME` changed from `int(1800 / DT_DMON)` to `int(30 / DT_DMON)` (1800 s → 30 s, i.e.
600 steps at DT_DMON = 0.05). This is the lockout that blocks re-engagement after 2 red alerts or
1 no-response event; `selfdrived.py:192` gates engagement on `driverMonitoringState.lockout`, which
clears once `lockout_time > _LOCKOUT_TIME`, so the one constant is the whole functional change.

Two follow-on fixes were required:

- **Alert text.** `too_distracted_alert` only rendered minutes: at 30 s it computed `round(0.5)` → 0,
  then `max(1, 0)`, so the car would have displayed *"1 minute Left"* for the entire 30-second
  lockout. Now renders seconds below a minute and keeps minutes wording above.
- **Tests.** `test_distracted_lockout` / `test_invisible_lockout` ran a 120 s sequence and asserted
  lockout state at the end. With a 600-step lockout it self-clears mid-run and resets
  `alert_3_cnt` / `no_response_cnt` / `too_distracted`. Both now truncate to end inside the lockout
  window, which preserves their intent and makes them independent of the duration.

**Verification:** 13/13 monitoring tests pass on-device (`/usr/local/venv` + `PYTHONPATH=/data/openpilot`).
The original test file was run against the new constant to confirm the fallout was real, not
theoretical: 2 failed on `assert d_status.alert_3_cnt == 1` → `assert 0 == 1`.

**Status:** live on device since the reboot. Uncommitted working-tree edit.

**Note:** `policy.py:21-24` carries comma's notice that nerfing driver-monitoring safety features can
get you and your users banned from comma's servers. Deliberate, user-directed change.

---

### 2. Low-pass filter on the e2e acceleration branch (longitudinal jitter)

**File:** `selfdrive/controls/lib/longitudinal_planner.py`

**Symptom:** without a lead, the car accelerates → coasts → accelerates, and the onset is distinctly
felt.

**Diagnosis** (route `0000000a--f31979c274`, 2026-07-24 09:26–09:37 local):

- Config: `openpilotLongitudinalControl=True`, `pcmCruise=False`, `kpV/kiV=[0.0]` (LongControl is
  pure passthrough — no PID to tune), `radarUnavailable=True`, Experimental + Alpha long both on.
- The jitter is **speed-banded** and tracks e2e authority: at 18–54 km/h the e2e branch wins
  `min(e2e, mpc)` 96–100% of the time and commanded jerk p95 is 0.68 m/s³ with 37.6 ripple
  cycles/min; at 108–126 km/h e2e wins only 18% and the ride is essentially perfect
  (aTarget std 0.026, speed ripple 0.40 km/h).
- Within-drive counterfactual: `longitudinalPlan.accels` is the pure MPC trajectory, logged
  unconditionally even in Experimental mode. In the problem band it shows jerk p95 **0.28** and
  ripple **8/min** vs the commanded 0.68 / 37.6 — the MPC branch is 2.4× smoother in jerk with ~5×
  fewer oscillation cycles. The e2e output is a raw per-frame model value with no jerk penalty
  (`A_CHANGE_COST` / `J_EGO_COST` shape only the MPC branch).

**Change:** `FirstOrderFilter` (TS = 0.5 s) on `output_a_target_e2e` *before* the `min()`, gated to
the **relaxed** personality. Deceleration below −1.0 m/s² bypasses the filter so real slowdowns are
never lagged, and `output_should_stop_e2e` is untouched. Bypass re-entry is rate-limited to
3.0 m/s³. NaN input re-seeds the filter instead of latching.

**Why the bypass is rate-limited:** hard-switching from filtered to raw at the boundary measured
**9.69 m/s³** across 16 real brake-onset crossings — 6× worse than the raw signal at the same frame
(1.66). 3.0 m/s³ stays just under the raw signal's own max (3.05), so the transition is never harsher
than stock, at a cost of ~0.08 s mean / 0.20 s max catch-up.

**Predicted effect** (replaying the real e2e signal; replay machinery validated to mean |err| 0.0000
against logged `aTarget`, though the counterfactual itself is first-order since a filtered command
would slightly alter next-frame inputs):

| 18–54 km/h | baseline | filtered |
|---|---|---|
| jerk p95 | 0.676 | **0.302 m/s³ (−55%)** |
| jerk max | 1.83 | 1.00 |
| ripple | 37.6/min | 20.5/min |
| mean accel | +0.154 | +0.164 (no responsiveness cost) |

Highway: jerk p95 0.136 → 0.044 (−67%). TS sweep: 0.3 → −45%, 0.4 → −51%, 0.5 → −55%, 0.7 → −62%.

**Verification:** longitudinal maneuver suite identical to the pristine baseline (4 failed, 2 passed,
56 subtests passed — the 2 NaN-recovery subtest failures are pre-existing on this checkout).
Road-tested 2026-07-24, no issues reported. Further driving planned 2026-07-25.

**Status:** on device, live after reboot. Uncommitted working-tree edit.

**Personality gating note:** `relaxed` and `standard` differ only in `T_FOLLOW` (1.75 vs 1.45) and
`get_jerk_factor` returns 1.0 for both, so with no lead they are otherwise identical — toggling
between them mid-drive is a clean A/B of the filter alone. Confounded when a lead is present.

---

### Investigated and ruled out (recorded so it isn't re-litigated)

- **Coast gate / `ALLOW_THROTTLE_THRESHOLD`** — `allowThrottle` was False for **0.0%** of the engaged
  no-lead regime (2 toggles in 388 s; 2.62% across the whole log, all outside the regime).
- **`accel_clip` rate limiter (`planner:165`, the ±0.05 constant)** — reconstructed exactly and it
  binds only 3.08% of samples in the problem band, with a mean 0.944 m/s² of headroom. It can only
  limit jerk to 1.0 m/s³ while commanded jerk p95 was already 0.68. Halving it would also slow the
  ceiling's recovery, nudging sluggishness the wrong way. *May become relevant after change 2:* with
  e2e smoothed, max jerk in that band pins at exactly 1.00 m/s³, which is the clip's own slew rate.
- **`A_CHANGE_COST`** — shapes only the MPC branch, which is already the smooth one and unselected
  97% of the time in the problem band.
- **PID tuning** — Hyundai leaves `kpV`/`kiV` at `[0.]`, confirmed from the device's own `CarParams`.
- **Driving personality (for jitter)** — `get_jerk_factor` is 1.0 for both relaxed and standard.

## 2026-07-28 — mapd port, Phase 2: cereal schema

Ported mapd's capnp schema into the fork's reserved custom slots, as the first phase of
PORT_MAPD_FROM_SUNNYPILOT.md (offline block).

Changes (both GRT-MOD sentinel-wrapped, category D):
- `openpilot/cereal/custom.capnp` — renamed reserved structs IN PLACE, struct IDs unchanged:
  CustomReserved17 → MapdExtendedOut (@0xa30662f84033036c), CustomReserved18 → MapdIn
  (@0xc86a3d38d13eb3ef), CustomReserved19 → MapdOut (@0xa4f1eb3323f5f582). Added supporting
  structs (MapdDownloadLocationDetails, MapdDownloadProgress, MapdPathPoint) and enums, copied
  verbatim from the authoritative Go schema the prebuilt binary was compiled against.
- `openpilot/cereal/log.capnp` — union members @143/@144/@145 renamed to mapdExtendedOut/mapdIn/mapdOut.

Decisions:
- Enums Mapd-prefixed (MapdWaySelectionType, MapdRoadContext, MapdSpeedLimitOffsetType). No
  collision exists today, but custom.capnp/log.capnp/deprecated.capnp all share
  $Cxx.namespace("cereal"), so an upstream-added RoadContext would collide later. Renaming enum
  *types* is wire-safe (enumerants serialize as ordinals), so this is free insurance.
- MapdOut kept at 24 fields @0–@23. Did NOT add nextHazardSpeedTarget @24 — it exists only in
  sunnypilot's python-side schema, not in the Go schema, so the binary never writes it.

Verification (pycapnp on Pi5; real scons build deferred to device):
- Union discriminants — the actual wire tag, which is positional and NOT the @143 ordinal —
  compared three ways: pristine HEAD 141/142/143, this branch 141/142/143, Go schema 141/142/143.
  All match, so the rename is wire-neutral AND agrees with the prebuilt binary.
- MapdOut = 24 fields, contiguous @0–@23, all addressable; build/serialize/parse round-trip OK.

Note: the Pi5 cannot build openpilot (no scons/cmake/capnproto). Verification 1 (scons -j4)
is deferred to the on-device block and must not be ticked off the schema check above.

## 2026-07-28 — mapd port, grt/ scaffold + Phase 1a (binary) + Phase 3 (registration)

Established the fork-owned `openpilot/grt/` package and wired mapd into the manager, params,
services and plannerd with one-line hooks.

Added (category A, fork-owned — no future merge conflicts):
- `openpilot/grt/__init__.py`, `openpilot/grt/registry.py` — MAPD_ROOT, GRT_SUB,
  GRT_IGNORED_PROCESSES, grt_procs(). Deliberately import-free at module level.
- `openpilot/common/grt_params_keys.inc` — MapdSettings (PERSISTENT/JSON) and
  SmartCruiseControlMap (PERSISTENT/BOOL "0").
- `third_party/mapd/mapd` + README.md — vendored fork binary, md5
  0c3b552c229addc273e2c39c28924fbc, 21211912 bytes, ELF aarch64 static. Verified distinct from
  the stale May-21 mapd_arm64 (2dda8f6e...). Provenance and "never auto-update" recorded.
- `GRT_MODS.md` — the sync checklist of every in-place upstream edit.

Upstream hooks (categories B/C, all GRT-MOD sentinel-wrapped):
- cereal/services.py — 3 mapd service entries at QueueSize.MEDIUM
- common/params_keys.h — single #include of the .inc, inside the keys initializer
- system/manager/process_config.py — procs += grt_procs()
- selfdrive/controls/plannerd.py — SubMaster + GRT_SUB
- selfdrive/selfdrived/selfdrived.py — not_running - GRT_IGNORED_PROCESSES (category C)

Key finding (changed the design): **services.py cannot import openpilot.grt.** It is executed
as a standalone script at build time to generate services.h, where the repo root is not on
sys.path; the import splice failed with ModuleNotFoundError and broke the build. The 3 service
entries are therefore inlined in services.py, and registry.py does not duplicate them (single
source of truth). Caught by testing the build path rather than assuming.

Verification (Pi5, no openpilot build available):
- services.py standalone run regenerates services.h; mapdOut/mapdIn/mapdExtendedOut all emit
  queue_size 2097152 (2 MB), matching the binary's compiled-in ServiceQueueSize table.
- All six touched python files compile.
- grt_procs() builds NativeProcess(name='mapd', cwd='/data/media/0/osm', cmdline=<BASEDIR>/
  third_party/mapd/mapd) and its should_run gate correctly returns False when the tile dir is
  absent (as on the Pi5), so mapd cannot spuriously start.
- selfdrived: mapd excluded from the processNotRunning set — adapted to this version's
  not_running comprehension, NOT copied from sunnypilot (which has no ignored_processes here).

## 2026-07-28 — mapd port, Phase 4: MapdSettings

Added `openpilot/grt/settings.py` — a write-once installer for mapd's own JSON settings blob
(the `MapdSettings` param), with `--force` / `--show` / `--no-notify` and a best-effort
`reloadSettings` mapdIn publish so a running mapd picks changes up immediately.

Baseline is sunnypilot's known-good config with three deliberate deltas:
- speed_limit_control_enabled: False -> True   (enables behaviour 3, auto speed-limit adoption)
- vision_curve_speed_control_enabled: True -> False  (nothing consumes visionCurveSpeed here)
- map_curve_speed_control_enabled: True (unchanged, behaviour 1)
Tuned braking profile deliberately preserved: target_speed_jerk/accel 0.6, time_offset 4.0.
speed_limit_offset stays 0.0, with an explicit in-file caveat that this holds EXACTLY the
posted limit and should be sanity-checked on a real drive.

Explicitly did NOT port sunnypilot's 1 Hz settings-rewrite loop. That exists only because
MapdSettings is unregistered there and clearAll() deletes it; we registered the key PERSISTENT
in grt_params_keys.inc, so one write survives manager restarts and ignition transitions.

Verified: compiles; 25 keys JSON-serializable; asserted the three deltas and that the tuning
constants are untouched.

Also ticked Verification 2 (PC schema round-trip) — completed earlier via pycapnp: a mapdOut
was built, serialized, parsed back, and all 24 fields were addressable with correct values.
Verification 1 (scons) remains NOT possible on the Pi5 and stays deferred to the device.

## 2026-07-28 — mapd port, Phases 5 + 6: control path and speed-limit adoption

Ported the controller and wired the speed-ceiling hook into the planner.

Added (fork-owned):
- `openpilot/grt/scc_map.py` — SmartCruiseControlMap ported from sunnypilot's map_controller.py.
  All tuning constants and their explanatory comments preserved verbatim (HAZARD_ACCEL_MAX
  -0.3 reverted-from--0.1 note, the sticky-latch rationale, the adaptive-decel gating note).
  Adaptations: MapState is a local IntEnum (LongitudinalPlanSP schema not ported); the
  vestigial LastGPSPosition / MapTargetVelocities param reads are dropped (unused, and they
  would raise UnknownKeyName here); MIN_V (20 km/h) and PARAMS_UPDATE_PERIOD (3 s) inlined;
  output_a_min_override renamed output_hazard_accel.
- `openpilot/grt/hooks.py` — limit_v_cruise() and extra_accel_candidates(); owns the
  controller singleton and the once-per-frame update ordering contract. limit_v_cruise is
  exception-guarded so the fork can never take down plannerd.
- `openpilot/grt/tests/test_scc_map.py` — 12 behavioural tests, all passing, runnable with
  stubbed deps on a box that cannot import openpilot.

Phase 6 (speed limits) is implemented inside update_calculations: mapdOut.speedLimitSuggestedSpeed
is taken as one more candidate for v_target. Sunnypilot's nextSpeedLimit/nextSpeedLimitDistance
pre-braking block was deliberately NOT ported — mapd already does that lookahead internally
(speed_limit.go SuggestNewSpeedLimit) and two integrators would fight the same slow-down.
mapdOut.suggestedSpeed is deliberately unused (it folds in vCruise and curve speeds with mapd's
own priority rules).

Upstream hook (category C, sentinel-wrapped) — longitudinal_planner.py, 11 insertions and 0
deletions: v_cruise = grt_hooks.limit_v_cruise(...) placed immediately before get_cruise_accel.
A lower v_cruise makes a_cruise negative and the existing min() selects it. limit_v_cruise only
ever lowers v_cruise, so the forceDecel v_cruise = 0.0 still wins.

Architecture note worth recording: this openpilot no longer passes v_cruise to the MPC
(long_mpc.update lost that parameter) and arbitrates by min() over acceleration candidates
with a_cruise = clip(v_cruise - v_ego, A_CRUISE_MIN=-1.2, max_accel). Hook 2 (Phase 7) will
therefore append a candidate rather than loosening an MPC slack floor. Since a_cruise saturates
at -1.2 and the adaptive hazard decel spans [-1.5, -0.3], that candidate only binds when it is
harder than -1.2 — granting authority beyond the cruise floor, which is what sunnypilot's
a_min_override achieved, and it can never brake more weakly than stock.

Tests: 12/12 pass — curve ceiling, speed-limit precedence, hazard engage + adaptive decel in
[-1.5,-0.3], MIN_V floor, lead blocks rising edge, lead-past-hazard does not block, sticky latch
survives a lead appearing mid-approach, and full inertness when the param is off / long disabled
/ overriding / road clear.

## 2026-07-28 — mapd port, Phase 7: firm hazard pre-braking (default OFF) — offline block complete

- `openpilot/common/grt_params_keys.inc` — registered SmartCruiseControlMapHazardAccel
  (PERSISTENT, BOOL, default "0"). Separate from the SmartCruiseControlMap master switch on
  purpose: this is the most aggressive part of the feature and should only be enabled after
  the speed-ceiling behaviour has been driven and validated.
- `openpilot/selfdrive/controls/lib/longitudinal_planner.py` — Hook 2, one functional line:
  `candidates += grt_hooks.extra_accel_candidates(v_ego)` immediately before the min().
- `openpilot/grt/tests/test_hooks.py` — 9 tests, all passing.

This replaces sunnypilot's a_min_override kwarg on long_mpc.update, which does not exist in
this openpilot version. Safety argument, asserted by test: a_cruise saturates at
A_CRUISE_MIN = -1.2 while the adaptive hazard decel spans [-1.5, -0.3], so the candidate only
wins the min() when it is HARDER than the cruise floor. It can therefore never make braking
weaker than stock, and the result is still clipped to ACCEL_MIN downstream.

Also asserted: inert while the param is off (the default), no candidate when a lead is present,
no candidate on a clear road, an unregistered param degrades to off instead of raising, and a
controller exception cannot propagate into plannerd.

Total upstream footprint in longitudinal_planner.py is 19 added lines, 0 deletions, all
GRT-MOD sentinel-wrapped.

### Offline block is COMPLETE. Everything from here needs the car.

Remaining work is the on-device block and must be run with the Staria powered and supervised:
Phase -1 (prebuilt marker), Phase 1b (deploy binary + tiles), Phase 0 (compatibility gate),
then boot/params/road verification. Verification 1 (scons build) was reassigned to the device
because the Pi5 has no capnp/scons toolchain at all - it was never run here and must not be
assumed to pass.

## 2026-07-29 — mapd port, on-device deployment + CRITICAL fail-safe fix

Deployed to comma4 and ran the Phase 0 gate. Found and fixed a bug of mine that would have
crashed plannerd on the car.

THE BUG: openpilot's Params raises UnknownKeyName for any key not in the COMPILED
params_keys.h table. Our keys live in grt_params_keys.inc, which only takes effect after a C++
rebuild - and this device runs prebuilt binaries. On device, all three of MapdSettings,
SmartCruiseControlMap and SmartCruiseControlMapHazardAccel raise. SmartCruiseControlMap's
__init__ read that param, and hooks.limit_v_cruise called the constructor OUTSIDE its
try/except, so the exception would have propagated into plannerd and killed longitudinal
planning on the next start.

THE FIX: added get_bool_safe() in scc_map.py (any failure -> False, i.e. feature off), and made
_scc_singleton() return None on construction failure, latched so it is not retried every frame.
Both hooks now degrade to a no-op. A fork feature must never be able to break the base system;
the failure mode is "disabled", not "crash". Regression tests added for both paths (26/26 pass).

DEVICE FINDINGS:
- Device was clean at dcb3550, exactly our base. Transferred via git bundle (atomic, no GitHub
  push, not the raw-file SCP the repo CLAUDE.md forbids) and fast-forwarded. Binary md5 verified
  on device.
- The build toolchain DOES exist, in /usr/local/venv/bin (scons 4.10.1, capnp/capnpc, pycapnp).
  A plain `which` misses it because the non-login ssh PATH excludes the venv.
- BUT a full scons build is blocked before it starts: SConstruct fails while READING
  SConscripts because openpilot/selfdrive/modeld/models/driving_supercombo.onnx is absent
  (chunked/LFS model, not tracked in git). Pre-existing device condition, unrelated to this
  port. Consequence: params_keys.h cannot be recompiled, so our param keys stay unknown and
  the feature stays disabled until that is resolved.
- Python needs NO build: cereal/__init__.py capnp.load()s the .capnp files at runtime, so our
  schema is already live on device. mapdOut is in SERVICE_LIST at 20 Hz / 2 MB.
- WIRE COMPAT PROVEN ON TARGET: device union discriminants are mapdExtendedOut 141 / mapdIn 142
  / mapdOut 143, matching the Go binary exactly; 24 fields; full round-trip OK.

PHASE 0 GATE: mapd runs, stays alive, creates msgq_mapd{Out,In,ExtendedOut,Cli}, and openpilot
python received 307/307 frames with tileLoaded=True at ~12 Hz. GATE-1 (messages arrive) and
GATE-2 (tileLoaded) PASS.

TILE BAND GOTCHA: band directories are floor(lat/2)*2, so tiles for latitude -34.x live in
source dir -36, NOT -34. The first deploy used -34 and mapd logged "could not unmarshal offline
data" with tileLoaded=False. Deploying -36 fixed it immediately. Deployed: -34 (83M) and -36
(24M) of 606M total; device has 8.8G free.

STILL OPEN: waySelectionType=fail and roadName empty because the car is stationary (vEgo=0.0,
bearingDeg=0.0) - way selection needs a heading. Resolves once driving. GATE-3 therefore needs
a road test.

## 2026-07-29 — mapd port: file-based config (works on a PREBUILT branch)

Resolved the params blocker properly, after re-reading this log's own warning: `nightly-dev` is
a PREBUILT branch that runs committed binaries and must NOT be built. So `grt_params_keys.inc`
will never be compiled into params_keys.h, and every fork param raises UnknownKeyName forever.
Fixing "the build" was therefore the wrong goal; the fork must simply not need a compiler.

(For the record: driving_supercombo.onnx is the build *input* that compiles into the shipped
driving_tinygrad.pkl chunks. Both machines have the outputs, neither has the input - it is
git-LFS and never fetched. Nothing is broken; this branch is just not meant to be built. I did
repeat the documented scons mistake, which dirtied panda/board/obj/{gitversion.h,version} on
the device; restored with git checkout -- and the device tree is clean again.)

Changes:
- `grt/registry.py` — GRT_CONFIG_DIR = /data/media/0/grt, deliberately OUTSIDE /data/params so
  Params::clear_all() can never delete it. Added MAPD_SETTINGS_PATH.
- `grt/scc_map.py` — get_bool_safe() now reads Params FIRST, then falls back to a plain file at
  <GRT_CONFIG_DIR>/<key> (1/true/on/yes). Params stays preferred, so this keeps working
  unchanged if the keys are ever compiled in. Any failure still yields False.
- `grt/settings.py` — added write_settings_file(): writes the JSON directly, atomically
  (tmp + os.replace, so mapd never sees a half-written file), bypassing Params entirely.
- `grt/registry.py` grt_procs() — mapd's launch command now runs write_settings_file()
  immediately before exec'ing mapd. clear_all() deleting MapdSettings no longer matters: mapd
  always reads a correct file at startup and keeps the values in memory. This is a single write
  per mapd start, NOT sunnypilot's 1 Hz rewrite loop.

To enable on device (no rebuild, no reflash):
  mkdir -p /data/media/0/grt && echo 1 > /data/media/0/grt/SmartCruiseControlMap
Hazard braking stays off until /data/media/0/grt/SmartCruiseControlMapHazardAccel is set to 1.

Tests: 29/29 (12 scc_map + 17 hooks), including both fallback polarities and absent-file.

## 2026-07-29 — mapd port: plannerd must ignore mapdOut in its SubMaster checks

Second latent bug caught on the car, same class as the params one: the fork degrading the base
system when its own process is absent.

plannerd's SubMaster gained 'mapdOut'. `LongitudinalPlanner.publish()` and plannerd both set
message .valid from `sm.all_checks()`, and all_checks() = all_alive() and all_freq_ok() and
all_valid(). mapd only runs when OSM tiles are installed, so on any device without tiles
mapdOut is never alive -> all_checks() False -> longitudinalPlan marked INVALID -> longitudinal
control faults. Nothing to do with whether the mapd feature is switched on.

Fix: pass ignore_alive / ignore_valid / ignore_avg_freq = GRT_SUB in plannerd's SubMaster.

Verified empirically on device with mapd stopped:
  WITHOUT ignores -> all_checks() = False
  WITH    ignores -> all_checks() = True

## 2026-07-29 — mapd ENABLED on the car + clean reboot

Enabled the speed-ceiling feature and rebooted. Device came back in ~136 s.

Post-reboot verification:
- manager, plannerd, controlsd, selfdrived, card, modeld all running; nothing crash-looping
  (managerState reports no process shouldBeRunning-but-not-running).
- **mapd is running under manager** (pid 10159, /data/openpilot/third_party/mapd/mapd), with
  msgq_mapd{Out,In,ExtendedOut,Cli} present. mapdOut publishing, tileLoaded=True.
- **longitudinalPlan VALID=True** with 400 msgs - confirming the SubMaster ignore fix on the car.
- SmartCruiseControlMap=1 survived the reboot, because the flag lives in /data/media/0/grt,
  outside the params dir. The file-based design works as intended.
- SmartCruiseControlMapHazardAccel still absent => hazard braking OFF, as planned.

One wrinkle understood and benign: MapdSettings was absent immediately after boot. Cause is
clear_all() running on a boot-time transition AFTER mapd had already started. Harmless, because
mapd reads its settings at startup and our launch cmdline rewrites the file before EVERY mapd
exec - so any future mapd start gets correct settings regardless. Verified the write command
works and the file persists in steady state; also sent a reloadSettings and mapd stayed healthy.

STILL OUTSTANDING - THE ROAD TEST. waySelectionType=fail and roadName empty because the car is
stationary (vEgo=0, bearingDeg=0); way selection needs a heading. Nothing more can be proven
parked. Drive order: (a) known curve, (b) posted speed-limit change, (c) stop sign with no lead
car. Hand near the wheel, ready to override. Then pull /data/media/0/mapd_debug.log.

## 2026-07-29 — ROOT CAUSE of the failed test drive: radarState lead field name

Test drive: neither speed-limit nor curve slow-downs happened. Root cause found in swaglog.

`scc_map.update_calculations()` did `raw_has_lead = lead1.status or lead2.status`. openpilot's
`radarState.LeadData` has NO `status` field - it is `present` ("true if a lead is present").
sunnypilot's schema used `status`, and I ported the line verbatim without checking. Result:
AttributeError on EVERY frame - 38,300 of them in that drive. hooks.limit_v_cruise caught each
one and returned v_cruise unchanged, so the feature was a silent, complete no-op. The car drove
normally throughout, which is the fail-safe design working exactly as intended, but the feature
never ran. It also explains the missing mapd_debug.log: update_calculations() throws before
_write_debug() is ever reached.

Fixed .status -> .present in all four places (lead gate, both lead-past-hazard checks, debug log).

THE REAL LESSON - why the tests did not catch it: the unit tests stub cereal with
SimpleNamespace, and my stub used `status` too. The tests were validating my own assumption, not
reality, so 29/29 passed while the car raised on every frame. Two fixes:
  1. the stubs now use `present`, mirroring the real schema;
  2. new `openpilot/grt/tests/test_schema_conformance.py` loads the ACTUAL log.capnp via pycapnp
     and asserts all 14 cereal fields the fork reads really exist. Verified it FAILS on `status`
     (listing the available field names) and passes on `present`. Stubbed tests can no longer
     drift from the schema silently.

Note dRel was fine - it does exist. Only `status` was wrong.

## 2026-07-29 — test drive forensics + a second prebuilt-branch limitation

Drive identified as route 00000034--aa6306c38e (28 segments): 167,025 carState frames, 58%
moving, max 66 km/h, average 35 km/h while moving, and carControl.enabled for 60,543 frames -
so openpilot WAS engaged and the drive was a valid test. Route 00000035--da076d2e95 is just
post-drive idling (0 moving, 0 engaged). The feature failed purely because of the per-frame
AttributeError, now fixed.

SECOND PREBUILT LIMITATION FOUND: **mapdOut is never written to rlog on this device** - 0 frames
across the whole drive, while carState had 2,738 per segment. Cause: loggerd is C++ and reads
the COMPILED services.h, generated back on Jul 23, which has no mapdOut. `should_log=True` in
services.py only affects the python view. So the earlier claim that "mapdOut is logged so drives
can be replayed for tuning" is FALSE on a prebuilt branch, and drives cannot be retrospectively
analysed for mapd behaviour.

Consequence: `/data/media/0/mapd_debug.log` is now the ONLY instrument for tuning and
diagnosis. It was absent after the failed drive because update_calculations() threw before
_write_debug() could run; with the fix in, it will be written from the next drive onward.

Still unknown and only answerable by driving: whether mapd's way selection succeeds in motion.
Parked it reports waySelectionType=fail with an empty roadName because vEgo=0 and bearingDeg=0.

## 2026-07-29 — fix verified on device after reboot

- scc_map update failures since boot: **0** (was 38,300 in the failed drive).
- `/data/media/0/mapd_debug.log` is now being WRITTEN (78 KB and growing) - proof that
  update_calculations() completes every frame instead of throwing.
- Sample entry parked: v_cruise_kmh=145, v_ego 0, all mapd values 0 (expected while stationary).
- mapd running, plannerd running, longitudinalPlan VALID=True, mapdOut live tileLoaded=True.
- waySelectionType still `fail` - expected while parked; only a drive can settle it.

Ready for a second test drive. mapd_debug.log is the instrument: check map_curve_speed_kmh,
speed_limit_suggested_kmh, v_target_kmh, state and is_active while moving.

## 2026-07-29 — SECOND DRIVE WORKED; slow-downs far too aggressive -> approach profile

Second drive: the feature engaged (869 active frames; stop, T-Junction, mini_roundabout,
turning_circle all detected; speed limits and curve speeds all present). User reported the
slow-downs were much too aggressive and reached target speed well before the hazard.

MEASURED from /data/media/0/mapd_debug.log (2.5 MB, 4,067 frames):
  turning_circle 69->20 km/h over 586 m: needed -0.28, USED -1.64 m/s^2 (5.8x), target 47 m early
  stop           63->20 km/h over 830 m: needed -0.17, USED -1.69 m/s^2 (10x),  target 365 m EARLY
  decel while active: mean -0.48, p10 -1.28, hardest -1.69 m/s^2

ROOT CAUSE: v_target stepped straight to the FINAL target the moment a hazard/limit came into
range. The planner's P-controller, a_cruise = clip(v_cruise - v_ego, A_CRUISE_MIN=-1.2, ...),
then saturates at maximum braking to close a huge speed error immediately - so it brakes hard,
arrives at target far too early, and coasts the rest of the way.

FIX - distance-based approach profile. Command the speed the car should be at RIGHT NOW to
arrive at the target exactly AT the hazard:
    v_now = sqrt(v_target^2 + 2 * APPROACH_DECEL * distance)
APPROACH_DECEL = 0.5 m/s^2 (~3.3x gentler than the -1.64 measured). At distance 0 it equals the
target exactly; far away it is high and has no effect; if a hazard appears late the formula
self-escalates (small d -> low command -> harder braking), so safety is preserved.
Applied to hazards AND to upcoming lower speed limits (nextSpeedLimit/nextSpeedLimitDistance).
The hazard engagement distance now also uses APPROACH_DECEL so the latch fires at the right point.

Also set "slow_down_for_next_speed_limit": false in MapdSettings. mapd's own lookahead
(speed_limit.go:111) was stepping speedLimitSuggestedSpeed down to the upcoming limit while the
sign was still far away - the same harsh step, from the mapd side. Current-limit ceiling still
comes from speedLimitSuggestedSpeed; the APPROACH is now ours to shape. This deliberately
reverses the earlier Phase 6 "let mapd do the lookahead" decision, on drive evidence.

Validated against the real drive episodes: at 0.5 m/s^2 braking would start ~336 m out
(turning_circle) and ~275 m (stop), landing exactly on 20 km/h AT the hazard.

Tests 18/18 incl. profile maths: lands on target at 30/120/400 m, monotonic in distance,
implied decel == APPROACH_DECEL, and MIN_V floor still applies at the hazard.

TUNING KNOB: APPROACH_DECEL in openpilot/grt/scc_map.py. LOWER = gentler and starts earlier.

## 2026-07-29 — THIRD DRIVE: approach profile VALIDATED ("felt perfect")

Measured on the frames where OUR target was actually binding (157 frames), which is the honest
metric - raw a_ego includes lead-car and driver braking:

               drive 2 (step)      drive 3 (profile)     design target
  median          -0.23 (spikes to -1.69)   -0.51            -0.50
  mean            -0.48                      -0.53           -0.50
  p10             -1.28                      -0.77             -
  overshoot       5.8x - 10x                 ~1.0x            1.0x

Median -0.51 against a 0.50 target: the profile does exactly what it was designed to do.
Visible in the raw frames: v_target 45.6 km/h with the hazard still 130 m away (an intermediate
speed, not a step to 20), and v_target 60.3 with the next limit 2 m ahead - landing on the limit
right at the sign. APPROACH_DECEL stays at 0.5; do not touch it without new evidence.

(My episode detector found 0 episodes this drive because it keyed off a large speed error at the
FIRST frame, which the profile no longer produces. The binding-frames metric replaces it.)

## 2026-08-19 (later) — cap split DEPLOYED to comma4

Device on `6c0f45c`, AGNOS 18.7, clean tree, healthy. 7.7 KB bundle, 2 commits, clean
fast-forward, manager back on the first poll.

The device was **offline on the first attempt** ("No route to host"); it returned after ~2 min.
It was also further along than I expected — at `01388ee` (the hook-8 jerk fix from another
session) — but `git merge-base --is-ancestor` confirmed that was an ancestor of this branch, so
the fast-forward carried it forward untouched. Deriving the range from the device's ACTUAL HEAD
rather than a remembered one keeps catching this.

Verified BEFORE the reboot — schema 30/30, suites 43 / 44 / 77 — and the whole point of the
change confirmed against the device's real modules:

```
V_CRUISE_MAX (driver buttons) = 145
AUTO_MAX_KPH (feature)        = 110.0
MAX_LIMIT_KPH (plausibility)  = 145.0
auto    _clamped(120)         = 110.0
confirm _clamped(120, cap=F)  = 120.0
```

Three constants, three different jobs, none of them tied to each other — which is exactly what
the last two rounds of bugs were about.

Verified AFTER the reboot: all processes up incl. `soundd`/`micd`; `managerState` reports nothing
missing; `onroadEvents` only `wrongGear`/`seatbeltNotLatched` — **no `commIssue`, no
`processNotRunning`**, engagement not blocked; `longitudinalPlan VALID=True`; `grtSetSpeedState`
at 20 Hz, `active=True`; **zero `grt:` exceptions**.

**On the next drive:**
1. **The reported case** — push the set speed above 110 with the +/- buttons and confirm it stays
   there, including through a limit change on a road posted above 110.
2. On a 120 road the feature settles at **110** and leaves it. If that is not what is wanted,
   `AUTO_MAX_KPH` is the single knob — but it is the operator's call, not a default to change
   quietly.
3. Accepting a prompt for a limit above the cap gives the **posted** number, not 110.

## 2026-08-19 — the 110 cap now binds the FEATURE, not the driver's buttons

Operator: the auto 110 cap works fine, but the steering-wheel +/- buttons must be able to go
above it.

**Cause:** I had implemented the cap as `V_CRUISE_MAX = 110` in upstream's `cruise.py` — and that
constant is exactly what the manual buttons clamp against (`_update_v_cruise_non_pcm`,
`initialize_v_cruise`). Capping there capped the driver. This is the §0.6 pattern again, in its
mildest form: the fork holding a limit the driver could not exceed.

**Fix:** `V_CRUISE_MAX` returns to upstream's 145 — **`cruise.py` is untouched by the fork again**,
one fewer touchpoint, GRT_MODS row removed. The cap moves inside as `AUTO_MAX_KPH = 110`, applied
in `_clamped()` to what the *feature* commands.

Three consequences, worked through here rather than discovered on the road:

1. **A confirmation bypasses the cap.** The prompt says "Speed limit 120 km/h — press RES/+ to
   accept"; accepting must give 120, not 110. A button press IS the manual override the cap is
   not meant to block. `_clamped(cap=False)` on the confirm path only.
2. **The cap must never claw back a manual choice.** Without a guard, a driver at 130 on a 120
   road gets dragged to 110, pushes + back to 130, and is dragged again every `REOFFER_S` —
   forever. Suppressed when the driver is above the cap AND the limit is above the cap AND the
   limit is not offering an increase. That last clause is load-bearing: at 116 with a 120 limit
   the ROAD offers more, so it is still a real decision to put to the driver.
3. **Seeding on a 120 road now yields 110**, so a driver sitting at 120 is no longer "owned" by
   the feature. One existing test was incidentally asserting the opposite.

### Two of your instructions genuinely conflicted, and I picked

- **08-06:** "I set cruise to 110 and the new limit is 120 — I expect it to auto change to 120."
- **08-07:** "V_CRUISE_MAX ... tone it down to 110."

Both cannot hold: the first requires commanding 120 automatically, the second forbids it. **The
cap won**, since you have since said it works fine. So 110 + a 120 limit now caps silently and
does **not** prompt — there is nothing to ask, and confirming would be the only way to reach 120
(which the buttons now also allow directly).

The 08-06 test was rewritten to 90 → 100, which is what it was actually for (ownership is not an
auto condition), and a new test covers the capped 110 + 120 case. **Lesson recorded in the plan:
when a new instruction contradicts an older one, say so and choose — do not let a test quietly
encode the loser.**

Tests: 43 scc_map, 44 hooks, 77 set_speed (5 new), 30/30 schema. **NOT YET DEPLOYED.**

## 2026-08-07 (later) — set-speed-is-final + 110 cap DEPLOYED to comma4

Device on `b500214`, AGNOS 18.7, clean tree, healthy. 12 KB bundle from the device's actual HEAD
(`277973d8e2..nightly-dev`, 3 commits), clean fast-forward, back up in ~75 s.

Verified BEFORE the reboot — schema 30/30, suites 43 scc_map / 44 hooks / 71 set_speed — and the
critical decoupling confirmed against the device's real modules:

```
V_CRUISE_MAX = 110  |  MAX_LIMIT_KPH = 145.0
120 limit recognised: True -> clamps to 110.0
```

That is the trap check: had `MAX_LIMIT_KPH` still tracked `V_CRUISE_MAX`, a real 120 limit would
have read as implausible and the feature would have gone silently inert on 120 roads.

Verified AFTER the reboot: all processes up incl. `soundd`/`micd` (no audio transient this time);
`managerState` reports nothing missing; `onroadEvents` only `wrongGear`/`doorOpen`/
`seatbeltNotLatched` — **no `commIssue`, no `processNotRunning`**, engagement not blocked;
`longitudinalPlan VALID=True`; `grtSetSpeedState` at 20 Hz with `active=True`; **zero `grt:`
exceptions**.

**What to check on the next drive**, in order of what would tell us most:
1. **The reported case is fixed** — in a zone the map gets wrong, raise the set speed and confirm
   the car actually holds it after you lift off. That is the whole point of this change.
2. **The 110 cap** behaves as intended, including on a 120 road (set speed should sit at 110, and
   there should be no repeating prompt — the `at_limit`-on-clamped-value fix).
3. **The ramp trade-off**: after any manual set-speed change, pre-sign shaping stays off until the
   feature re-takes ownership. If in-band sign approaches now feel abrupt after a manual nudge,
   that is this, and it is tunable.

## 2026-08-07 — the driver's set speed is now the FINAL authority + V_CRUISE_MAX 145 → 110

Drive report, 14:45–15:00: map wrongly showed 60, driver set cruise to 100, **both the Staria
cluster and the comma MAX displayed 100** — and the car still dropped back to 60 the instant they
lifted off the throttle.

**This also answers the open question from 08-06.** MAX was never "out of sync" in the sense I
assumed: the display was right and the *car* was wrong. The set speed followed the driver
correctly; the physical behaviour was pinned by a stale map authorisation.

**Cause:** `scc_map`'s steady-state ceiling used `authorisedLimit`, which held the last authorised
limit (60). **Nothing revoked it when the driver raised the set speed**, so hook 1 pinned the
planner's `v_cruise` at 60 permanently. The driver could not overrule the map by any means.

**Fix:** the steady-state posted-limit ceiling is REMOVED while the set-speed feature is active.
The argument generalises — *a ceiling only ever does anything when it is below the set speed*, so
"the map may never hold the car below the set speed" and "there is no ceiling" are the same
statement. It was redundant anyway: with feature B the limit already reaches the car through the
set speed. One authority, not two.

Untouched: curve braking, hazard braking, the pre-sign ramp, and the whole fail-open path. **All
three FAILS-OPEN tests pass unchanged** — that is how I know the removal did not leak outside
`if gated:`. Two gated-ceiling tests were replaced, one of them asserting the reported case
verbatim (stale 60 authorisation + driver-set 100 → no ceiling).

Pre-authorisation now also requires the set speed to still be **ours**, so the map cannot pull the
driver below a number they dialled in even transiently. **Cost, stated rather than discovered:**
after any manual set-speed change the ramp stays off until the feature re-takes ownership.

### V_CRUISE_MAX 145 → 110, and the trap in it

Sentinel-wrapped in `cruise.py`, with a `GRT_MODS.md` row recording that 110 is a *preference*,
not a technical limit — so a future rebase does not "restore" 145 as a bugfix.

**The trap, caught before it shipped:** `set_speed.py MAX_LIMIT_KPH` was `float(V_CRUISE_MAX)`.
At 110 a real **120 limit would read as implausible**, and the feature would go inert on exactly
the roads it matters most — while the heartbeat logged a reassuring `implausible_limit`. It is now
a literal 145: a 120 limit is recognised and then clamped to 110. `at_limit` also compares the
**clamped** limit, or a 120 road would re-decide every `REOFFER_S` forever. Both covered by tests.

### The pattern this is the third instance of — now §0.6 of the plan

| # | Defect | What the driver could not do |
|---|---|---|
| 1 | 60 km/h no-map fallback | stop the fork inventing a limit the road never posted |
| 2 | ownership term in the auto rule | keep a round set speed they dialled in themselves |
| 3 | un-revoked `authorisedLimit` ceiling | overrule a *wrong* map limit by raising the set speed |

> Every value the fork commands must be one the driver can override by an ordinary control input,
> immediately, with the override sticking. If a fork feature can hold a value against the driver's
> own most recent instruction, that is a bug regardless of how correct the value is.

Each looked reasonable in isolation; each was only visible from the driver's seat, never from a
test. Map data being wrong or stale is the normal condition, not an edge case — which is why the
driver has to win by construction.

Tests: 43 scc_map, 44 hooks, 71 set_speed, 30/30 schema. **NOT YET DEPLOYED.**

## 2026-08-07 — auto-rule fix DEPLOYED to comma4

Device on `277973d`, AGNOS 18.7, clean tree, healthy. **One commit** — the 2026-08-03 fallback
removal (`005d0035`) had already gone out in the 08-04 sync, so only the auto-rule fix was
outstanding. 6.4 KB bundle, clean fast-forward from `fe51b09`, back up in ~75 s.

Worth noting for the runbook: my first instinct was to bundle from the old `dcb3550cac` base and
I listed 23 "missing" commits — but the device HEAD `fe51b09` was *itself in that list*. Checking
`git merge-base --is-ancestor <device HEAD> nightly-dev` and bundling `fe51b09..nightly-dev`
turned a 6 MB transfer into 6.4 KB and made the real delta obvious. **Always derive the range
from the device's actual HEAD, not from a remembered base.**

Verified BEFORE the reboot — schema 30/30, suites 42 scc_map / 44 hooks / 67 set_speed, real
imports — and the reported case evaluated against the shipped rule **on the device**:

| set speed | new limit | Δ | result |
|---|---|---|---|
| 110 | 120 | +10 | **AUTO** (was ASK — the reported bug) |
| 116 | 120 | +4 | ASK — not a multiple of 10 |
| 103 | 90 | −13 | ASK — original hand-tuned protection intact |
| 100 | 60 | −40 | ASK — the >20 km/h safety rule intact |

`_why_not_auto(116, 4)` returns `set_speed_not_multiple_of_10`, i.e. the misleading
`driver_owns_set_speed` string is gone from the logs.

Verified AFTER the reboot: all processes up incl. `soundd`/`micd` (no audio transient this time);
`managerState` reports nothing missing; `onroadEvents` only `wrongGear`/`doorOpen`/
`seatbeltNotLatched` — **no `commIssue`, no `processNotRunning`**, so engagement is not blocked;
`longitudinalPlan VALID=True`; `grtSetSpeedState` at 20 Hz with `active=True`; **zero `grt:`
exceptions**.

**On the next drive, the one open question from 08-06:** if MAX ever jumps while a prompt is
open, look for a `seed_from_map` line in `/data/media/0/grt/set_speed.log` at that moment. That
is the discriminator for the engage-seed path, which is the only route that can write the set
speed while a prompt is pending and which I could not rule out statically.

## 2026-08-06 — auto-adopt rule corrected: ownership dropped; nothing pre-authorised while asking

Two issues from the drive. **Local only — comma4 offline.**

### Issue 2 (the clear one): a driver-set round speed must keep auto-tracking

Reported verbatim: set 110 by hand, new limit 120, and it asked for confirmation. Expected: auto
change. And 116 should still ask, because it is not a multiple of 10.

My auto test had **three** conditions where the operator had specified two — I had added
`self.tracking` (the feature still owns the set speed). A driver-set 110 fails it, so it
prompted. **Removed.** Roundness alone is the hand-tuned test and it does the job: 103 and 116
still prompt, 110 does not. The rule is now exactly:

> auto iff `set speed is a multiple of 10` AND `|Δ| ≤ 20 km/h`; otherwise ask.

`tracking`, `_owned_kph` and `_in_force_kph` are KEPT — they still drive `at_limit`, the
heartbeat and `grtSetSpeedState`. They are instrumentation now, not decision inputs.
`_why_not_auto` no longer returns `driver_owns_set_speed`, which would have actively misled the
next diagnosis.

### Issue 1: same root cause, plus an invariant

`tracking` was the **only time-varying term** in the auto test, and that test is evaluated at two
different moments: once for the UPCOMING limit (which pre-authorises it and unlocks the approach
ramp) and again when the limit becomes CURRENT (which decides prompt vs auto). If it flipped in
between, **the car slowed while the display waited** — exactly "Max speed is out of sync". Δ and
roundness cannot disagree that way, so dropping `tracking` removes the disagreement entirely.

Belt and braces, because the operator must be able to rely on "nothing changes until I answer":
**nothing is pre-authorised while a prompt is open.** Early return in `_preauthorise_upcoming`,
plus an explicit revoke on the frame a prompt is created — that method runs *before* the pending
branch, so it had a one-frame window where both could be set.

**NOT fully explained, and I am not claiming otherwise.** The above explains the car slowing. It
does not explain MAX itself moving, because `update()` cannot write the set speed while a prompt
is open. The one path that bypasses the pending branch is the **engage seed**: `engage_edge`
calls `_reset()` (clearing the prompt) and then seeds from the map. `grt_engage_edge` is
byte-identical to upstream's own condition on the next line — the one gating
`initialize_v_cruise` — so a momentary `carControl.enabled` glitch resets the set speed with or
without the fork; we only change what it resets *to*. **Cannot be ruled out statically.**
Discriminator for the next drive: a `seed_from_map` line in `set_speed.log` at the moment MAX
jumped. It is already logged — just look for it.

### The generalisation worth keeping

> If the same predicate is evaluated at two different times, every time-varying term in it is a
> bug waiting to happen. Here it produced a feature that acted on the car and not on the display.

Tests: 67 set_speed (3 new — the reported 110→120 case verbatim, the 116 counter-case, and one
asserting `authorisedNextLimit` stays 0 for **every** frame a prompt is open), 42 scc_map, 44
hooks, 30/30 schema. Plan doc §11.2 updated: rule 2a struck through with the reason kept.

## 2026-08-03 — REMOVED the 60 km/h no-map fallback (operator: problematic)

The engage seed used to fall back to **60 km/h** when no trusted limit had arrived within 10 s.
Removed entirely at the operator's instruction. **Local only — comma4 is offline; deploy
tomorrow.**

**Why it was wrong.** "No fix yet" is not an exceptional state — it is the *normal* state at the
moment of engagement (parked: `vEgo=0`, `bearing=0`, so `waySelectionType=fail`), and the
persistent state anywhere off-tile. So the timeout fired routinely and forced the set speed to 60
regardless of the road. Engage at highway speed, wait 10 s, and the set speed dropped to 60 — the
car then braking for a limit that was never posted anywhere.

**What happens now instead:**
- the seed **waits indefinitely** for a real posted limit; upstream's own `V_CRUISE_INITIAL*`
  stands until one arrives, and if none ever does the feature stays out of the way for the whole
  drive (owns nothing, authorises nothing, so `scc_map` also fails open);
- if the driver touches the cruise buttons while it is still waiting, the seed is **abandoned** —
  the speed is theirs, and a limit arriving later will not overwrite it. New `seed_abandoned`
  line in `set_speed.log`.

Deleted: `NO_MAP_DEFAULT_KPH`, `SEED_TIMEOUT_S`, and the whole `seed_no_map` path. `_seed()` is
now only ever called with a limit that passed `_read_limit`, which simplifies it — seeding is
unambiguously an adoption, so the limit is in force, decided and authorised in one place.

**The general rule, now written into the plan (§11.5) because it outlives this feature:**

> A map-driven feature may only ever command a value the map actually supplied. A "sensible
> default" for missing data is an invented measurement, and it will be wrong in exactly the
> situations where the data is missing. The correct behaviour for absent data is to do *nothing*
> and leave the base system in charge.

That is the same prime directive as §0.2, in a form I had not applied to *data* — only to
crashes. Worth remembering: the fallback did not fail safe, it failed *confidently*.

Docs updated: `PORT_MAPD_FROM_SUNNYPILOT.md` §11.2 (rule 1 restated) and §11.5 (rewritten, with
the old behaviour struck through and the reason kept, per this doc's convention), plus the §2.6
illustration which had used the 60 seed as its example. `PROGRESS.md` status + next step.

Tests: 62 set_speed (3 seed tests rewritten, incl. a REGRESSION test asserting no fallback value
is ever forced over 30 s without a fix), 31 scc_map, 44 hooks, 29/29 schema conformance.

## 2026-07-30 — ramp + coast-down DEPLOYED to comma4

Device on `19c3568`, AGNOS 18.7, clean tree, healthy. Fast-forward from `4a9305f`, 11 files,
+273/-13. Back up in ~75 s; no AGNOS update needed.

Verified BEFORE the reboot (cheapest place to find a problem):
- schema conformance **29/29** against the device's own log.capnp, including all four wire
  discriminants — mapd still 141/142/143, `grtSetSpeedState` 140 after adding `authorisedNextLimit`.
- unit suites on device: 31 scc_map, 44 hooks, 59 set_speed.
- real-import gate: `longitudinal_planner` imports with the new hook, `COAST_DECEL = 0.5`,
  `soften_cruise_decel(-1.2) -> -0.5`, `authorisedNextLimit` round-trips.

Verified AFTER the reboot:
- all processes up; `managerState` reports **nothing** shouldBeRunning-but-not-running.
- `onroadEvents` = `wrongGear` + `seatbeltNotLatched` only. **No `commIssue`, no
  `processNotRunning`** — engagement is not blocked.
- `longitudinalPlan VALID=True`; `grtSetSpeedState` at exactly 20 Hz (201 msgs / 10 s), `active=True`.
- **Zero `grt:` exceptions.**

**soundd, again — worth watching.** It crashed once during startup (`soundd_thread()` audio-stream
init) and manager restarted it successfully; it is running and stable. The other tracebacks in the
log are `athenad`/urllib3 network retries, which are routine. This is an audio-init race at boot,
not fork-related — but it matters because if soundd ever fails to come back, `processNotRunning`
is `ET.NO_ENTRY` and **blocks engagement**, which is exactly the state the operator hit and cleared
with a manual reboot. If the car ever refuses to engage, check `micd`/`soundd` first.

**Next drive judges two things:** whether the pre-sign ramp now feels right for in-band limit
changes (it should ease down at ~0.5 m/s² and land on the limit AT the sign, not step at it), and
whether the coast-down after an overtake feels natural. Both instruments are in place:
`/data/media/0/grt/set_speed.log` and `/data/media/0/mapd_debug.log`.

## 2026-07-30 — ramp restored when no confirmation is needed + gentle coast-down to set speed

Operator review of the authorisation gate, two requirements.

### 1. The pre-sign ramp must be ON whenever confirmation is not required

The gate had switched the approach ramp off wholesale, because an UPCOMING limit cannot be
authorised before it becomes current. Correct observation from the operator: when the auto rules
already say no confirmation is needed, there is nothing to wait for.

`set_speed` now PRE-AUTHORISES an upcoming limit that passes the **same three rules** as a normal
auto-adopt (feature owns the set speed AND it is a multiple of 10 AND |Δ| ≤ 20), and `scc_map`
lets the ramp shape the run-up at `APPROACH_DECEL = 0.5 m/s²` again. A change that WOULD prompt
is still not pre-authorised, so the confirmation flow is untouched.

**Published in its own field, `authorisedNextLimit`, deliberately NOT reusing `authorisedLimit`.**
That separation is the entire point: if the CEILING saw the upcoming value, the target would drop
to the new limit while the sign was still hundreds of metres away — exactly the harsh step the
ramp exists to prevent. There is a test asserting the pre-authorised value is not used as a
ceiling.

### 2. Gentle coast-down to the set speed (hook 5, new)

Operator's example: driving 110 to overtake with cruise set to 100, lift off the throttle, and
the car should ease back to 100 rather than braking hard. Stock clips `a_cruise` at
`A_CRUISE_MIN = -1.2 m/s²`, and a 10 km/h error saturates it, so it braked at the full −1.2.

`COAST_DECEL = 0.5` raises that floor for PLAIN overspeed only. One line in
`longitudinal_planner`, right after `get_cruise_accel`.

**Why this cannot make the car less able to stop** (each asserted by test):
- it only ever RAISES `a_cruise`, and `a_cruise` is one candidate in the planner's `min()` — with
  a lead the MPC candidate is harder and wins; with a hazard, hook 2's candidate wins. So it can
  only bind when the cruise branch is the sole reason for braking, i.e. plain overspeed on a
  clear road;
- skipped entirely when hook 1 lowered `v_cruise`, so the map approach profile keeps full
  authority and its late-hazard self-escalation still works;
- skipped when `v_cruise ≈ 0`, which is how `forceDecel` demands a stop.

`COAST_DECEL = 1.2` restores stock behaviour exactly. Trade-off worth knowing: the car now spends
longer above its set speed after an overtake.

### One spec ambiguity resolved by precedent, not by asking again

The operator's parenthetical read "auto change if <=20 km/h **OR** cruise set speed is a factor
of 10". The earlier explicit statement was AND — *"auto-adopt only changes within ±20 km/h if set
max speed is a factor of 10"* — and AND is what is implemented. OR would let a hand-tuned 103 be
overwritten silently, which rule 2a exists to prevent. Flagged rather than silently chosen.

Tests: 31 scc_map, 44 hooks, 59 set_speed, 29/29 schema conformance. **NOT YET DEPLOYED.**

### Device note

The `micd`/`soundd` processes were down after the deploy reboot, raising `processNotRunning`
(which is `ET.NO_ENTRY` and blocks engagement). The operator rebooted and it cleared — a boot-time
audio-init transient, not caused by the fork (neither file is touched by it). **Correction to what
I said at the time:** I claimed the qlog proved this pre-existed the deploy, but the route I
checked (`00000043`) was created at 08:50, *after* the 08:48 reboot — so it proved nothing. The
claim was unsupported; the reboot resolved it either way.

## 2026-07-30 — drive 2 of set-speed: two issues root-caused from the logs and FIXED (not deployed)

**The alert DOES render** — the operator saw the confirmation box, which closes the one open
question from yesterday. `set_speed.log`: 782 lines, 12 engagements all seeded from the map,
**544 heartbeats reporting `at_limit`** (set speed sitting exactly on the posted limit), 2 clean
auto-adopts (40→20, 60→80). The core works.

### ISSUE 1 — the car obeyed the posted limit while the display waited for confirmation

Root cause is **feature A, not feature B**: `scc_map` applies the posted limit as a ceiling inside
the planner and knew nothing about feature B's confirmation state. Two independent notions of
"the limit". Measured in `mapd_debug.log` (34,001 frames):

| binding source | frames |
|---|---|
| current-limit **ceiling** (`speedLimitSuggestedSpeed`) | **1,069** |
| upcoming-limit **approach ramp** (`nextSpeedLimit`) | **95** |

Example frames: set speed 105, no hazard, commanded target pinned at the posted 40. And the ramp
at 80 m from a 20 zone commanding 37.9 — literally "it slowed down before the sign".

**Fix (operator chose "confirmation gates behaviour"):** card publishes `authorisedLimit` +
`active` on `grtSetSpeedState`; plannerd subscribes via `GRT_SUB` (already passed as all three
ignore lists — plannerd calls `all_checks()` UNSCOPED, §2.2 of the plan); `scc_map` obeys only
authorised limits. **Fails OPEN** to mapd's own value when the feature is inactive or the message
is missing/stale — infrastructure failure must never silently stop the car obeying limits.
Curve and hazard braking are deliberately **not** gated; they are not speed limits.

**⚠️ Accepted consequence, stated for the record:** a declined or unanswered limit change means
the car does **not** slow for that sign. openpilot stops being an automatic speed-limit follower
and becomes one the driver authorises.

**⚠️ Documented trade-off:** the pre-sign approach ramp is now OFF while gated, because it acts on
the *upcoming* limit, which by definition cannot be authorised yet — the driver is only asked once
it becomes current. Slow-downs therefore happen AT the sign via the ceiling, at the planner's
`A_CRUISE_MIN = -1.2 m/s²` floor instead of the validated 0.5 m/s² ramp. **If that feels abrupt,
the follow-up is two-stage pre-authorisation: authorise the upcoming limit early, move the set
speed at the sign.** Deliberately not built blind — it needs a drive to know if it is warranted.

### ISSUE 2 — the prompt vanished before it could be answered. NOT the timeout.

The operator was right to push back on my first read. The 10 s window did expire twice, but the
prompts that could not be reacted to died in **0.2–0.3 s**:

```
-5.80  settling  limit=120                 ← 120 has held ~1 s
-5.55  pending   120  way=extended
-5.22  stale     current reads 60          ← retired 0.33 s later
-1.22  way_possible raw=120 way=possible
-0.20  pending   120  way=current
+0.00  stale     current reads 60          ← retired 0.20 s later
```

`mapdOut.speedLimit` was **alternating 60↔120 every 1–2 s** while `waySelectionType` churned
`current`→`possible`→`extended`: a spot where a freeway and a parallel service road overlap. Both
values passed the 1 s stability gate, so the feature offered 120 — **the wrong road** — and the
next flip killed it. Both of those prompts were spurious.

A stability requirement on the *retirement* cannot fix this: the 60 is equally stable. Two fixes:
- `LIMIT_STABLE_S` **1.0 → 3.0 s**, so neither value in a flip-flop qualifies at all;
- an offer is retired as stale **only once a DIFFERENT limit has become established in its own
  right**. Retiring on a single differing frame WAS the 0.2 s bug. This required moving the
  stability tracker so it keeps running while a prompt is open — it previously stopped, which is
  why the new test could not otherwise fire.

`PENDING_TIMEOUT_S` stays at **10 s** at the operator's instruction.

### Also per operator spec: DIRECTION-MATCHED confirmation

"Push the switch the way the speed is going." A **higher** pending limit is accepted with RES/+
and declined with SET/−; a **lower** one is accepted with SET/− and declined with RES/+. The
direction is captured when the offer is MADE, so a set-speed nudge mid-window cannot invert the
buttons under the driver. The alert text names the correct button.

This replaces "RES/+ accepts, SET/− declines", which in this drive read a routine **81→80 km/h
nudge as a decline** — visible in the log as `decline` 6.9 s after the offer.

New state stays single-writer, per the rule that earned itself last session: `_established_kph`
(the debounced limit) and `_authorised_kph` (what the driver accepted).

Tests: 28 scc_map (9 new, covering fail-open AND fail-closed), 35 hooks, 55 set_speed, 28/28
schema conformance including the four wire discriminants. **NOT YET DEPLOYED.**

## 2026-07-29 — set-speed test drive: GOOD PRELIMINARY RESULTS; plan doc consolidated

Operator reports **good preliminary results** from the first drive with set-speed tracking live.
Not yet a validation — see the open item below.

**STILL UNVERIFIED, and it is the one thing that decides whether half the feature works:**
whether the confirmation prompt actually RENDERS. It only fires while engaged, so it could not
be checked during deployment. If it never appears, the failure is benign but total — no prompt
means no confirmation means the set speed simply never moves for any out-of-band limit change,
which is the same silhouette as the 38,300-exception drive: fine from the driver's seat, doing
nothing. `/data/media/0/grt/set_speed.log` settles it: `pending` lines prove the tracker offered;
a `confirm` following one proves the driver could see and answer it. **Offered to pull and
analyse that log — not yet done.**

### `PORT_MAPD_FROM_SUNNYPILOT.md` rewritten as a two-feature record + reusable recipe

Merged everything learned from the set-speed work into the main plan rather than starting a
second document, at the operator's request. The doc now covers feature A (mapd control) and
feature B (set-speed tracking) and shares every constraint between them.

What is new in it:
- **§0.3** — the session's most important lesson: *a passing suite is evidence your tests agree
  with your model, not that your model is right.* 104 tests passed over two real defects, and
  one test actively asserted a bug was correct. Every set-speed defect was caught by review.
- **§0.4** — settle design questions with device data before choosing thresholds, with the two
  one-line queries that did so here (set speed sits at 105; limits are only ever multiples of 10).
- **§0.5** — restate an operator's prose spec as testable predicates and put genuine ambiguity
  back as a choice. Two of three answers changed the design.
- **§2 is now SIX bugs, not three** — added §2.4 (the SubMaster mistake in a process where it
  blocks ENGAGEMENT, with the per-process blast-radius table and the grep that finds it),
  §2.5 (an exact-frame `==` gate drops the event permanently; deferral vs decision), §2.6 (a
  terminal "handled" state killed the feature for a drive; single-writer-per-fact). Log flooding
  folded into §2.1 as the second half of the same rule rather than a seventh entry.
- **§3.2** — the two speed variables, the full chain from `v_cruise_kph` to the Staria cluster,
  and why `DT_CTRL` vs `DT_MDL` silently changes every timeout by 5×.
- **§4.3/§4.4** — the reusable patterns: a fork-owned message on a renamed `CustomReserved` slot,
  and the finding that a driver-facing alert needs **no schema change at all**.
- **§5** corrected on two counts I had recorded wrongly: `pkill -f "[m]anager\.py"` (bracket
  form) DOES work over ssh — only the bare form self-matches — and the flag needs no second
  reboot, since `update_params()` re-reads every 3 s.
- **§6** — added the real-import gate (2b) and the engagement gate (6b), and the instruction to
  run the cheap gates BEFORE the reboot.
- **§7.1** — instrument the negative case; a benign-sounding heartbeat reason can hide a dead
  feature.
- **§9** — an ordered recipe for the next fork feature, plus five rules worth memorising.
- **§11** — the full feature-B record, including why the first ±20-only design was nearly inert.

## 2026-07-29 — set-speed tracking DEPLOYED to comma4 and ENABLED

Device is on `dc6e2b5`, AGNOS 18.7, clean tree, feature ON. No reboot loop, no errors.

Sequence: git bundle `dcb3550cac..nightly-dev` (5.8 MB) → scp → `pkill -f "[m]anager\.py"` →
`git fetch <bundle> && git merge --ff-only` → reboot. Device was 8 commits behind at `cbe0818`;
fast-forward was clean, 16 files / +2044 −352. No scons, no cereal SCP. Bundle deleted after.

Device came back in ~120 s. AGNOS_VERSION 18.7 == /VERSION 18.7, so no OS update was triggered
this time (unlike the Jul 28 deploy).

**Verified ON DEVICE, before the reboot:**
- `test_schema_conformance.py` against the device's own log.capnp: **25/25**, including the four
  wire discriminants — mapdExtendedOut/mapdIn/mapdOut still **141/142/143**, so renaming
  `customReserved16` did not move mapd's slots. `grtSetSpeedState` = 140.
- Real imports with the actual openpilot deps (not the test stubs — the class of check the
  `.status` bug proved is necessary): `grt.hooks`, `grt.set_speed`, `grt.registry` all import,
  `grtSetSpeedState` is in `SERVICE_LIST`, `messaging.new_message('grtSetSpeedState')` builds,
  and `SetSpeedLimitTracker()` constructs reporting `enabled = False` (flag absent = safe
  default).

**Verified AFTER the reboot:**
- manager, card, plannerd, controlsd, selfdrived, modeld and mapd all up; managerState reports
  **nothing shouldBeRunning-but-not-running**; 0 Traceback/CRITICAL in the newest swaglog.
- `msgq_grtSetSpeedState` exists and `grtSetSpeedState` arrives at exactly **20 Hz** (240 msgs
  in 12 s) — card's `frame % 5` publish is behaving.
- **THE CRITICAL ONE — engagement is not blocked.** `onroadEvents` carries only `wrongGear` and
  `seatbeltNotLatched`, i.e. the legitimate physical blockers for a parked car. **No
  `commIssue`, no `commIssueAvgFreq`** — so adding `grtSetSpeedState` to selfdrived's SubMaster
  did not trip its unscoped `all_checks()` at `:381`/`:469`. That was the single highest-risk
  edit in this change and it is clean.
- `carState.vCruise` 255 (UNSET, not engaged); `grtSetSpeedState` pending=False tracking=False.

Then enabled: `echo 1 > /data/media/0/grt/SmartCruiseControlSetSpeed`, confirmed the tracker
reads `enabled = True`. No restart needed — `update_params()` re-reads every 3 s.

**WHAT COULD NOT BE VERIFIED PARKED, and must be watched on the first drive:**
1. **Does the alert actually render?** The prompt only fires while ENGAGED, and `ET.WARNING`
   alerts are cleared when the state machine is not in a warning-capable state — so a parked
   car cannot show one. The failure mode is benign (no visible prompt → no confirmation → the
   set speed simply does not change), but it would make every out-of-band limit change a silent
   no-op. **Watch for the text "Speed limit N km/h / Press RES/+ to accept" plus a chime.**
2. The set speed at engage being the posted limit (or 60) rather than 105.
3. `carState.cumLagMs` vs a pre-change segment — card now carries a subscriber AND a publisher.

Disable at any time with `rm /data/media/0/grt/SmartCruiseControlSetSpeed` (takes effect within
3 s, no restart). The device is one docs-only commit behind the Pi5 (`08aedfa`, PROGRESS.md).

## 2026-07-29 — set-speed tracking REDESIGNED to the user's spec, and part (b) BUILT

The user replaced the ±20-only design below after seeing the measurement that the set speed
sits at 105 km/h all drive (ExperimentalMode initial), which made part (a) nearly inert on the
roads actually driven. The new rules fix the cause rather than the symptom.

**AGREED RULES (this supersedes the "AGREED BEHAVIOUR" further down):**

1. **At engage, seed the set speed from the posted limit**, or **60 km/h if there is no map
   data** — replacing upstream's fixed `V_CRUISE_INITIAL` (40) / `V_CRUISE_INITIAL_EXPERIMENTAL_
   MODE` (105). The user chose that this wins even on a RES/resume engage, which upstream would
   otherwise answer with the previous set speed.
2. **A later limit change is adopted automatically only if ALL of:**
   a. the feature still OWNS the set speed — it equals the limit in force, or the value we
      ourselves last wrote;
   b. the set speed is a **multiple of 10** (60, 70, ... 120) — a non-round value is hand-tuned;
   c. the change is **within ±20 km/h**.
3. **Otherwise the new limit is offered as a PENDING prompt for 10 s**, adopted only on a RES/+
   tap. SET/− declines it; a limit change under it retires it as stale.

The >20 km/h rule is absolute — it applies even while the feature owns the set speed. On these
roads limits of 20 and 40 exist, so 120→80 and 60→20 both prompt. That is the user's explicit
safety call, and it means prompts will be common; that is intended, not a defect.

Rule 2a is what makes "set your own speed and keep it" work: dial in 103 in a 100 zone and the
feature never touches it again — it only asks. Dial back to exactly 100 and tracking resumes.

**THE ALERT COST NOTHING IN THE END.** `AlertManager.add_many(frame, alerts)` keys on
`alert.alert_type`, a plain string, and `selfdriveState.alertText1` is free-form Text with
`alertSound` reusing the existing `AudibleAlert` enum. So the prompt is a plain `Alert` object
with a fork-owned `alert_type` — **no `EventName` enumerant, no schema addition, nothing to
recompile**. That kills the on-device experiment the previous entry called for. `ET.WARNING` is
the right event type: `update_alerts` only clears WARNING when not engaged, and this feature
only runs engaged.

**THE REAL WORK WAS THE CHANNEL.** The pending state lives in `card`; only `selfdrived` can
raise an alert. Added a fork-owned message on reserved slot 16 — `CustomReserved16` →
`GrtSetSpeedState` renamed IN PLACE (struct ID kept, `log.capnp` ordinal `@142` kept), so the
wire discriminant stays 140 and **mapd's 141/142/143 do not move**. Now asserted permanently by
`test_schema_conformance.py` rather than checked by hand. card publishes at 20 Hz (not 100 — it
must not spend its CAN budget on a status message); selfdrived subscribes.

**THE DANGEROUS BIT, caught before writing it.** selfdrived calls `sm.all_checks()` **unscoped**
at `:381` and again at `:469` where it gates `self.initialized`. Adding `grtSetSpeedState` to
its SubMaster without the `ignore` list would have **blocked engagement entirely** on any device
where card doesn't publish it — strictly worse than the `longitudinalPlan` invalidation bug from
earlier today, and the third instance of the same class. The service is in selfdrived's `ignore`
list, and GRT_MODS.md now flags that row as the most safety-critical in the table.

Also settled empirically, not assumed:
- 11 MB of on-device `mapd_debug.log`: every limit seen is 20/40/60/80/120 — all multiples of
  10 — so rule 2b cannot latch the feature into permanent-prompt mode by itself.
- `mapdIn` already proves Python can publish on a service absent from the compiled
  `services.h`, so `grtSetSpeedState` needs no device experiment.
- Float comparisons use a 0.5 km/h epsilon. An `==` on values that have been through
  `round()`, `+=` and `clip()` would end tracking permanently after one wobble, and the symptom
  would read as "the feature got annoying", not as a bug.

Seeding detail worth knowing: engaging from standstill reports `waySelectionType=fail`, so the
seed WAITS up to 10 s for a first fix before falling back to 60. Without the wait every drive
would start on 60 and immediately prompt to move to the real limit. Upstream's own value stands
during the window.

**TWO BUGS IN THE OWNERSHIP LOGIC, both found in review, both in the seams between state
variables that were maintained on some code paths and not others:**

1. **An ignored prompt killed the feature for the rest of the drive.** Seeding 60 with no map,
   then meeting a 100 zone, gives |Δ|=40 → prompt. If the driver missed it, the limit was
   marked handled *permanently*: no further prompt, no adopt, set speed stranded at 60 in a
   100 zone — with the heartbeat reporting a benign-looking `already_handled` forever. Fixed
   with `REOFFER_S = 60 s`: an UNANSWERED prompt is offered again while the mismatch persists.
   A deliberate SET/− decline is never re-offered, because that was an answer.
2. **A stale prompt left residue.** When the road changed under an open prompt, the offer was
   marked "acted", so returning to that limit later skipped the decision entirely. A stale
   offer was never decided and is now not recorded as one.

The fix was structural, not three patches: `_owned_kph` ("the set speed WE established") and
`_in_force_kph` ("the posted limit in force") each now have exactly ONE writer, and the limit
in force is recorded at a single point — right after the stability gate, as a fact about the
road, independent of what we decide about it. The old `_prev_limit_kph` juggling, including a
save/restore around prompt creation, is gone.

Tests: 51 set_speed (rewritten — the ±20-only assertions were wrong, not failing), 35 hooks
(hook 3 + the new hook 4 alert), 18 scc_map, 25/25 schema conformance including the four wire
discriminants. NOT YET ON THE CAR.

## 2026-07-29 — set speed tracks the posted limit: adoption core IMPLEMENTED (part a, SUPERSEDED ABOVE)

Implemented the auto-adopt half of the feature designed below. **Part (b), the >20 km/h
PENDING + RES/+ confirmation, is coded but shipped DISABLED** — see "what is deliberately not
shipped" at the end.

New (fork-owned): `openpilot/grt/set_speed.py` — `SetSpeedLimitTracker`, plus
`grt/tests/test_set_speed.py` (27 tests).

Upstream hook (category C, sentinel-wrapped) — `selfdrive/car/card.py`, +12 lines:
- `SubMaster` gains `mapdOut` via `GRT_SUB_CARD`, **with `ignore_alive`/`ignore_valid`/
  `ignore_avg_freq`**, exactly as plannerd needed. card's own checks are scoped to
  `all_checks(['carControl'])` / `all_alive(['carControl'])`, verified, so `mapdOut` cannot
  invalidate `carOutput` — the ignores are belt-and-braces for the same bug class.
- One line `grt_hooks.track_set_speed(...)` after `initialize_v_cruise` and before the
  `CS.vCruise` assignment.

DEVIATION FROM THE DESIGN BELOW, deliberate: the design said "ONE sentinel-wrapped line in
cruise.py's non-pcm path". The hook is in card.py instead. `VCruiseHelper.update_v_cruise` has
no access to a SubMaster, and `selfdrive/car/tests/test_cruise_speed.py` calls
`update_v_cruise(CS, enabled, is_metric)` directly in five places — changing that signature
would break an upstream test suite. card.py already owns both the helper and a SubMaster, so
`cruise.py` is now untouched by the fork.

BEHAVIOUR (what actually ships):
- Edge-triggered on a CHANGE of `mapdOut.speedLimit`, never continuous. One decision per limit
  VALUE. A driver who overrides an adopted speed is never overridden back.
- |new limit − set speed| ≤ 20 km/h → adopt automatically, up AND down. Writes BOTH
  `v_cruise_kph` and `v_cruise_cluster_kph` (upstream keeps them equal in the non-pcm path and
  we run after it), so the comma UI MAX and the Staria cluster both follow.
- |difference| > 20 km/h → currently IGNORED, logged as `ignore`.
- Gates: feature flag on, openpilot engaged, set speed initialised, `mapdOut` alive+valid,
  `tileLoaded`, `waySelectionType` ∈ {current, predicted, extended} (parked reports `fail`),
  limit stable for 1.0 s, limit within [20, 145] km/h, and no cruise-button activity that frame.
- **Raising** the set speed additionally requires `waySelectionType` ∈ {current, extended}.
  `predicted` is mapd guessing which way we will take at a junction; acting on a guess to slow
  down is conservative, acting on it to speed up is not. Slowing down still honours `predicted`.
- Result clamped to upstream's `[V_CRUISE_MIN, V_CRUISE_MAX]`.

Both "not now" gates (driver on the buttons, upward-on-`predicted`) are DEFERRALS: they return
before `_acted_limit_kph` is set, so the limit is reconsidered on the next clear frame. The
stability counter is compared with `>=`, not `==`. This was a real bug in the first cut — with
an exact `==`, a button press landing on the single frame the gate fired dropped that limit
**permanently and silently**, because the counter kept incrementing and `==` was never true
again. The original test asserted the no-change and blessed it. Caught in review; the test now
asserts adoption on the following clear frame.

Two design points worth keeping:
- The [20, 145] km/h band REJECTS rather than clamps. Clamping would launder a garbage value
  into a legal set speed, and the band doubles as a units-error trap: mapd publishes m/s, so a
  km/h value leaking through reads ~3.6× high and gets rejected. Confirmed with pycapnp that
  `speedLimit` 16.667 → 60 km/h and that `str(waySelectionType)` yields the enumerant name.
- Confirmation/adoption acts on button RELEASE, the same edge upstream's `+1` bump uses. Our
  hook runs after it and assigns an absolute value, so the adopted limit wins cleanly instead
  of landing on limit+1. Staria RES/+ → `ButtonType.accelCruise`, verified statically in
  `opendbc/car/hyundai/carstate.py` `BUTTONS_DICT`, not assumed.

Enable on device (no rebuild): `echo 1 > /data/media/0/grt/SmartCruiseControlSetSpeed`.
Default OFF, and separate from `SmartCruiseControlMap` because this is the first fork feature
that can ACCELERATE the car on OSM data.

Instrumentation: `/data/media/0/grt/set_speed.log` — one line per DECISION **plus a 2 s
heartbeat naming the gate that rejected** (`mapd_not_alive`, `no_tiles`, `way_fail`,
`no_limit_posted`, `implausible_limit`, `settling`, `already_handled`, `driver_busy`,
`defer_up_on_predicted`), with the raw m/s limit, `waySelectionType` and the candidate counter.
Decision-only logging was not enough: if the feature never fires on the road the log would be
empty and the five possible causes indistinguishable — and `mapdOut` is still not in rlog on a
prebuilt branch, so there is no retrospective path. One failure mode to look for specifically:
`_read_limit` returning None resets the stability counter, so a `waySelectionType` flickering
between `current` and `fail` faster than 1 s means a limit never clears the gate at all.
Unlike the mapd controller, the OUTCOME here is retrospectively analysable: `carState.vCruise`
/ `vCruiseCluster` are in rlog.

Also registered `SmartCruiseControlSetSpeed` in `grt_params_keys.inc` for consistency with the
other two fork keys. It has no effect on this prebuilt branch (never compiled in) — the file
fallback is what actually works — but the pattern stays uniform.

AFTER DEPLOY, one free check: card is the 100 Hz realtime CAN loop and now carries an extra
subscriber. `carState.cumLagMs` IS in rlog — compare a post-change segment against a pre-change
one. plannerd's precedent does not cover this risk.

HOW OFTEN WILL IT ACTUALLY FIRE? Measured, not guessed. `ExperimentalMode=1` on the device, so
`V_CRUISE_INITIAL_EXPERIMENTAL_MODE = 105` is the set speed at engage — and from
`mapd_debug.log`, while moving the set speed was **105 km/h for 1,918 frames and 145 for 26**.
The driver essentially never changes it. With a ±20 band off 105 that adopts limits in
**[85, 125]**: 100 and 120 zones fire, 80 and 60 zones do not.

Drive 3 was urban (max 66 km/h, mean 35 while moving), so on the roads actually driven,
**part (a) alone will mostly log `ignore` and do nothing.** It is not inert in principle — the
band applies to the CURRENT set speed, so a route that steps down gradually ratchets:
105 → 100 (adopt) → 80 (adopt) → 60 (adopt). But a direct 105-to-60 transition is out of band.

Two consequences: (1) design the first road test around a 100 or 120 zone, or a route with
graduated limits, otherwise "nothing happened" is the expected result and proves nothing;
(2) part (b) is the MAJORITY case on this car's roads, not an edge case — which raises its
priority above where the original design put it. Widening the band is the alternative, but that
trades away exactly the confirmation step the >20 rule exists to provide.

WHAT IS DELIBERATELY NOT SHIPPED — part (b), the pending/confirm alert. The state machine and
its tests exist and pass, but `PENDING_ENABLED = False`. A pending limit the driver cannot
perceive is worse than not offering one, and the alert needs an event that this PREBUILT branch
can actually render. Adding an `EventName` enumerant is NOT the same claim as the in-place
struct renames we proved wire-neutral — those kept struct IDs and ordinals; a new enumerant is
a genuine schema addition on a device whose compiled artefacts are frozen. The discriminating
check, to run on the car before building anything: **can a Python-published `onroadEvent`
carrying an EventName the compiled side does not know about reach the UI and soundd?** If not,
the cheaper route is reusing an existing enumerant the Staria can never raise and overriding its
text in the Python `EVENTS` dict — zero schema change. Nothing about part (b) should be built
until that question is answered on device.

FOLLOW-UP FINDING (local, reduces that risk a lot): the alert the driver actually sees and hears
does NOT travel as an EventName. `selfdrived.publish_selfdriveState` puts
`AM.current_alert.alert_text_1` into `selfdriveState.alertText1` (plain Text) and
`audible_alert` into `selfdriveState.alertSound`, an **existing** `AudibleAlert` enum whose
`prompt` enumerant already maps to `warning.wav` in `ui/soundd.py`. The UI and soundd consume
`selfdriveState`, not `onroadEvents`. So the text is free-form and the sound can reuse an
existing enumerant — **no schema addition is needed for rendering**. An `EventName` is only the
dict key that selects the Alert inside selfdrived, and that whole path (events.py, selfdrived,
ui, soundd) is Python on this branch.

THE REAL REMAINING PROBLEM for part (b) is therefore not the alert — it is the CHANNEL. The
pending state lives in `card`; the alert must be raised in `selfdrived`, which does not
subscribe to `mapdOut` and must not (it gates engagement — worse blast radius than the
`longitudinalPlan` invalidation bug). Options, in rough order of preference:
  1. a fork-owned message on one of the 17 free `CustomReserved0..16` slots (rename in place,
     struct ID and ordinal unchanged — the pattern already proven wire-neutral for mapd),
     published by card, subscribed by selfdrived WITH ignore lists;
  2. move the whole pending state machine into selfdrived and have it drive card — worse, splits
     ownership of the set speed;
  3. a file in `/data/media/0/grt` written only on state edges — crude but zero schema risk.
Pick one deliberately; do not let it default.

Tests: 33/33 set_speed, 18/18 scc_map, 17/17 hooks, 20/20 schema conformance (now covering
`mapdOut.speedLimit`/`tileLoaded`/`waySelectionType` and `carState.buttonEvents`/`vCruise`/
`vCruiseCluster`). `cruise.py` untouched, so `test_cruise_speed.py` is unaffected; it needs the
full openpilot venv and was not run on the Pi5.

NOT YET ON THE CAR. Deploy + road test outstanding.

## FEATURE DESIGN (part a now implemented above): set speed tracks the speed limit

User asked for the comma 4 "MAX" and the Staria cluster to follow posted limits. Today we only
lower v_cruise INSIDE longitudinal_planner - a local planning variable that never reaches
carState.vCruise, so no display changes. Expected, not a bug.

FEASIBILITY - both displays are achievable (verified on device):
- CarParams.pcmCruise = False and openpilotLongitudinalControl = True on the Staria, so
  openpilot owns the set speed via VCruiseHelper._update_v_cruise_non_pcm (cruise.py).
- VCruiseHelper.v_cruise_kph / v_cruise_cluster_kph -> carState.vCruise / vCruiseCluster
  -> comma UI hud_renderer AND controlsd.py:166 hudControl.setSpeed
  -> hyundai/carcontroller.py:86 set_speed_in_units -> the CLUSTER. So the car's dash follows.

AGREED BEHAVIOUR:
- |new limit - current set speed| <= 20 km/h : adopt automatically, both up AND down.
- |difference| > 20 km/h : do NOT adopt. Show the new limit as PENDING for 10 s with an alert
  sound; adopt only if the driver taps RES/+ (ButtonType.accelCruise) within that window.

IMPLEMENTATION NOTES for whoever builds it:
- New fork module (e.g. openpilot/grt/set_speed.py) owning the pending state machine and the
  10 s timer. Upstream touch must stay ONE sentinel-wrapped line in cruise.py's non-pcm path.
- Button press: cruise.py already parses CS.buttonEvents for accelCruise/decelCruise - reuse
  that, do not add a second parser.
- Alert/sound: needs an event; check what is available without adding to the compiled alert
  tables (this is a PREBUILT branch - see the constraints note).
- SAFETY: this is the first change that lets the feature ACCELERATE the car on OSM data. It must
  respect V_CRUISE_MIN/MAX clamping, must not fight the driver's own button presses, and must be
  behind its own flag (/data/media/0/grt/SmartCruiseControlSetSpeed) default OFF.
- The speed-limit VALUE should come from mapdOut (current limit), not from our v_target, which
  is an approach profile and deliberately transient.

## 2026-08-20 — Exponential averaging on the hook contribution: MEASURED, NOT SHIPPED

Operator reported throttle hunting at 11:26, 11:52, 11:55, 11:59, 12:00, 12:01 and asked whether
exponential averaging would smooth the aggressive-mode hook transitions. Answer: **no.** Recorded
here so it is not re-litigated.

### What the drive actually contained
Replay of hooks 6+8 over all 13,201 planner frames, using the CORRECT internal `v_cruise` from
`cruise_log.py` (not `carState.vCruise`). Replay validated against the logged command:
**mean error +0.0033, sd 0.0310, |err|<0.05 on 94.9%** of e2e-source frames — so conclusions
below are about the drive, not about the harness.

- **Hook 6 armed 0 times.** Gate was armable on 47.3% of frames, but personality was `aggressive`
  for all 13,201 frames (no switch to detect) and only 5 strong-accel runs met `_T_STRONG` while
  otherwise armable, none of which completed a taper-to-quiet. Every bit of hook delta on this
  drive is hook 8.
- **Hook 8's transients are already clean.** 2 steps beat the 0.015/frame slew limit in 13,201
  frames: one at the 30 km/h floor, one at driver disengage. Both legitimate, neither in a
  reported window. There is no transient defect to fix.

### The EMA, open loop vs closed loop
Open-loop (filtering the *logged* delta) looked excellent: tau=0.3 pulled cmd/raw from 1.05-1.34
down to ~1.00-1.09 with the mean contribution preserved to 4 decimals. **That result was wrong**,
because filtering a logged signal does not put the filter's own lag inside the loop that produced
it. Hook 8 is a feedback controller carrying 0.70 s of lag reference + 1.0 s of smoothing on `u`
around GAIN 3.0; another 0.3 s inside that loop erodes phase margin.

Closed loop (plant `a_ego(t) = cmd(t-LAG) + d(t)`, EMA on the target with the rate limiter left
downstream as an unchanged backstop), 1-3 Hz of the resulting command, engaged contiguous runs
only — windows straddling an engagement edge were re-scoped, because band power over a step is
meaningless (`longActive False->True` at 11:26:12.300 is a +1.09 m/s^2 step and is ENGAGEMENT,
not a defect):

    win        no-hook   hook8   EMA0.2  EMA0.3  EMA0.5  EMA1.0   hook active
    11:26*      0.0334  0.0371   0.0374  0.0370  0.0370  0.0382       19%
    11:55       0.0202  0.0236   0.0213  0.0209  0.0205  0.0201       51%
    11:59       0.0117  0.0127   0.0134  0.0132  0.0128  0.0121       61%
    12:00       0.0109  0.0134   0.0125  0.0120  0.0115  0.0112       33%
    12:01       0.0129  0.0139   0.0144  0.0143  0.0142  0.0138       44%
    mean                0.0201   0.0198  0.0195  0.0192  0.0191
    mean corr           0.0347   0.0366  0.0367  0.0367  0.0368  (authority preserved)

The EMA helps at 11:55 and 12:00, **hurts at 11:59 and 12:01**, and **does nothing at 11:26** —
the only window above the ~0.03 m/s^2 perceptibility floor. Mean effect 0.0201 -> 0.0195 (3%).
The sign flips with how active the correction is: smoothing helps when the correction is large,
hurts when it is marginal and the servo is already chasing. Not shipped.

### Why no hook change can fix the reported symptom
- At 11:26 (the one perceptible window, 0.0371 vs a ~0.03 floor) the hook contributes +0.0037 of
  it. **Deleting hook 8 entirely leaves 0.0334 — still above threshold.** Decomposed on the
  35.2 s contiguous engaged span (705 frames, all `longActive`, 19-50 km/h):

      model raw                0.0175
      after min()              0.0334   <- arbitration alone, no hook: 1.91x the model
      as-driven command        0.0379

  So `min()` arbitration nearly DOUBLES the model's own roughness at low speed, and the hook
  adds the remainder. For contrast the same decomposition at 12:00 (85-100 km/h) gives
  0.0105 -> 0.0109 -> 0.0136, i.e. arbitration is 1.04x — benign. The low-speed case is the
  outlier. Note it is NOT switch chatter: only 2 source switches in 35 s (3/min), 58% of
  frames non-e2e.

  (An earlier draft of this entry cited "71% non-e2e" from the retired 00-18s window. That
  window was ~247/360 frames PRE-ENGAGEMENT with `acmd = 0`, and the classifier
  `'e2e' if acmd >= raw` mislabelled every one of them as cruise/other. Figure withdrawn;
  the numbers above are computed on engaged frames only.)
- 11:52 is `lead0` 79% with the hook active 3% — the MPC lead branch, untouchable from here.
- 11:59/12:00/12:01 sit at 0.0127-0.0139, at or below the perceptibility floor, with hook
  contributions of 0.0010-0.0025.

Corroborating: on 08-19 the operator reported hunting in **relaxed**, where hooks 6 and 8 are
gated off entirely (hook 7, the relaxed-only rising-edge jerk cap, was active). Together with
hook 6 arming 0x across this entire drive, that is two independent observations of hunting with
hooks 6 and 8 inert.

**Conclusion: the aggressive-mode hook section is clean. The residual hunting lives in `min()`
arbitration at low speed and in the lead/MPC branch.** Diagnosis only — no code changed.

`cruise_log.py` is STILL ENABLED on the device. The cruise branch is now the live suspect, so it
is worth keeping rather than removing; operator's call.

### 2026-08-20 addendum — the jolts are v_cruise STEPPING, and my 1-3 Hz metric was blind to them

Operator asked whether the hunting was `min()` flip-flopping between model and planner. Chasing
that question overturned two claims made higher up in this entry. Both are corrected here.

**Metric limitation, found by sanity-testing the estimator.** `band()` is length-independent and
leakage-free (a 2 Hz sine reads 0.0707 at every window length; a 0.05 Hz oscillation reads
0.0000). But **a single 0.5 m/s^2 step in a 35 s window reads 0.0260 in the 1-3 Hz band** — right
at the perceptibility floor. So a whole-window band figure cannot distinguish one jolt from
continuous hunting. Worse, the planner runs at 20 Hz, so a one-tick command step puts its energy
up to 10 Hz — mostly ABOVE the 1-3 Hz band. **The metric I used throughout systematically misses
exactly the events that feel like a jolt.**

**CORRECTION 1.** "min() arbitration nearly doubles the model's roughness at low speed (1.91x)"
is withdrawn as a statement about steady roughness. Measured WITHIN each contiguous block at
11:26, the command is smooth everywhere: `other` blocks 0.0079-0.0148, `e2e` block 0.0153 — all
well under the floor. The elevated 35 s figure came entirely from the 2 handovers.
**CORRECTION 2.** "NOT switch chatter: only 2 source switches in 35 s" had the sign of the
argument backwards. Those 2 switches ARE the event.

**MECHANISM (verified at frame level in two independent sources).** The internal `v_cruise`
changes in DISCRETE STEPS. `a_cruise = clip(v_cruise - v_ego, -1.2, 2.0)` has gain 1.0 and no
rate limit, so the step lands on the candidate at full size, `min()` takes it the same frame, and
hook 5 pins the magnitude at exactly `-COAST_DECEL = -0.500`:

    12:01:55.748  v_cruise 100.00 km/h  a_cruise +2.000 (saturated high)   cmd -0.056 (e2e)
    12:01:55.798  v_cruise  80.00 km/h  a_cruise -0.501                    cmd -0.500 (cruise)
                  ^ -5.556 m/s in ONE 50 ms frame; model raw was -0.052 throughout

    11:26:34.273  v_cruise  60.00 km/h  a_cruise +2.000                    cmd +0.209
    11:26:34.321  v_cruise  40.00 km/h  a_cruise -1.200 (saturated low)    cmd -0.500
                  ^ 100 Hz carControl confirms +0.209 -> -0.500, a 0.709 step in one frame

**READ THE RECORDER CORRECTLY:** `cl.record(...)` sits at `hooks.py:198`, BEFORE the softening
block at 202-209, so the CSV's `a_cruise` is the RAW, PRE-hook-5 value. At 11:26 raw was -1.200
and the command was -0.500, which means **hook 5 BOUND and softened it** — it did not skip. (An
earlier draft of this addendum claimed the opposite. Withdrawn.) At 12:01 raw was -0.501 vs a
-0.500 command, a 0.001 difference, so that frame cannot discriminate either way.

**WHY hook 5's skip-guard did not fire — a hook 3 / hook 5 interaction defect.** Hook 5's
docstring promises it is *"skipped entirely when the mapd controller lowered v_cruise this frame,
so the approach profile keeps full authority."* The guard is
`_v_cruise_lowered = target < v_cruise` (hooks.py:124) — a STRICT comparison against the INCOMING
driver-facing set speed. At 11:26:34.274 `carState.vCruise` AND `vCruiseCluster` both step
**60 -> 40 km/h**, i.e. **hook 3 (`track_set_speed`) moved the set speed itself** to follow the
posted limit; the internal value follows one frame later. So by the time hook 1 runs,
`target == v_cruise` (40 == 40), `target < v_cruise` is **False**, and hook 5 does not skip.

Consequence: approaching a 40 km/h posted limit at 49 km/h, the cruise branch is softened from
-1.200 to -0.500 and the car will overshoot the new limit. The stated safety property does not
hold whenever hook 3 has already moved the set speed — which is precisely the mapd-driven case
the guard exists to protect. **This is a real fork defect, independent of the jolt question.**

**MAGNITUDE — the earlier "gain 1.0, lands at full size" claim overstated by ~8x.** In both
traces `a_cruise` was SATURATED at +2.000 before the step, so the clip absorbs most of it. The
jolt is self-bounding: `(previous command) - (whatever floor the candidate lands on)`, i.e.
0.209 -> -0.500 = **0.709** at 11:26 and -0.056 -> -0.500 = **0.444** at 12:01 — not 5.556.
Note hook 5 REDUCED the 11:26 jolt (it would have been 1.409 to the -1.200 clip). Hook 5 is
mitigating here, not causing.

**RATE.** Over today's 109 min: 180 single-frame `v_cruise` steps >= 0.5 m/s (1.6/min), of which
**13 drove the cruise candidate to <= -0.5** — hard enough to win `min()` against a quiet model
and produce a braking jolt. ~1 every 8 min. The dominant step size (43 of 180) is 5 km/h, i.e.
the driver's own button presses, harmless. The 20 and 40 km/h steps are speed-limit transitions.

**Hooks 6 and 8 are not involved:** hook delta was 0.000 at both jolts, which REINFORCES the
entry above. Hook 5 IS in the path, but softening the magnitude. Scope the claim to 6/8.

**It is NOT flip-flopping.** At 12:00, 21 contiguous blocks with sub-second source switching
produced NOT ONE command step >= 0.05 in the whole minute — because two candidates that cross in
`min()` are equal at the crossing, so a handover is inherently continuous. A step requires an
INPUT to jump. The answer is "min() handing over once, hard, because its input stepped", not
oscillation between model and planner.

**COVERAGE — only 2 of the 6 reported windows are explained.** Cross-tabbing all 13 hard
down-steps against the operator's list:

    11:26  v_cruise 60->40   a_cruise raw -1.200   49 km/h   <- explained
    12:01  v_cruise 100->80  a_cruise raw -0.501   82 km/h   <- explained
    11:52, 11:55, 11:59, 12:00                               <- NO hard step; unexplained

11:52 is `lead0` 79% (MPC branch). 11:55 has 4 steps/min >= 0.05, largest 0.430, cause not yet
traced. 11:59 and 12:00 have ZERO command steps >= 0.05 in the entire minute and 1-3 Hz at or
below the perceptibility floor — nothing measured in the command explains what was felt there.
Do not let the two explained windows imply all six are solved.

**NOT a mapd defect.** mapd is correctly reporting a new posted limit; a speed-limit change is
inherently a step. What is missing is any rate limit between the set speed and `a_cruise`.
Diagnosis only, no code changed — per the operator's standing split, whether to open that front
is their call. The hook 3 / hook 5 guard defect above is separate and does not need the jolt
question resolved first.

`cruise_log.py` earned its keep here: none of the above is reconstructable without the internal
`v_cruise`. Note the file ACCUMULATES ACROSS DRIVES (spans 2026-06-05, 08-19, 08-20) — filter by
epoch, not time-of-day, or you will mix days.

### 2026-08-20 addendum 2 — redone against the LOGGED plan source; two retractions

Everything above that used a plan-source *proxy* (`'e2e' if a_cmd >= raw - 1e-3`) is superseded.
`longitudinalPlan.longitudinalPlanSource` is logged and is ground truth. The proxy is biased in
a way that matters here: hooks 6/8 RAISE the e2e candidate, so the cruise branch can win `min()`
at a value still ABOVE the model's raw output, and the proxy labels those frames `e2e`.

Re-extracted at the planner's own 20 Hz rate (`longitudinalPlan`, 13,200 frames). True source
distribution for the drive: **e2e 9972, cruise 1975, lead0 1032, lead1 221**.

**RETRACTION 1 — 11:52 is NOT the lead branch.** I reported "`lead0` 79%, hook active 3%" and
EXCLUDED 11:52 from the closed-loop EMA table on that basis. Ground truth: **e2e won 100% of the
engaged frames in that minute.** The exclusion was unjustified and the reason given was wrong.
11:52 is also the WORST window on the drive by discrete steps — see below.

**RETRACTION 2 — at 11:26 the command is SMOOTHER than the model, not rougher.** Ground truth
split is 51% e2e / 49% cruise, and 1-3 Hz is cmd 0.0259 vs raw 0.0268, a ratio of **0.97**. The
earlier "arbitration multiplies model roughness by 1.91x" came from the proxy plus a whole-span
band figure; both are withdrawn. (The 11:26:34 v_cruise-step jolt still stands — that was
verified against the recorder CSV and the 100 Hz `carControl` stream, not the proxy.)

**RESTRICTED TO THE FRAMES THE OPERATOR ASKED FOR** — e2e won `min()`, aggressive selected,
longitudinally engaged: 6,693 frames = 5.6 min = 50.7% of the drive. On these frames `aTarget`
IS the hook-raised e2e candidate. Replay validated directly against it:
`aTarget` vs `raw + hook delta` = mean **-0.0038**, sd **0.0286**, |err| < 0.02 on **92.6%**.

    1-3 Hz, frame-weighted over 14 contiguous runs (5.4 min):
      model raw                 0.0146
      as-driven aTarget         0.0170     ratio 1.17x
    hook active on 45.9% of these frames, mean delta +0.0979, max +0.300

    per window, e2e-won frames only:
      win     e2e   cruise  lead    cmd     raw   ratio  hook%
      11:26   51%    49%     0%   0.0259  0.0268  0.97    35%
      11:52  100%     0%     0%   0.0290  0.0276  1.05    30%
      11:55   86%     1%    12%   0.0166  0.0107  1.56    54%
      11:59   76%    24%     0%   0.0105  0.0104  1.00    65%
      12:00   78%    22%     0%   0.0148  0.0098  1.51    37%
      12:01   79%    21%     0%   0.0128  0.0128  1.00    46%

**THE DECISIVE ATTRIBUTION.** 56 command steps >= 0.05 occur INSIDE e2e-won runs (10.3/min).
Splitting each into its model component and its hook component:

    attributable to the MODEL (|d raw| >= |d hook|):  56  (100%)
    attributable to the HOOKS:                         0

The largest single-frame hook-delta change anywhere in those 56 is **0.0150** — exactly
`_FLOOR_JERK * DT_MDL`, i.e. the rate limiter is honoured to the last digit. The largest command
step is **-0.618 at 11:51:11.84**, entirely the model. The steps concentrate hard:
**11:52 holds 38 of the 56**, at 27-52 km/h, with e2e winning 100% of that minute.

**CONCLUSION, now on ground truth.** Over the half of the drive where the hook-raised e2e
candidate WAS the command, the hooks add 17% to 1-3 Hz roughness (0.0146 -> 0.0170, both under
the ~0.03 floor) and contribute **zero** discrete steps. Every jolt in those windows is the
model's own `desiredAcceleration` stepping. The aggressive hook section remains clean; the
lead for the felt hunting is now the MODEL OUTPUT at low-to-mid speed, with 11:52 the case to
examine. Diagnosis only, no code changed.

### 2026-08-20 addendum 3 — filtering output_a_target_e2e ITSELF (the right target)

Operator's point: the earlier EMA test filtered the HOOK DELTA, which the attribution above shows
contributes zero steps and is already rate-limited to 0.0150/frame. The thing producing the 56
jolts is `output_a_target_e2e` -- the model's own `desiredAcceleration` -- upstream of everything
previously filtered. Retested on that signal, over the 14 e2e-won/aggressive/engaged runs (5.4 min).

Of the 56 in-run steps >= 0.05: **29 rises, 27 falls** (incl. the -0.618). A rising-edge-only cap
structurally cannot touch the falls.

    variant                  steps>=.05  max step   1-3Hz   accel lost   brake delay
    as-driven                        56     0.618  0.0170       0.00        0.00
    B rising cap 1.0 m/s^3           50     0.618  0.0170       0.03        0.00
    B rising cap 1.5/2.0/3.0         56     0.618  0.0170       0.00        0.00
    E ema tau 0.2s                    7     0.097  0.0080       3.58        3.70
    E ema tau 0.3s                    3     0.062  0.0059       4.52        4.55
    E ema tau 0.5s                    0     0.039  0.0038       5.77        5.65
    S symmetric cap 1.5 m/s^3        66     0.075  0.0167       0.00        0.14
    S symmetric cap 3.0 m/s^3        60     0.150  0.0170       0.00        0.06

**HOOK 7's SHAPE DOES NOT WORK HERE (variant B).** At 1.5-3.0 m/s^3 it changes NOTHING: the cap
allows 0.075-0.150 per frame and the rises are mostly 0.05-0.09, so they pass untouched; and it
cannot address the 27 falls at all, including the -0.618. Hook 7 works in relaxed because it
targets sustained rising ramps; these are brief bidirectional steps.

**METRIC CAVEAT:** "steps >= 0.05" is misleading for a rate limiter -- S turns one 0.618 step into
~8 frames of 0.075 and the COUNT goes UP while the jolt gets 8x smaller. Read `max step` for the
rate-limiter rows.

**EMA works but is expensive.** tau 0.3 removes essentially every step (max 0.618 -> 0.062) but
withholds **4.55 m/s** of integrated braking and gives up 4.52 m/s of accel authority.

**Symmetric jerk cap at 1.5 m/s^3 is the better trade:** worst jolt 0.618 -> 0.075 (8x), brake
delay 0.14 m/s (32x less than EMA tau 0.3), zero accel authority lost. 1-3 Hz barely moves
(0.0170 -> 0.0167) -- correct and expected: a slew limit attacks large transients, not small
oscillation, and the jolts are what the operator feels.

That 0.14 is an UPPER BOUND: the cap raises the candidate on a fall, so e2e may stop winning
`min()` and a lower branch takes over, clawing braking back. Also note the wire already jerk-limits
at 3-5 m/s^3 (`hyundaicanfd.py`), so 1.5 planner-side is tighter than what CAN enforces.

Not yet implemented -- discussing with advisor before proposing.

### 2026-08-20 addendum 4 — CORRECTION to addendum 3, and the verdict on filtering the e2e candidate

**GAP ARTIFACT — addendum 3's table is wrong.** Runs were contiguous in INDEX, not TIME. The drive
has 3 missing segments (gaps of 1436 s, 120 s, 60 s), and one "run" straddled the 24-minute gap,
making the frame either side look like a single -0.618 step. Enforcing `T[n]-T[n-1] <= 0.2`:

  * steps >= 0.05 inside e2e-won runs: **55**, not 56;
  * **worst real step is +0.091**, not 0.618;
  * S@1.5's headline "0.618 -> 0.075, 8x" was entirely that artifact.

Same class of bug as the concatenated per-date CSVs. Index contiguity is not time contiguity.

**CORRECTED comparison** (14 time-contiguous e2e-won/aggressive/engaged runs, 5.4 min):

    variant                  steps>=.05  max step   1-3Hz   accel lost  brake delay
    as-driven                        55     0.091  0.0163      0.00        0.00
    B rising cap 1.0-3.0 m/s^3    49-55  0.089-0.091 0.0163   0.00        0.00
    S symmetric cap 1.5              56     0.075  0.0163      0.00        0.00
    S symmetric cap 3.0              55     0.091  0.0163      0.00        0.00
    E ema tau 0.2s                    2     0.055  0.0077      3.58        3.59
    E ema tau 0.3s                    0     0.049  0.0057      4.52        4.41
    E ema tau 0.5s                    0     0.039  0.0037      5.77        5.48

**1. Hook 7's shape does not transfer (variant B).** No effect at any jerk value. Two reasons, and
the first is a DESIGN CHOICE not a tuning miss: `max(prev, 0.0)` resets the allowance to zero
whenever the previous output was negative — hook 7's deliberate instant-brake-release feature —
so a large rise from below zero passes unbounded. Second, it structurally cannot touch the 27
falls (of 55 steps: 29 rises, 27 falls).

**2. A jerk cap has almost nothing to bite on.** The worst real step is 0.091 m/s^2 in one 50 ms
frame. The CAN layer already jerk-limits at 3-5 m/s^3 = 0.15-0.25 per 50 ms, so 0.091 does not
even clip at the wire. A 1.5 m/s^3 planner cap moves the worst step 0.091 -> 0.075.

**3. The EMA works on the signal but withholds real braking.** tau 0.3 removes every step and cuts
1-3 Hz 0.0163 -> 0.0057. Cost, measured per EVENT rather than as an integral (the integral's
4.5 m/s is a mean of only ~0.03 m/s^2 and hides the shape):

    tau 0.3: 309 shortfall events / 5.4 min; WORST 0.293 m/s^2 withheld for 1.75 s
             (= 0.513 m/s of speed); 54 events longer than 1.0 s
             includes 11:58:15.55 where the plan asked -1.086 and 0.167 was withheld for 1.35 s
    tau 0.5: WORST 0.397 m/s^2 for 1.80 s; 62 events > 1.0 s

These are UPPER BOUNDS: the EMA raises the candidate on a fall, so e2e may stop winning `min()`
and a lower branch claws the braking back. That cannot be quantified offline — the losing
candidates are not logged, only the winner.

**RECOMMENDATION: do not filter the e2e candidate.** There is no jolt to remove — the worst step
is 0.091 m/s^2, inside what CAN already passes. What is actually there is a sustained low-amplitude
modulation (+-0.05 to 0.09), and **38 of the 55 steps fall in the single 11:52 minute** at
27-52 km/h. The only filter that touches it buys ~0.01 m/s^2 of smoothness for up to 0.29 m/s^2 of
withheld braking, which violates the fork's own rule that a feature must never degrade the base
system. If this is pursued, the lever is upstream — understand what the model is doing at 11:52 —
not a global filter on the candidate.

Standing caveat: `aEgo` 1-3 Hz is ~0.10 m/s^2 flat across personality, speed and command level,
and the IMU shows 3.5 m/s^2 p2p broadband. The felt hunting may not be in the command at all.

### 2026-08-20 addendum 5 — THE METRIC WAS WRONG. The hooks DO add hunting, and an EMA fixes it.

**Retract addendum 4's recommendation and the "the hook section is clean" conclusion.** Both
rested on two metrics that are structurally blind to hunting:

  * **single-frame steps >= 0.05** — the hooks are rate-limited to 0.0150/frame so they CANNOT
    produce a step. Measuring steps guarantees the hooks score zero. Worse, the threshold
    (1.0 m/s^3) is below ordinary driving jerk, so it counted a normal pull-away ramp at 11:52
    as 38 "jolts". Sign-alternation there was only 30% — monotonic ramps, not oscillation.
  * **1-3 Hz band power** — already shown to read one step as 0.026 and to miss 20 Hz planner
    content above 3 Hz.

Hunting is direction REVERSAL. A rate-limited signal that ramps up then down adds reversals while
never stepping. That is precisely what hook 8 does.

(The first reversal counter returned 0 for every input — `direc` never left 0 because the extremum
was updated before the test. Rewritten and sanity-tested against square waves, ramps and
sub-ruler dither before use.)

**MEASURED, e2e-won / aggressive / engaged, 5.4 min, fixed amplitude ruler:**

    ruler        cmd rev/min   model rev/min   hooks add
    0.02 m/s^2          94.1            92.5        +1.7
    0.05                52.6            41.7       +10.9
    0.10                31.9            19.1       +12.8   <- hooks nearly DOUBLE it

Per window at ruler 0.05 (cmd vs model): 11:52 **99.0 / 77.4**, 11:55 57.4/46.9,
11:26 56.9/42.7, 11:59 44.5/34.1, 12:01 44.0/36.9, 12:00 38.2/30.3. **Every window.**

**THIS ANSWERS "why is hunting worst in aggressive"** — asked 2026-08-19, never properly answered.
The first draft of this claim compared aggressive's command against the MODEL on aggressive
frames, which is not a personality comparison at all. Measured PROPERLY on the 08-19 drive, where
the same loop was driven twice, one personality each, e2e-won engaged runs:

    personality    minutes   rev/min @0.05   @0.10   model @0.10   mean v
    relaxed            3.0            36.3    10.2          9.8      83 km/h
    aggressive        16.4            48.4    26.9         15.6      81 km/h

**In relaxed the command tracks the model almost exactly (10.2 vs 9.8, +0.4). In aggressive the
command has +11.3 more reversals than the model.** At matched mean speed (83 vs 81 km/h), the
driver feels 2.6x the reversal rate in aggressive. That is the hooks, measured directly rather
than inferred.

**THE FIX — EMA on hook 8's correction target, closed loop** (plant `a_ego = cmd(t-LAG) + d`,
rate limiter left downstream as an unchanged backstop, `reset()` zeroing the EMA state):

    variant              rev/min @0.05  @0.10   mean corr   km/h vs no-hook   peak corr
    model only (floor)          41.7     19.1      —              —              —
    as-driven                   52.6     31.9      —              —              —
    replay, no EMA              51.5     32.2    0.0437         48.13          0.300
    EMA tau 0.3s                50.2     30.0    0.0462         51.16          0.300
    EMA tau 0.5s                49.5     26.9    0.0464         51.30          0.300
    EMA tau 1.0s                45.4     22.1    0.0455         50.19          0.295
    EMA tau 2.0s                45.8     19.6    0.0432         47.74          0.264

**tau 1.0 s removes 77% of the added hunting (+13.1 -> +3.0 rev/min at ruler 0.10) at NO MEASURED
COST to speed holding**, with peak correction unchanged (0.300 -> 0.295). tau 2.0 removes 96% but
starts costing speed. The authority is carried by the correction's MEAN, which the EMA preserves;
what it removes is the wander. `corr>0` rises 44% -> 83%: a smaller correction applied more
continuously instead of ramping to full and back.

**DO NOT claim the speed holding IMPROVES.** An earlier draft did, from the 48.13 -> 50.19 km/h
summed figure. Per-run breakdown kills that reading: the +2.06 total is **61% one run** (+1.26 of
it), and **6 of the 14 runs are WORSE** with tau 1.0. The defensible claim is that speed holding
is UNCHANGED within run-to-run scatter — which is enough, because the reversal reduction is the
point and it costs nothing.

Also note a low-pass filter reduces a zigzag reversal count almost by construction, so the
reversal column alone is not proof the fix works — it is the *paired* result (reversals down,
speed holding flat, peak correction intact) that carries it.

The earlier EMA test (addendum "closed loop", 3% effect) is not contradicted — it was scored with
1-3 Hz band power, which this signal barely moves. Same filter, right metric, opposite verdict.

**CAVEATS.** (1) Hook 6 armed 0x on this drive, so this is a hook 8 result; hook 6's own
contribution is untested. (2) The plant replays the LOGGED disturbance — the real model would see
different motion and respond differently. (3) The sim passed placeholder throttle_prob=1.0 /
curvature=0.0 to hook 6, harmless only because it never armed.

Proposal, not implemented. comma4 offline. Needs operator go-ahead per the diagnosis/implementation
split.

### 2026-08-20 — IMPLEMENTED: hook 8 EMA + hook 9 (aggressive candidate rising-edge cap)

Both changes written, tested, verified closed-loop against the SHIPPED code (not a simulation
subclass). Not yet deployed.

**hook 8 — `openpilot/grt/hold_speed.py`**
  * `_HS_TAU = 1.00` s, `_HS_ALPHA = DT_MDL / (_HS_TAU + DT_MDL)`, `self.tgt_f` state.
  * new `_smooth()` EMAs the correction TARGET; `_ramp()` unchanged downstream as the slope
    backstop. Order is EMA -> rate limiter: the first bounds wander, the second bounds slope.
  * `_smooth()` is called on EVERY live frame including the no-headroom path
    (`self._ramp(self._smooth(0.0))`), or the filter state goes stale and re-entry steps.
  * `reset()` zeroes `tgt_f`, so the hard-release path still releases hard.

**hook 9 — `openpilot/grt/accel_ramp.py`, `AggressiveCandidateRamp`, `JERK_AGGRESSIVE = 1.0`**
  * mirrors hook 7's shape but applies to the e2e CANDIDATE (before `min()`), not the final
    command (after). Wired at the end of `floor_e2e_accel()` so it shapes the candidate after
    hooks 6/8 raise it, gated on `aggressive and long_pid and not driver_input`.
  * can only LOWER the candidate => the planner can only become more conservative. Falls pass
    through in the same frame. Ramp restarts from `max(prev, 0.0)` so brake release is instant.

**VERIFIED, closed loop, shipped code, 5.4 min of e2e-won/aggressive frames:**

    build                            @0.05   @0.10    km/h    peak   mean corr
    BEFORE (no EMA, no hook 9)        51.5    31.9   48.13   0.300     0.0431
    EMA only                          45.0    22.1   50.19   0.295     0.0450
    SHIPPED (EMA + hook 9)            44.3    21.7   50.11   0.295     0.0449
    (model's own floor @0.10 = 19.1)

**Added hunting over the model: +12.8 -> +2.6 rev/min, 80% removed.** Peak correction
0.300 -> 0.295, mean correction preserved (0.0431 -> 0.0449).

The km/h column is NOT an improvement claim — 6 of 14 runs are worse and 61% of the delta is one
run. It is there to show the smoothing costs nothing.

**TESTS** — `openpilot/grt/tests/test_hunting_fix.py`, 21 checks, including the reversal counter's
own sanity tests (square wave / ramp / sub-ruler dither) before anything depends on it. Full GRT
suite: e2e_floor 26, accel_ramp 14, hold_speed 24, cruise_log 9, hunting_fix 21 = **94 PASS**.

`test_hold_speed.py` needed its settle window widened: it was sized for the rate limiter alone
(20 frames) and the EMA needs ~8 tau (160). Now DERIVED from `_HS_TAU` rather than hardcoded, and
steady-state comparisons use a 1e-3 tolerance because an EMA approaches its target asymptotically
(measured residual 1.7e-5 after 10 s, 5.8e-14 after 30 s).

CAVEATS carried forward: hook 6 armed 0x on the 08-20 drive so this is validated as a hook 8
result; the plant replays the logged disturbance; a low-pass filter reduces a zigzag count almost
by construction, so it is the PAIRED result (reversals down, speed flat, peak intact) that carries
this, not the reversal column alone.

**Why JERK_AGGRESSIVE (1.0) < JERK_RELAXED (1.5)** — asked 2026-08-20, looks backwards, is not.
The caps see different signals: hook 7 caps the FINAL command (still carrying the model's raw
steps, max 1.634 m/s^2 per tick); hook 9 caps the e2e CANDIDATE, already through hook 8's EMA and
0.30 m/s^3 rate limiter. Measured on each personality's own frames: hook 7 @1.5 binds 0.5% with a
worst shortfall of 1.22 m/s^2 (7.9 h fleet); hook 9 @1.0 binds 0.49% with a worst shortfall of
0.068 — same frequency, ~18x gentler consequence. Aggressive IS the less restricted personality
in effect. The appearance cannot be fixed by raising the constant: J=1.5 binds 0.03%, J=2.0 binds
0.00%, and hook 9's whole benefit (22.1 -> 21.7 rev/min) lives in that 0.49%. Documented in
accel_ramp.py so it is not "tidied" later.

---

## CURRENT STATE — longitudinal comfort (as of 2026-08-20, end of session)

This log is long and contains several RETRACTIONS from the same day. Read this block before
acting on anything above it.

### On the car (comma4, commit `59fbcf3a8`, deployed and rebooted 2026-08-20)

| hook | file | personality | what it does |
|---|---|---|---|
| 5 | `hooks.py` | all | cruise braking floor `-COAST_DECEL` (0.5) for plain overspeed |
| 6 | `e2e_floor.py` | aggressive | offers back unused headroom below the set speed |
| 7 | `accel_ramp.py` | **relaxed** | rising-edge jerk cap 1.5 m/s^3 on the FINAL command |
| 8 | `hold_speed.py` | aggressive | under-delivery servo + **EMA tau 1.0 s** (new today) |
| 9 | `accel_ramp.py` | aggressive | rising-edge jerk cap 1.0 m/s^3 on the CANDIDATE (new today) |

94 tests pass on device. 0 grt hook failures since boot.

### SOLVED and operator-confirmed
* speed-up ramp (hook 7) and speed droop (hook 8) — "both solved", 2026-08-19.
* aggressive-mode hunting — hooks were adding +12.8 reversals/min over the model at a 0.10
  ruler; now +2.6, **80% removed**, peak correction and speed holding unchanged. Deployed
  2026-08-20, awaiting the operator's 2-day road test.

### OPEN
1. **Set-speed stepping.** Internal `v_cruise` jumps in one frame (100->80, 60->40 km/h),
   `a_cruise = v_cruise - v_ego` has no rate limit, so `min()` takes a ~0.7 m/s^2 step
   straight to the command. 13 hard events in the 109-min 08-20 drive, ~1 per 8 min.
   NOT a mapd defect — a limit change is inherently a step; what is missing is a rate limit
   between the set speed and `a_cruise`. Diagnosis only, no fix attempted.
2. **hook 3 / hook 5 guard defect.** `_v_cruise_lowered = target < v_cruise` reads False once
   hook 3 has moved the set speed itself down to the posted limit, so hook 5 softens a
   mapd-driven approach that its own docstring promises to skip. Car decelerates at 0.5
   instead of 1.2 toward a lower limit and will overshoot.
3. **Hook 6 is effectively untested in the field.** It armed **0 times** across the entire
   08-20 drive (the operator never left aggressive, so its personality-edge trigger had no
   edge; only 5 strong runs qualified and none completed a taper). Today's hunting result is
   a HOOK 8 result. Hook 6's own contribution is unmeasured.
4. **`cruise_log.py` is still recording** (temporary, 50 MB cap). Keep until (1) is closed —
   the internal `v_cruise` reaches no logged message, so it is the only instrument for it.
5. 11:59 / 12:00 on 08-20: operator reported hunting, but zero command steps >= 0.05 and
   1-3 Hz at or below the perceptibility floor. **No mechanism identified.** Note `aEgo`
   1-3 Hz is ~0.10 m/s^2 flat across personality/speed/command and the IMU shows 3.5 m/s^2
   p2p broadband — some of what is felt may not be in the command at all.

### METHOD — do not repeat these
* Measure hunting as **REVERSAL rate** with a fixed amplitude ruler. Step-counting is blind to
  it (the hooks are rate-limited, so they score zero by construction) and 1-3 Hz band power
  reads one step as 0.026 while missing 20 Hz planner content above 3 Hz. Both metrics led to
  a wrong "the hook section is clean" verdict on this same day.
* Sanity-test any new metric on synthetic input BEFORE trusting it.
* Enforce **time** contiguity, never index contiguity — a run straddling a 24-min gap invented
  a -0.618 m/s^2 step that drove a whole wrong recommendation.
* Use logged `longitudinalPlan.longitudinalPlanSource`, never an `a_cmd >= raw` proxy.
* Test anything touching hook 8 **closed loop** — it feeds back on `aEgo` with gain 3.

---

## 2026-08-25 — hook 10 (throttle hold) + seam edits in hooks 6 and 8. NOT DEPLOYED.

Implements `grok_fix_stutter.md` (2026-08-24). Diagnosis is that file's; this entry records
what was built, what deviated, and what is not covered.

### The two 2026-08-22 symptoms

**Distinct throttle off-on.** On this car `aReq ~ 0` is the SCC throttle deadband, so a command
crossing zero is throttle OFF then ON. It is a SIGN CHANGE, not a step — the worst single-frame
step at 14:35 was 0.058 m/s^2 and CAN already clips jerk at 5 m/s^3. At 14:35-14:36 (110 vs 110,
no lead) cruise is `clip(v_cruise - v_ego)`, which at equality is 0, so a +-0.4 km/h ripple went
slightly negative, beat e2e's +0.07 in the `min()`, and dumped the throttle: **65 zero-crossings
/min, 59 source flips, worst 8 s at 90/min**. Also 15:28 in RELAXED, uphill, 100% e2e — the model
dipped through zero and hook 7 only rate-limits RISES, so the cut was instant.

**Slow ~4.5-5.5 s speed hunt, mostly uphill.** Not the fast hook-8 zigzag (already EMA'd). At the
set speed `a_cruise = 0`, so `min(raised_e2e, 0) = 0` and hooks 6/8 were STRUCTURALLY VETOED.
16:40 is the clean picture: 60.0 -> 50.9 -> 60.0 against a 60 set. At 17:34-17:40 the hooks
raised on 7% of frames while cruise won 77% of the `min()`.

### What was built

**Hook 10 — `openpilot/grt/throttle_hold.py` (new, all personalities, no feature param)**
  * **layer B** `deadband_cruise_accel()` on the CRUISE candidate, after hook 5, before `min()`:
    at or below the set speed return `ACCEL_MAX` so cruise cannot veto a raised e2e candidate;
    above it, the ordinary P-term, plus a tiny-negative clip (`-BAND < a < 0 -> 0`) so a ripple
    just over set does not click the SCC. `forceDecel` (v_cruise ~ 0) is untouched.
  * **layer A** sign debounce on the FINAL command, after `min()` and hook 7. Holds the
    PRE-GLITCH command (not 0 — clipping to 0 IS the deadband) through a sign flip shorter than
    T_HOLD (0.30 s), and never emits 0 while holding throttle on (floor EPSILON = 0.04).
  * **layer C** backstop: with >= 5 km/h unused headroom and a request milder than ABANDON,
    refuse to cut throttle. Adds no acceleration; mainly the relaxed 15:28 path.
  * In every layer a request at or beyond **ABANDON (-0.20)** passes through unfiltered on the
    same 50 ms tick. There is no time constant anywhere in the file.

**Hook 8 — `hold_speed.py`, two seams**
  * zero-cap `a_e2e < 0.0` -> `a_e2e < -0.20`. The old threshold made the servo a no-op on the
    one case it exists for: on a grade the model sits at -0.02..+0.02 and the car bleeds speed.
  * handoff no longer at 1 km/h of headroom ("cruise owns the set speed") but only when
    `v_ego > v_cruise`. Cruise cannot hold a speed on a hill; the 1 km/h handoff IS the pump.

**Hook 6 — `e2e_floor.py`, abandon fade.** `_THROTTLE_FALL_JERK = 1.5 m/s^3` runs the THROTTLE
portion of the floor down instead of assigning `self.floor = a_e2e` in one frame (15:22:44 was
+0.50 -> -0.175 in 0.01 s, -62 m/s^3 planner-side). Once the floor is at or below zero the
request is taken on the SAME frame. Hook 6 still raises at raw ~ 0 — that is purpose (2).

### DEVIATIONS AND LIMITS — read before tuning

1. **The abandon fade is cut short by hook 6's own `_ABANDON_T`.** 0.50 at 1.5 m/s^3 needs
   0.33 s, but the latch-out releases at 0.30 s (6 frames). Measured: the floor fades
   +0.500 -> 0.425 -> 0.350 -> 0.275 -> 0.200 -> 0.125, then the release hands over at -0.25.
   The snap is roughly **halved (0.75 -> 0.375), not eliminated**. Closing it needs
   `_THROTTLE_FALL_JERK` ~ 1.67, and the spec fixes 1.5 and forbids retuning `_ABANDON_T`.
2. **A large SUDDEN drop never reaches the fade at all** — `_ABANDON_DROP` (0.35 over 0.5 s)
   releases the hook outright first, returning the request untouched. Correct, and asserted.
3. **Layer B removes the cruise APPROACH TAPER.** Below set, the stock candidate is +0.28 m/s^2
   at 1 km/h below, and that P-term is what eases the car in. Returning `ACCEL_MAX` deletes it,
   so the approach is now shaped only by e2e + hooks 6/8, and the frame `v_ego` crosses
   `v_cruise` the branch snaps to its negative P-term. **Overshoot-then-snap at the set speed is
   a plausible NEW oscillation and none of the five replay gates covers it** — all five test the
   droop side. If the car starts hunting AT the set speed rather than below it, look here first.
4. **Layer A's EPSILON hold has no timeout.** While `last_sign` is positive the command never
   goes to 0; it goes to 0.04. That ends only when the model asks below zero for 0.30 s, or when
   `v_ego` crosses `v_cruise`. Sustained EPSILON is ~8.6 km/h per minute, so in RELAXED (no
   hooks 6/8) layers A+C become the mechanism that walks the car to the set speed — behaviour
   relaxed did not have before.
5. **Spec §7's grep order lists hook 2 after the `min()`.** That is a slip: hook 2 APPENDS a
   candidate, so it must precede `min()`. Hook 2 was not moved. Actual order is
   hook 1 -> get_cruise_accel -> hook 5 -> **B** -> 6/8/9 -> hook 2 -> min -> hook 7 -> **A+C**.
6. **Spec §5's layer-A prose says "positive through frame 6, negative from frame 7"; its own
   pseudocode gives accept-at-frame-6** (`pending_t` reaches 0.30 on the 6th increment and
   `0.30 < 0.30` is false). The pseudocode was followed — it holds 0.25 s, not 0.30 s, which is
   the more conservative of the two readings.

### Tests

`test_throttle_hold.py` is new (32 checks). `test_hold_speed.py` (29) and `test_accel_ramp.py`
(22) were CHANGED, not weakened: the old "output capped at 0 while the model asks to slow" and
"inert at the set speed" assertions encoded the exact behaviour being removed. Full suite:
throttle_hold 32, hold_speed 29, accel_ramp 22, hunting_fix 21, e2e_floor 26, cruise_log 9 =
**139 PASS**.

One trap worth recording: the first version of the abandon-fade test armed hook 6 by stepping
`a_e2e` 0.60 -> 0.02, which trips `_ABANDON_DROP` and releases the hook, so the floor was 0.000
and every fade assertion passed **vacuously**. It now arms via the personality edge with a steady
mild request and ASSERTS `floor > 0` as a precondition. Same failure mode as "hook 6 armed 0x"
on 2026-08-20 — a hook-6 test can look green while the hook never ran.

### Replay gates — NOT yet run against rlogs

1. 16:40: the 60 -> 51 -> 60 pump must not be `min(..., cruise=0)` at 60.
2. 17:34: cruise must not sit at 0 while e2e+corr is positive and `v_ego <= v_cruise`.
3. 15:22:44: no 1-frame +0.50 -> -0.18; throttle fade then brake.
4. 14:35:50 and 15:28:39: no sign flip out.
5. A step to -0.5 still appears on the SAME 50 ms tick once floor/hold is already <= 0.

Gates 3, 4 and 5 are covered by unit tests. Gates 1 and 2 need an rlog replay against the
2026-08-22 drive and have NOT been run. **NOT DEPLOYED — no device change from this work.**

### 2026-08-25 — replay gates 1 and 2 RUN against the 2026-08-22 logs. Both PASS.

Script `analysis/gates.py`. OLD chain = hooks 6/8/9 loaded from git at HEAD (not re-implemented);
NEW chain = working tree (hook 10 A/B/C + the 6/8 seams). Eight operator-named windows.

**A RECORDER FAULT HAD TO BE FIXED FIRST.** `cruise_log.py` hit its 50 MB cap and LATCHED OFF at
**16:28:51** on this drive. Every row after that is frozen at `v_cruise 110.0 / a_cruise +0.593`
— which is exactly windows (e) through (h). Using it there put the reconstruction 0.09-0.13 m/s^2
above the logged command and claimed a 110 set speed at 16:40 where the car was actually on 60.
The set speed now comes from `carState.vCruise` and the candidate is rebuilt as
`clip(v_cruise - v_ego, -1.2, 2.0)`. That reproduces the logged command exactly.

**VALIDATION of the OLD reconstruction against the command the car actually sent** (this is what
makes the rest meaningful): sd 0.0009 / 0.0033 / 0.0046 / 0.0048 / 0.0405 / 0.0043 / 0.0034 /
0.0027 across (a)-(h); |err| < 0.05 on **100%** of frames in seven windows and 95% at 16:40 (the
residual there is hook 1 lowering v_cruise on that hill, which a dash-based set speed cannot see).

    window                          n  cruise won  VETO   @~0   zc/min old   new   B only   A+C
    (a) 14:35-36 stutter   109-110/110      584    441   350           69     18       70    18
    (b) 14:45-46 uphill    105-110/110      209    131   123           54     17       40    17
    (c) 15:08-09 hunting    96-105/110        0      0     0           44      6       34     6
    (d) 15:22-23 hesitant   66-102/110        0      0     0           47      7       45     7
    (e) 16:40-41 uphill      51-78/ 80      515    452   136           40      8       40     8
    (f) 16:58-59 flat      106-108/110        0      0     0           50     12       46    12
    (g) 17:13-15 uphill     93-108/110        0      0     0           39      3       26     3
    (h) 17:34-40 uphill    104-111/110     5347   4158  2773           58     17       57    17
    TOTAL                                  6655   5182  3382

**GATE 1 (16:40) PASSES.** Cruise won 515 of 1200 engaged frames; **452 of those were vetoing a
POSITIVE raised-e2e candidate while the car was at or below its set speed**, 136 of them with the
cruise candidate inside +-0.05 of zero. Layer B releases all of them.

**GATE 2 (17:34-17:40) PASSES.** Cruise won 5347 of 7200 frames; **4158 vetoes, of which 2773 had
cruise at ~0** — i.e. 2.3 minutes of a 6-minute window where cruise sat at zero while the e2e
candidate was positive and the car was at or below 110. Layer B releases all of them.

Across all eight windows the veto ran on **5182 frames, 30.8% of engaged time**. That is the
structural defect hooks 6 and 8 were being defeated by, measured.

**Zero-crossings of the command fall in every window**, 39-92%: (a) 69->18, (b) 54->17,
(c) 44->6, (d) 47->7, (e) 40->8, (f) 50->12, (g) 39->3, (h) 58->17.

**ATTRIBUTION — the two layers do different jobs, and the columns prove it.** `A+C` alone
reproduces the full new chain's zero-crossing count in EVERY window (18/17/6/7/8/12/3/17).
`B only` barely moves it (70/40/34/45/40/46/26/57) and at 14:35 is marginally WORSE than the old
chain (70 vs 69). So **layer A does essentially all of the anti-chatter work; layer B does none of
it** — B's entire value is authority, i.e. the veto column, not smoothness. That 70-vs-69 is the
first empirical hint of the documented UNTESTED DIRECTION: releasing cruise hands the approach to
e2e, and if e2e wanders across zero there are marginally MORE crossings until A cleans up.

**STILL NOT COVERED.** These gates test the droop/veto side only. Overshoot-then-snap at the set
speed — the consequence of B removing the cruise approach taper — is not measurable from a replay
of logs recorded WITHOUT the change, because the speed trajectory itself would differ. It needs a
road test. Watch for hunting AT the set speed rather than below it.

Open loop throughout: layer A is applied to the reconstructed command, and the plant would respond
differently. This measures whether the sign flips are removed, not the resulting speed.
NOT DEPLOYED.

### 2026-08-25 — `cruise_log.py` REMOVED

The temporary recorder wired into hook 5 on 2026-08-19 is gone: module, singleton, the
`record()` call, its 9 tests, and the GRT_MODS entry.

It earned its keep — the internal `v_cruise` reaches no logged message, and without it neither
the 08-20 set-speed stepping (100->80 and 60->40 km/h in a single frame) nor the hook 3 / hook 5
guard defect would have been findable. But it **filled its 50 MB cap and latched off mid-drive on
08-22 at 16:28:51**, and it did so SILENTLY: every subsequent row is frozen at
`v_cruise 110.0 / a_cruise +0.593`. That stale data made the first run of the hook 10 replay
gates claim a 110 set speed on a road where the car was on 60, and put the reconstruction
0.09-0.13 m/s^2 above the command the car actually sent. It was only caught because the
reconstruction was validated against `carControl.actuators.accel` — a validation step that exists
precisely so a bad input cannot pass as a result.

**Lesson recorded in the code:** a diagnostic that can go stale without saying so is worse than
no diagnostic. If the internal `v_cruise` is needed again, publish it on `longitudinalPlan`
(the `aCruise`/`vCruise` ordinals already exist in log.capnp but sit in the `deprecated` group
and are never assigned) rather than writing a CSV with a silent cap.

The replay path no longer needs it: the set speed comes from `carState.vCruise` and the cruise
candidate is rebuilt as `clip(v_cruise - v_ego, -1.2, 2.0)`, which reproduced the logged command
with sd <= 0.0405 and |err| < 0.05 on 100% of frames in seven of eight windows.

Suite is now 130 (was 139 with cruise_log's 9). The 52.4 MB `cruise_log.csv` still sits on the
device at `/data/media/0/grt/` — dead weight on a volume that is 90% full. NOT deleted from the
device by this commit; nothing was deployed.

### 2026-08-25 (evening) — layer B's set-speed RELEASE reverted. It failed exactly as warned.

Operator drove `2fe8ee07d` and reported hunting AT the set speed — the one direction the replay
gates could not cover, and the one flagged in `throttle_hold.py` as UNTESTED. Confirmed, root-
caused, reverted. Data: `/home/pi5-ubuntu/drives/2026-08-25/`, 7 routes, 08:35-13:16, 106 segs.

**MEASURED**, engaged frames within 2 km/h of the set speed, 08-22 route 118 (before) vs 08-25:

    metric                          before   with release
    zero-crossings/min                82.1       25.0     <- layer A, KEPT, works
    reversals/min at a 0.10 ruler     18.9       22.7     <- WORSE
    share of time ABOVE the set        23%        54%     <- WORSE

Layer A is vindicated: the SCC chatter that started all of this is down 70%. Layer B is not.

**MECHANISM** — a bang-bang limit cycle across the set speed, 12:33:54 at a 110 set:

    109.6 (-0.43 below)  aTgt +0.238  src e2e     still accelerating 0.4 km/h from target
    110.3 (+0.33 above)  aTgt +0.241  src cruise
    110.6 (+0.57 above)  aTgt -0.157  src cruise  snaps negative, model still wants +0.245

Aggregated: just BELOW set, 9003 frames, e2e wins 95%, mean command **+0.064**. Just ABOVE set,
10519 frames, cruise wins 96%, mean **-0.008**. Accelerate, cross, brake, cross back: ~4-5 s,
~1 km/h. The cause is that layer B was a STEP at `v_ego == v_cruise` — below it the cruise
branch contributed nothing at all, above it the full P-term. Nothing eased the car in.

**AND IT WAS NEVER NEEDED.** `a_cruise` IS the headroom in m/s, so cruise can only out-bid a
hook candidate of ~0.3-0.8 while headroom is under ~3 km/h. Of the 5182 frames on 2026-08-22
where cruise won the min() at or below set: **5159 were within 1 km/h of the set speed, 23 were
1-3 km/h, and NOT ONE had more than 3 km/h of headroom.** Inside that last km/h the car is at
its target and cruise easing off is correct behaviour, not a veto to defeat. Gate 2 (17:34) was
measuring cruise doing its job — the car was 0.2 km/h below a 110 set, oscillating 0.2 km/h.

The genuine droop, 16:40 (51 km/h against a 60 set), had **9 km/h of headroom and cruise
saturated at ACCEL_MAX** — cruise was never what held it down. That case is fixed by hook 8's
zero-cap seam, which is independent of this layer and is unchanged.

**THE FIX.** Layer B keeps only the SCC ripple clip; the set-speed release is gone:

    5 km/h below set   a_cruise +1.390 -> +1.390     taper intact
    1 km/h below set   a_cruise +0.280 -> +0.280     taper intact
    exactly at set     a_cruise +0.000 -> +0.000     cruise owns the set speed again
    0.2 km/h over set  a_cruise -0.050 -> +0.000     ripple clipped, SCC not clicked
    genuinely over set a_cruise -0.280 -> -0.280     untouched

Its worst-case authority is now 0.08 m/s^2 of withheld braking, only while within a few tenths
of a km/h over the set speed. Layers A and C, and the hook 6/8 seams, are UNCHANGED — they are
what actually earned their place.

**LESSON.** The flag was right and the gates were structurally incapable of catching it: you
cannot measure a trajectory change by replaying logs recorded without it. When a change alters
where the car sits relative to a control boundary, the replay can only test the side it already
saw. Say so explicitly, and treat the first drive as the experiment.

Suite 128 (30 in test_throttle_hold, down 2 with the release's assertions replaced by
REGRESSION tests that the taper survives). NOT YET DEPLOYED.

### 2026-08-25 — 10:49:44 lead approach: why the car did not slow. Hooks are NOT the cause,
### but the investigation found a real gap in hook 8.

Operator report: at 10:49-10:50 the car did not slow coming up to a slow lead. Route
`00000128` (10:37-11:00), locally `2026-08-25/dNNN.zst`. Device was on `2fe8ee07d` at the time
— hook 10 WITH layer B's set-speed release, which was not reverted until 11:32.

**WHAT HAPPENED** (aggressive personality throughout, 110 km/h, set 110):

    10:49:44   lead appears  dRel 115 m  vRel -0.61     cmd  +0.000   src cruise
    10:49:47                 dRel 104 m  vRel -2.39     cmd  -0.014   src e2e
    10:49:50                 dRel  77 m  vRel -2.29     cmd  -0.073   src e2e
    10:49:51                 dRel  62 m  vRel -1.63     cmd  -0.031   src lead0   <- MPC takes over
    10:49:52                 dRel  50 m  vRel -3.42     cmd  -0.679   DRIVER BRAKES
    10:49:55                 dRel  30 m  vRel -6.05     cmd  -3.278

The lead branch did not become the `min()` until **62 m**, about a 2 s headway at 30.6 m/s. For
the six seconds before that, from 115 m down to 77 m, the command was ~0. Then the lead braked
hard — vRel went -1.6 -> -3.4 -> -4.8 -> -6.05 in four seconds — and the driver intervened one
second after the MPC engaged. Closest approach 2.0 m; the car came to a stop behind it.

**THE HOOKS DID NOT CAUSE THIS.** Over the 8.6 s approach the command was above the model's own
request on 88/171 frames, mean **+0.022**, max +0.064 m/s^2. Integrated that is **0.097 m/s =
0.35 km/h** of speed not shed. Against a 68 m gap closure at 110 km/h that is nothing. Hook 6
never armed (0/171). Layer C never fired (headroom -0.3 to +1.8 km/h, its gate is 5 km/h).
Note also that raising the e2e candidate makes the MPC win `min()` SOONER, not later, so the
hooks cannot have delayed the takeover.

The dominant factor is stock behaviour: **aggressive personality has the shortest follow
distance**, so braking starts latest, and the lead decelerated hard.

**THE REAL FINDING — HOOK 8 IS LEAD-BLIND.**

    HoldSpeed.update(a_e2e, a_commanded, a_ego, v_ego, v_cruise, aggressive, long_pid,
                     driver_input, experimental)
    E2EAccelFloor.update(a_e2e, v_ego, v_cruise, LEAD, throttle_prob, curvature, ...)

Hook 6 takes `lead` and refuses to arm when one is present. **Hook 8 has no lead input at all**
— it cannot know. That was tolerable while its zero-cap sat at 0.0, because any negative model
request folded the cap and the output could not exceed 0. Moving the cap to -0.20 today opened a
band (-0.20 < a_e2e < 0) in which hook 8 may add POSITIVE correction, and a gentle lead approach
lives exactly there.

Measured over the 10:47-10:52 extract: 2062 engaged frames with a lead, 1114 of them CLOSING
(vRel < -0.5), 264 with the model mildly braking inside the newly-opened band, and **166 frames
where braking was withheld, up to +0.2267 m/s^2**. (That peak is un-contextualised — I have not
checked what the gap was on that frame.)

Hook 8's existing defence is that its error is measured against the ACTUAL command, so when the
MPC brakes, u reads ~0 and it stops correcting. That holds once the MPC OWNS the plan. It does
not hold during the approach, when cruise or e2e is still the source, the plant is tracking fine,
and hook 8 is free to push up.

**PROPOSED FIX — give hook 8 the lead gate hook 6 already has.** Thread `lead` through
`floor_e2e_accel` (it is already read there for hook 6) and decay the correction out while a lead
is present. Behind a lead the MPC owns the longitudinal decision and a speed-holding servo has no
business adding throttle. Cost is small: hook 8 exists for grade droop on an open road, and
behind a lead the MPC already commands what following requires. NOT IMPLEMENTED — reported first.

## 2026-08-26 — hook 11 (far-lead pre-brake) IMPLEMENTED and replay-verified. Same 10:49
## incident, a DIFFERENT and lower-level root cause than the hook 8 finding above.

Separate investigative thread from the 2026-08-25 entry above, same underlying drive
(`00000128--201591a1fc`, `2026-08-25/d011-d014.zst`). That entry found the hooks did not cause
the late brake and proposed a hook 8 lead-gate fix (not implemented). This thread asked a
different question — is the MPC's OWN input trustworthy? — pulled the raw rlog data directly
(`radarState`, `longitudinalPlan`, `carState`, `carControl`, all four segments), and found: it
is not. Full numeric derivation is upstream of this repo's captains_log (Claude conversation,
2026-08-26); the load-bearing measurement:

    t+0.0s-8.0s (dRel 120m -> 56m): true 1s-baseline closing rate averaged -6.7 to -16.1 m/s;
    reported radarState.leadOne.vRel averaged -1.56 m/s over the same window (measured
    3-6x understatement). model_v_ego tracks carState.vEgo within 0.3-1.6 m/s (ruled out as the
    source); the error is specific to the model's lead-velocity head, and specific to long range
    + high closing speed -- at dRel<30m later in the same episode, vRel and true closing rate
    agree well. Root cause: radard.py's KF1D only runs for radar-matched tracks; this car
    (`radarUnavailable=True`) always takes the vision-only path, so `vLeadK`/`vRel` are a raw,
    unfiltered copy of the model's single-frame velocity estimate, which is low-SNR for a small,
    distant, fast-closing object.

Two designs were reviewed (advisor-consulted) before implementation: an "other agent's" 4-point
plan touching `radard.py`/`long_mpc.py` directly (Category C, the highest-risk class in
GRT_MODS.md, since it edits core longitudinal control rather than a fork-owned add-on), and
`FAR_LEAD_PREBRAKE_PROMPT.md` (repo root) — a bounded, fork-owned `min()` candidate, RELAXED
PERSONALITY ONLY, that fills the gap without touching either stock file. The narrower design was
chosen and IMPLEMENTED: `openpilot/grt/far_lead.py` (hook 11), wired into `hooks.py` and one
`candidates +=` line in `longitudinal_planner.py`. See `GRT_MODS.md` for the file-level diff
summary.

**One correction made to the spec doc before implementation** (Claude + advisor, this repo):
the original release condition (`dRel < 50` hard cutoff) was replaced. Checked against this log:
at dRel=50.24 m, stock `aTarget` was still -0.298 — weaker than hook 11's own -0.40 floor — only
crossing -0.40 at dRel~50.08 m. A hard release at 50 m would land inside that gap and could step
the commanded accel back toward -0.30 for a frame or two at the tightest part of the approach.
Replaced with: release once the OTHER candidates already built this frame (`stock_min`, passed
in by the caller — this hook's call signature is `(sm, v_ego, stock_min)`, not the spec's
`(sm, v_ego)`, because the MPC/e2e candidates are local variables in `longitudinal_planner`, not
recoverable from `sm`) have themselves reached `<= -0.40`, still returning this hook's own
candidate on that same frame so `min()` decides, then dropping the latch. `RELEASE_DIST = 20 m`
is only an absolute backstop.

**Two further bugs found by testing, before any replay** (see `far_lead.py` module docstring for
full detail):

1. An early cut gated ARMING on `min(lead.vRel, v_filt)` — i.e. let the model's raw, noisy
   `vRel` help decide WHETHER to arm, not just how hard to brake once armed. Replayed, it
   false-armed at t=0.35s on one noisy `vRel` sample, AND false-armed on an unrelated 4.8s noisy
   pre-episode blip elsewhere in the same recording (flickering, non-closing detections at
   111-114m, prob 0.5-0.7). Fixed: arming gates on `a_req` computed from the FILTER's own
   velocity only, held above threshold for `HOT_PERSIST_S=0.5s` continuously; the raw-`vRel`
   pessimistic pairing is used only for the command once already armed, where it is safely
   bounded by the `[-1.2, -0.40]` clip.
2. The `dRel > 100m` arm-distance gate was checked at the moment the persistence timers
   completed (using LIVE `dRel`), not at first lock. A synthetic "fully stopped lead at 120m"
   test — the single most dangerous case this hook exists for — never armed at all, because a
   lead closing at the maximum possible rate (`vRel = -v_ego`) closes 20+m during the 0.8s
   persistence/hot-gate delay, crossing under 100m before the check ever ran. Fixed: capture
   `dRel_at_lock` once, at the first frame of the qualifying presence run, and gate on that.

**Filter tuning** (`ALPHA=0.10, BETA=0.003` on an `[x, v]` position-measurement filter over
`leadOne.dRel` — NOT a reuse of `radard.py`'s `KF1D`, which measures Doppler velocity into a
`[SPEED, ACCEL]` state, a different measurement entirely): chosen empirically against this log
and the pre-episode blip, not derived analytically. Faster tunings (0.15, 0.20) arm the real
episode slightly earlier but still false-arm on the blip even with 1.0s of hot-persistence.

**Replay bar (kinematic, not acados — pycapnp via `.venv`, all 4 segments, real absence/presence
pattern):**

    Aggressive: hook fires ZERO times across the entire ~160s recording (bit-identical to stock).
    Relaxed:    hook arms at t+2.3s, dRel=115.0m, a=-0.40 (spec target was "~118m").
                Stock (as-logged, aggressive) does not reach source=lead0 until t+7.35s,
                aTarget=-0.023 at that point -- this hook is ~5s earlier and ~50m further out.

Tests: `openpilot/grt/tests/test_far_lead.py`, 24/24 pass (stubbed, no openpilot import needed).
`test_hooks.py` unaffected (44/44). Full `grt/tests/` suite re-run clean, including
`test_schema_conformance.py` (30/30, run via `.venv/bin/python3` for pycapnp).

This numeric design has not had a second advisor pass on the final tuning constants (advisor
was unavailable at that point in the session) — flagged in the `far_lead.py` module docstring
so a future reader sees the gap. Tier 2 lead-icon cluster jitter (2026-08-2x, deferred) shares
this same root cause (`radarState.leadOne.vRel`) and is NOT fixed by this change — hook 11 only
wins the planner's `min()` during the 115m-75m window, it does not touch `radarState` itself, so
the dash display is unaffected.

## 2026-08-26 (later) — hook 11 DEPLOYED to comma4

Device was on `09c5c5e`, 2 commits behind. Bundle `09c5c5e..nightly-dev` (24 KB, 2 commits) →
scp → `pkill -f "[m]anager\.py"` → `git fetch <bundle> && git merge --ff-only` → reboot.
Fast-forward clean, 7 files / +883 −3. No scons, no cereal SCP. Bundle deleted after.

**Verified ON DEVICE, before the reboot:**
- `test_schema_conformance.py` against the device's own `log.capnp`: 30/30.
- `test_far_lead.py` 24/24, `test_hooks.py` 44/44.
- Real imports (actual openpilot deps, not the test stubs): `grt.hooks`, `grt.far_lead` both
  import; `hooks._far_lead_singleton()` constructs a real `FarLeadPreBrake`; an inert call
  through the real `far_lead_candidates(sm, v_ego, stock_min)` with aggressive/not-longActive
  returns `[]`. `longitudinal_planner.py` imports and its compiled `update()` source contains
  the `far_lead_candidates` call site.

Device came back in ~110s. Commit `2ff409b` confirmed running post-reboot.

**Verified AFTER the reboot:**
- manager, card, plannerd, controlsd, selfdrived, modeld, dmonitoringmodeld, radard, micd,
  soundd all up (no repeat of the 2026-08-25 micd/soundd incident this time).
- `managerState`: **nothing shouldBeRunning-but-not-running**.
- **THE CRITICAL ONE — engagement is not blocked.** `onroadEvents` carries only `wrongGear` and
  `seatbeltNotLatched`, the legitimate physical blockers for a parked car. No `commIssue`.
- Zero swaglog entries mentioning `far_lead`/`hook 11` since boot — no exceptions from the new
  code. Two unrelated tracebacks present (`pandad` panda-DFU SPI NACK during boot handshake,
  `athenad` TLS cert-not-yet-valid) — neither touches any file this change modified; the athenad
  one is consistent with the previously-documented pre-GPS clock drift at boot, not a
  regression.
- `longitudinalPlan` publishing cleanly, sane values while parked (`aTarget≈0.004`,
  `source=e2e`, `vEgo=0`).

**NOT YET road-tested.** hook 11 only arms in RELAXED personality — a drive to confirm it fires
sanely (and does not fire in aggressive/standard) still needs a personality switch and a real
approach to slow/stopped traffic to be conclusive; the replay bar above is the only evidence so
far that it behaves as designed.

## 2026-08-27 — hook 11 FAILED TO ARM on a real approach, driver intervened. Root cause found
## (absence-gate lockout). NOT YET FIXED — diagnosis only, no code changed.

Operator report: 07:55 local, closing on a lead, "hook failed, no slow down, had to intervene."
Route `00000139--cdb525d5c8` (segments 0-6, 05:55-06:01 UTC). Personality `relaxed` 100% of the
drive, so hook 11 was eligible the whole time.

**First mistake, caught before drawing any conclusion:** my first replay script fed the device's
OWN prior route (`00000138--...--7`) into the same merged timeline as today's route for extra
margin. Checking `logMonoTime` across the two showed route 138's minimum was LARGER than route
139's despite route 138 being chronologically earlier — different boot sessions, incompatible
monotonic-clock epochs. Merging them was invalid and would have scrambled the absence/presence
timing the whole diagnosis depends on. Re-extracted route 139 standalone once this was caught.

**What happened, replayed against the real captured `radarState`/`carState`/`carControl` stream
using the actual deployed `far_lead.py`, fed from t=0 (not mid-stream — a second harness bug,
also caught before trusting the result: feeding the hook starting partway through a recording
corrupts its `absent_s` accumulation, since that state only exists in the samples actually fed
to it):**

    t+47.02s  lead flickers in at 108.2m, then gone            (qualifying_absence=True — fine,
    t+49.42s  lead flickers in at 105.6m, gone again by 50.67s   preceded by a real >2s gap)
    t+51.12s, 51.62s   two more sub-second flicker locks         (qualifying_absence=False —
                                                                   each followed the last by <0.5s)
    t+51.87s  lead locks CONTINUOUSLY at 101.4m                  (qualifying_absence=False — the
                                                                   preceding gap was only 0.20s)
    t+51.87s -> ~t+61s   dRel falls 101 -> 12m, real, sustained, a_req_filt climbs to ~3.0 (10x
                         the 0.30 arm threshold) -- HOOK NEVER ARMS, hot_elapsed pinned at 0.00
                         the entire time
    t+56.74s  driver disengages, dRel≈37m, closing at -5 to -10 m/s

Confirmed by direct instrumentation of the hook's own internal state (`hot_elapsed`,
`qualifying_absence`, `dRel_at_lock`) frame by frame — not inferred from the published plan.

**Root cause:** `qualifying_absence` is captured ONCE, at the instant a presence run begins, and
frozen for that run's entire duration (`self.present_s == 0.0` is the only place it's set). The
lock that turned into the real, dangerous, sustained approach (51.87m) happened to follow a
0.20s gap, not the required 2.0s — an accident of exactly when the vision model's confidence
crossed 0.5 during acquisition, not a property of the danger itself. Once locked, presence
never dropped again until well past the danger, so the hook got no second chance: `a_req_filt`
reaching 3.0 could not un-gate a flag that was already false.

**This is NOT the same bug as the two found in testing before deployment** (raw-vRel arming
noise, and the dRel-at-persistence-vs-at-lock timing) — both of those were correctly fixed and
are not implicated here. This is a third, distinct failure mode in the SAME arming gate, only
exposed by a flicker-then-sustain acquisition pattern that did not appear in the pre-deployment
replay log (that log had one clean rising edge).

**Ruled out, so the finding doesn't overreach:** the EARLIER 49.42-50.63s run also never armed,
but for a different and correct reason — instrumented separately, `vRel` was genuinely
non-closing there (dRel net 105.6 -> 110.3), `hot_elapsed` climbed to 0.30s on transient noise
in `dRel` and correctly reset when the noise reversed. That run's failure to arm is the hot-
persistence gate working exactly as designed, not a bug.

**Quantified cost, computed by patching `qualifying_absence` true at the 51.87s lock ONLY and
re-running the same replay (counterfactual, not a proposed fix):** hook would have armed at
t+52.77s, dRel=93.6m, released at t+55.92s, dRel=52.2m — 3.15s armed, upper-bound ~3.6 m/s of
speed shed if it had won the `min()` throughout that window. Materially more than a rough
manual estimate suggested; this failure mode cost real, non-trivial benefit in this event, not
a rounding error.

**Not fixed.** Loosening `ABSENCE_S` naively risks reopening the pre-deployment false-arm defect
(the noisy blip that motivated the 2.0s gate in the first place) — this run's own near-miss at
49.42-50.63s shows the hot-persistence gate alone is not obviously sufficient replacement
without checking. The right fix needs the same rigor as the original design: advisor review,
then a redesign of how "was this really a fresh appearance" is decided, then re-replay against
BOTH this log and the two prior validation logs before anything ships. Operator has been told
explicitly: hook 11 did not and cannot help in this event class as deployed — drive as if it is
not there until this is resolved.
