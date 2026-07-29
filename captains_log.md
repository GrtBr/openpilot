# Captain's Log — `nightly-dev`

Running record of code changes to **this checkout only** (`~/Comma/openpilot/nightly-dev`, branch
`nightly-dev`). Newest entry first. Each entry: what changed, why, how it was verified, and current
deploy status.

The sibling `~/Comma/openpilot/release-mici-staging/` checkout keeps its **own** `captains_log.md`.
The two branches diverge — changes logged here are not present there unless cherry-picked.

---

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
