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

- **Status:** SET-SPEED TRACKING part (a) IMPLEMENTED, NOT YET DEPLOYED — `grt/set_speed.py` +
  a card.py hook; auto-adopt within ±20 km/h, default OFF via
  `/data/media/0/grt/SmartCruiseControlSetSpeed`. 27/27 tests. Part (b) (>20 km/h pending +
  RES/+ confirm) is coded but `PENDING_ENABLED = False`, **blocked on one on-device question:
  can a Python-published onroadEvent with a new EventName render + play a sound on this prebuilt
  device?** Do not build the alert until that is answered. Prior: APPROACH PROFILE VALIDATED on drive 3 (median decel -0.51 vs -0.50 target; user: "felt perfect"). NEXT: set-speed-tracks-limit feature — DESIGNED and feasibility-verified, NOT implemented; see captains_log 2026-07-29. Prior status: FIRST TEST DRIVE FAILED (feature was a silent no-op) -> ROOT-CAUSED AND FIXED (radarState lead field is `present`, not `status`). Fix deployed; awaiting a SECOND test drive. Prior: ENABLED AND LIVE ON THE CAR (offroad-verified). Speed ceiling ON; hazard braking still OFF. Only the road test remains. Prior: ON-DEVICE IN PROGRESS. Offline block complete; deployed to comma4; Phase 0 gates 1&2 PASS. **Feature is currently INERT on device (params unknown until a C++ build) — this is the safe designed fallback.** Prior status: ✅ **OFFLINE BLOCK COMPLETE.** Phases 1a, 2, 3, 4, 5, 6, 7 all done and committed; Verification 2 done; Verification 1 reassigned to the device (impossible on Pi5). Auto-resume cron cancelled. **Next work requires the car.**
- **Last action:** Set-speed tracking part (a): `grt/set_speed.py`, hook 3 in card.py, 27 new tests. Committed, not deployed.
- **Next step:** Deploy set-speed tracking to comma4 via git bundle, enable
  `/data/media/0/grt/SmartCruiseControlSetSpeed`, and road-test that the comma UI MAX and the
  Staria cluster follow posted limits. While on device, answer the part-(b) alert question above.
- **Prior next step:** ON-DEVICE BLOCK — requires the Staria powered and SSH-reachable, with the user supervising. Start at Phase -1 (`prebuilt` marker), then deploy, then the Phase 0 gate. **Do NOT auto-run any of it.** Enable `SmartCruiseControlMap` only after Phase 0 passes; leave `SmartCruiseControlMapHazardAccel` OFF until the speed-ceiling behaviour has been driven.
- **Blockers / gotchas:**
  - **mapdOut is NEVER logged to rlog on this device.** loggerd is C++ and uses the compiled services.h (Jul 23), which has no mapdOut; `should_log=True` only affects python. Drives CANNOT be retrospectively analysed for mapd behaviour — `/data/media/0/mapd_debug.log` is the only instrument.
  - **`nightly-dev` is a PREBUILT branch — DO NOT run scons on it, on either machine.** It ships a `prebuilt` marker and runs committed binaries. A build fails on the missing `driving_supercombo.onnx` (the ONNX is the build *input* that compiles into the shipped `driving_tinygrad.pkl` chunks; it is git-LFS and never fetched) and dirties `panda/board/obj/{gitversion.h,version}`, which then need `git checkout --`. This is recorded in captains_log from a previous session; I repeated the mistake and cleaned it up. **Consequence: `grt_params_keys.inc` will never be compiled in, so all fork params raise UnknownKeyName — hence the file-based fallback below.**
  - **The Pi5 CANNOT build openpilot.** No `scons`, no `cmake`, no `capnproto`/`capnpc`; the repo `.venv` was empty. So **Verification 1 (`scons -j4`) cannot be run here** — it must be done on the comma4 during the on-device block. Do NOT tick Verification 1 on the strength of the schema check below; they are different claims (capnp schema validity + Python addressability vs. a real C++ codegen/compile).
  - Installed `pycapnp` (2.2.4, prebuilt wheel, venv-local at `.venv/`, no sudo, no source compile) purely to validate schemas and do round-trips on the Pi5. This is a dev-only dep; it is NOT required on device and must not be added to any requirements file.
  - pycapnp caches by filename: loading two different `log.capnp` files in one process silently returns the first. Probe each schema in a separate process.
  - **`cereal/services.py` cannot import `openpilot.grt`.** It is executed as a standalone script at build time (`python3 services.py > services.h`) where the repo root is NOT on sys.path — an import there fails with `ModuleNotFoundError: No module named 'openpilot'` and breaks the build. Verified empirically. The 3 mapd service entries are therefore INLINED in services.py; `registry.py` deliberately does not duplicate them.
  - **Hook 2 semantics differ from sunnypilot and this is intentional.** sunnypilot loosens the MPC slack floor via an `a_min_override` kwarg (permissive). That kwarg does not exist here, so Hook 2 appends an accel candidate to the planner's `min()` instead. Because `a_cruise` saturates at `A_CRUISE_MIN = -1.2` while the adaptive hazard decel spans [-1.5, -0.3], the candidate only BINDS when harder than -1.2 — i.e. it grants authority beyond the cruise floor, exactly what loosening the slack floor achieved, and it can never make braking weaker than stock. Re-verify this if `A_CRUISE_MIN` or the arbitration changes upstream.
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
- [x] **Phase 5 — control path** — DONE. `grt/scc_map.py` ported (MapState IntEnum, vestigial param reads dropped, MIN_V/PARAMS_UPDATE_PERIOD inlined), `grt/hooks.py`, Hook 1 in longitudinal_planner (11 added lines, 0 deletions). 12/12 behavioural tests pass (`openpilot/grt/tests/test_scc_map.py`) — port `grt/scc_map.py` from sunnypilot map_controller.py; `grt/hooks.py` (limit_v_cruise + extra_accel_candidates); two sentinel hooks in `longitudinal_planner.py`
- [x] **Phase 6 — speed-limit adoption** — DONE inside scc_map.update_calculations: takes `speedLimitSuggestedSpeed` as a candidate; sunnypilot's nextSpeedLimit pre-braking block deliberately NOT ported (would fight mapd's own lookahead); `suggestedSpeed` deliberately unused — add `speedLimitSuggestedSpeed` candidate in scc_map.py; do NOT duplicate mapd's nextSpeedLimit lookahead
- [x] **Phase 7 code (separate commit, disabled by default)** — DONE. `SmartCruiseControlMapHazardAccel` registered (PERSISTENT BOOL "0", DEFAULT OFF); Hook 2 = one line `candidates += grt_hooks.extra_accel_candidates(v_ego)` before the `min()`. 9/9 hook tests pass incl. never-weaker-than-stock. **Enable only after the speed-ceiling behaviour has been driven and validated.**
- [~] **Verification 1 — clean local `scons -j4`** — **NOT POSSIBLE ON THE PI5** (no scons/cmake/capnproto). MOVED to the on-device block. Schema validity + python addressability were verified instead via pycapnp; that is a weaker claim and does NOT substitute for a C++ build.
- [x] **Verification 2 — PC schema round-trip** — DONE on Pi5 via pycapnp: built/serialized/parsed a `mapdOut`, all 24 fields addressable, values correct (see Phase 2 evidence above)
- [x] **GRT_MODS.md** — DONE (touchpoint table + sync procedure) — touchpoint table (file, line, category C/D, why)

### ← OFFLINE BLOCK COMPLETE = cron cancels here, notify user, hand off to on-device block.

## ON-DEVICE BLOCK — DO NOT auto-run. Car powered + user supervising, one session.

- [~] **Phase -1 — prebuilt marker** — marker EXISTS at /data/openpilot/prebuilt. **DO NOT delete it**: a full scons build currently FAILS (missing driving_supercombo.onnx), so deleting the marker would make the device try, and fail, to build at boot — stranding it in the fallback launcher. Left in place deliberately.
- [x] **Phase 1b — deploy** DONE. Repo fast-forwarded via git bundle; binary md5 verified on device. Tiles: bands **-34 and -36** deployed. **GOTCHA: band dir = floor(lat/2)*2 — tiles for lat -34.x live in dir -36, not -34.**
- [x] **Phase 0 — compatibility gate (gates 1&2)** — PASSED, and the feature is now ENABLED and running under manager after a clean reboot. Prior note: - [~] **Phase 0** — GATE-1 (messages arrive/decode) **PASS**, GATE-2 (tileLoaded 307/307 @12Hz) **PASS**. GATE-3 (roadName/speedLimit/curve) BLOCKED: car stationary (vEgo=0, bearing=0) so waySelectionType=fail. **Needs a road test.**
- [x] **Verification 3–5** — DONE. `prebuilt` deliberately KEPT (this is a prebuilt branch; building it is a documented mistake). **Boot clean**: manager + plannerd/controlsd/selfdrived/card/modeld all up, mapd running under manager (pid 10159), nothing crash-looping, longitudinalPlan VALID=True. Config survived the reboot via the file fallback (it lives outside /data/params).
- [~] **Verification 6–9** — static check done (mapdOut live, tileLoaded=True). **ROAD TESTS STILL OUTSTANDING** — need the car moving: waySelectionType is still `fail` and roadName empty because vEgo=0/bearing=0. Order: (a) known curve, (b) posted-limit change, (c) stop sign with no lead. Keep a hand near the wheel; then review /data/media/0/mapd_debug.log.
