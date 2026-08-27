# Prompt: far-lead pre-brake (relaxed only)

Implement a new GRT planner hook. Advice is settled; this is an implementation brief. Do not retune MPC costs. Do not invent a traffic-light detector. Fill the 115 m → 75 m hole with an extra `min()` accel candidate, then let stock MPC/e2e take over.

Repo: `/home/pi5-ubuntu/Comma/openpilot/nightly-dev` (source under `openpilot/`). Fork convention: one new shim in `openpilot/grt/hooks.py`, logic in a new `openpilot/grt/` module, stubbed tests in `openpilot/grt/tests/` matching `test_hooks.py`. A fork feature degrades to disabled, never crashes plannerd, never makes braking weaker than stock.

Drive data (the acceptance log) lives at `/run/media/pi5-ubuntu/Lexar/openpilot/drives` (`~/drives` is a symlink to that). Route `00000128--201591a1fc`, files `2026-08-25/d012.zst` and `d013.zst`.

---

## 1. Read first

In this order:

1. `openpilot/grt/hooks.py` — injection surface, personality gating, hook 2 / 5 / 7 / 10 contracts
2. `openpilot/selfdrive/controls/lib/longitudinal_planner.py` — the `candidates` `min()`, then hook 7, then hook 10 `hold_throttle`
3. `openpilot/grt/throttle_hold.py` — `ABANDON = -0.20`, layer C
4. `openpilot/grt/scc_map.py` — `approach_speed()`, `APPROACH_DECEL = 0.5`, how hazards self-escalate when they appear late
5. `openpilot/grt/tests/test_hooks.py` — stub style, inert-by-default, exception must not escape
6. `openpilot/selfdrive/controls/lib/longitudinal_mpc_lib/long_mpc.py` — for understanding only. Do not change weights, `T_FOLLOW`, `LEAD_DANGER_FACTOR`, `A_CHANGE_COST`, or the OCP.

---

## 2. Why this exists (measured, 2026-08-25 10:49)

Engaged, **aggressive**, experimental on, vision-only (`liveTracks` empty). Lead first stayed present at **10:49:43.8, dRel 119.8 m**. Plan source stayed cruise/e2e at ~0 until **lead0 at 10:49:51.1, dRel 64.9 m, aTarget −0.02**. First real brake **−0.21 at 53.8 m**. Driver brake / disengage at 50 m.

Vision `vRel` before 10:49:52 averaged **−1.56 m/s**. Finite-difference of `dRel` over the same stretch averaged **−8.16 m/s** (lead ~80 km/h, ego 110). `aLeadK ≈ 0`. `hardBrakePredicted` never fired. Hook 10 C did not fire (no 5 km/h headroom at 110 vs 110).

Stock danger slack (`LEAD_DANGER_FACTOR = 0.75`) would not bind until ~76 m even with the *true* 80 km/h lead. Mapd hazards (`stop` / `give_way` / `T-Junction`) **yield to any lead**, so a far lock also kills the map approach profile.

The hole to fill: first far lock → nothing until ~65–75 m.

This drive was aggressive. The hook is **relaxed-only**, so it would not have fired on that commute until the driver flipped personality. That is the sandbox. Replay must prove aggressive is bit-identical.

---

## 3. What to build

A **hot-signal pre-brake** (originally "first-lock", amended 2026-08-27 after a real miss — see §4): when a lead has been continuously present and range-rate says we are closing fast (traffic-light queue, slow pack, stopped cars) while still far away, start decelerating immediately.

Injection: **new extra `min()` candidate**, same family as hook 2. Add a second shim next to `candidates += grt_hooks.extra_accel_candidates(v_ego)` in `longitudinal_planner.py`. Do not fold this into hook 2. Do not lower `v_cruise` (hook 1): cruise saturates at `A_CRUISE_MIN = -1.2` and hook 5 may soften plain overspeed.

Positive shape:

```
candidates += grt_hooks.far_lead_candidates(sm, v_ego)
```

Return `[]` when inert. When armed, return one tuple `(a, source, should_stop)` with `a <= -0.40`. Use `LongitudinalPlanSource.lead0` as the source so logs show the branch. `should_stop` from `drive_helpers.should_stop` as hook 2 does.

`min()` is the handoff: once MPC or e2e is more negative, they win. This hook only owns 115 m → ~75 m.

---

## 4. Arming (all must hold)

**AMENDED 2026-08-27, v2.** v1 shipped with a rising-edge design (absent ≥ 2.0 s, then present
≥ 0.30 s) and a "distance at first lock" gate (> 100 m). On a real drive that day, a lead
closing at up to ~3.0 m/s² of `a_req` — ten times the arming threshold, sustained for several
seconds — was preceded by only a 0.20 s gap, not 2.0 s. The rising-edge requirement latched
false for that entire presence run and the hook could never arm, no matter how hot the danger
signal got. Root-caused, quantified at ~3.15 s of denied armed time (driver had to intervene
manually), and fixed by removing the rising edge entirely — see `far_lead.py`'s module
docstring, "THIRD BUG", for the full writeup and the replay evidence that the absence gate was
never actually doing the flicker-rejection job it looked like it was doing (§4.3 below already
covers that).

1. `selfdriveState.personality == relaxed`. Any other personality → `[]`, drop latch.
2. `carControl.longActive`. Not engaged → drop latch, `[]`.
3. `radarState.leadOne.present` for **≥ 0.30 s continuously** (same idea as hook 10 `T_HOLD`;
   kills the 10:48-style flicker). No absence precondition — see amendment note above.
4. Once continuous presence and a hot closing signal (item 6) have both held long enough,
   capture `dRel_at_hot_start`: the dRel at the **first frame the hot signal (item 6) went
   true**, not at first presence. This must be `> 80` (`ARM_MIN_DIST`, lowered from 100 —
   anchoring later means the anchor distance is naturally smaller for the same encounter).
   Captured once and frozen for the rest of this presence run; a fully-stopped lead first
   detected already inside ~87 m will never satisfy this (known limitation, same failure class
   v1 had at 100 m — outside this hook's declared envelope, see `far_lead.py` docstring).
5. Closing from **range-rate**, not from vision `vRel`. Vision `vRel` at 10:49:43 was −0.77 m/s
   and would miss the event.
6. Hot enough: `a_req > 0.30 m/s²`, where
   `a_req = (v_ego² − v_lead_range²) / (2 · max(dRel − 6, 1))`
   with `v_lead_range = v_ego + vRel_range` and `vRel_range = min(lead.vRel, vRel_from_dRel_filter)` whenever both are closing (pessimistic). Equivalent TTC ≲ 15 s at highway speed is the same cut: 110 vs 80 at 120 m fires (`a_req ≈ 0.31`); 110 vs 100 at 150 m does not (`a_req ≈ 0.14`).
   Must hold **continuously for 0.5 s** (`HOT_PERSIST_S`) before arming.

Do not require a traffic light, OSM tag, `leadTwo`, or `hardBrakePredicted`. Optional one-of confirms are allowed later; they are not in v1 or v2.

---

## 5. Range-rate filter

A raw `ΔdRel/Δt` on this log spikes −55 to +53 m/s. Do not feed that to control.

Implement a small `[x, v]` (or `[x, v, a]`) filter on `leadOne.dRel` for this hook only. Measurement is position. Do not copy radar `KF1D` in `radard.py` — that filter measures Doppler **speed**, not range.

Constraints:

- Memory of **several seconds**, not a 1 s window. At first lock `xStd` was 7–12 m; 1 s of true close (~8 m) is one σ of position noise.
- Inflate trust in vision `vRel`: published `vStd` was 1.7–2.5 m/s while the actual error vs range-rate was ~6 m/s.
- `vRel_used = min(v_model, v_from_filter)` when both closing.
- Filter is for **this hook’s gate and `a_req` only**. Do not write it into `long_mpc.process_lead` in this change.

---

## 6. Command while latched

Do not assume the lead is stopped unless range-rate says so. `v_target = 0` at 115 m / 110 km/h is an emergency profile (~4.3 m/s²), not a comfort bleed.

Every armed frame:

```
a = clip( −a_req, −1.2, −0.40 )
```

with `a_req` from §4 using `v_lead_range`.

Two modes fall out of the same formula:

- Slow pack (10:49, ~80 km/h at 115 m) → about **−0.40** (the floor).
- Nearly stopped (`v_lead_range ≲ 20 km/h`) or TTC ≲ 6 s → formula self-escalates toward −1.2. Same idea as mapd hazard late-appearance.

Floor **−0.40**, not −0.15. After this works, speed drops while set speed stays 110, hook 10 C (`ABANDON = -0.20`, `MIN_HEADROOM = 5 km/h`) would raise a milder coast to 0. Hazard already lives in `[-1.5, -0.3]` for that reason. Do not special-case hook 10; clear `ABANDON` instead.

Release the latch when any of: personality not relaxed; not `longActive`; lead lost > 1 s; range-rate no longer closing; the stock MPC/e2e candidate has itself reached `<= -0.40` (i.e. stock has caught up to this hook's own floor — return the hook's tuple on this same frame too, `min()` picks whichever is harder, then drop the latch); `dRel < 20` as an absolute backstop regardless of what stock is doing; driver gas or brake. Do not re-arm on the same appearance.

Do NOT release on a bare `dRel < 50` distance cutoff. Checked against the 10:49 log: at dRel=50.24 m stock `aTarget` was still −0.298 (weaker than this hook's own −0.40 floor), only crossing −0.40 at dRel≈50.08 m. A hard release at 50 m lands inside that gap and can make the commanded accel step from −0.40 back up to −0.30 for a frame or two at the tightest part of the approach — the one failure mode where this hook would make things worse, not merely unhelpful. Gating release on stock's own value avoids it: the hook keeps supplying its floor for exactly as long as stock hasn't matched it yet, and `min()` — not a distance threshold — decides the handoff.

Rate-limit the candidate’s falling edge if needed so the first armed frame is not a −1.2 step on a noisy lock. Rising (release) may follow the formula immediately.

---

## 7. Personality and params

Relaxed is the switch. Do not add a Params key unless the existing GRT pattern for a brand-new behaviour requires one; if you add one, default **off** (same as `SmartCruiseControlMapHazardAccel`) and still require relaxed.

Aggressive and standard: hook is a no-op, no state leak, no log spam.

---

## 8. Safety / fork rules

- `try/except` in the shim; on failure return `[]` and `_log_exception`. Never raise into plannerd.
- Candidate can only make the `min()` more negative. Returning `[]` is the fail-safe.
- Clip to `[-1.2, -0.40]` while armed. Emergency stopping stays with MPC / the driver.
- No cereal schema changes. No scons. No copy of cereal files to the device.

---

## 9. Tests

Add `openpilot/grt/tests/test_far_lead.py` in the same stubbed style as `test_hooks.py`. Cover at least:

| case | expect |
|---|---|
| aggressive, 120 m, true vRel −8 m/s | `[]` |
| relaxed, lead present 1 frame at 120 m | `[]` (no 0.30 s persistence) |
| relaxed, flicker then gone (10:48 style) | `[]` |
| relaxed, 120 m, vRel_model −0.8, range-rate −8, after 0.30 s continuous presence + hot | one candidate, `a <= -0.40` |
| same, 110 vs 100 at 150 m (`a_req ≈ 0.14`) | `[]` |
| same, 110 vs 0 at 120 m | candidate, `a` harder than the slow-pack case, ≥ −1.2, `dRel_at_hot_start` clear of `ARM_MIN_DIST` (80 m) with margin |
| KNOWN LIMIT: 110 vs 0, lead first seen already at 86 m | never arms (`dRel_at_hot_start` freezes below `ARM_MIN_DIST` on the first hot frame; same failure class v1 had at 100 m) |
| relaxed, lead present+steady (non-closing) for several seconds, then starts closing hard | arms once the hot signal starts, `dRel_at_hot_start` anchored at that later instant — not at first presence |
| once armed, dRel falls under `ARM_MIN_DIST` | still armed (the arm-distance check is one-time, at hot-start) |
| armed, stock candidate reaches -0.40 while dRel still > 20 m (e.g. 50 m, per the 10:49 log) | this frame still returns the candidate, `min()` picks the harder one; latch drops after |
| armed, stock candidate stuck near 0 (bad `vRel`) all the way to dRel = 20 m | still returns candidate down to the 20 m backstop |
| dRel < 20 m | `[]`, latch cleared regardless of stock |
| not longActive | `[]`, latch cleared |
| exception inside the module | shim returns `[]`, does not raise |
| candidate `a > -0.20` | never (hook 10 C would eat it) |

Run: `python3 openpilot/grt/tests/test_far_lead.py` and existing `openpilot/grt/tests/test_hooks.py`.

---

## 10. Replay bar (this log)

Against `d012.zst`/`d013.zst`, 10:49:43–10:49:52, engaged frames:

- **Aggressive (as logged):** `aTarget` and `longitudinalPlanSource` match pre-change through 10:49:51. A 10:49:43–51 mean `aTarget` still ~0. No new source `lead0` before ~64 m.
- **Relaxed (synthetic personality override in the replay, or a unit-level kinematic replay of the logged `dRel`/`vEgo` series):** `aTarget <= -0.40` by the time dRel is still **> 80 m** (`ARM_MIN_DIST`, v2; target: at or shortly after the 0.30 s persist + 0.5 s hot, ~10:49:44.1, dRel ~118 — this specific log's hot streak starts essentially at first lock, so v1's and v2's arm points coincide here). By ~65 m, stock lead/e2e may be more negative and win `min()` — that is success, not a bug.

If you cannot run acados, a kinematic replay of the hook on the logged series is enough for this bar. Do not claim on-car proof.

**Second replay bar, added 2026-08-27** (route 00000139, ~07:55 incident, `.venv` pycapnp, kinematic replay): v1 (deployed) never arms — root cause in §4's amendment note. v2 arms at t+52.77 s, dRel=93.6 m, releases at t+56.77 s, dRel=37.0 m (stock caught up). Re-ran the original 08-25 log's relaxed-override case against v2 to confirm no regression: still arms at dRel=115.0 m, releases at dRel=50.1 m. Single arm/release cycle on both logs — no re-arm chatter.

---

## 11. Docs

- One block in `openpilot/grt/hooks.py` module docstring, same tone as hooks 2 and 7: what it is, relaxed-only, `min()` candidate, floor −0.40 because of hook 10 C, measured hole 10:49.
- One GRT-MOD comment at the planner call site.
- Do not write a new design markdown. This prompt is the spec.

---

## 12. Done when

1. New module + shim + planner one-liner exist.
2. Relaxed first-lock at > 100 m with range-rate close produces `a <= -0.40` while dRel is still > 100 m.
3. Aggressive path unchanged.
4. Stub tests in §9 pass, including the hook-10-C floor.
5. Replay bar in §10 is reported with numbers, not vibes.
6. `long_mpc.py` cost weights untouched. `radard.py` / `process_lead` untouched.

Out of scope for this change: feeding range-rate into MPC, raising `LEAD_DANGER_FACTOR`, stopping mapd from yielding to far leads (a later one-line experiment), a Params UI toggle unless you need default-off.
