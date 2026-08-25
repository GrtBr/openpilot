# Claude Code prompt — hook 10 + seams in hooks 6/8

You are implementing a longitudinal fix on the GRT `nightly-dev` fork of openpilot
(Hyundai Staria, Comma 4, `openpilotLongitudinalControl=True`, experimental mode on).

Work in `/home/pi5-ubuntu/Comma/openpilot/nightly-dev`. Read this whole file before editing.
Do not invent a different mechanism.

**Forbidden:** EMA / time-constant / low-pass on `a_cmd`; `LAT_SMOOTH_SECONDS`; “don’t raise
hook 6’s floor while raw ≤ 0” (that *is* hook 6’s job); delaying `a ≤ -0.20`. Those were
measured and rejected, or they fight the operator’s design.

**Never SCP cereal files to the device.** Do not touch `cereal/custom.capnp`,
`cereal/log.capnp`, or `cereal/services.py` — no schema change is required.

---

## 0. What hooks 6 and 8 are FOR (do not “fix” this away)

Aggressive personality only. Operator design, 2026-08-14:

1. **Hook 8 (`hold_speed.py`)** — e2e outputs **desired acceleration**, not a target speed.
   On a hill the model often does not add throttle, so speed **bleeds**. Hook 8 is a
   disturbance-rejection servo: make the car actually deliver what was asked, so speed does
   not fall on grade.
2. **Hook 6 (`e2e_floor.py`)** — the model accelerates, then **settles near 0** even though it
   would not mind going faster and there is headroom to the set. Hook 6 kicks in and **keeps
   accelerating** until set cruise speed **or** e2e goes negative *enough* (`_ABANDON_ACCEL`).

Both raise the **e2e candidate**, then `min(e2e_raised, a_cruise, mpc)` in
`longitudinal_planner.py`. Personality is the switch. No feature param. Do not add one.

The 2026-08-22 drive shows 6 and 8 **are firing**, then **losing `min()`** or **zero-capping
themselves**, so they cannot do (1) or (2). The fix is to open their output path and stop
the SCC deadband chatter — not to replace them.

---

## 1. Diagnosis (2026-08-22)

Logs: `/home/pi5-ubuntu/drives/2026-08-22/`
`aNNN.zst` = route `00000117--2921c4a98c` (from ~12:24). `bNNN.zst` = `00000118--60edcba242`
(from ~15:45). Wall clock UTC+2.

### A. Distinct throttle off–on (SCC deadband)

Hyundai CAN-FD: `aReq ≈ 0` is the throttle deadband
(`opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py` `create_acc_control`). Crossing zero is
throttle **off**, then **on**. It is a **sign change**, not a sharp step. 14:35 `maxstep` is
only 0.058 m/s²; CAN already clips jerk at 5 m/s³. Smoothing the slope does not help.

**14:35–14:36 aggressive**, 110 vs 110, no lead. `min()` of cruise vs e2e. Cruise is
`clip(v_cruise - v_ego)` — at equality that is **0**. A ±0.4 km/h ripple is ~0.11 m/s²; a
tiny **negative** cruise beats e2e’s +0.07 and dumps throttle. 65 zero-crossings/min, 59
source flips. Worst 8 s (14:35:48–56): 90/min. Same as captains_log 2026-08-19 (`j_cruise`
will not fix it).

**15:28 relaxed, uphill**, 62–80 vs 110, no lead. 100% e2e, 6/8/9 off. `a_cmd == raw`.
Model dips through zero; hook 7 only rate-limits **rises**, so the cut is instant:

```
15:28:39.358  +0.066  aEgo +0.16
15:28:39.537  -0.015  aEgo -0.40   OFF
15:28:39.687  +0.025  aEgo -0.17   ON
15:28:39.987  +0.116  aEgo +0.81
```

Felt vs unnoticed: 14:35 had 63 off–on pairs; median cut from **+0.034**, off **0.16 s**
(trickle torque, SCC barely unloads). The named stutter is the **14:35:50–14:36:00 cluster**
(0.25–1.40 s off, aEgo p-p 1.08–1.30) and **15:28:10 / 15:28:39** (loaded engine on a hill).

EMA on logged `a_cmd` (dt=0.01): 14:35 65 → 41/min at τ=0.10, still 37 at τ=0.20; only τ≈1 s
kills it and that is the time constant already rejected for hook 7. Hysteresis that **clips
to 0** still hits the deadband. Hold the **pre-glitch command**.

### B. Slow speed hunt (~4.5–5.5 s), mostly uphill, aggressive

This is **not** the fast hook-8 zigzag (already EMA’d, `rev@0.05` ≈ 0 on these windows).
It is a slow pull / let-go / droop. On this car `aReq = 0` means **coast**, not hold speed.

| window | hooks raising | `min()` winner | v vs set | period / amp | seam |
|---|---|---|---|---|---|
| 14:45–46 hill | **48%** | e2e 91% | 95–110 vs 110 | 4.6 s / 1.0 km/h | 6/8 add, then withdraw / cruise clips |
| 15:08–09 | 24% | e2e 97% | ~103 vs 110 | 4.6 s / 1.3 | same |
| 15:22–23 hesitant | **61%** | e2e | 66–101 vs 110 | — | floor at +0.50 vs negative model, then 1-frame dump |
| 16:40 hill | 24% | cruise 25% | 51–60 vs **60** | 5.4 s / 1.3 | hit 60 → cruise **0** beats e2e+hooks → 51 → pull |
| 16:58–59 flat | 27% | e2e 95% | 106–110 vs 110 | 4.8 s / 0.45 | trickle + 56 zc/min |
| 17:13–15 hill | 38% | e2e 83% | 94–110 vs 110 | 4.4 s / 0.5 | bleed while hooks still raising |
| 17:34–40 hill | **7%** | **cruise 77%** | 109.6 vs 110 | 4.4 s / 0.2 | at set, 6/8 almost never reach the wheels |

**Structural veto of (1) and (2):** at set speed `a_cruise = 0`, so
`min(e2e + 0.30, 0) = 0`. Hook 8’s grade correction is thrown away. Coast on grade → droop
→ headroom opens → 8 works again → 5 s pump. 16:40 is the clean picture (60.0 → 50.9 → 60.0).

**Hook 8 zero-cap is too tight for (1).** If `a_e2e < 0`, corr is limited so
`a_e2e + corr ≤ 0`. On a hill the model sits at **−0.02…+0.02** (“I don’t desire more
throttle”). That is exactly (1), and the cap makes 8 a no-op.

**Handoff at 1 km/h** (`_HS_MIN_HEADROOM = 0.28 m/s`): hook 8 decays out and “cruise owns
the set.” Cruise cannot own a hill.

Typical hunt **downstroke** jerk p50 is already **−0.08 m/s³** (slow). Softening that further
does not stop the pump. The pump is command going to **0**.

### C. 15:22 hesitant then jerk (hook 6 abandon snap)

```
15:22:54  a_cmd +0.28  raw -0.01   floor still climbing   ← (2) working, do not remove
15:22:55  a_cmd +0.50  raw -0.04   at cap, not “negative enough” yet
15:22:44  +0.50 → -0.175 in 0.01 s  jerk -62 m/s³ planner-side
```

`if a_e2e < _ABANDON_ACCEL: self.floor = a_e2e` in one frame. CAN stretches at 5 m/s³
(~0.13 s) but it is still a snap. **Do not stop raising the floor near 0** — that is (2).
Soften only the throttle-off portion of abandon.

---

## 2. What to implement

Three files of behaviour, two planner injection lines. Constants — reuse this fork’s numbers:

| name | value | from |
|---|---|---|
| `EPSILON` | 0.04 m/s² | stay off SCC deadband |
| `BAND` | 0.08 m/s² | `_DECAY_DEADBAND` |
| `T_HOLD` | 0.30 s | `_DECAY_T` / `_ABANDON_T` (6 × `DT_MDL`) |
| `ABANDON` | −0.20 m/s² | `_ABANDON_ACCEL` |
| `MIN_HEADROOM` | 1.39 m/s (5 km/h) | `_MIN_HEADROOM` (layer C only) |
| `THROTTLE_FALL_JERK` | 1.5 m/s³ | `JERK_RELAXED` — 0.33 s from hook-6 cap to coast |

`DT_MDL = 0.05`. `ACCEL_MAX` is already imported in the planner (2.0 experimental).

---

### Hook 10 — `openpilot/grt/throttle_hold.py` (all personalities)

New class `ThrottleHold`. Two shims in `hooks.py`. No feature param.

#### Layer B (strong) — `deadband_cruise_accel(a_cruise, v_ego, v_cruise)`

Run on the **cruise candidate**, after hook 5, before `min()`.

```
if v_cruise is ~0:          # forceDecel, match hook 5
    return a_cruise
if v_ego <= v_cruise:       # at or below set — do not cap
    return ACCEL_MAX        # let e2e+hooks/mpc choose
return a_cruise             # overshoot only: existing P-term, possibly hook-5 softened
```

This is how (1) and (2) reach the SCC at set speed. Without it, `min(raised_e2e, 0) = 0`.

Lead/MPC still win `min()` if they are lower. Hook 1 lowers `v_cruise` for map curves/limits/
hazards; then `v_ego > v_cruise` and cruise goes negative as designed.

Also keep the tiny-negative clip for the overshoot branch (optional, cheap): if
`-BAND < a_cruise < 0`, set `a_cruise = 0` so a 0.4 km/h ripple just over set does not
click the SCC. Do **not** apply that clip when `v_cruise ~ 0`.

#### Layer A — sign debounce on the **final** command

After `min()` and after hook 7. All personalities.

State: `last_sign` ∈ {+1, −1}, `last_a` (last **emitted** command), `pending_t`.

On each planner tick, `a` is the post-hook-7 target:

1. If `a <= ABANDON`: accept immediately. `last_sign = -1`, `last_a = a`, `pending_t = 0`,
   return `a`. Never delay a real brake (lead / curve / hazard / vision).
2. Else if sign agrees with `last_sign` (`a == 0` is **not** a sign change away from `+`;
   it is a glitch toward the deadband):
   - `pending_t = 0`
   - `out = a`
   - if `last_sign > 0`: `out = max(out, EPSILON)` — never emit 0 while holding throttle-on
   - `last_a = out`; return `out`
3. Else (sign disagrees, not abandon):
   - `pending_t += DT_MDL`
   - if `pending_t < T_HOLD`: return `last_a` **unchanged** (pre-glitch command, **not** 0)
   - else: accept the new sign. `a == 0` after T_HOLD → coast (`last_sign = -1`).
     If new sign is +: `out = max(a, EPSILON)`; else `out = a`. `last_a = out`; return `out`.

First active frame adopts `a` (do not seed at 0). `longActive` false → drop state.

Replay: 14:35:50 (0.25 s) and 15:28:39 (0.20 s) **killed**; 14:35:58 (1.40 s to −0.09)
coasts only after 0.30 s; `a = -0.50` the frame after +0.10 is **−0.50 this frame**.

#### Layer C — backstop, especially relaxed (no 6/8)

Same function as A, **before** the debounce. If `v_cruise - v_ego >= MIN_HEADROOM` and
`v_cruise` is not ~0 and `a > ABANDON`:

```
a = max(a, 0.0)
```

If `last_sign > 0`, prefer `max(a, EPSILON)`. Does **not** add acceleration (unlike hook 6).
Only refuses to cut throttle while unused set-speed headroom exists and the request is not
a real brake. Aggressive hills should already be held by B + hook 8; C is the relaxed 15:28
path and a belt if 6/8 are not armed.

---

### Hook 8 — `openpilot/grt/hold_speed.py` (aggressive, purpose 1)

Two constant/logic changes. Do not retune GAIN/CAP/TAU/CORR_JERK.

**Zero-cap:** today, if `a_e2e < 0.0`, `target = min(target, max(0.0, -a_e2e))` so
`a_e2e + corr ≤ 0`. Change the condition to `a_e2e < ABANDON` (**−0.20**). Import or
duplicate the −0.20; do not silently drift from `_ABANDON_ACCEL`.

Mild “I don’t want more throttle” must still allow a **positive** hold. A real vision/lead
brake (`< -0.20`) still folds the cap so we do not accelerate against it.

**Handoff:** today `_HS_MIN_HEADROOM = 0.28` (1 km/h) decays corr out and “cruise owns the
set.” Cruise cannot own a hill. Decay only when `v_ego > v_cruise` (headroom `< 0`). Keep
the decay-through-both-filters behaviour; only change **when** it triggers.

Update the module docstring: the old “never accelerate against a deceleration request”
sentence is now “never accelerate against a request `≤ ABANDON`.” The 08-19 output-clamp
history stays; we are not bringing the hard output clamp back.

---

### Hook 6 — `openpilot/grt/e2e_floor.py` (aggressive, purpose 2)

**Do not** stop raising the floor while raw is ~0. That is the settle (2) is built on.

**Do** change the abandon branch (`a_e2e < _ABANDON_ACCEL`):

```
if a_e2e < _ABANDON_ACCEL:
    if self.floor > 0.0:
        # throttle release only — then snap to the brake request
        self.floor = max(0.0, self.floor - THROTTLE_FALL_JERK * DT_MDL)
        return self.floor          # still >= 0 this frame
    self.floor = a_e2e             # already at/below 0: instant brake
    return a_e2e
```

`THROTTLE_FALL_JERK = 1.5` m/s³ → 0.33 s from `_FLOOR_MAX` (0.50) to coast, **then** snap
to e.g. −0.20. Never hold +0.50 against −1.2. Lead-branch dumps do not go through hook 6.

Define `THROTTLE_FALL_JERK` in `e2e_floor.py` (or import `JERK_RELAXED` from `accel_ramp`
if that does not create a cycle — prefer a local constant equal to 1.5 with a comment).

The `_ABANDON_T` latch-out path is unchanged. Immediate deference still exists; it is no
longer a 1-frame drop from cap through the throttle band.

---

## 3. Injection

`openpilot/selfdrive/controls/lib/longitudinal_planner.py` — two new `GRT-MOD` one-liners:

**After hook 5:**

```python
self.a_cruise = grt_hooks.soften_cruise_decel(self.a_cruise, v_cruise, v_ego)
# GRT-MOD-START — hook 10 layer B: at/below set, do not let cruise=0 veto hooks 6/8
self.a_cruise = grt_hooks.deadband_cruise_accel(self.a_cruise, v_ego, v_cruise)
# GRT-MOD-END
```

**After hook 7:**

```python
output_a_target = grt_hooks.ramp_relaxed_accel(output_a_target, sm, sm['carControl'].longActive)
# GRT-MOD-START — hook 10 A+C: SCC sign debounce + no mild coast with headroom.
# AFTER min() and hook 7. All personalities. See grt/throttle_hold.py.
output_a_target = grt_hooks.hold_throttle(output_a_target, sm, v_ego, v_cruise,
                                          sm['carControl'].longActive)
# GRT-MOD-END
```

`hooks.py`: shims with the same exception latch as the others (return input unchanged).
Singleton of `ThrottleHold`. Pass `longActive` so state drops when disengaged.

Update the hook list in the `hooks.py` module docstring (5–9 paragraph → include 10, and
note the hook 8 zero-cap / headroom seam change).

---

## 4. Files to touch

| file | what |
|---|---|
| `openpilot/grt/throttle_hold.py` | **new**. Layers A/B/C. Docstring in the style of `e2e_floor.py` (WHAT THIS IS FOR, SAFETY, what it does not address). |
| `openpilot/grt/hooks.py` | two shims + docstring. |
| `openpilot/grt/hold_speed.py` | zero-cap at −0.20; headroom trigger `v_ego > v_cruise`; docstring. |
| `openpilot/grt/e2e_floor.py` | abandon: fade throttle to 0 at 1.5 m/s³, then snap. |
| `openpilot/selfdrive/controls/lib/longitudinal_planner.py` | two GRT-MOD lines, positions above. |
| `openpilot/grt/tests/test_throttle_hold.py` | **new**. Stub `DT_MDL = 0.05` like `test_accel_ramp.py`. |
| `openpilot/grt/tests/test_hold_speed.py` | **update** the zero-cap / “at the set speed” tests to the new seams (see §5). |
| `openpilot/grt/tests/test_accel_ramp.py` | if the personality-churn test assumes 1-frame abandon from a large positive floor, update it to allow 0.33 s of non-negative fade then snap; **keep** the “−1.2 this frame once floor is already ≤ 0” assertion. |
| `GRT_MODS.md` | planner row + `throttle_hold.py` under fork-owned; note 6/8 seam edits (those files are already category A). |
| `captains_log.md` | newest-first: 22 Aug stutter + hunt windows, what changed, NOT DEPLOYED. |

Do not change hook 6/7/8/9 GAIN/CAP/TAU/FLOOR_MAX/FLOOR_JERK. Do not change `hyundaicanfd.py`.

---

## 5. Tests

Runnable as `python3 openpilot/grt/tests/test_throttle_hold.py` (and existing suites).
Stub DT_MDL. `check(name, cond)`. Exit 1 on failure.

### test_throttle_hold.py

**Layer B**
- `v_ego <= v_cruise`, `a_cruise = -0.02` or `0.0` → returns `ACCEL_MAX` (2.0)
- `v_ego > v_cruise` by ~1 km/h, `a_cruise = -0.28` → unchanged (−0.28)
- `v_cruise ~ 0` (forceDecel), `v_ego > 0`, negative `a_cruise` → **not** replaced with max_accel
- overshoot branch: `-0.05` → `0.0` if you implemented the tiny-negative clip

**Layer A**
- First active frame adopts current command
- Inactive drops state
- Chatter: +0.05 for 1 s, then 5 frames (0.25 s) of −0.02, then +0.05 → output never ≤ 0;
  during the glitch holds the pre-glitch value (**not** 0)
- Held negative: +0.05 then −0.09 for 8 frames (0.40 s) → still positive through frame 6,
  accepted negative from frame 7
- `a = -0.50` the frame after +0.10 → **−0.50 that frame** (ABANDON)
- While last_sign is + and `a` is +0.01 → output ≥ EPSILON

**Layer C**
- `v_ego = 62/3.6`, `v_cruise = 110/3.6`, `a = -0.13` for 10 frames → never negative
- Same but `a = -0.50` → −0.50 this frame
- Headroom < 5 km/h: −0.13 is **not** clamped by C (A still applies)

**Replay-shaped**
- 15:28:39-like: +0.07, 4 frames −0.017, then +0.14 → no sign change out
- 14:35:50-like: cruise −0.03 for 5 frames, e2e +0.07, `min()` would be −0.03 → B makes
  cruise max_accel so min() is e2e+; A must not emit a negative
- 16:40-like: `v_ego == v_cruise`, e2e+corr = +0.30, cruise raw 0 → after B, `min` is +0.30
  not 0

### test_hold_speed.py (must change, not “keep green by weakening asserts”)

Old test: “while the model asks for DECELERATION the output is capped at 0.”  
New test:

- `a_e2e = -0.04`, under-delivery `u = +0.20` → output **may be positive**
  (`a_e2e + corr`, corr still capped). This is purpose (1).
- `a_e2e = -0.50`, same under-delivery → output **≤ 0** (zero-cap at ABANDON still holds).
- `v_ego` 0.5 km/h below `v_cruise` → servo still **corrects** (old 1 km/h handoff is gone).
- `v_ego` 0.5 km/h **above** `v_cruise` → corr decays toward 0 through the existing filters.

Plant-delivering / inert-outside-aggressive / lag-reference-is-commanded tests stay.

### test_accel_ramp.py

The personality-churn / abandon test that required a 1-frame drop from a large positive
floor to a large negative e2e: allow up to `ceil(0.50 / (1.5 * 0.05))` = 7 frames of
`out >= 0` decreasing, then the next frame at `a_e2e` if still `< ABANDON`. Once floor is
already 0, the next abandon frame must equal `a_e2e` (instant brake).

---

## 6. Safety argument (put in `throttle_hold.py` and the hook 8/6 docstring deltas)

- **B** can make the car less conservative at/below set: cruise no longer vetoes a positive
  e2e+hook candidate. That is the point of (1) and (2). Overspeed still capped. Lead/MPC
  still `min()`. Map curves still via hook 1 lowering `v_cruise`.
- **Hook 8 zero-cap change** can command positive accel while e2e is mildly negative
  (`ABANDON < a_e2e < 0`). Bounded by `_HS_CAP` (0.30). A request `≤ ABANDON` still cannot
  be accelerated against.
- **A** can hold a pre-glitch **positive** through a dip shorter than 0.30 s. `a ≤ ABANDON`
  is unfiltered this frame.
- **C** can refuse mild coasts with ≥ 5 km/h headroom. Same bound: `a ≤ ABANDON` passes.
- **Hook 6 abandon fade** can keep a **non-negative** command for ≤ 0.33 s after e2e is
  already `< ABANDON`. It cannot keep +0.50 against −1.2; the first 0.33 s only runs floor
  down to 0, then the brake request is instant. This is weaker than holding the floor.

If you find yourself adding a time-constant on `a_cmd`, delaying `a ≤ ABANDON` past that
0.33 s throttle fade, or stopping hook 6 from raising at raw ~0, you have left the spec.

---

## 7. Done when

```
python3 openpilot/grt/tests/test_throttle_hold.py
python3 openpilot/grt/tests/test_hold_speed.py
python3 openpilot/grt/tests/test_accel_ramp.py
python3 openpilot/grt/tests/test_hunting_fix.py
```

all PASS.

`grep -n "GRT-MOD" openpilot/selfdrive/controls/lib/longitudinal_planner.py` order:

hook 1 → get_cruise_accel → hook 5 → **B** → hook 6/8/9 → min → hook 2 → hook 7 → **A+C**.

`GRT_MODS.md` and `captains_log.md` updated. No device deploy from this prompt.

Do not commit unless tests pass. Local commit only if asked; message should name hook 10,
the hook 8/6 seams, and the 2026-08-22 stutter + hunt windows.

Replay gates to cite in captains_log (not necessarily automated on rlogs in this pass):

1. 16:40: 60→51→60 pump must not be `min(..., cruise=0)` at 60.
2. 17:34: cruise must not sit at 0 while e2e+corr is positive and `v_ego ≤ v_cruise`.
3. 15:22:44: no 1-frame +0.50→−0.18; throttle fade then brake.
4. 14:35:50 and 15:28:39: no sign flip out.
5. A step to **−0.5** still appears on the **same 50 ms planner tick** once floor/hold is
   already ≤ 0.
