<!--
  Lead-vehicle dash icon on a PREBUILT openpilot fork — SCOPE + PLAN, not yet implemented.

  Written 2026-08-05 after a research session, then updated the same day once Tier 1 was coded.
  Follows the conventions of PORT_MAPD_FROM_SUNNYPILOT.md: read that doc's §0-§4 for the general
  rules of this branch before touching anything here — they are not repeated in full below, only
  the parts specific to this feature. §9 of that doc ("reusable recipe") is the checklist this
  plan was built against.

  Status: **Tier 1 IMPLEMENTED locally (offline block only) — NOT YET ON THE DEVICE.**
  Advisor reviewed §3's DBC findings and two follow-up checks (§4a) before the edit landed;
  both cleared, see §4's Tier 1 section. Offline gates (syntax parse, real import of the
  modified module) pass. Packer-level and on-device verification (§6) are outstanding — same
  class of gate as the mapd port's Verification 1, which this branch's Pi5 cannot run (no
  scons/cmake/capnproto toolchain). Tier 2 is unstarted by design (see §4a).
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

### Tier 2 — real distance/speed, full parity with sunnypilot — NOT STARTED (see Tier 1's advisor note above)

Needs actual `dRel`/`vRel`, which `hudControl` doesn't carry. Confirmed clean, minimal-blast-radius
path (all verified against this branch's actual code, not assumed from sunnypilot's architecture):

1. **`selfdrive/car/card.py`**: add `'radarState'` to the existing `SubMaster` (`card.py:72`, next to
   the current `GRT_SUB_CARD` list). This needs **no ignore lists** — `radarState` is a stock,
   always-alive service, not a fork one. Doubly safe because card's own checks are hardcoded scoped
   to `['carControl']` only:
   ```
   card.py:221: co_send.valid = self.sm.all_checks(['carControl'])
   card.py:257: if self.sm.all_alive(['carControl']):
   ```
   confirmed by grepping the file as it stands today (the mapd doc's §2.4 table said this; re-checked
   directly per that doc's own instruction to never trust the table without re-grepping).
2. **`selfdrive/car/card.py`**: right before the existing `self.CI.apply(CC, now_nanos)` call
   (`card.py:260`), stash `(dRel, vRel, present)` from `sm['radarState'].leadOne` onto
   `self.CI.CS` as a fork-only attribute, e.g. `self.CI.CS._grt_lead = (...)`. This works because
   `CarControllerBase.apply()` forwards `self.CS` unchanged into `CC.update(CC, self.CS, now_nanos)`
   (`opendbc/car/interfaces.py:117`), and `self.CI.CS` is already reached this exact way elsewhere in
   `card.py` (line 143, `secoc_key`) — **no signature change to `interfaces.py` or any other car
   brand.** The attribute is Hyundai-only, read with `getattr(CS, '_grt_lead', None)`.
3. **`opendbc/car/hyundai/carcontroller.py`**: port sunnypilot's hysteresis debounce
   (`opendbc/sunnypilot/car/hyundai/lead_data_ext.py`'s `_hysteresis_update`, ~15 lines) as a plain
   method — **no mixin class needed**: this branch's `CarController` is the single-class stock
   version (confirmed: no `MadsCarController`/`EsccCarController`/`LongitudinalController` mixins
   present), so none of sunnypilot's MRO machinery applies here.
4. **`opendbc/car/hyundai/hyundaicanfd.py`**: extend `create_acc_control()` to pack `ACC_ObjDist`
   (0.1 m/count, range 0–204.7 m) and `ACC_ObjRelSpd` (0.1 m/s/count, −16.4 offset, range
   −16.4–34.7 m/s) from the debounced lead state.

**§2.3 trap, called out explicitly because it bit the mapd port exactly this way**: mainline's
`radarState.LeadData` field is `present`, **not** `status` — sunnypilot's own field name. Any stub
or test written against this must use `present`; `openpilot/grt/tests/test_schema_conformance.py`
must assert this against the real `log.capnp` before any code that reads it ships, per the existing
pattern in that file.

## 5. Files touched (both tiers)

| File | Change | Category (GRT_MODS.md scheme) |
|---|---|---|
| `opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py` | `create_acc_control()`: `SCC_ObjSta` (Tier 1) + `ACC_ObjDist`/`ACC_ObjRelSpd` (Tier 2) from lead state instead of hardcoded constants | C |
| `opendbc_repo/opendbc/car/hyundai/carcontroller.py` | Tier 2 only: hysteresis state + read `CS._grt_lead`, pass to `create_acc_control()` | C |
| `openpilot/selfdrive/car/card.py` | Tier 2 only: add `'radarState'` to SubMaster; stash lead tuple onto `self.CI.CS` before `apply()`, `# GRT-MOD` sentinel per existing convention | C |
| `openpilot/grt/tests/test_schema_conformance.py` | Tier 2 only: assert `radarState.leadOne.dRel`/`vRel`/`present` against the real `log.capnp` | A (fork-owned test) |

No category D (schema) rows at all — see §1. Roughly 3-4 files touched, well under the mapd port's
footprint, because there's no service/registration layer to build.

Note `opendbc_repo/` is vendored (plain files, not a submodule per prior fork research), and it is
pure Python — edits there take effect immediately on this prebuilt branch with no rebuild, same as
every other Python change on this branch (§1.1 of the mapd doc).

## 6. Verification order (mirrors the mapd/set-speed runbooks in `PROGRESS.md`)

Offline (Pi5), before any device step:
1. `test_schema_conformance.py` — new assertions on `radarState.leadOne.dRel`/`vRel`/`present` pass
   against the real `log.capnp`; deliberately verify it *fails* on `status` first, per the mapd
   port's own lesson, before trusting it.
2. Real-import gate: `opendbc.car.hyundai.carcontroller` and `hyundaicanfd` still import cleanly with
   the new code; `CarController()` constructs.
3. Unit test for the hysteresis debounce (mirrors `lead_data_ext.py`'s own logic, ported not copied).

On-device, offroad first (car powered, supervised, per this branch's standing rule — never
unattended):
1. **Engagement still works.** Lowest-risk item here (card's checks are scoped, no ignore lists
   needed at all for `radarState`), but verify it anyway before anything else, per house convention.
2. `carState.cumLagMs` vs. a pre-change segment — card is the 100 Hz CAN loop; Tier 2 adds a
   subscriber (no new publisher this time, unlike the set-speed feature).
3. Confirm via `candump`/whatever CAN tooling is available that `SCC_CONTROL.SCC_ObjSta` actually
   varies with `hudControl.leadVisible` while stationary with a target in front (e.g. wall/car in a
   driveway) — don't wait for a drive to learn the two-line diff has a typo.
4. **Road test — this is what actually answers the open question in §4's Tier 1 caveat.** Drive
   behind traffic and watch the cluster: does the lead icon light, and does it look right (steady,
   not flickering, positioned sensibly)? This is the one thing that can't be determined from DBC
   inspection or unit tests — the DBC confirms `SCC_ObjSta` is *routed* to the cluster, not what the
   cluster's firmware *does* with each of its three documented values.

## 7. Status

- **Advisor review of §3's DBC findings and the Tier 1 implementation: DONE (2026-08-05)**, both
  rounds — see §4's Tier 1 section for the two follow-up checks and their resolution.
- **Tier 1 is coded, offline-gated (syntax + real-import pass), not yet on the device.**
- Tier 1 vs. Tier 2 as separate deploys, deliberately: Tier 2 is unstarted until Tier 1 is driven —
  §4 flags Tier 1 as an unproven probe, and a road-test surprise with both changes present
  wouldn't be attributable to either one.
- No captains_log/PROGRESS.md conflicts found — grepped for prior work on this signal/feature before
  writing this doc; none exists. This is new ground, not a rework.
- **Next step is the on-device block (§6)** — requires the car powered and supervised; do not run
  unattended, per this branch's standing rule. Not started as of this doc's last edit.
