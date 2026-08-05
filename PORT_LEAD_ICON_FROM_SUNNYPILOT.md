<!--
  Lead-vehicle dash icon on a PREBUILT openpilot fork — SCOPE + PLAN, not yet implemented.

  Written 2026-08-05 after a research session, then updated the same day once Tier 1 was coded.
  Follows the conventions of PORT_MAPD_FROM_SUNNYPILOT.md: read that doc's §0-§4 for the general
  rules of this branch before touching anything here — they are not repeated in full below, only
  the parts specific to this feature. §9 of that doc ("reusable recipe") is the checklist this
  plan was built against.

  Status: **BOTH TIERS DONE AND ROAD-TESTED.** Tier 1: operator confirmed the icon appears and
  looks sane. Tier 2: operator confirmed the distance/speed reading "looks plausible — steady,
  roughly matches what I expect for the gap," with one open item — a little jitter, flagged for
  more drive-time observation before deciding whether it needs fixing (see §7 and captains_log
  2026-08-05 "ROAD TEST" entry). No code changes pending. Advisor reviewed Tier 1's plan (twice);
  Tier 2's design did not get an advisor pass (overloaded both attempts) — deployed anyway on an
  informed operator call, backed by full offline + offroad verification (see captains_log for the
  complete record: schema conformance, 5 behavioural cases, live CAN capture confirming exact
  field-level correctness including the `ACC_ObjRelSpd` omission on real hardware).
-->

# Lead-vehicle dash icon — porting sunnypilot's behaviour into `nightly-dev`

Repo: **`/home/pi5-ubuntu/Comma/openpilot/nightly-dev`** (openpilot source under `nightly-dev/openpilot/`),
fork `github.com/GrtBr/openpilot`, branch **`nightly-dev`**, base **openpilot v0.11.2**.
Reference implementation (read-only): **`/home/pi5-ubuntu/Comma/sunnypilot`**.
Target: **comma 4**, Hyundai Staria 4th gen, CANFD (`HYUNDAI_STARIA_4TH_GEN`, no `UNSUPPORTED_LONGITUDINAL`/`CANFD_CAMERA_SCC`/`LEGACY` flags).

Current device state (from `captains_log.md`, 2026-08-04 SYNC entry): device at `005d003592`,
healthy, `openpilotLongitudinalControl=True`, `pcmCruise=False`, `radarUnavailable=True` — openpilot
owns longitudinal control and is the sole author of the CAN message this feature touches (see §2).

## 0. What this feature is

sunnypilot's Hyundai `CarController` fills the dash's lead-vehicle icon/distance readout from real
model+radar lead data. Mainline openpilot (this branch's base) sends the same CAN message but with
the lead fields **hardcoded static**, so the Staria's cluster never shows a dynamic lead icon today.
Full research trail (DBC pull, cross-process data path, prior sunnypilot-vs-mainline diff) is in the
originating chat session; this doc distills it into an actionable, verifiable plan.

## 1. Why this port is smaller than the mapd port

The mapd port needed a brand-new cross-process cereal message because nothing already carried its
data anywhere (§4.3 of the mapd doc: rename a `CustomReserved` slot, splice `services.py`, wire a
`PubMaster`/`SubMaster` pair). **This feature needs none of that.**

- The data source, `radarState`, is a **stock, already-compiled service** (`cereal/services.py:34`,
  `"radarState": (True, 20., 5)`), published continuously by the stock `radard` process
  (`system/manager/process_config.py:113`, always runs onroad). It is not fork-owned and needs no
  schema change.
- **Zero `custom.capnp` / `log.capnp` / `services.py` edits.** That means the one hard rule in this
  repo's `CLAUDE.md` — *never SCP `cereal/custom.capnp`, `cereal/log.capnp`, or `cereal/services.py`
  from the Pi5 to comma4, because the device's compiled pycapnp schema cache will mismatch and crash
  `manager.py` on boot* — **cannot be triggered by this feature at all.** Worth stating explicitly:
  this is the first fork feature in this repo's history that touches zero cereal files.
- No new `CustomReserved` slot is spent. All 20 are still free after this (mapd used 17/18/19, the
  set-speed alert channel used 16 — this feature needs none).

## 2. Confirmed: the Staria's `SCC_CONTROL` is authored by openpilot, not a live factory ECU

Two independent confirmations (advisor flagged this as a precondition worth checking before
committing to the plan):

1. **Code path.** `opendbc/car/hyundai/carcontroller.py:196-202`: `create_acc_control()` (which
   builds `SCC_CONTROL`) and `create_adrv_messages()` (whose own function comment says it exists to
   *"keep the car happy after disabling the ADAS Driving ECU to do longitudinal control"*) both only
   fire `if self.CP.openpilotLongitudinalControl`. Nothing in `HYUNDAI_STARIA_4TH_GEN`'s flags
   (`opendbc/car/hyundai/values.py:327`) statically blocks this.
2. **On-device fact**, already recorded in `captains_log.md:310-311,1319` from a prior session:
   `openpilotLongitudinalControl=True`, `pcmCruise=False` on the Staria — confirmed live on the car,
   not inferred.

So the factory ADAS Driving ECU is disabled and openpilot is the sole author of `SCC_CONTROL` today.
A code change here reaches the cluster; it isn't competing with a live stock message.

## 3. `SCC_CONTROL` DBC — what's actually documented vs. guessed

Pulled from `opendbc_repo/opendbc/dbc/generator/hyundai/hyundai_canfd.dbc:374-407` (message ID 416,
32 bytes; the generated `hyundai_canfd_generated.dbc` this compiles to isn't checked in, but the
generator source is authoritative and unambiguous):

| Signal | Bits | Scale, offset | Range | Receivers |
|---|---|---|---|---|
| `ACC_ObjDist` | 24\|11 | 0.1, 0 | 0–204.7 m | `XXX` (unconfirmed) |
| `ACC_ObjRelSpd` | 35\|9 | 0.1, −16.4 | −16.4–34.7 m/s | `XXX` |
| `ObjValid` | 46\|1 | 1, 0 | 0–1 | `XXX` |
| **`SCC_ObjSta`** | 108\|3 | 1, 0 | 0–7 | **`CLU,CGW`** |
| `OBJ_STATUS` | 176\|3 | 1, 0 | 0–7 | `XXX` |

**`SCC_ObjSta` is the only signal in this message with a documented comment and value table:**
```
CM_ SG_ 416 SCC_ObjSta "state of in-path object and information of controllable state.";
VAL_ 416 SCC_ObjSta 0 "No in-path object detected"
                    1 "In-path object detected (uncontrollable)"
                    2 "In-path object detected (controllable:longitudinal)";
```
It is also the **only** signal explicitly receiver-tagged `CLU` (cluster) + `CGW` (gateway) — every
other signal in this message shows unconfirmed `XXX`. **This is DBC-level evidence, not an assumption
carried over from sunnypilot's code, that `SCC_ObjSta` is what the cluster actually reads.**

This resolves a real risk raised in review: sunnypilot's CANFD path negates `ObjValid`
(`int(not lead_visible)`) while its non-CANFD path does not (`int(lead_visible)`), and mainline
hardcodes two different constants for it between the two message families (`0` on CANFD, `1` on
non-CANFD) — for a signal whose CANFD destination isn't even confirmed. `ObjValid`'s "correct"
polarity for the cluster is genuinely undocumented. **Rather than guess it, this plan avoids the
signal entirely and targets `SCC_ObjSta`, whose semantics are unambiguous from the DBC alone.**

Contrast with the older (non-CANFD) DBC family, for context only — not this car:
`hyundai_can.dbc`/`hyundai_palisade_2023.dbc` tag `ObjValid` as `CLU,ESC,TCU` (genuinely
cluster-bound there) while `ACC_ObjDist`/`ACC_ObjRelSpd` go to `ABS,ESC` only. Different DBC,
different signal roles — these are not interchangeable assumptions across platforms.

## 4. Design — two tiers

### Tier 1 — icon on/off via `SCC_ObjSta`, no new plumbing — IMPLEMENTED

**Advisor review (2026-08-05), two checks before coding, both cleared:**

1. **Is `SCC_ObjSta` already packed by a second `SCC_CONTROL` builder?** `hyundaicanfd.py` has two:
   `create_acc_control()` (line ~127) and `create_acc_cancel()` (line ~86, whose own comment says
   it exists to preserve fields "verbatim from the previous SCC_CONTROL frame" for camera-SCC
   cancel handling). Checked the call sites in `carcontroller.py:196-217`: `create_acc_control` is
   called only when `self.CP.openpilotLongitudinalControl`; `create_acc_cancel` only in the `else`
   branch, i.e. only when that flag is `False`. The Staria is confirmed `True` (§2), so exactly one
   `SCC_CONTROL` builder is ever active for this car — no double-packing, no alternating-value risk.
2. **Is `gas_override` the right value for the 1-vs-2 distinction?** `carcontroller.py:202` passes
   `CC.cruiseControl.override` (driver-on-the-pedal) into that argument — matches the DBC's
   documented "uncontrollable" semantics for `SCC_ObjSta=1` exactly.

`CC.hudControl.leadVisible` is already set every frame by stock `controlsd.py:169`
(`hudControl.leadVisible = longitudinalPlan.hasLead`) and already reaches Hyundai's
`CarController.update()` as part of `CC` — **zero new subscriptions, zero signature changes.**

In `opendbc/car/hyundai/hyundaicanfd.py`, `create_acc_control()`, add to the `values` dict (mirrors
sunnypilot's formula, now independently corroborated by the DBC's value table rather than trusted on
sunnypilot's say-so):
```python
"SCC_ObjSta": 0 if not (enabled and hud_control.leadVisible) else (1 if gas_override else 2),
```
`enabled`, `hud_control`, and `gas_override` are already parameters of this function. Leave
`ObjValid`/`OBJ_STATUS`/`ACC_ObjDist` at mainline's current hardcoded values for this tier — their
cluster relevance is unconfirmed by the DBC, so changing them adds risk without a confirmed payoff.

**This is a probe, not a guaranteed full win** (per advisor review) — no reference implementation
ships an icon-only variant; sunnypilot always drives `ObjValid`, `SCC_ObjSta`, and `ACC_ObjDist`
together from one debounced source. If the cluster's glyph positioning or animation depends on
`ACC_ObjDist` moving too, Tier 1 alone may show a static or oddly-parked icon rather than nothing —
that's a real possible outcome, not a defect in the plan, and it's exactly what the road test in §6
is for.

**Advisor's implementation instruction, followed deliberately:** write Tier 1 only — the one dict
entry — and stop. Do not build Tier 2 in the same pass: Tier 1 is an unproven probe (§4 caveat
above) whose payoff is decided by a road test, and building both means a road-test surprise can't
be attributed to either change. Tier 2 stays unstarted until Tier 1 is driven.

The edit landed in `opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py`, `create_acc_control()`,
wrapped in `# GRT-MOD-START/END` per this repo's convention, row added to `GRT_MODS.md`:
```python
"SCC_ObjSta": 0 if not (enabled and hud_control.leadVisible) else (1 if gas_override else 2),
```

### Tier 2 — real distance/speed — IMPLEMENTED, offline-verified, NOT YET DEPLOYED

Needs actual `dRel`/`vRel`, which `hudControl` doesn't carry. Implemented path (all verified against
this branch's actual code as it stands after Tier 1, not assumed from sunnypilot's architecture):

1. **`selfdrive/car/card.py`**: added `'radarState'` to the existing `SubMaster` (base list, not
   `GRT_SUB_CARD`). Needs **no ignore lists** — `radarState` is a stock, always-alive service, not a
   fork one. Doubly safe because card's own checks are hardcoded scoped to `['carControl']` only:
   ```
   card.py: co_send.valid = self.sm.all_checks(['carControl'])
   card.py: if self.sm.all_alive(['carControl']):
   ```
   re-grepped directly against the file as it stands, per house convention (never trust a prior
   table without re-checking).
2. **`selfdrive/car/card.py`**: right before the existing `self.CI.apply(CC, now_nanos)` call,
   stash `(dRel, vRel, present)` from `sm['radarState'].leadOne` onto `self.CI.CS._grt_lead`.
   Works because `CarControllerBase.apply()` forwards `self.CS` unchanged into
   `CC.update(CC, self.CS, now_nanos)` (`opendbc/car/interfaces.py`), and `self.CI.CS` is already
   reached this exact way elsewhere in `card.py` (the `secoc_key` line) — **no signature change to
   `interfaces.py` or any other car brand.** Read with `getattr(CS, '_grt_lead', None)`.
3. **Design change from the original sketch — no separate hysteresis class was ported.**
   sunnypilot's `LeadDataCarController` debounces *presence* with a 50-frame hysteresis before
   deciding whether to show a lead at all. This branch already has a working, road-tested presence
   signal from Tier 1: `hud_control.leadVisible`, which itself derives from
   `radarState.leadOne.present` one hop upstream (`longitudinal_planner.py`:
   `longitudinalPlan.hasLead = sm['radarState'].leadOne.present`). Porting a second, independent
   hysteresis on `radarState.leadOne.present` directly would risk `SCC_ObjSta` (gated on
   `hud_control.leadVisible`) and `ACC_ObjDist`/`ACC_ObjRelSpd` (which would be gated on the new
   hysteresis) disagreeing frame-to-frame — icon on, no number, or vice versa. Instead: gate the
   numeric fields on **both** `hud_control.leadVisible` AND `lead[2]` (`radarState.leadOne.present`
   from card's own, slightly fresher read) **agreeing**. When they don't — a real but narrow window,
   since the two views are on independent update cycles — the fail-safe direction is "no number",
   per the same rule the mapd port's 60 km/h fallback removal established: never command a value the
   data didn't actually, currently supply.
4. **`opendbc/car/hyundai/hyundaicanfd.py`**: `create_acc_control()` gained an optional `lead=None`
   parameter. Packs `ACC_ObjDist` (clipped to the DBC's 0–204.7 m) and `ACC_ObjRelSpd` (clipped to
   −16.4–34.7 m/s) only when the tier-3 gate above is true. **`ACC_ObjRelSpd` is omitted from the
   dict entirely** (not set to `0`) when no lead is shown — mainline never packed it either, and its
   DBC receiver is unconfirmed (§3); writing an explicit `0` physical value would be a real
   behaviour change on a signal that might feed something else, versus the packer's prior default
   (unset → raw 0 → −16.4 m/s physical, unchanged from before this port).
5. **`opendbc/car/hyundai/carcontroller.py`**: the `create_acc_control()` call site passes
   `lead=getattr(CS, '_grt_lead', None)` — `CS` already reaches this call unchanged, no mixin class
   needed (this branch's `CarController` is single-class stock, no MRO machinery to navigate).

**Offline verification (Pi5, comma4 offline):**
- `ast.parse` on all 3 modified files: clean.
- `openpilot/grt/tests/test_schema_conformance.py`: **30/30**, including the new
  `radarState.leadOne.vRel` assertion (`.dRel`/`.present` were already covered from the mapd port).
- Real import of the `opendbc`-side files (`uv run --no-project --with numpy`) succeeds.
  `card.py`'s own import could not be exercised — needs the on-device path layout the Pi5 doesn't
  replicate (`opendbc`/`cereal` resource resolution), a pre-existing limitation from every prior
  port on this branch, not something new here.
- **5 behavioural cases run against the real `create_acc_control()` logic via a stub packer**
  (captures the `values` dict instead of encoding real CAN bytes, since the compiled `CANPacker`
  needs a build the Pi5 can't do): no-lead parity with pre-Tier-2 behaviour (`ACC_ObjDist=1`,
  `ACC_ObjRelSpd` absent), lead-shown values pack correctly, source disagreement falls back to
  no-number, out-of-range `dRel`/`vRel` get clipped rather than passed through raw, `gas_override`
  still selects `SCC_ObjSta=1` with a lead shown. All pass.

**§2.3 trap, checked explicitly because it bit the mapd port exactly this way**: mainline's
`radarState.LeadData` field is `present`, **not** `status` — sunnypilot's own field name. Re-grepped
`log.capnp` directly before writing any code that reads it (`dRel @0`, `vRel @2`, `present @11` —
confirmed on this repo's actual schema, not assumed from memory of an earlier, different repo).

## 5. Files touched (both tiers)

| File | Change | Category (GRT_MODS.md scheme) |
|---|---|---|
| `opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py` | `create_acc_control()`: `SCC_ObjSta` (Tier 1, deployed) + `ACC_ObjDist`/`ACC_ObjRelSpd` via new `lead=None` param (Tier 2, implemented) | C |
| `opendbc_repo/opendbc/car/hyundai/carcontroller.py` | Tier 2: pass `lead=getattr(CS, '_grt_lead', None)` at the `create_acc_control()` call site — no hysteresis class, reuses Tier 1's `hud_control.leadVisible` gate (§4) | C |
| `openpilot/selfdrive/car/card.py` | Tier 2: add `'radarState'` to the base `SubMaster` list (not `GRT_SUB_CARD` — stock service); stash `(dRel, vRel, present)` onto `self.CI.CS._grt_lead` before `apply()`, `# GRT-MOD` sentinel | C |
| `openpilot/grt/tests/test_schema_conformance.py` | Tier 2: added `radarState.leadOne.vRel` assertion (`.dRel`/`.present` were already covered from the mapd port) — 30/30 pass | A (fork-owned test) |

No category D (schema) rows at all — see §1. Roughly 3-4 files touched, well under the mapd port's
footprint, because there's no service/registration layer to build.

Note `opendbc_repo/` is vendored (plain files, not a submodule per prior fork research), and it is
pure Python — edits there take effect immediately on this prebuilt branch with no rebuild, same as
every other Python change on this branch (§1.1 of the mapd doc).

## 6. Verification order (mirrors the mapd/set-speed runbooks in `PROGRESS.md`)

Offline (Pi5), before any device step:
1. `test_schema_conformance.py` — DONE, 30/30 including the new `radarState.leadOne.vRel`
   assertion, against the real `log.capnp`.
2. Real-import gate — DONE for the `opendbc`-side files (`hyundaicanfd`, `carcontroller`).
   `card.py`'s own import could not be exercised on the Pi5 (needs the on-device path layout;
   pre-existing limitation).
3. Behavioural verification of the packing logic — DONE, 5 cases via a stub packer (§4's Tier 2
   section has the full list): no-lead parity, lead-shown values, fail-safe on source
   disagreement, clipping, gas-override interaction.

On-device, offroad first (car powered, supervised, per this branch's standing rule — never
unattended):
1. **Engagement still works.** — DONE for both tiers. Tier 2 (2026-08-05, second deploy):
   `onroadEvents` = benign parked-car signature only, confirming the new `radarState` subscriber
   on `card` didn't trip anything, as predicted (card's checks are scoped to `carControl` only).
2. `carState.cumLagMs` vs. a pre-change segment — DONE for both. Tier 2 measured **28.45 ms**,
   DOWN from the Tier-1 baseline of 36.86 ms — no lag regression from the new subscriber.
3. Confirm via CAN capture that the packed fields decode correctly — DONE for both tiers.
   Tier 2's capture decoded all three touched signals at once: `SCC_ObjSta=0` (Tier 1 unaffected),
   `ACC_ObjDist=1.0m` (the pre-existing no-lead constant, correctly preserved), and
   `ACC_ObjRelSpd=-16.4 m/s` — the packer's default for an genuinely UNSET signal, confirming the
   omission-not-zero design actually holds on the real compiled packer, not just the offline stub
   test. The `1`/`2` `SCC_ObjSta` branches and the lead-shown `ACC_ObjDist`/`ACC_ObjRelSpd` values
   are NOT yet exercised — both need engagement with a real lead present.
4. **Road test — OUTSTANDING for Tier 2. This is the only remaining item.** Drive behind traffic
   and watch the cluster: does the lead icon light (Tier 1, already confirmed sane), and do the
   distance/speed numbers (Tier 2, new) look right — plausible values, not jumping around, not
   stuck? This is the one thing that can't be determined from DBC inspection, unit tests, or a
   parked CAN capture.

## 7. Status

- **Tier 1: DONE.** Deployed to comma4, all offroad checks passed, road-tested — operator confirmed
  the lead icon appears and looks sane. No further action needed.
- **Tier 2: DONE.** Deployed to comma4 (`a6e183a`), all offroad checks passed (engagement
  unblocked, zero exceptions, `cumLagMs` down not up, all three CAN fields decoding correctly
  including the `ACC_ObjRelSpd` omission on real hardware), road-tested — operator confirmed the
  distance/speed reading "looks plausible — steady, roughly matches what I expect for the gap."
  Advisor did not review Tier 2's design (overloaded both attempts this session, unlike Tier 1's
  two successful reviews) — deployed on an informed operator call backed by full offline +
  offroad + on-road verification instead.
- **One open item, deliberately not acted on**: operator noted "a little bit of jumping around" in
  the reading, wants more drive time before concluding anything — explicitly NOT asking for a fix
  yet. See captains_log 2026-08-05 "ROAD TEST" entry for the candidate follow-up (smoothing
  `dRel`/`vRel` themselves, not built) if further drives confirm it's a real, bothersome jitter
  rather than noise or a one-off.
- **Both tiers of this feature are complete.** No code changes pending on either.
- No captains_log/PROGRESS.md conflicts found for either tier — this remains new ground.
