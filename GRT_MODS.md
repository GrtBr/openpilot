# GRT_MODS — every in-place edit to upstream-owned files

This fork keeps its own code in **`openpilot/grt/`** (plus `third_party/mapd/`). Upstream files
carry only thin hooks. This table is the **sync checklist**: after rebasing onto a new upstream
openpilot release, re-verify each row.

Fast audit: `grep -rn "GRT-MOD" openpilot/ --include=*.py --include=*.h --include=*.inc --include=*.capnp`

Categories: **A** new fork-owned file (no conflict) · **B** registration splice (low risk) ·
**C** behavioural injection (HIGH risk — re-verify semantics, not just that it applies) ·
**D** schema (low risk by design, but wire-critical).

## In-place edits to upstream files

| File | Where | Cat | What / why |
|---|---|---|---|
| `openpilot/cereal/custom.capnp` | reserved slots 17/18/19 | D | Renamed to `MapdExtendedOut`/`MapdIn`/`MapdOut` **keeping struct IDs**; added mapd support structs + Mapd-prefixed enums. Wire-critical: IDs and field ordinals must never change. |
| `openpilot/cereal/log.capnp` | union members `@143/@144/@145` | D | Renamed to `mapdExtendedOut`/`mapdIn`/`mapdOut`. Ordinals unchanged; union **discriminants stay 141/142/143** — that is what the binary puts on the wire. |
| `openpilot/cereal/custom.capnp` | reserved slot 16 | D | Renamed to `GrtSetSpeedState` **keeping the struct ID**, with 5 fields added. Fork-owned card→selfdrived channel; nothing outside this repo consumes it. Speeds are **km/h**, unlike the mapd structs. |
| `openpilot/cereal/log.capnp` | union member `@142` | D | `customReserved16` → `grtSetSpeedState`. Ordinal unchanged, so the wire discriminant stays **140** and mapd's 141/142/143 do not move. Asserted by `grt/tests/test_schema_conformance.py`. |
| `openpilot/cereal/services.py` | end of `_services` dict | B | 3 inlined mapd service entries + `grtSetSpeedState` (20 Hz, SMALL, `should_log=False`). **Deliberately inlined, not imported** — services.py runs as a standalone script at build time (no repo root on `sys.path`), so an `openpilot.*` import here breaks the build. Queue size must stay `MEDIUM`. |
| `openpilot/common/params_keys.h` | last line of `keys` initializer | B | One `#include "openpilot/common/grt_params_keys.inc"` inside the braces, so new fork params never touch this file again. |
| `openpilot/system/manager/process_config.py` | after `procs` list | B | `procs += grt_procs()`. |
| `openpilot/selfdrive/controls/plannerd.py` | import block + `SubMaster(...)` | B | `+ GRT_SUB` on the service list (now `mapdOut` **and** `grtSetSpeedState`, the latter so `scc_map` obeys only limits the driver AUTHORISED), **plus `ignore_alive`/`ignore_valid`/`ignore_avg_freq` for both**. The ignores are NOT optional: mapd only runs when OSM tiles are installed, and without them a missing `mapdOut` makes `sm.all_checks()` False, marking `longitudinalPlan` INVALID and faulting longitudinal control on any device without tiles (verified on the car). Upstream occasionally adds entries to this list — expect a trivial re-resolve. |
| `openpilot/selfdrive/selfdrived/selfdrived.py` | import block; `ignore` list + `SubMaster(...)`; `update_alerts` | **C** | `+ GRT_SUB_SELFDRIVED` on both the service list **and the `ignore` list**, plus one line `self.AM.add_many(frame, grt_hooks.set_speed_alerts(...))` between the existing `add_many` and `process_alerts`. **The ignore entry is the most safety-critical in this table**: `sm.all_checks()` is called UNSCOPED at ~`:381` (raises `commIssue`) and ~`:469`, where it gates `self.initialized` — so a fork service with no publisher would BLOCK ENGAGEMENT ENTIRELY, not merely invalidate one message. The alert is a plain `Alert` with a fork-owned `alert_type` string; `AlertManager.add_many` keys on that, so no `EventName` enumerant and no schema change is needed. |
| `openpilot/selfdrive/selfdrived/selfdrived.py` | `not_running` set (~line 341) | **C** | Subtracts `GRT_IGNORED_PROCESSES` so mapd cannot raise `processNotRunning` and block engagement. Adapted, **not** copied from sunnypilot — this openpilot version has no `self.ignored_processes`. If upstream restructures this block, re-apply by meaning, not by patch. |
| `openpilot/selfdrive/car/card.py` | import block; `SubMaster(...)`; `PubMaster(...)`; after `initialize_v_cruise`; `state_publish` | **C** | `+ GRT_SUB_CARD` on the service list **with the same three ignore lists as plannerd**, `+ GRT_PUB_CARD` on the PubMaster, a `grt_engage_edge` local mirroring upstream's own engage condition, one hook line `grt_hooks.track_set_speed(...)`, and a 20 Hz `grtSetSpeedState` publish in `state_publish`. The hook MUST stay after `initialize_v_cruise` (which resets `v_cruise` on the engage edge) and before the `CS.vCruise` assignment. **Deliberately here and not in `cruise.py`**, contrary to the original plan: `update_v_cruise(CS, enabled, is_metric)` is called directly by `selfdrive/car/tests/test_cruise_speed.py` in five places, so changing its signature to pass a `SubMaster` would break an upstream test suite. card's own health checks are scoped (`all_checks(['carControl'])`), so `mapdOut` cannot invalidate `carOutput`. |
| `openpilot/selfdrive/controls/lib/longitudinal_planner.py` | import block; before `get_cruise_accel`; before `min(candidates)` | **C** | Two hooks (19 added lines, 0 deletions). Hook 1 `limit_v_cruise()` lowers the ceiling — must stay BEFORE `get_cruise_accel` and only ever lower `v_cruise` so `forceDecel` still wins. Hook 2 `extra_accel_candidates()` appends a hazard decel candidate — must stay AFTER hook 1 (which runs the controller for the frame) and before the `min()`. **Highest-risk rows in this table.** If upstream changes `A_CRUISE_MIN` or the min() arbitration, re-verify the hook-2 safety argument (see PROGRESS.md). |
| `opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py` | `create_acc_control()`, `values` dict | C | Adds `SCC_ObjSta` (lead-vehicle dash icon), derived from `enabled`/`hud_control.leadVisible`/`gas_override` — all pre-existing function args, no new plumbing. `ObjValid`/`OBJ_STATUS` left at mainline's constants (unconfirmed cluster relevance per the DBC — see `PORT_LEAD_ICON_FROM_SUNNYPILOT.md` §3). Only active for CANFD Hyundais with `openpilotLongitudinalControl=True` (confirmed sole caller: `create_acc_cancel`, the only other `SCC_CONTROL` builder in this file, fires only when that flag is `False`). |

## Fork-owned files (category A — no merge conflicts)

- `openpilot/grt/` — `__init__.py`, `registry.py`, `hooks.py`, `scc_map.py`, `settings.py`, `set_speed.py`, `tests/`
- `openpilot/common/grt_params_keys.inc` — param keys, `#include`d by `params_keys.h`
- `third_party/mapd/` — vendored fork binary + provenance `README.md`
- `PORT_MAPD_FROM_SUNNYPILOT.md`, `PORT_LEAD_ICON_FROM_SUNNYPILOT.md`, `PROGRESS.md`, `GRT_MODS.md`, `captains_log.md`

## Sync procedure

1. `git rebase --onto <new-upstream-tag> <old-base> nightly-dev`.
2. Categories A/B should rebase clean. Resolve C/D by **meaning**.
3. `grep -rn "GRT-MOD"` and walk this table.
4. Re-run the wire-compat check (union discriminants pristine vs branch vs the Go schema) if
   anything in `cereal/` moved — see PROGRESS.md for the exact method.
5. Rebuild on the target device; never SCP generated schema artefacts.
