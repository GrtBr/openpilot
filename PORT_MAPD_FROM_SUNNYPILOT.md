<!--
  mapd port — PLAN + HARD-WON LESSONS.  Rewritten 2026-07-29, AFTER the port was implemented
  and driven three times on a Hyundai Staria.

  This is no longer a forward-looking guess. Every "trap" below is something that ACTUALLY
  WENT WRONG on this vehicle, with the evidence. The original plan's advice is preserved only
  where it survived contact with the car; where it was wrong, the wrong advice is shown struck
  through with what actually happened, because knowing WHY a plausible instruction was wrong is
  what stops it being reinvented.

  Status: implemented and working. Remaining work is tuning, not building.
-->

# Port sunnypilot's mapd into openpilot — implementation record + hardened plan

Repo: **`/home/pi5-ubuntu/Comma/openpilot/nightly-dev`** (openpilot source under `nightly-dev/openpilot/`),
fork `github.com/GrtBr/openpilot`, branch **`nightly-dev`**, base **openpilot v0.11.2**.
Reference implementation (read-only): **`/home/pi5-ubuntu/Comma/sunnypilot`**.
Target: **comma 4**, Hyundai Staria 4th gen, CANFD, deployed over SSH.

---

# 0. READ THIS BEFORE PLANNING ANYTHING

## 0.1 Read the prior art first. It already contains the answers.

**My single biggest process failure.** `captains_log.md` at this repo root already said, from an
earlier session:

> *"Do not run scons on this branch … Attempting a build fails on a missing
> `driving_supercombo.onnx` … and dirties `panda/board/obj/{gitversion.h,version}`."*

I did not read it, spent a long time diagnosing the build as if it were a novel problem,
**repeated the documented mistake on the car**, and had to restore the dirtied files. Everything
in §1 below was already known.

**Procedure: before writing any plan, read — in this order —**
1. `captains_log.md` (repo root) — the operator's own hard-won notes
2. `CLAUDE.md` (repo root and `~/`) — standing rules
3. any `PROGRESS.md` / prior plan docs
4. `git log` on the branch

## 0.2 The prime directive for a fork

> **A fork feature must never be able to degrade the base system.**
> Its failure mode is *disabled*, never *crash*, never *invalid plan*, never *unbootable*.

Three separate bugs in this port violated that and each would have hurt the car. They are §2.1,
§2.2, §2.3. Treat that list as a checklist for any future fork feature, not as history.

---

# 1. HARD CONSTRAINTS OF THIS BRANCH (`nightly-dev` is PREBUILT)

This branch ships a `prebuilt` marker at the repo root and **runs committed binaries**.
`launch_chffrplus.sh` gates the build on `[ ! -f $DIR/prebuilt ]`. Everything below follows.

| Constraint | Consequence | What to do |
|---|---|---|
| **Never run `scons` here** | Fails while *reading* SConscripts: `driving_supercombo.onnx` is absent. That ONNX is the build *input* which compiles into the shipped `driving_tinygrad.pkl` chunks; it is git-LFS and never fetched. The attempt also dirties `panda/board/obj/{gitversion.h,version}`. | Don't build. If you did: `git checkout -- panda/board/obj/gitversion.h panda/board/obj/version` |
| **Do NOT delete the `prebuilt` marker** | ~~Original plan Phase -1 said delete it so the device rebuilds cereal at boot.~~ **That would strand the device in the fallback launcher**, because the build cannot succeed. | Leave it. Python changes need no build. |
| **C++ never changes** | `params_keys.h`, `services.h`, `loggerd`, `Params` are all compiled artifacts frozen at the last real build. | Design so nothing depends on recompiling. |
| **Python DOES take effect immediately** | `cereal/__init__.py` calls `capnp.load()` on the `.capnp` files at **runtime**. Schema changes are live with zero compilation. | This is why the port works at all. |

### 1.1 What "C++ never changes" actually costs you

- **`params_keys.h` is inert.** Any fork key raises `UnknownKeyName` forever.
  `Params::clear_all()` unlinks every file in the params dir whose name is not in the
  **compiled** table (`params.cc`: `if (it == keys.end() || …) unlink(...)`).
  → **Fork config must live OUTSIDE `/data/params`.** See §4.2.
- **`services.h` is inert.** `loggerd` is C++ and reads the compiled table, so
  **`mapdOut` is NEVER written to rlog** — confirmed: 0 frames across a whole drive while
  `carState` had 2,738 per segment. ~~The original plan said "mapdOut is logged so drives can
  be replayed for tuning."~~ **False here.**
  → **`/data/media/0/mapd_debug.log` is the ONLY tuning instrument. Protect it.**

---

# 2. THE THREE SAFETY BUGS (each one would have hurt the car)

## 2.1 An unregistered param crashed plannerd

`SmartCruiseControlMap.__init__` called `params.get_bool(...)`, and `hooks.limit_v_cruise`
called the **constructor outside its try/except**. On a prebuilt branch that raises
`UnknownKeyName` → the exception propagates into plannerd → **longitudinal planning dies**.

**Rule:** construction *and* update must both be guarded, and a construction failure must latch
so it is not retried every frame. Param reads use a `get_bool_safe()` that returns `False` on
any failure.

## 2.2 Adding a service to SubMaster invalidated the longitudinal plan

`plannerd` sets `msg.valid = sm.all_checks()`, and `all_checks() = all_alive() and
all_freq_ok() and all_valid()`. mapd only runs when tiles are installed, so on any device
without tiles `mapdOut` is never alive → **`longitudinalPlan` marked INVALID** → longitudinal
control faults. This happens *regardless of whether the feature is switched on*.

Proven on the car: without ignores `all_checks()` = `False`; with them, `True`.

**Rule:** any optional service added to a SubMaster **must** be passed in
`ignore_alive`, `ignore_valid` and `ignore_avg_freq`.

## 2.3 A wrong field name made the feature a silent no-op for a whole drive

```
scc_map.py: raw_has_lead = lead1.status or lead2.status
AttributeError: struct has no such member; name = status
```
openpilot's `radarState.LeadData` has **`present`**, not `status` (sunnypilot's name). The
controller raised on **every frame — 38,300 in one drive**. The guard from §2.1 caught each one
and returned `v_cruise` unchanged, so the car drove fine and **nothing whatsoever happened**.

Two failures compounded here:
- **The tests validated my assumption, not reality.** Unit tests stub cereal with
  `SimpleNamespace`, and my stub *also* used `status`. 29/29 passed while the car threw
  continuously.
- **The guard was silent-in-effect.** A totally dead feature looked identical to a working one.

**Rules:**
1. **Schema conformance test is mandatory** — `openpilot/grt/tests/test_schema_conformance.py`
   loads the real `log.capnp` via pycapnp and asserts every cereal field the fork reads exists.
   Verify it *fails* on a deliberately wrong name before trusting it.
2. **Stubs must mirror the real schema.** A stub is an assumption written down.
3. **Never port a field name from sunnypilot without checking it against this cereal.**

---

# 3. THE CONTROL DESIGN (this openpilot arbitrates by `min()`)

~~Original plan: thread `a_min_override` through `long_mpc.update(radarState, v_cruise, …)`.~~
**That signature does not exist here.** `long_mpc.update()` takes no `v_cruise`. The planner:

```python
a_cruise = clip(v_cruise - v_ego, A_CRUISE_MIN=-1.2, max_accel)   # crude P-controller
candidates = [(a_mpc, mpc, …), (a_cruise, cruise, …)] (+ e2e if experimental)
output_a_target, source, _ = min(candidates, key=lambda c: c[0])
```

So there are exactly two injection points, both one line, both sentinel-wrapped:

- **Hook 1** — lower `v_cruise` *before* `get_cruise_accel`. Delivers curve / speed-limit /
  hazard targets. Must only ever LOWER, so a `forceDecel` `v_cruise = 0.0` still wins.
- **Hook 2** — append an accel candidate *before* the `min()`. Since `a_cruise` saturates at
  `A_CRUISE_MIN = -1.2` and the hazard decel spans `[-1.5, -0.3]`, this only *binds* when
  harder than the cruise floor — it can never brake more weakly than stock.

## 3.1 THE BIG CONTROL LESSON: never command a step, command a profile

**Measured on the first working drive:**

| approach | decel needed | decel used | result |
|---|---|---|---|
| turning_circle 69→20 km/h over 586 m | −0.28 | **−1.64** | 5.8× too hard, target 47 m early |
| stop 63→20 km/h over 830 m | −0.17 | **−1.69** | 10× too hard, target **365 m early** |

**Cause:** `v_target` stepped straight to the final speed the moment the hazard came into range.
With a 40 km/h error the P-controller instantly saturates at `A_CRUISE_MIN`, dumps the speed as
fast as possible, and coasts the rest of the way.

**Fix — command the speed you should be at *now* to arrive exactly AT the hazard:**

```
v_now = sqrt(v_target² + 2 · APPROACH_DECEL · distance)        APPROACH_DECEL = 0.5 m/s²
```

- `distance = 0` → exactly the target
- far away → high, no effect
- hazard appears late → small `d` → low command → self-escalates to harder braking (safe)

Apply it to **hazards** *and* **upcoming lower speed limits**, and use the same decel for the
engagement latch distance.

**Corollary — disable mapd's own lookahead.** `slow_down_for_next_speed_limit: false`.
~~Original Phase 6 said "mapd already does the lookahead, don't duplicate it".~~ Reversed on
evidence: mapd's lookahead (`speed_limit.go:111`) steps `speedLimitSuggestedSpeed` down to the
upcoming limit while the sign is still far away — the identical harsh step, from mapd's side.
Current-limit ceiling still comes from `speedLimitSuggestedSpeed`; **the approach is ours to shape.**

**Generalisation for any future map/vision speed feature:** a speed *ceiling* and a speed
*approach* are different things. Ceilings may be applied instantly; approaches must be shaped
by distance.

---

# 4. MERGE-PROOF STRUCTURE (this held up well — keep it)

All fork code lives in **`openpilot/grt/`**; upstream files carry only thin GRT-MOD-wrapped hooks.
Final footprint: **8 upstream files, 190 insertions**, of which 145 are inert capnp schema —
**the code to re-verify each sync is ~45 lines across 6 files.** See `GRT_MODS.md`.

## 4.1 Registration splices — one exception that matters

`cereal/services.py` **must be edited inline, NOT via an import.**
~~Original plan: `from openpilot.grt.registry import GRT_SERVICES; _services.update(...)`.~~
**This breaks the build**: `services.py` is executed as a standalone script to generate
`services.h`, where the repo root is not on `sys.path` → `ModuleNotFoundError: No module named
'openpilot'`. Verified empirically. Keep the 3 service entries inline and do **not** duplicate
them in `registry.py`.

Also: keep `registry.py` **import-free at module level** — it is imported by `services.py`-adjacent
early code and must not drag in capnp/zmq. Build the process list lazily inside a function.

## 4.2 Config that survives a prebuilt branch

- Fork flags: plain files under **`/data/media/0/grt/<KeyName>`** (`1`/`true`/`on`/`yes`).
  Outside `/data/params`, so `clear_all()` can never delete them. Survives reboots — proven.
- `get_bool_safe()` tries **Params first, then the file** → upgrades cleanly if the keys are
  ever compiled in, works today when they are not.
- `MapdSettings`: mapd reads a fixed absolute path inside the params dir, so `clear_all()` will
  delete it. **Solution: mapd's own launch command writes it immediately before `exec`.**
  One atomic write (`tmp` + `os.replace`) per mapd start — *not* sunnypilot's 1 Hz rewrite loop.
  Deletion afterwards is harmless: mapd holds settings in memory.

---

# 5. DEPLOYMENT PROCEDURE (what actually worked)

1. **Recon first, read-only.** Device branch/HEAD vs your base, `prebuilt` present?, tiles
   present?, disk free, `IsOffroad`, AGNOS `/VERSION` vs `launch_env.sh`.
2. **Transfer with a git bundle**, never raw file copies:
   ```
   git bundle create mapd.bundle <base>..nightly-dev
   scp mapd.bundle comma4:/tmp/ && ssh comma4 'cd /data/openpilot &&
     git fetch /tmp/mapd.bundle nightly-dev:refs/remotes/bundle/x && git merge --ff-only refs/remotes/bundle/x'
   ```
   Atomic, verifiable, no GitHub push, and it honours `CLAUDE.md`'s "never SCP cereal files".
3. **Tiles.** `<MAPD_ROOT>/offline/<band>/<lon>/…`.
   **BAND = `floor(lat/2)*2`, NOT the latitude.** Tiles for lat −34.x live in dir **−36**.
   Deploying `-34` produced `could not unmarshal offline data` and `tileLoaded=False`.
   Get the device's real position first (subscribe to `gpsLocationExternal`) and deploy that band.
   Use `rsync -aq` — **not** `--info=progress2`, which floods the transcript.
4. **Reboot to apply.** Never `pkill -f` over ssh — it matches your own session and kills it
   (documented in captains_log, twice). Managed processes don't restart on kill anyway.
5. Verify §6, then enable the flag, then reboot again.

---

# 6. VERIFICATION — what is actually runnable

~~"Clean local `scons -j4`"~~ is impossible on the Pi5 (no scons/cmake/capnproto) **and wrong**
on the device (prebuilt). Replace with:

| # | Check | Where |
|---|---|---|
| 1 | **Schema conformance** — every cereal field the fork reads exists | Pi5 + device |
| 2 | Unit suites (`test_scc_map`, `test_hooks`) | Pi5 + device |
| 3 | **Wire compat**: union discriminants pristine vs branch vs the Go schema all equal (141/142/143), 24 fields, round-trip | Pi5, then device |
| 4 | `services.py` still runs standalone and emits `queue_size 2097152` for the mapd services | Pi5 |
| 5 | **Phase 0 gate**: run mapd by hand, `tileLoaded=True`, messages decode | device |
| 6 | Boot clean: manager + plannerd/controlsd/selfdrived/card/modeld up, nothing crash-looping, **`longitudinalPlan VALID=True`** | device |
| 7 | **Zero `grt:` exceptions since boot** — compare swaglog mtimes against `uptime -s`; do not count old files | device |
| 8 | **`mapd_debug.log` is growing** — this is the proof the controller actually runs | device |
| 9 | Road test, then measure the log (§7) | car |

**Gate 7+8 together are the ones that would have caught the silent no-op.** A feature that
throws every frame looks *exactly* like a working one from the outside — except the debug log
is missing and swaglog is full.

---

# 7. TUNING FROM DATA (the loop that actually works)

`mapd_debug.log` is JSON-lines, every 10th frame. Pull it and measure — never tune by feel alone.

For each approach episode compute:
- `needed = (v_target² − v_ego²) / (2·d₀)` — the decel required to land ON the hazard
- `used = min(a_ego)` over the episode
- distance remaining when the target speed was first reached (**want ≈ 0**)

If `used / needed` ≫ 1 or the target is reached early, the command is stepping rather than
profiling. **Knob: `APPROACH_DECEL` in `openpilot/grt/scc_map.py` — lower = gentler, starts earlier.**

Parse defensively: `except Exception: continue` around `json.loads`/`LogReader` hid a real error
from me for several minutes. Log the error instead of swallowing it.

---

# 8. SMALL FACTS THAT COST TIME

- `radarState.LeadData` → `present`, `dRel`, `modelProb`. **No `status`.**
- `MapdOut` is **24 fields, `@0`–`@23`**. ~~Not 25.~~ `nextHazardSpeedTarget @24` exists only in
  sunnypilot's *python* schema, never in the Go schema the binary was built from.
- Union **discriminants** (141/142/143) are what goes on the wire — **not** the `@143` ordinal.
  Compare discriminants, three ways, when verifying wire compat.
- `carControl` was **already** in plannerd's SubMaster.
- `selfdrived` has **no** `ignored_processes` in this version — adapt the `not_running`
  comprehension instead of copying sunnypilot's line.
- `Paths` is `openpilot.common.hardware.hw`; but put `MAPD_ROOT` in `grt/registry.py` and avoid
  editing `hw.py` entirely — one less touchpoint.
- `/usr/local/venv/bin/python` is a **symlink to `/usr/bin/python3.12`**, so `readlink -f` and a
  non-login `which` both mislead. Use a login shell, or trust `sys.executable`.
- Way selection needs a **heading**: parked (`vEgo=0`, `bearing=0`) mapd always reports
  `waySelectionType=fail` with an empty `roadName`. Not a bug; nothing more can be proven parked.
- Binary md5 `0c3b552c229addc273e2c39c28924fbc` (21211912 bytes). The stale `mapd_arm64`
  (`2dda8f6e…`) predates the T-junction fix. Never run `mapd_installer.py` — it fetches
  *upstream* mapd and silently destroys every fork feature.

---

# 9. CURRENT STATE (2026-07-29)

Implemented, deployed, driven. Speed ceiling **ON**; firm hazard accel (Hook 2,
`SmartCruiseControlMapHazardAccel`) still **OFF** by design until the ceiling behaviour is
signed off. Approach profile at `APPROACH_DECEL = 0.5 m/s²` awaiting its first drive.

Enable/disable without any rebuild:
```
echo 1 > /data/media/0/grt/SmartCruiseControlMap        # or 0 to disable
```
Kill switch needs no reflash and no reboot to take effect within ~3 s (param poll period).

**Next:** drive, pull `mapd_debug.log`, measure `used/needed` and the distance-remaining, and
adjust `APPROACH_DECEL` only if the data says so.
