<!--
  Hand-off for the CODING AGENT: implement hook 12, "danger_hook", on the nightly-dev fork.
  Written 2026-09-30 from the lead_filter investigation (analysis/lead_filter/tracker/FINDINGS.md §§44-55).
  Status: DESIGN VALIDATED OFFLINE ONLY. Not coded, not deployed. Operator decisions D1-D5 taken 2026-09-30 (§10).
-->

# Hook 12 — `danger_hook`: early braking for a stopped or slow lead that appears far away

Repo **`~/Comma/openpilot/nightly-dev`**, fork `github.com/GrtBr/openpilot`, branch **`nightly-dev`**, base openpilot
v0.11.2. Target **comma 4**, Hyundai Staria 4th gen (CANFD, **camera only — there is no radar**), deployed over SSH.

---

## 0. Read first — hard constraints (breaking any of these has broken the car before)

1. **Read `captains_log.md` and `GRT_MODS.md` before touching anything.** Hook 11 (`openpilot/grt/far_lead.py`) is the
   neighbour you are joining; read its module docstring.
2. **Prebuilt branch.** Never run `scons`; never delete `prebuilt`. `params_keys.h`, `services.h`, loggerd are frozen:
   **no new params, no new services, no cereal edits.** Everything here is Python inside `openpilot/grt/`, plus one
   sentinel-wrapped line in the planner. Fork configuration/logs live in `/data/media/0/grt` (`registry.GRT_CONFIG_DIR`).
3. **Never copy `cereal/custom.capnp`, `cereal/log.capnp` or `cereal/services.py` from the Pi5 to the device.**
4. **Hook 12 must never weaken braking.** It only ever adds a `min()` candidate, and returns `[]` when inert.
5. **Never raise.** Every entry point is wrapped; any failure logs once (`_log_exception`) and returns `[]`.
   If construction fails, latch the hook off for the drive (same pattern as `_far_lead_singleton`).
6. **Do not modify hook 11's logic (`far_lead.py`).** Hook 11 and hook 12 are independent state machines; the planner's
   `min()` combines them. The ONE hook-11 change allowed is the driver-pedal outcome logging of §3a (D4, D5), and it lives
   in `hooks.py` around the existing `fl.step(...)` call, not in `far_lead.py`.
7. **Do not deploy.** Implementation and tests only. Deployment needs the operator's go-ahead and follows §9.

---

## 1. What it is for, in one paragraph

On this camera-only car a stopped or slow vehicle first appears ("is published") at roughly 130-160 m true range while
we travel at 20-30 m/s. Beyond ~90 m the camera range is **compressed** (it changes only ~0.43 m per true metre; a stopped
car at 151 m read 110 m), and the camera's own lead speed is wrong (a stopped car read 65 km/h). Hook 11 arms in time but
its closing-speed estimate lags the compressed range, so it brakes softly (-0.2 to -0.6) and stock ends up braking at
-3.5 m/s² near the car (27 Sep bookmark 1: -3.71 peak, 2.7 m final gap). Hook 12 triggers on a signal that does **not**
come from the lead range — the driving model's own brake prediction, combined with speed and distance — brakes firmly at
once, decides after 2 s whether the lead is really stopped/slow, and then either escalates or releases.

**Do not overclaim in the docstring: hook 12 does not detect stopped leads earlier than hook 11.** It triggers ~1 s after
the lead is published (bm1 0.95 s, 1fc 1.05 s); hook 11 arms at about the same moment (bm1 0.15 s later, 1fc 1.1 s
EARLIER). The gain is braking level and a stopped-or-moving verdict, not detection time.
**Timing, stated plainly:** trigger = 0.5 s after the gate first holds; the command ramps to -A1 (1.0 during the
evaluation period, D1) at 1.5 m/s³, i.e. reaches -1.0 about 0.7 s after trigger; the stopped/moving verdict comes 2.0 s
after trigger = 2.5 s after the gate first held. The operator asked for "identify within 1 s"; this delivers braking from
the trigger and a verdict at 2.5 s.

---

## 2. Inputs — all already available in `grt/hooks.py` (it reads `sm` for hook 11)

| name | source | notes |
|---|---|---|
| `p6` | `sm['modelV2'].meta.disengagePredictions.brakeDisengageProbs[i]` where `disengagePredictions.t[i] == 6.0` | probability the driver brakes within 6 s. On this model `t == [2, 4, 6, 8, 10]` → index 2, but **look the index up from `t` every frame**; if `t` lacks 6.0 or the list is empty, the hook is inert (return `[]`). |
| `present`, `dRel` | `sm['radarState'].leadOne.present`, `.dRel` | `dRel` is 0.0 on absent frames — never use it then. |
| `z` (camera range) | `sm['modelV2'].leadsV3[0].x[0] - far_lead.MODEL_RANGE_OFFSET` | continuous every frame (same conversion hook 11 uses, `hooks.py` ~line 722). Import the offset, do not duplicate it. |
| `v_ego` | argument to the hook (planner passes it) | also integrate `v_ego * DT_MDL` inside the hook for odometry `x` (wheel distance). |
| `long_active` | `sm['carControl'].longActive` | inert when False |
| `driver_input` | `sm['carState'].gasPressed or .brakePressed` | reset on driver input |
| `personality` | `str(sm['selfdriveState'].personality)` | see §10 decision D3 |

---

## 3. Algorithm (constants in §4). One instance, stepped every planner frame (DT_MDL = 0.05 s).

Keep these rolling buffers **every frame, even while idle**: last 10 camera ranges `z` (for the 0.5 s median `zmed`),
last 20 `(x, z)` pairs (for the ratio `D`), and a 10-frame run counter for the gate.

**IDLE → TRIGGER** when the GATE has held for `GATE_HOLD` consecutive frames:
`p6 >= P6_GATE` and `v_ego > V_GATE` and `present` and `dRel >= D_GATE` (and the eligibility conditions: `long_active`,
no driver input, personality per D3). On trigger record `t0`, `x0 = x`, `z0 = zmed` (median of the last 10 camera ranges),
and write a `trigger` log record (§6).

**STAGE 1** (from trigger): target `-A1`.
- **2 s TEST**, evaluated **every frame from `t0 + TEST_S` until a verdict is reached or stage 2 begins** (a frame where
  `z0 - pred <= 3.0` gives no verdict and the test simply runs again next frame):
  `ds = x - x0`. Predict where a **stopped** lead's camera range would be, allowing for the far compression:
  `tr0 = z0 if z0 <= KNEE_M else KNEE_M + (z0 - KNEE_M) / KNEE_SLOPE`; `tr = tr0 - ds`;
  `pred = tr if tr <= KNEE_M else KNEE_M + (tr - KNEE_M) * KNEE_SLOPE`.
  If `z0 - pred > 3.0`: `f = (z0 - zmed) / (z0 - pred)`, where **`zmed` is the TRAILING median of the last 10 camera
  ranges at the first frame with `t - t0 >= TEST_S`** (the same trailing median that produced `z0` at trigger). Never a
  single frame. This is exactly what the validated controller does.
  - `f >= F_STOP` → verdict **stopped**: from now on the stage-1 target is
    `-min(max(v_ego² / (2 * max(zmed - 6, 1)), A1), CAP)` (stopped car at the camera range — errs toward MORE braking,
    because the camera reads short far away). **At speeds above ~25 m/s this saturates at `CAP` immediately** (27 m/s,
    camera 110 m → 3.5 m/s² requested). That is intended — do not "fix" it.
  - `f < F_STOP` → verdict **moving**: RELEASE (reason `test_moving`).
  - If `z0 - pred <= 3.0` (we barely moved): no verdict; keep stage 1.
  Log a `verdict` record either way.
  *This test is a hypothesis test only. The compression constants describe what a stopped car looks like to this camera;
  they are never used to "correct" a range that is then braked on.*
- **Enter STAGE 2** when `zmed < ENTRY_M` (0.5 s median — **never a single frame**; a single noisy frame read < 85 m while
  the true range was 101 m, and entering there caused a collision in replay).
- Stage-1 timeout: `t - t0 > STAGE1_MAX_S` and `zmed > 90` → RELEASE (`timeout_far`).

**STAGE 2** (camera range now mostly trustworthy): start a **fresh** 1 s window at entry (discard stage-1 samples).
- Until `WAIT` samples exist: target `-min(max(v_ego² / (2 * max(z - 6, 1)), A1), CAP)` (assume stopped).
- Then each frame: `est` = least-squares slope over the last 1 s of `(t, z + x)` (the lead's own ground speed), floored at
  0; `v_lead = min(est over the last 1 s)` (conservative: under-estimating the lead's speed is the safe error);
  `c = v_ego - v_lead`; `need = c² / (2 * max(z - (1.75 * v_lead + 6), 1))` if `c > 0` else 0;
  **Range choice, deliberate:** stage-2 `need` and the stopped-first target use the SINGLE-FRAME camera range `z`; the
  entry test, the floor test and the release test use the 0.5 s median `zmed`. That split is what passed replay. The
  median lags ~0.25 s — about 5-6 m at 21-27 m/s — in the stretch where every metre counts, while single-frame noise in
  `need` is absorbed by the floor, the cap and the falling-edge jerk limit. Keep it as specified.
  `floor = A1 if (zmed >= TRUST_M or c > 0.5) else 0`; target `-min(max(need, floor), CAP)`.
  The floor holding until `TRUST_M` is essential: between 85 and 60 m the camera is still partly compressed and the fresh
  estimate first reads a stopped car as moving (56 → 40 → 23 km/h before converging to 0).
- Stage-2 release: `c <= 0.5` for `CALM_S` with `zmed < TRUST_M` → RELEASE (`closing_ended`).

**Always-on releases** (checked every frame while triggered):
- **Pulling away:** `D < D_NEG` held for `D_NEG_S`, where `D = -(least-squares slope of z against x over the last 20
  frames)` (camera shrink per metre we drive). Compression can slow the shrink but can never reverse it, so a negative `D`
  can only mean the lead is faster than us. (`pulling_away`)
- Lead lost: `present` False for more than `LEAD_LOST_S` (`lead_lost`).
- Throttle → RELEASE (`driver_gas`, outcome `false`); brake → RELEASE (`driver_brake`, outcome `fail`) — see §3a.
  `long_active` False or personality gate (D3) → RESET immediately (`disengaged` / `personality`).

**Command shaping:** the falling edge (more braking) is rate-limited to `JERK` m/s³ from the previous command; a rise (less
braking, including release) is immediate. On RELEASE output `[]` at once — **no residual braking after release** (an
analysis replay once kept braking -1.0 after release and that hid a real failure).

**Re-trigger:** after a release the gate must be False for `REARM_S` **continuously** before it may trigger again: count
consecutive gate-False frames; any gate-True frame resets that count to zero; only when the count reaches `REARM_S` does
the gate's own 10-frame hold (`GATE_HOLD`) start counting again. Measured on the 48 drives (§8): one trigger per event.

### 3a. Driver pedals — operator decisions D4 and D5 (apply to BOTH hook 11 and hook 12)

- **D4 — throttle pressed while the hook is armed/triggered:** the hook releases at once and the event is logged with
  outcome **`false`** (the driver overrode the braking: a false alarm).
- **D5 — brake pressed while the hook is armed/triggered:** the hook releases at once and the event is logged with outcome
  **`fail`** (the hook did not brake enough; the driver had to).
- Both pressed on the same frame → `fail` (brake takes precedence).
- A pedal pressed while the hook is NOT armed/triggered logs nothing.
- Hook 11 already releases on either pedal (`driver_input` → `_reset()`); only the logging is new. In `far_lead_candidates`
  read `was_armed = fl.armed` before `fl.step(...)`; if `was_armed and not fl.armed` on a frame where `cs.gasPressed` or
  `cs.brakePressed` is True, write to hook 11's existing rotating log (`lead_filter.log`, `_lead_write`):
  `{"ev": "driver", "hook": 11, "outcome": "false"|"fail", "pedal": "gas"|"brake", "t", "armed_s", "dRel", "v_ego", "cmd"}`
  (`armed_s` = time since hook 11 armed; `cmd` = its last emitted command). Never raise; a logging failure must not change
  control.
- Hook 12: the same two pedals produce `release` records (§6) with `reason: "driver_gas"` / `"driver_brake"` and
  `outcome: "false"` / `"fail"`. Every other release reason gets `outcome: "ok"`.
- **Check order — required, or D4/D5 never fire.** On this car a brake press disengages openpilot and a gas press puts it
  in override, so `longActive` drops on the same planner frame or the next. Hook 12 must evaluate `gasPressed` /
  `brakePressed` FIRST on every frame, before `long_active` and the personality gate: if a pedal is down, the release
  reason is the pedal, whatever else is also False on that frame. (Hook 11's detection in `hooks.py` is order-safe:
  `far_lead.step()` resets on the OR of all three, so only the window below matters there.)
- **Attribution window (both hooks).** `carState` (pedals) and `carControl` (`longActive`) arrive on different messages
  and can straddle a frame. If a hook released for any other reason and a pedal registers within the next **0.25 s
  (5 frames)**, attribute that release to the pedal: write ONE record, on the pedal frame, with the pedal's outcome (hook 12:
  the pending `release` record is held for up to 5 frames and then written with the final reason; hook 11: the `driver`
  record is written on the pedal frame, with `armed_s` measured to the actual release).
- **What the labels mean.** `fail` and `false` are the operator's review labels, not verdicts on the hook. A `fail` on hook
  11 means the driver braked while hook 11 was armed — often for an unrelated reason (a junction, a roundabout). Treat each
  as a candidate for review against the road video. Do not describe their counts as a failure rate in any docstring or log
  message. Baseline measured before hook 12 existed: §8.

**Output:** `[(cmd, LongitudinalPlanSource.lead0, should_stop(v_ego, cmd))]` while triggered, else `[]`. Same tag and
`should_stop` import as hook 11 (`far_lead.py` lines 517-518). No hand-off to stock: hook 12 stays active alongside stock
until it releases; the `min()` means it can never fight stock. (Replay: hand-off OFF gave 5.9 m vs 3.9 m with it ON.)

---

## 4. Constants (module-level, commented with the FINDINGS section they come from)

| constant | value | source |
|---|---|---|
| `P6_GATE` | 0.046 | bm1's sustained minimum in its first second after publication (§48b) — **fitted to one event** |
| `GATE_HOLD` | 10 frames (0.5 s) | §48b |
| `V_GATE` | 21.0 m/s | §48a (bm1 was 21.7 m/s; do not raise without new data) |
| `D_GATE` | 100.0 m | §48a-b |
| `A1` | **1.0** m/s² — operator D1, for the practical safety evaluation period (2.0 validated as the stronger setting) | §52, §55, §57 |
| `CAP` | **2.5** m/s² (see D2) | §52 |
| `JERK` | 1.5 m/s³ | hook 11's `JERK_ARM` |
| `TEST_S` | 2.0 s | §53 (1 s is too noisy) |
| `F_STOP` | 0.5 | §53, §55 |
| `KNEE_M`, `KNEE_SLOPE` | 90.0 m, 0.43 | §44c (bm1), cross-checked on 1fc (§53) |
| `ENTRY_M` | 85.0 m (0.5 s median) | §49b, §52 |
| `WAIT` | 10 frames | §49c, §49e |
| `TRUST_M` | 60.0 m | §52 |
| `CALM_S` | 1.0 s | §49b |
| `D_NEG`, `D_NEG_S` | -0.1, 1.0 s | §51 |
| `LEAD_LOST_S` | 0.5 s | hook 11 |
| `STAGE1_MAX_S` | 5.0 s | §49a |
| `REARM_S` | 1.0 s | hook 11's `RE_ARM_HOLD_S` |

---

## 5. Integration

- New module **`openpilot/grt/danger_hook.py`**, split in two so the offline replays can import the controller directly:
  - **`DangerController`** — pure controller, no `sm`, no I/O, no logging. Constructed with the §4 constants (keyword
    arguments, defaults = §4) plus `z0`; `step(t, v_ego, z, x) -> float` returns the command (≤ 0, or 0.0 once released)
    for the triggered state only (stage 1, 2 s test, stage 2, pulling-away and calm releases, timeout, jerk shaping).
    Public attributes: `stage`, `rel`, `why`, `verdict`, `f`. It must be a line-for-line port of
    `analysis/lead_filter/early_mode.py` `EarlyMode` (same constructor names A1, CAP, WAIT, JERK, ENTRY, TRUST, DT, TEST,
    F_STOP, z0 accepted as keywords), so the replays run unchanged with `from openpilot.grt.danger_hook import
    DangerController as EarlyMode`.
  - **`DangerHook`** — the adapter: reads `sm`, keeps the idle buffers and the gate counter, integrates odometry,
    handles eligibility (long_active, driver, personality), lead lost, the re-trigger hold and logging; creates a
    `DangerController` on trigger; returns the candidate tuple list.
  Module docstring summarising §1, §3 and the evidence. Pure Python, no imports beyond what `far_lead.py` imports.
- **`openpilot/grt/hooks.py`**: add `danger_candidates(sm, v_ego) -> list` modelled exactly on `far_lead_candidates`
  (singleton with `_danger_broken` latch, all reads inside `try`, `_log_exception("danger_candidates")`, returns `[]` on
  any failure). Add a hook-12 entry to the hook index docstring at the top of `hooks.py`.
- **`openpilot/selfdrive/controls/lib/longitudinal_planner.py`**: one line directly after hook 11 (line ~203), wrapped:
  ```python
  # GRT-MOD-START — hook 12: danger_hook (grt/danger_hook.py). Early braking for a stopped/slow lead first seen far away.
  # Returns [] when inert, so it can only compete in the min() below, never make braking weaker than stock or hook 11.
  candidates += grt_hooks.danger_candidates(sm, v_ego)
  # GRT-MOD-END
  ```
  The existing `min(candidates)` then gives the car `min(stock, hook 11, hook 12)`.
- **Confirm (and pin in a test)** that the layers after the `min()` cannot soften hook 12: `ramp_relaxed_accel` (hook 7)
  only caps RISES; `hold_throttle` (hook 10 A+C) passes any request at or below -0.20 through unfiltered; the final
  `np.clip(..., ACCEL_MIN, ACCEL_MAX)` has `ACCEL_MIN = -3.5`, below `CAP`.

---

## 6. Logging (operator requirement: "log it when triggered")

File **`/data/media/0/grt/danger_hook.log`** (`GRT_CONFIG_DIR`), JSON lines, rotating at 4 MB with ONE rolled file and a
`rotated` record — copy `_lead_write` in `hooks.py` (~line 764), including its reason: a cap that stops writing silently
once produced a wrong analysis. Never raise; on a write failure, latch logging off and `_log_exception` once.
Records (`t` = `logMonoTime`-compatible seconds, as hook 11c uses):
- `trigger`: `t, v_ego, dRel, z0, zmed, p6, personality`
- `verdict`: `t, f, verdict, ds, pred, zmed`
- `stage2`: `t, zmed, v_ego`
- `release`: `t, reason, outcome ("ok" | "false" | "fail", §3a), dur_s, v_at_trigger, v_at_release, min_dRel, hardest_cmd,
  stage, verdict, f` (the first drives' key question is what `f` real stopped cars produce, so `f` goes in both `verdict`
  and `release`)
- `hb` every 30 s while running (counters: triggers, releases by reason) so a quiet drive still proves it was alive.

---

## 7. Tests — new `openpilot/grt/tests/test_danger_hook.py` (same runner style as `test_far_lead.py`)

Must pass on the Pi5 and on the device (`/usr/local/venv/bin/python3`, `PYTHONPATH=/data/openpilot`). Each test must be
shown to FAIL against a deliberately broken variant (the `test_far_lead` convention: a test that cannot fail is not a test).
1. Inert: no trigger when any single gate condition is missing; gate needs `GATE_HOLD` consecutive frames.
2. Inert and no exception when `meta.disengagePredictions` is empty, `t` lacks 6.0, `leadsV3` is empty, or any read raises.
3. Stage 1: first output after trigger is the jerk-limited step toward `-A1`; never below `-CAP`.
4. 2 s test, synthetic camera following the §3 compression model: stopped lead → `stopped` and escalation; lead at 0.8 × our
   speed → `moving` and release; `ds` too small → no verdict.
5. Stage-2 entry needs the 0.5 s median below `ENTRY_M`: one noisy frame at 80 m inside a 100 m series must NOT enter.
6. The `A1` floor holds while `zmed >= TRUST_M` even when the fresh estimate reads the lead as fast.
7. Each release reason fires, and output is `[]` on the very next frame (no residual braking).
8. Re-trigger is blocked for `REARM_S` after a release.
9. `min()` integration: with a stock candidate of -3.0, hook 12 at -2.0 does not change the planner output; with stock at
   0.0 the output is hook 12's. Hook 12 never makes the output less negative than without it.
10. Post-`min()` layers (§5) do not soften a -2.0 command.
11. Log: each record type written once per event; rotation writes a `rotated` record; a write failure does not raise.
11a. D4/D5 for hook 12: gas while triggered → released, output `[]` next frame, `release` record with outcome `false`;
    brake → outcome `fail`; both → `fail`; pedal while idle → no record. Check order: gas AND `longActive` False on the same
    frame → reason `driver_gas` (not `disengaged`). Window: `longActive` False first, brake 3 frames later → exactly one
    record, reason `driver_brake`, outcome `fail`; brake 6 frames later → the `disengaged`/`ok` record stands.
11b. D4/D5 for hook 11 (extend `test_hooks.py`, not `test_far_lead.py`): drive `far_lead_candidates` with a stub `sm` until
    hook 11 is armed, then press gas → one `driver` record, outcome `false`; repeat with brake → `fail`; pedal while not
    armed → no record; a logging failure does not change the returned candidates.
12. Real-trace fixtures, run OPEN LOOP on the logged inputs exactly as they were (the car did not brake with hook 12, so
    the inputs are not shifted). Source: `/run/media/pi5-ubuntu/Lexar/openpilot/drives/model_meta/<route>.tsv` (columns:
    see `analysis/lead_filter/tools/model_meta_extract2.py`; camera range = col lead x0 - 1.52, `v_ego`, leadOne present,
    dRel, p@6 s = brakeDisengageProbs[2]). Save each window as a small CSV under `openpilot/grt/tests/fixtures/`.
    Expected (computed 2026-09-30 with `early_mode.py`, CAP 2.5, test on; open loop, so trigger, verdict and `f` do not
    depend on `A1`):
    | route, window | trigger | verdict | `f` |
    |---|---|---|---|
    | `000001f1` 335.0-346.0 s (27 Sep bm1) | 339.63 s (0.95 s after publication at 338.68) | stopped (at 341.63) | 0.97 ± 0.05 |
    | `000001f3` 1114.0-1126.0 s | 1120.46 s | moving (at 1122.51) → release | 0.17 ± 0.05 |
    | `000001fc` 1070.0-1082.0 s (optional) | 1074.66 s | stopped | 0.77 ± 0.05 |
    | `000001de` 810.0-822.0 s (optional) | 814.60 s | moving | 0.30 ± 0.05 |
    Trigger times ± 0.1 s. (Closed-loop replays, where the simulated car brakes and the camera view is shifted, give
    different `f`; do not use those numbers here.)
Also run `test_far_lead.py`, `test_hooks.py`, `test_schema_conformance.py` — all must still pass unchanged.

---

## 8. Offline validation this design already has (for the coding agent's context, not to repeat)

The analysis controller is `analysis/lead_filter/early_mode.py`; the replays import it. **Before declaring done, change
the import in `bm1_stock_replay.py` and `moving_lead_cost2.py` to `from openpilot.grt.danger_hook import DangerController
as EarlyMode` (PYTHONPATH=nightly-dev, harness stubs as the scripts already set up) and confirm the same numbers
(±0.2 m, ±0.05 m/s²):**

CAP 2.5, 2 s test on. **Acceptance uses A1 1.0** (operator D1); the A1 2.0 column is for reference.

| case | replay world | what happened | hook 12, A1 **1.0** | hook 12, A1 2.0 |
|---|---|---|---|---|
| 27 Sep bm1, stopped Kiger | exact odometric truth, **with** the stock proxy (×1.4) | gap 2.7 m, peak -3.71, 4.5 s ≤ -2.5 | **gap 4.8 m, peak -3.48, 3.2 s ≤ -2.5** | gap 9.4 m, peak -2.80, 1.2 s |
| 27 Sep bm1 | exact truth, hook 12 **alone** (no stock) | — | **contact (-11.6 m): relies on stock** | gap 3.0 m, peak -2.44 |
| 1fc queue (lead 16 km/h) | camera-built path, hook 12 alone | gap 2.6 m, peak -3.09 | gap 5.5 m, peak -2.42 | gap 37.3 m, peak -2.07 |
| 1f3 off-ramp (lead 74 km/h) | camera-built path, hook 12 alone | — | released at +2 s (f 0.17), 6 km/h shed | 9 km/h |
| 1de slow lead (83 km/h) | camera-built path, hook 12 alone | — | released at +2 s (f 0.30), 6 km/h shed | 10 km/h |

At A1 1.0 hook 12 cannot stop a bm1-type approach on its own; it relies on stock to finish (stock is always in the
`min()`), and the combination still improves on what happened. That is the operator's deliberate choice for the
evaluation period.

Gate frequency on 48 drives (2.3 h above 21 m/s): 5 events (bm1, 1fc, 1f3, 1f0, 1de); operator judged all five as needing
braking. At hook 11's own arm moments the gate held on 2 of 176 arms — bm1 and 1de (on 1fc the gate held 1.1 s after hook 11
armed, so it is not counted there). The brake prediction on its own is on at 41 % of hook-11 arms (mostly in slower
driving, and more often on arms that release), which is why every gate condition is required.

**D4/D5 baseline, measured before hook 12 existed** (`analysis/lead_filter/hook11_pedal_baseline.py`: today's hook 11
replayed on the Lexar route TSVs, which carry both pedals; pedal on the release frame or within the next 0.25 s): hook 11
armed **336** times; the driver braked during the arm on **12 (4 %)** — would log `fail` — and pressed the throttle on
**9 (3 %)** — would log `false`. Read the first drives' `fail`/`false` counts against these rates; each record is a
candidate for video review, not by itself a hook failure.

**Re-triggers, measured** (`analysis/lead_filter/retrigger_count.py`, open loop on the logged inputs, the §3 rule with the
reset-on-gate-True hold): 5 triggers in 5 events, **no event re-triggered**, total stage-1 time 12.3 s over 2.3 h above
21 m/s — bm1 2.4 s (closing ended), 1fc 4.2 s (closing ended), 1f3 2.1 s (test: moving), 1de 2.1 s (test: moving),
1f0 1.6 s (lead lost). Identical with a 5, 10 or 20 s hold after moving/timeout/lead-lost releases, so `REARM_S` stays
1.0 s. Caveat: open loop — on a real drive the car is slower after hook 12 brakes, so check re-triggers in the first
drives' `danger_hook.log` (§6: every trigger and release is logged).

---

## 9. After implementation (only with the operator's go-ahead)

- `captains_log.md`: dated entry (what, why, how verified, "Deploy status: NOT deployed").
- `GRT_MODS.md`: hook 12 row.
- Commit (no force-push), message ending with the attribution lines the session provides.
- Deploy by SSH file writes only — back up, write with fsync + atomic rename, run the tests on the device, `sync; sudo
  reboot`, then verify md5 + NUL scan + live import after boot; log it in `captains_log.md`. Never `git pull` on the device.
- First drives: pull `danger_hook.log` + the route's rlog **and GPS** (`analysis/lead_filter/tools/gps_extract.py`).

---

## 10. Operator decisions (2026-09-30) — implement exactly; each a single named constant

- **D1 — `A1` = 1.0** during the practical safety evaluation period. Hook 12 then relies on stock to finish stopped-traffic
  approaches (§8). Raising it later (2.0 was validated) is a one-constant change with a captains_log entry.
- **D2 — `CAP` = 2.5.**
- **D3 — personality: relaxed only**, like hook 11 (`if personality != 'relaxed': inert`).
- **D4 — throttle pressed while armed: hook 11 and hook 12 release, logged `false`** (§3a).
- **D5 — brake pressed while armed: hook 11 and hook 12 release, logged `fail`** (§3a).

## 11. Known limits (say these plainly in the captains_log entry)

- Only **two** stopped-traffic events in the data (bm1, 1fc); `P6_GATE` is bm1's own minimum; `KNEE_SLOPE` is from bm1
  (1fc fits it independently). **No data at 27 m/s.** Validation needs recorded stopped-traffic approaches at 25-27 m/s.
- The stock behaviour in the replays is a proxy (validated on bm1 only, scaled ×1.4 to reproduce the real stop).
- The camera's near-range error is not a fixed scale (bm1 read ~15 % short at 10-35 m; 1fc read 0-11 % long vs GPS).
- The model's brake prediction is a learned output; a model update can change its scale. Re-check `P6_GATE` after any
  model change.
- The 2 s test's `KNEE_SLOPE` (0.43) describes this camera/model's far-range compression and is used to predict a stopped
  car's camera track. If a model update changes that compression, `f` shifts for stopped leads: open-loop `f` was 0.97
  (bm1) and 0.77 (1fc) against `F_STOP` 0.5, so the margin is real but not unlimited — a softer compression (slope → ~0.6)
  would pull stopped-lead `f` toward the threshold. Re-check `f` on the first stopped-traffic drive after any model change.
  FINDINGS §53's bm1 value of 0.65 used CENTRED 0.5 s medians; the controller's TRAILING-median value is 0.97. The fixtures
  in §7 use the controller's.
