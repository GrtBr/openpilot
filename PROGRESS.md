# mapd port — PROGRESS (durable resume state)

**This file is the single source of truth for where the implementation stands.**
Update it after every phase, in the same commit as the code for that phase.

## RESUME PROMPT (what a fresh session / cron run should do)

> Read `nightly-dev/PORT_MAPD_FROM_SUNNYPILOT.md` and `nightly-dev/PROGRESS.md`; continue the **offline** block from the last incomplete phase; commit per phase; update `PROGRESS.md`; stop and notify when the offline block is done or a device step is next.

Resume rules:
- **Offline block only.** Never run an on-device step (anything needing the car powered / SSH to comma4) from an unattended resume. When the next incomplete item is in the ON-DEVICE block, STOP and notify the user — do not proceed.
- **Commit per phase** on branch `nightly-dev` (local commit only, do **not** push). Put the phase name in the message.
- After each phase: tick its box below, fill in "Last action / Next step", and add a dated heading to `captains_log.md` (user convention).
- Wrap every category-C/D upstream edit in `# GRT-MOD-START/END` and record it in `GRT_MODS.md`.
- If a `scons -j4` full build is too heavy on the Pi5, build just `openpilot/cereal` to verify codegen and note it.

## STATE

- **Status:** IN PROGRESS (offline block) — Phases 2, 1a, 3, 4 done + grt/ scaffold + Verification 2
- **Last action:** Phase 4 (grt/settings.py) written and asserted. Committed.
- **Next step:** Phase 5 — the big one. Port `grt/scc_map.py` from sunnypilot `map_controller.py` (377 lines, read it in FULL first), write `grt/hooks.py` (limit_v_cruise + extra_accel_candidates), then the two GRT-MOD hooks in `longitudinal_planner.py`. Note the injection is the min()-candidate design in the plan, NOT the old a_min_override kwarg.
- **Blockers / gotchas:**
  - **The Pi5 CANNOT build openpilot.** No `scons`, no `cmake`, no `capnproto`/`capnpc`; the repo `.venv` was empty. So **Verification 1 (`scons -j4`) cannot be run here** — it must be done on the comma4 during the on-device block. Do NOT tick Verification 1 on the strength of the schema check below; they are different claims (capnp schema validity + Python addressability vs. a real C++ codegen/compile).
  - Installed `pycapnp` (2.2.4, prebuilt wheel, venv-local at `.venv/`, no sudo, no source compile) purely to validate schemas and do round-trips on the Pi5. This is a dev-only dep; it is NOT required on device and must not be added to any requirements file.
  - pycapnp caches by filename: loading two different `log.capnp` files in one process silently returns the first. Probe each schema in a separate process.
  - **`cereal/services.py` cannot import `openpilot.grt`.** It is executed as a standalone script at build time (`python3 services.py > services.h`) where the repo root is NOT on sys.path — an import there fails with `ModuleNotFoundError: No module named 'openpilot'` and breaks the build. Verified empirically. The 3 mapd service entries are therefore INLINED in services.py; `registry.py` deliberately does not duplicate them.
  - The Pi5 venv lacks openpilot's runtime deps (`opendbc`, `setproctitle`, ...), so anything importing `openpilot.system.manager.process` or `openpilot.cereal` cannot be exercised here directly. `grt_procs()` was verified by stubbing just those two lazy imports.

### Phase 2 verification evidence (recorded so it need not be redone)

Three-way union-discriminant check — the real wire-compat proof (discriminants are positional, NOT the `@143` ordinal, so this is what the binary actually puts on the wire):

| schema | mapdExtendedOut | mapdIn | mapdOut |
|---|---|---|---|
| pristine openpilot HEAD | 141 | 142 | 143 |
| this branch (modified) | 141 | 142 | **143** |
| Go schema the prebuilt binary was built from | 141 | 142 | **143** |

All match → the rename is wire-neutral and agrees with the binary. Also verified: schema compiles, `MapdOut` has exactly 24 fields with contiguous ordinals @0–@23, all 24 addressable, and a build/serialize/parse round-trip returns correct values.

## OFFLINE BLOCK — auto-resumable, no car needed

- [x] **grt/ scaffold** — DONE (`__init__.py`, `registry.py`; `hooks.py`/`scc_map.py`/`settings.py` pending Phases 4-5) — `openpilot/grt/{__init__.py,registry.py,hooks.py,scc_map.py,settings.py,README.md,params_keys.inc}`
- [x] **Phase 2 — cereal schema** — DONE. `custom.capnp` (CustomReserved17/18/19 → MapdExtendedOut/MapdIn/MapdOut, IDs unchanged; MapdOut 24 fields @0–@23; enums Mapd-prefixed for upstream-collision safety, wire-safe) + `log.capnp` union renames @143/144/145; GRT-MOD sentinels on both. Verified via pycapnp (see evidence above). Real `scons` codegen still pending on device.
- [x] **Phase 1a — vendor binary** — DONE. md5 `0c3b552c...fbc` verified, 21211912 bytes, ELF aarch64; provenance in `third_party/mapd/README.md` — copy `~/Comma/sunnypilot/third_party/mapd_pfeiferj/mapd` → `third_party/mapd/mapd`, verify md5 `0c3b552c229addc273e2c39c28924fbc`, write `grt/README.md` provenance
- [x] **Phase 3 — registration splices** — DONE. services.py (INLINED, see gotcha), params_keys.h `#include` + `.inc`, process_config `procs += grt_procs()`, plannerd `+ GRT_SUB`, selfdrived `- GRT_IGNORED_PROCESSES` — `registry.py` (GRT_SERVICES/GRT_SUB/GRT_PROCS/MAPD_ROOT) + one-line splices in `services.py`, `process_config.py`, `params_keys.h`(+`.inc`), `plannerd.py`, and the `selfdrived.py` not_running exclusion
- [x] **Phase 4 — MapdSettings** — DONE. `grt/settings.py` (write-once, `--force`/`--show`, best-effort reloadSettings notify). speed_limit_control ON, vision_curve OFF, tuning preserved. NO 1 Hz rewrite loop (key is PERSISTENT) — `grt/settings.py` writes defaults (speed_limit_control_enabled=true, curve on, vision off, tuned jerk/accel/offset)
- [ ] **Phase 5 — control path** — port `grt/scc_map.py` from sunnypilot map_controller.py; `grt/hooks.py` (limit_v_cruise + extra_accel_candidates); two sentinel hooks in `longitudinal_planner.py`
- [ ] **Phase 6 — speed-limit adoption** — add `speedLimitSuggestedSpeed` candidate in scc_map.py; do NOT duplicate mapd's nextSpeedLimit lookahead
- [ ] **Phase 7 code (separate commit, disabled by default)** — Hook 2 firm-hazard-accel candidate is written but committed on its own and left for the on-device drive phase to enable; do not enable in default path
- [ ] **Verification 1 — clean local `scons -j4`** (or `openpilot/cereal` if full build too heavy)
- [x] **Verification 2 — PC schema round-trip** — DONE on Pi5 via pycapnp: built/serialized/parsed a `mapdOut`, all 24 fields addressable, values correct (see Phase 2 evidence above)
- [x] **GRT_MODS.md** — DONE (touchpoint table + sync procedure) — touchpoint table (file, line, category C/D, why)

### ← OFFLINE BLOCK COMPLETE = cron cancels here, notify user, hand off to on-device block.

## ON-DEVICE BLOCK — DO NOT auto-run. Car powered + user supervising, one session.

- [ ] **Phase -1 — prebuilt marker** reconcile on device (delete stale marker so cereal rebuilds)
- [ ] **Phase 1b — deploy** binary + tiles (`~/Comma/sunnypilot/tiles/`) to `/data/media/0/osm/`; verify sizes/md5
- [ ] **Phase 0 — compatibility gate** — mapd by hand, `tileLoaded==True`, sane moving `mapCurveSpeed`/`nextHazardDistance` (proves trap 6)
- [ ] **Verification 3–5** — prebuilt reconciled, boot clean, params survive a reboot+ignition cycle
- [ ] **Verification 6–9** — static drive-through, road tests (curve, speed-limit change, stop sign), log review
