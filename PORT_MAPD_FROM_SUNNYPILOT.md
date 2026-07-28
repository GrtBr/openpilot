<!--
  HAND-OFF PROMPT — port sunnypilot's mapd into the GrtBr openpilot fork.
  Rewritten 2026-07-28 for (a) the nightly-dev worktree layout, (b) the current
  min()-arbitration longitudinal architecture in this checkout, and (c) a
  merge-proof `grt/` package structure so future upstream syncs stay cheap.

  Verified against the live repos this session. Read the whole file before editing.
-->

# Port sunnypilot's mapd (OSM map-based speed control) into openpilot — merge-proof

You are working in **`/home/pi5-ubuntu/Comma/openpilot/nightly-dev`** — a git worktree on branch **`nightly-dev`** of the GrtBr openpilot fork (`github.com/GrtBr/openpilot`), openpilot source under **`nightly-dev/openpilot/`**. A knowledge graph exists at `graphify-out/` — read `graphify-out/GRAPH_REPORT.md` before broad codebase questions.

Your job: bring **mapd** — OSM offline map-based longitudinal speed control — into this repo, matching the behaviour working in the user's sunnypilot fork at **`/home/pi5-ubuntu/Comma/sunnypilot`** (branch `precompute-curvature-and-added-stop-signs`). That repo is your reference; read from it freely, **do not modify it**.

Target device: a **comma 4** ("Staria", Hyundai Staria 4th gen, CANFD). Deployment over SSH/SCP.

## Work sequencing — do all offline work first, batch the on-device work

The car is not always powered or reachable. **Do not interleave device steps with code steps.** Sequence the whole port so the device is only needed once, in a contiguous block:

1. **Offline block (no device needed) — do this entirely first:** all code, schema, and build work — the `grt/` package (Phases 1 binary-vendor prep, 3, 4, 5, 6), the cereal edits (Phase 2), and a clean local `scons -j4` plus the PC schema round-trip (Verification steps 1-2). Get everything compiling and self-consistent on the Pi5 with the car absent.
2. **On-device block (car powered and reachable) — batch into one session:** reconcile the `prebuilt` marker (Phase -1), deploy the binary + tiles (Phase 1 copy), run the **Phase 0 compatibility gate**, then boot-clean, params-survival, static drive-through, and the road tests (Verification steps 3-9). Only enter this block when the Staria is on and SSH-reachable, and expect to stay in it until the drive tests are done.

Rationale: the offline block is pure Pi5 work that never blocks on the car; the on-device block is a single "car is here" window. Splitting them avoids stranding half-finished device state between sessions and keeps the risky, safety-relevant steps together under supervision. (Note also the Pi5 is RAM-constrained — never generate tiles on it; tiles are copied, not built.)

## Resume protocol (survives token-limit interruptions)

Implementation may span multiple sessions (token limits). Durable state lives in **`nightly-dev/PROGRESS.md`** — a phase checklist plus a "Last action / Next step" block. **The rule: update `PROGRESS.md` in the same commit as each phase's code.** Any session — a manual restart or the auto-resume cron — resumes losslessly by:

> Reading `nightly-dev/PORT_MAPD_FROM_SUNNYPILOT.md` and `nightly-dev/PROGRESS.md`, continuing the **offline** block from the last incomplete phase, committing per phase, updating `PROGRESS.md`, and **stopping + notifying when the offline block is done or a device step is next.**

A cron (every 310 min) fires this resume prompt unattended and **cancels itself once the offline block is complete**. It is scoped to the offline block only — it must never run an on-device step (that block needs the car powered and you supervising). Commits are local to `nightly-dev`; do not push from an unattended run.

## The overriding design constraint: keep `nightly-dev` cheap to re-sync with upstream

This fork must remain easy to rebase onto new upstream openpilot releases. Every edit you make to an **upstream-owned file** is a future merge conflict. Therefore the whole port is built around one rule:

> **All feature logic lives in a fork-owned `openpilot/grt/` package. Upstream files receive only thin, one-line, sentinel-wrapped hooks that call into `grt`.**

This is not optional polish — this checkout has *already* moved out from under an earlier version of this plan (the longitudinal MPC signature changed and the planner was restructured, see the injection section). Concentrating logic in `grt/` means a future sync re-verifies a handful of one-line hooks instead of re-applying a scattered patch set.

### Touchpoint taxonomy — know which kind each edit is

| Cat | What | Conflict risk | Rule |
|---|---|---|---|
| **A** | New fork-owned files (`openpilot/grt/*`, binary, tiles, scripts) | none | You own them; put everything you can here |
| **B** | Registration splices (`services.py`, `process_config.py`, `plannerd.py`, `params_keys.h`) | low | Exactly one splice line each, calling a `grt` registry |
| **C** | Behavioural injection (`longitudinal_planner.py`, `selfdrived.py`) | **high** | 1–2 sentinel-wrapped lines; re-verify semantics every sync |
| **D** | capnp schema (`custom.capnp`, `log.capnp`) | low by design | Irreducible in-place edit of reserved slots; keep IDs/ordinals |

Every category-C/D edit **must** be wrapped in `# GRT-MOD-START` / `# GRT-MOD-END` (or `/* GRT-MOD */` in capnp/C++) and listed in `GRT_MODS.md` (see final section). That converts a post-rebase audit into `grep -rn GRT-MOD`.

## The `grt/` package layout (build this first — category A, zero conflict)

```
openpilot/grt/
  __init__.py
  scc_map.py        # ported SmartCruiseControlMap (feature logic — the 380-line port)
  hooks.py          # the thin shims upstream calls: limit_v_cruise(), extra_accel_candidates()
  registry.py       # GRT_SERVICES dict, GRT_SUB list, GRT_PROCS list, MAPD_ROOT constant — dependency-light
  params_keys.inc   # C++ include fragment for params_keys.h (category B′)
  settings.py       # writes /data/params/d/MapdSettings (run once at install)
  README.md         # binary provenance (see Phase 1)
```

Keep `registry.py` import-cheap: it is pulled in by `services.py`, which loads very early and everywhere. It may import `QueueSize` and the process classes, nothing heavy.

## What "mapd functionality" means here

Three behaviours, in priority order:

1. **Map curve speed control** — mapd reads precomputed per-node curvature from offline OSM tiles, computes a safe speed for the curve ahead; openpilot slows to it before entry.
2. **Hazard pre-braking** — mapd detects stop signs, give-way, roundabouts, level crossings and T-junctions as node-level hazards ahead on the current way; openpilot brakes to ~20 km/h before them.
3. **Automatic speed-limit adoption** — mapd reads posted `maxspeed` from OSM; openpilot adopts it as a speed ceiling (with pre-braking for an upcoming lower limit).

**Out of scope**: settings UI, `liveMapDataSP`, `longitudinalPlanSP`, `SpeedLimitResolver`, `SpeedLimitAssist`, Dynamic Experimental Control, the `:8080` dashboard, tile generation, the mapd auto-installer.

## Architecture you're porting into

```
  OSM tiles (capnp)        gpsLocation / gpsLocationExternal
  /data/media/0/osm/         carState, modelV2   (msgq, read directly)
          │                          │
          ▼                          ▼
     ┌──────────────────────────────────────┐
     │  mapd  (Go binary, forked, prebuilt) │  50 ms loop
     │  own vendored capnp schemas          │
     └──────────────────────────────────────┘
          │ publishes mapdOut @20 Hz over msgq
          ▼
     openpilot.grt.hooks  (called from longitudinal_planner)
          │ lowers v_cruise ceiling  +  appends a hazard accel candidate
          ▼
     LongitudinalPlanner.update()  →  min() accel arbitration
```

mapd is a **standalone process** speaking msgq+capnp through its own vendored schema copies — it imports no openpilot Python. Your integration is: make the schemas line up, register the process, write the `grt` consumer, and add the two planner hooks.

## The traps — read before writing anything

1. **Do NOT port `sunnypilot/mapd/mapd_installer.py` or any download logic.** It pins `VERSION = "v1.12.0"` from `pfeiferj/openpilot-mapd/releases` — *upstream* mapd, which lacks the user's curvature/stop-sign/T-junction work. Downloading it silently replaces the working binary. The binary is vendored by hand (Phase 1), never auto-updated.

2. **Use exactly one binary**: `/home/pi5-ubuntu/Comma/sunnypilot/third_party/mapd_pfeiferj/mapd` — md5 `0c3b552c229addc273e2c39c28924fbc`, 21211912 bytes, built 2026-06-01. Byte-identical to `mapd_source/build/mapd`. **Ignore `mapd_source/mapd_arm64`** (May 21, predates the T-junction fix). Verify md5 after every copy.

3. **Keep every capnp struct ID and union field number identical.** The prebuilt binary has the schema compiled in. You are *renaming* reserved structs, never renumbering. A changed `@0x...` ID or `@NNN` ordinal = silent layout disagreement.

4. **Queue sizes must match the binary's compiled-in table.** `mapd_source/settings/const.go` hardcodes `mapdOut`/`mapdIn`/`mapdExtendedOut`/`mapdCli` → 2 MB (MEDIUM). Your `GRT_SERVICES` entries must use `QueueSize.MEDIUM`. Mismatch corrupts the shm mapping with no obvious error.

5. **Never subscribe to `modelV2` from a new standalone process.** BIG (10 MB) queue; extra subscriber costs real CPU on device. mapd already subscribes internally. Your `grt` consumer reads only `mapdOut` plus what `plannerd` already has.

6. **mapd reads *stock* messages through its own vendored schemas — verify those ordinals too.** `main.go` subscribes to `carState`, `modelV2`, `gpsLocation`/`gpsLocationExternal` via `mapd_source/cereal/*`, synced against *sunnypilot's* cereal, not this checkout's. capnp tolerates added/removed fields but **not renumbered ordinals**. Before trusting output, diff the ordinals of the fields mapd actually reads — `carState.vEgo`/`vCruise`/`cruiseState`, `modelV2.position`/`velocity`/`action`, `GpsLocationData` — between `mapd_source/cereal/*` and this repo's `openpilot/cereal/log.capnp` + opendbc `car.capnp`. A mismatch does **not** crash: mapd reads the wrong field and emits plausible-but-wrong speeds, and since curve/hazard math depends on `vEgo`, the error is subtle and safety-relevant.

## Phase -1 — The `prebuilt` boot gate (before ANY schema change reaches the device)

**Most likely single cause of a bricked boot.** `launch_chffrplus.sh` runs the scons rebuild (`./build.py`) **only if `$DIR/prebuilt` does not exist**. If a `prebuilt` marker is present on the device, it boots the already-compiled schema and will **not** rebuild cereal after you push capnp changes — so the mapd binary (new schema) and openpilot (old compiled schema) disagree, the exact failure the device's `CLAUDE.md` warns strands it in the fallback launcher.

Before shipping schema changes:
1. `ssh comma4 'ls -la /data/openpilot/prebuilt'` (adjust path to the on-device repo root).
2. If present, prefer to **delete it** so the next boot rebuilds cereal on-device from source.
3. Confirm the first post-change boot actually recompiled (watch manager output / check `cereal/gen/` mtimes).

Not optional. A schema push with a stale `prebuilt` marker is how the device ends up in the fallback launcher.

## Phase 0 — Fail-fast compatibility gate (before any control code)

Prove the one unproven assumption: *does the prebuilt Go binary attach to this openpilot's msgq and emit a message Python can decode?*

1. Do Phase 2 (schema) and the `GRT_SERVICES` part of Phase 3 first — prerequisites for a decode test.
2. Reconcile the `prebuilt` marker (Phase -1) and rebuild cereal on device.
3. Copy the binary + a small tile set to the device (Phase 1).
4. Run mapd **by hand** on the device (not under manager), onroad or with a GPS fix:
   ```
   cd /data/media/0/osm && ./mapd
   ```
5. From a second SSH session:
   ```python
   import openpilot.cereal.messaging as messaging
   sm = messaging.SubMaster(['mapdOut'])
   while True:
       sm.update()
       print(sm['mapdOut'].tileLoaded, sm['mapdOut'].roadName, sm['mapdOut'].speedLimit,
             sm['mapdOut'].mapCurveSpeed, sm['mapdOut'].nextHazard, sm['mapdOut'].nextHazardDistance)
   ```

**Pass criteria (all three):**
- messages arrive, `tileLoaded` is `True`, `roadName` matches the actual road — proves gps + tiles + msgq;
- `speedLimit` matches the posted limit;
- **with the car moving**, `mapCurveSpeed` is sane (plausible m/s on a curve, `0` on a straight) and `nextHazardDistance` counts *down* toward a hazard.

The last two prove trap 6. `tileLoaded == True` only proves GPS+tiles; mapd can have a perfect fix while reading `vEgo` from the wrong ordinal, making every downstream number quietly wrong.

If `tileLoaded` is False: tiles missing/misplaced. If nothing arrives: check (a) **prefix mismatch** — gomsgq `IsPrefixedMsgq()` (`~/go/pkg/mod/github.com/pfeiferj/gomsgq@v0.1.10/msgq.go:36`) vs openpilot's `/dev/shm/msgq_` + `$OPENPILOT_PREFIX`; compare what lands in `/dev/shm`; (b) **segment-size mismatch** — diff `mapd_source/settings/const.go` `ServiceQueueSize` against your `GRT_SERVICES`.

**Do not proceed past this gate until it passes.**

## Phase 1 — Vendor the binary and deploy tiles (category A)

**Binary.** Create `third_party/mapd/` and copy the binary in (keep exec bit; verify md5). Put provenance in `openpilot/grt/README.md`: source repo `github.com/GrtBr/mapd`, commit `07ea8db` ("mapd: detect hazard on current way exit node (T-Junction fix)"), build date, md5 — future-you must know this binary is *not* upstream.

**Tiles.** Offline tiles live at `/data/media/0/osm/offline/` on device (latitude-band dirs). User's set: `/home/pi5-ubuntu/Comma/sunnypilot/tiles/` (~606 MB). Generated with the **fork's** tile schema (`mapd_source/cereal/offline/offline.capnp` — `curvature @3` on `Coordinates`, `highway @14`), so they only work with the fork's binary. Copy to device, confirm sizes; do not regenerate (full-planet gen OOMs the Pi 5). Ensure `/data/media/0/osm/` exists — it doubles as mapd's working dir.

## Phase 2 — cereal schema (category D — irreducible in-place)

Openpilot's reserved custom slots already carry the exact IDs mapd expects (verified this checkout).

**`openpilot/cereal/custom.capnp`** (lines 64/67/70) — rename three reserved structs *in place*, keeping their `@0x...` IDs, filling them from `/home/pi5-ubuntu/Comma/sunnypilot/mapd_source/cereal/custom/custom.capnp` (the Go copy is authoritative — it's what the binary compiled against). Wrap each in `# GRT-MOD-START/END`:

| openpilot today | becomes | struct ID (unchanged) |
|---|---|---|
| `CustomReserved17` | `MapdExtendedOut` | `@0xa30662f84033036c` |
| `CustomReserved18` | `MapdIn` | `@0xc86a3d38d13eb3ef` |
| `CustomReserved19` | `MapdOut` | `@0xa4f1eb3323f5f582` |

Also copy the enums/structs these reference: `MapdInputType`, `WaySelectionType` (→ `MapdWaySelectionType`, avoiding collisions as sunnypilot does), `RoadContext` (→ `MapdRoadContext`), `SpeedLimitOffsetType`, `MapdDownloadProgress`, `MapdPathPoint`.

**`MapdOut` has 24 fields, `@0` through `@23`** — last is `speedLimitAccepted @23`. Copy verbatim from the Go schema. **Do NOT add `nextHazardSpeedTarget @24`** — it exists only in sunnypilot's Python-side `custom.capnp`, NOT the Go schema the binary compiled against, so the binary never writes it. Match the Go copy exactly.

**`openpilot/cereal/log.capnp`** (lines 2626-2628) — rename the union members, keeping field numbers, wrap in `# GRT-MOD`:
```
    mapdExtendedOut @143 :Custom.MapdExtendedOut;
    mapdIn          @144 :Custom.MapdIn;
    mapdOut         @145 :Custom.MapdOut;
```
Cross-check against `/home/pi5-ubuntu/Comma/sunnypilot/mapd_source/cereal/log/log.capnp` (the binary's copy).

Rebuild cereal (`scons -j4 openpilot/cereal` or full `scons -j4`); confirm generated headers pick up the new names.

## Phase 3 — Registration (category B — one splice line each)

Put the *data* in `openpilot/grt/registry.py`; put a single splice in each upstream file.

**3a. `openpilot/grt/registry.py`:**
```python
from openpilot.cereal.services import QueueSize

MAPD_ROOT = "/data/media/0/osm"   # on-device; PC path handled in settings.py if needed

GRT_SERVICES = {
    "mapdOut":         (True, 20., 20, QueueSize.MEDIUM),
    "mapdIn":          (False, 0., None, QueueSize.MEDIUM),
    "mapdExtendedOut": (False, 1., 1, QueueSize.MEDIUM),
}
GRT_SUB = ["mapdOut"]   # spliced into plannerd's SubMaster
# GRT_PROCS defined here too (see 3c) — import NativeProcess lazily to keep this module light
```
Only **`mapdOut`** is load-bearing (the one `plannerd` subscribes to; the only one sunnypilot registers). `mapdIn`/`mapdExtendedOut` are for completeness — the Go side creates its own shm with `O_CREAT` regardless; drop them if they cause friction. `mapdOut` is logged so drives can be replayed for tuning.

**3b. `openpilot/cereal/services.py`** (category B) — the `_services` dict literal ends before the comprehension at line ~96. Splice one line after the literal closes:
```python
# GRT-MOD-START
from openpilot.grt.registry import GRT_SERVICES; _services.update(GRT_SERVICES)
# GRT-MOD-END
```

**3c. `openpilot/system/manager/process_config.py`** (category B) — the `procs` list starts at line ~73. After it, splice:
```python
# GRT-MOD-START
from openpilot.grt.registry import GRT_PROCS; procs += GRT_PROCS
# GRT-MOD-END
```
Define `GRT_PROCS` in `registry.py`:
```python
def _mapd_procs():
    import os
    from openpilot.system.manager.process import NativeProcess
    from openpilot.common.params import Params
    from opendbc.car.structs import car
    from openpilot.common.basedir import BASEDIR
    MAPD_PATH = os.path.join(BASEDIR, 'third_party/mapd/mapd')
    def mapd_ready(started: bool, params: Params, CP: car.CarParams) -> bool:
        return bool(os.path.exists(MAPD_ROOT))
    return [NativeProcess("mapd", MAPD_ROOT, ["bash", "-c", f"{MAPD_PATH} > /dev/null 2>&1"], mapd_ready)]
GRT_PROCS = _mapd_procs()
```
This keeps `MAPD_ROOT` in `grt` and **avoids editing `common/hardware/hw.py`** entirely (one fewer touchpoint than the old plan). Do **not** port `mapd_manager` (its jobs — settings-restore hack, OSM download, publishing `liveMapDataSP` — are all out of scope).

**3d. `openpilot/common/params_keys.h`** (category B′) — the initializer `keys = { ... }` starts at line 8. Splice one include *inside* the braces:
```cpp
    // GRT-MOD-START
    #include "common/grt_params_keys.inc"
    // GRT-MOD-END
```
Create `openpilot/common/grt_params_keys.inc` (category A — a copy of `openpilot/grt/params_keys.inc`, or symlink; place where the C++ include path resolves):
```cpp
{"MapdSettings",           {PERSISTENT, JSON}},
{"SmartCruiseControlMap",  {PERSISTENT, BOOL, "0"}},
```
`MapdSettings` **must** be `PERSISTENT`: `Params::clear_all` (`common/params.cc`) unlinks every params file not in this table, and `manager.py` calls it on manager start and every onroad/offroad/ignition transition. sunnypilot works around this by rewriting the file every second (`mapd_manager.py:31-35`) — **do not copy that hack**; the registered key is the correct fix. Note this in your commit.

**3e. `openpilot/selfdrive/selfdrived/selfdrived.py`** (category C) — mapd is not a normal process and must not trip the process-not-running safety event. **Locate the `not_running` / `processNotRunning` path in THIS checkout** (grep for both; line numbers differ from older versions and from sunnypilot, which uses a `self.ignored_processes` set that does **not** exist here). Exclude `'mapd'` from the set before the event is raised, wrapped in `# GRT-MOD`. Adapt to the actual code — do not paste sunnypilot's line.

## Phase 4 — MapdSettings (category A)

`openpilot/grt/settings.py` writes the JSON blob at `/data/params/d/MapdSettings`, read by mapd at startup and on a `mapdIn` `reloadSettings`. Base defaults on `sunnypilot/mapd/mapd_manager.py:36-62`, with:

- `"map_curve_speed_control_enabled": true` — behaviour 1.
- `"speed_limit_control_enabled": true` — **changed from sunnypilot's `false`**; turns on automatic speed-limit adoption (behaviour 3). See Phase 6.
- `"vision_curve_speed_control_enabled": false` — no consumer here; leaving it on burns CPU.
- Keep `target_speed_jerk: 0.6`, `target_speed_accel: 0.6`, `target_speed_time_offset: 4.0` — the user's tuned map-curve braking profile (SCC tuning `accel=0.6 / offset=3.0`, `_A_LAT_REG_MAX=3.5`). Do not "improve" them.
- `speed_limit_offset` starts `0.0`. **Caveat:** 0 means openpilot holds *exactly* the posted limit, which many find slower than expected. Consider a small positive offset before the first road test; flag it to the user as a tunable.

Write it once at install (with the key registered `PERSISTENT` it survives), not in a loop.

## Phase 5 — The control path (category A logic + category C hooks)

### 5a. Port the controller — `openpilot/grt/scc_map.py`

Port `SmartCruiseControlMap` from `/home/pi5-ubuntu/Comma/sunnypilot/sunnypilot/selfdrive/controls/lib/smart_cruise_control/map_controller.py` — **read all 377 lines first**; the comments encode reverted experiments and why constants sit where they do. Port:

- `update_calculations()` — reads `mapdOut.mapCurveSpeed`, `speedLimitSuggestedSpeed` (see Phase 6), `nextHazard`/`nextHazardDistance`, folds into `self.v_target`.
- `_HAZARD_SPEED_TARGETS` + all `HAZARD_*` constants, **values unchanged** (e.g. `HAZARD_ACCEL_MAX = -0.3`, reverted from `-0.1` because a looser floor let the controller coast and 5/6 stop-sign approaches needed driver braking).
- The lead-vehicle gate + sticky `_hazard_engaged` latch — a lead blocks only the *rising edge*, never cancels an engaged slow-down.
- The adaptive-decel loop (`_adaptive_hazard_accel`) and jerk-limited helpers `calculate_distance`/`calculate_accel`/`calculate_velocity`.
- The `MapState` machine (`disabled/enabled/turning/overriding`) + `_update_state_machine()`.

Adapt:
- **Imports**: `openpilot.cereal.messaging`, `openpilot.common.realtime.DT_MDL`, `openpilot.selfdrive.car.cruise.V_CRUISE_UNSET`. Drop all `openpilot.sunnypilot.*` imports.
- **`MapState`**: sunnypilot's lives in the `LongitudinalPlanSP` schema (not ported) — replace with a plain `IntEnum` in this file.
- **`__init__`**: delete the `coordinate_from_param("LastGPSPosition", ...)` and `velocities_from_param("MapTargetVelocities", ...)` reads. Vestigial — nothing downstream uses them (mapd computes curve speed itself and publishes `mapCurveSpeed`; see `map_controller.py:142`), and they raise `UnknownKeyName` on this repo.
- **`PARAMS_UPDATE_PERIOD`**: inline the constant (sunnypilot imports from `openpilot.sunnypilot`).
- **`_DEBUG_LOG`**: keep it (JSON lines to `/data/media/0/mapd_debug.log` every 10th frame) — primary tuning/diagnosis tool, near-zero cost.
- **Gate**: read the `SmartCruiseControlMap` bool param; when off, `output_v_target = V_CRUISE_UNSET` and `output_hazard_accel = None`.

Expose two outputs the hooks read: `output_v_target` (float, `V_CRUISE_UNSET` when inactive) for speed ceilings, and `output_hazard_accel` (float or `None`) for firm hazard braking — the latter is sunnypilot's `output_a_min_override`, triple-gated `is_active AND hazard_active AND not has_lead`.

### 5b. The injection — how longitudinal control works in THIS checkout (READ CAREFULLY)

**This is different from older openpilot and from any earlier version of this plan.** In `openpilot/selfdrive/controls/lib/longitudinal_planner.py`, `LongitudinalPlanner.update(self, sm)`:

- `v_cruise` is computed (~line 83) and zeroed if `sm['controlsState'].forceDecel` (~line 85).
- `self.mpc.update(sm['radarState'], personality=...)` (~line 113) — **the MPC no longer receives `v_cruise`**; `long_mpc.update`'s signature is `(self, radarstate, personality=...)`.
- `self.a_cruise = get_cruise_accel(experimentalMode, v_cruise, v_ego, ...)` (~line 134) — a P-controller: `clip(v_cruise - v_ego, A_CRUISE_MIN, max_accel)`.
- Final output is a **`min()` over acceleration candidates** (~line 139-145):
  ```python
  candidates = [(output_a_target_mpc, mpc, ...), (a_cruise, cruise, ...)]
  if experimentalMode: candidates.append((output_a_target_e2e, e2e, ...))
  output_a_target, self.mpc.source, _ = min(candidates, key=lambda c: c[0])
  self.output_a_target = np.clip(output_a_target, ACCEL_MIN, ACCEL_MAX)
  ```

So there are **two** natural, physically-correct injection points, each a single sentinel-wrapped line:

**Hook 1 — speed ceiling (curve + speed limit), before `get_cruise_accel` (~line 134):**
```python
# GRT-MOD-START
from openpilot.grt import hooks as grt   # (import once at top of file, also sentinel-wrapped)
v_cruise = grt.limit_v_cruise(sm, v_cruise, v_ego)
# GRT-MOD-END
```
Lowering `v_cruise` makes `a_cruise` negative via the P-controller; the `min()` then selects it. `limit_v_cruise` must only ever *lower* `v_cruise` (`return min(v_cruise, target)`), so a `forceDecel` `v_cruise == 0.0` still wins. **This hook also drives the single per-frame `scc.update(sm, ...)`** — it runs before Hook 2 in the same `update()`, so Hook 2 reuses the cached result.

**Hook 2 — firm hazard pre-braking, after `candidates` is built and before `min()` (~line 143):**
```python
# GRT-MOD-START
candidates += grt.extra_accel_candidates(sm, v_ego)
# GRT-MOD-END
```
`extra_accel_candidates` returns `[]` normally, or `[(hazard_accel, LongitudinalPlanSource.cruise, should_stop(v_ego, hazard_accel))]` when `scc.output_hazard_accel is not None`. Because the final value is `min(candidates)` and is then clipped to `ACCEL_MIN`, a firm negative hazard accel wins safely. **This replaces the old, invasive `a_min_override` kwarg on `long_mpc.update` — no MPC signature change is needed anymore.** That is a real simplification the new architecture buys you.

`openpilot/grt/hooks.py` owns the singleton and the ordering contract:
```python
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET
_scc = None
def _scc_singleton():
    global _scc
    if _scc is None:
        from openpilot.grt.scc_map import SmartCruiseControlMap
        _scc = SmartCruiseControlMap()
    return _scc

def limit_v_cruise(sm, v_cruise, v_ego):
    scc = _scc_singleton()
    scc.update(sm, v_ego, v_cruise)                 # the one per-frame update
    t = scc.output_v_target
    return min(v_cruise, t) if 0 < t < V_CRUISE_UNSET else v_cruise

def extra_accel_candidates(sm, v_ego):
    from openpilot.selfdrive.controls.lib.longitudinal_planner import LongitudinalPlanSource, should_stop
    a = _scc_singleton().output_hazard_accel        # already updated this frame by limit_v_cruise
    if a is None:
        return []
    return [(a, LongitudinalPlanSource.cruise, should_stop(v_ego, a))]
```
(Confirm the exact import location of `LongitudinalPlanSource` and `should_stop` in this checkout.)

**Behavioural caveat to give the user:** map/hazard decel *feel* is now governed by `A_CRUISE_MIN` (Hook 1's P-controller) and the hazard candidate (Hook 2), **not** the MPC cost that sunnypilot's `target_speed_jerk: 0.6` was tuned against. And Hook 2 feeds the same `min()` crossover the user found responsible for Staria low-speed jitter — replay a drive on the bench before trusting the feel.

### 5c. `openpilot/selfdrive/controls/plannerd.py` (category B)

The `SubMaster` at line ~22 already includes `carControl`, `carState`, `radarState`, `selfdriveState`, `modelV2`. Splice `mapdOut` in on that line:
```python
# GRT-MOD-START  (add GRT_SUB to the SubMaster service list on the next line)
from openpilot.grt.registry import GRT_SUB
# GRT-MOD-END
sm = messaging.SubMaster(['carControl', ..., 'selfdriveState'] + GRT_SUB, ...)
```
Do **not** add any `modelV2` subscriber anywhere new. Expect this list to occasionally gain upstream entries — a cheap re-resolve at sync time.

## Phase 6 — Automatic speed-limit adoption

**Do not port `SpeedLimitAssist`** (`sunnypilot/.../speed_limit/speed_limit_assist.py`, 429 lines) — it's a UI-confirmation state machine with `longitudinalPlanSP` deps.

**mapd already does this — including pre-braking lookahead.** With `speed_limit_control_enabled: true` (Phase 4), `State.SuggestedSpeed()` (`mapd_source/state.go:45`) folds the posted limit + `speed_limit_offset` + hold-last-seen + the **next-limit lookahead/jerk profile** in `speed_limit.go` (`SuggestNewSpeedLimit`) into `mapdOut.speedLimitSuggestedSpeed`, set **unconditionally** at `state.go:89`. So in `scc_map.py`'s `update_calculations()`:

- Add `mapdOut.speedLimitSuggestedSpeed` as one more candidate for `self.v_target` (take it when `> 0` and lower than current target), alongside curve and hazard sources.
- **Do NOT also re-implement the `nextSpeedLimit` / `nextSpeedLimitDistance` jerk-limited pre-braking block from `map_controller.py:156-162`** — it duplicates mapd's internal lookahead; running both = two controllers fighting the same slow-down. Prefer mapd's `speedLimitSuggestedSpeed`. If it's too gentle in testing, that's a `MapdSettings` tuning question, not a reason for a second Python integrator.
- **Do NOT use `mapdOut.suggestedSpeed`** (the fully-combined value) — it already folds in `vCruise` and curve speeds by mapd's own priority rules, which would fight the Python-side arbitration.

**Behavioural note for the user:** this applies the limit as a *speed ceiling in the planner* (via Hook 1). The dash set-speed doesn't change; the driver keeps full accelerator authority. It does not simulate pressing SET. Tracking the displayed set speed to the limit is a separate change in `selfdrive/car/cruise.py`, not here.

## Phase 7 — (folded into Hook 2; no longer a separate invasive phase)

In the old architecture, firm stop-sign braking required threading `a_min_override` through `long_mpc.update` — the single most invasive edit. **This checkout's `min()`-candidate arbitration removes that need**: Hook 2 (Phase 5b) injects the adaptive hazard decel directly as a candidate. Still, **land Phases 1-6 and drive them before enabling Hook 2's firm braking** — commit Hook 2 separately so a bad outcome is one revert. Historical trap from the user's notes: an earlier version passed the *baseline* constant instead of the *adaptive* value, so the adaptive loop never reached the output and `a_ego` plateaued near −0.9 m/s². Pass `self._adaptive_hazard_accel`, not `HAZARD_TARGET_ACCEL`.

## Verification

Run in order; don't skip ahead on failure.

1. **Build** — `scons -j4` clean from repo root. cereal codegen is the likely failure point.
2. **Schema round-trip (PC)** — construct/read back a `mapdOut`; confirm all **24** fields (`@0`–`@23`) addressable.
3. **`prebuilt` reconciled (Phase -1)** — device will rebuild cereal next boot, or already has.
4. **Phase 0 gate on device** — mapd by hand, `tileLoaded == True`, plausible `roadName`, sane moving `mapCurveSpeed`/`nextHazardDistance`. Do not proceed without this.
5. **Boot clean** — reboot; confirm `manager` comes up with mapd in the process list, no crash loop. Check `/data/log/` + manager output. **Never SIGKILL a managed process to restart it** — the manager won't restart unless `restart_if_crash=True`; reboot. To replace the binary: stop the process, then SCP.
6. **Params survive a transition** — after a reboot *and* an ignition cycle, `/data/params/d/MapdSettings` still exists. If it vanishes, `params_keys.h`/`.inc` wasn't rebuilt into the running binary.
7. **Static drive-through** — car on, not moving: `mapdOut` shows correct road name + speed limit for where parked.
8. **Road test, in order**: (a) a known curve — slows before entry, `_DEBUG_LOG` shows `map_curve_speed_kmh > 0`, `state == turning`; (b) a posted-limit change — eases down before the sign; (c) a stop sign, no lead — decel to ~20 km/h by the line. Hand near the wheel, ready to override; log every approach.
9. **Log review** — pull `/data/media/0/mapd_debug.log`; check `hazard_active`, `has_lead`, `output_hazard_accel`, `adaptive_hazard_accel` behave as `map_controller.py` comments describe.

## Rollback

Every phase independently revertable. Fastest kill switch, no reflash: `params.put_bool("SmartCruiseControlMap", False)` — `scc.update` then yields `V_CRUISE_UNSET` / `None`, both hooks become no-ops. Second: remove the `GRT_PROCS` splice so the binary never starts. The cereal renames are inert alone (nothing publishes those slots if mapd isn't running).

## Merge-proofing deliverables (do these as you go)

- **`GRT_MODS.md` at the repo root** — a table of every in-place edit: file, line, category (C/D), one-line why. This is the sync checklist. Example row: `openpilot/selfdrive/controls/lib/longitudinal_planner.py | ~134,~143 | C | v_cruise ceiling + hazard accel candidate`.
- **Sentinels** — `# GRT-MOD-START/END` (or `/* GRT-MOD */`) around **every** category-C/D edit. Post-rebase triage = `grep -rn GRT-MOD openpilot/`.
- **Commit topology** — keep all upstream in-place edits in **one commit** (`grt: upstream hook points`), separate from the feature commits that only add `openpilot/grt/*` and `third_party/mapd/*`. Sync via `git rebase --onto <new-upstream-tag> <old-base> nightly-dev`; categories A/B rebase clean, only C/D need eyes.
- **Write final code changes as dated headings into `captains_log.md`** at the repo root (user convention).
- After modifying code, run `graphify update .` to keep the graph current.

## Ground rules

- **Read sunnypilot reference files before porting.** The `map_controller.py` comments document reverted experiments; values that look arbitrary usually aren't.
- **Never SCP cereal files between machines.** Per this repo's `CLAUDE.md`: schema mismatches crash `manager.py` on boot and strand the device. Edit cereal in-place on device via SSH, or base off `git show HEAD:cereal/file`. Rebuild on target; don't SCP generated artefacts. (See Phase -1 for the `prebuilt` interaction.)
- **Commit per phase**, phase name in the message; only commit files the device needs; keep probes/scratch untracked.
- **Ask before deviating on scope.** UI toggles, tile generation, the dashboard, full SP parity were all explicitly ruled out.
