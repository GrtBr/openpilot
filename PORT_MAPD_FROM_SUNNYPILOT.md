<!--
  Map-based speed features on a PREBUILT openpilot fork — PLAN + HARD-WON LESSONS.
  Rewritten 2026-07-29 after the mapd port was implemented and driven three times on a Hyundai
  Staria, then extended the same day with the set-speed-tracking feature (§11).

  This is no longer a forward-looking guess. Every "trap" below is something that ACTUALLY
  WENT WRONG on this vehicle, with the evidence. The original plan's advice is preserved only
  where it survived contact with the car; where it was wrong, the wrong advice is shown struck
  through with what actually happened, because knowing WHY a plausible instruction was wrong is
  what stops it being reinvented.

  Two features are covered and they share every constraint:
    A. mapd longitudinal control — curve/hazard/speed-limit slow-downs (§1-§8)
    B. set-speed tracking — the DRIVER-FACING set speed follows the posted limit (§11)
  §9 is the reusable recipe distilled from both. Read §0 and §9 before planning a third.

  Status: both implemented, deployed and driven. Remaining work is tuning and one unverified
  claim (does the confirmation alert render? — §11.6).
-->

# Map-based speed features on a prebuilt openpilot fork — implementation record + hardened plan

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

Six separate bugs across the two features violated that, and each would have hurt the car or
silently killed the feature. They are §2.1–§2.6. Treat that list as a checklist for any future
fork feature, not as history.

## 0.3 A passing test suite is evidence your tests agree with your model — not that your model is right

**This is the single most important lesson of the whole session, and it cost two drives.**

- The mapd controller shipped with 29/29 green while it threw an `AttributeError` on every
  frame on the car (§2.3). The stub and the code shared the same wrong field name.
- The set-speed feature reached **104 passing tests** while containing a gate that silently
  dropped a limit forever (§2.5) and a terminal state that killed the feature for a whole drive
  (§2.6). One test actively *asserted the buggy behaviour was correct*.

**Every defect in the set-speed feature was found by review, not by tests.** Tests confirmed the
code did what I thought; they could not tell me what I thought was wrong. So:

1. **Review the design before writing it, and the diff before deploying it.** Budget for it.
2. Ask specifically for the classes tests structurally cannot see:
   *blast radius per process* (§2.4), *exact-frame gates* (§2.5), *terminal states with no
   recovery* (§2.6), *the fail-safe's own side effects* (§2.1).
3. Prefer checks that consult **reality** over checks that consult your model — the schema
   conformance test (§2.3), the real-import gate (§6 #2b), and the device queries below.

## 0.4 Settle design questions with device data BEFORE choosing thresholds

Two one-line queries against data already on the car settled two design arguments before any
code was written. Both were cheap; neither was obvious in advance.

| Question | Query | Answer | What it changed |
|---|---|---|---|
| What IS the set speed in practice? | count `v_cruise_kmh` while moving in `mapd_debug.log` | **105 km/h for 1,918 frames**, 145 for 26 | Killed the whole first design — see §11.1 |
| Are posted limits ever non-round? | distinct `speed_limit_suggested_kmh` values | only **20/40/60/80/120** | Proved the round-number rule cannot latch the feature off today |

**Generalisation: a threshold is only meaningful relative to the operating point.** Before
choosing one, measure where the system actually sits. `carState.vCruise` and `vCruiseCluster`
ARE in rlog even on a prebuilt branch, so driver-facing state is always retrospectively
measurable, unlike `mapdOut` (§1.1).

## 0.5 Get the operator's rules in their terms, then restate them as testable predicates

The operator's spec arrived as prose with an inconsistent worked example (it said "103 km/h"
then referred to the same value as "105 km/h"). Restating it as explicit predicates and
**putting the ambiguity back to them as a multiple-choice question** was what turned it into
something implementable. Two of the three answers changed the design materially.

Do not silently pick a reading of an ambiguous spec when the readings produce different cars.

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

# 2. THE SIX SAFETY BUGS (each would have hurt the car or silently killed the feature)

## 2.1 An unregistered param crashed plannerd

`SmartCruiseControlMap.__init__` called `params.get_bool(...)`, and `hooks.limit_v_cruise`
called the **constructor outside its try/except**. On a prebuilt branch that raises
`UnknownKeyName` → the exception propagates into plannerd → **longitudinal planning dies**.

**Rule:** construction *and* update must both be guarded, and a construction failure must latch
so it is not retried every frame. Param reads use a `get_bool_safe()` that returns `False` on
any failure.

**Second half of the same rule — the guard's own logging must not become the new failure.**
`cloudlog.exception` inside a per-frame guard is itself unbounded: the §2.3 bug produced
**38,300 exception logs in one drive at 20 Hz**, and the same bug in `card` would be ~190,000 at
100 Hz, in the loop that has to hold a CAN deadline. Route guard logging through a counter that
logs the first occurrence and then every Nth, carrying the running count
(`hooks._log_exception`). A fail-safe that floods is not a fail-safe.

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

## 2.4 The same SubMaster mistake, but in a process where it blocks ENGAGEMENT

§2.2 is not one bug, it is a **class**, and the blast radius depends entirely on which process
you splice into. Before adding a service to any SubMaster, grep that file:

```
grep -n "all_checks\|all_alive\|all_valid\|all_freq_ok" <process>.py
```

| Process | What it does with the checks | Blast radius of a missing fork service |
|---|---|---|
| `card` | `all_checks(['carControl'])` — **scoped** | none; scoping already protects you |
| `plannerd` | `msg.valid = sm.all_checks()` — unscoped | `longitudinalPlan` INVALID → longitudinal control faults |
| `selfdrived` | `all_checks()` unscoped at `:381` (raises `commIssue`) **and `:469`, where it gates `self.initialized`** | **openpilot will not engage at all** |

The `selfdrived` case is the worst and was caught *before* writing the code, purely by running
that grep. **Always pass `ignore_alive` / `ignore_valid` / `ignore_avg_freq` — and know what you
were protecting against, because "it worked in `card`" does not transfer.**

Verified on the car after deploy: `onroadEvents` carried only `wrongGear` and
`seatbeltNotLatched` — the legitimate physical blockers — and **no `commIssue`**.

## 2.5 A gate that fires on an EXACT frame drops the event permanently

```python
self._cand_frames += 1
if self._cand_frames != STABLE:     # "fires once"
    return unchanged
if driver_is_pressing_buttons:
    return unchanged                # ← returns without recording a decision
```

If the skip condition happens to be true on the single frame the counter equals `STABLE`, the
counter keeps incrementing and `!=` is never true again. **That limit is never adopted, for the
rest of the drive, silently.** A test asserted the no-change and blessed it.

**Rule:** a "once" gate must be `>=`, with idempotency provided by a separate
*decision-recorded* flag. Then every skip is a **deferral to the next clear frame**, not a drop.
Distinguish deliberately, in code and in comments, between:

- **deferral** — return *before* recording a decision; it will be reconsidered
- **decision** — record it; it will not be revisited

Two gates in the set-speed feature are deferrals on purpose (driver on the buttons; an upward
adopt on a merely `predicted` way) and it is not obvious from the shape of the code which is
which. Say so explicitly.

## 2.6 A terminal "handled" state left the feature dead for the rest of the drive

The confirmation prompt marked a limit as *decided* when it expired unanswered. A driver who
simply **missed one prompt** was then stuck with the old set speed — no further prompt, no
adopt, for the whole drive — while the debug heartbeat reported a reassuring `already_handled`
the entire time. (The original illustration used the 60 km/h no-map seed, which made it far
worse: 60 in a 100 zone. That seed has since been removed — §11.5 — but the terminal-state bug
was independent of it.)

**Rules:**
1. **Audit every state that is entered and never left.** For each, ask: *what re-opens this?*
   If the answer is "nothing until a reboot / a new drive", that is a bug unless it is a
   deliberate latch with a stated reason.
2. Distinguish **the driver answered** from **the driver did not answer**. An explicit decline
   is final; an unanswered prompt is re-offered after a cooldown (`REOFFER_S = 60 s`).
3. A log line that reads as benign (`already_handled`) while the feature is dead is a
   monitoring bug of its own — see §7.1.

**Structural fix, and the general rule:** the bug lived in the seam between four overlapping
state variables. Collapsing them so **each fact has exactly one writer** —
`_owned_kph` ("the set speed WE established") and `_in_force_kph` ("the posted limit in force",
recorded at a single point as a fact about the road, independent of any decision) — fixed the
class rather than the instance. **When state variables overlap, the bugs live in the seams.**

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

## 3.2 There are TWO speed variables and they live in different processes

This confused the operator ("why doesn't the MAX display change?") and it is worth being
explicit about, because the answer is *by design*, not a bug.

| | planning-local `v_cruise` | driver-facing set speed |
|---|---|---|
| Owner | `longitudinal_planner.update()` | `VCruiseHelper` in **`card`** |
| Rate | 20 Hz (`DT_MDL`) | **100 Hz (`DT_CTRL`)** |
| Touched by | Hooks 1 & 2 (`scc_map`) | Hook 3 (`set_speed`) |
| Visible to driver? | **No.** Never reaches `carState`. | **Yes** — see the chain below |
| Lifetime | transient; the approach profile deliberately commands intermediate values | sticky; it is a setting |

The driver-facing chain, verified on this car:

```
VCruiseHelper.v_cruise_kph ─→ carState.vCruise        ─→ comma UI "MAX"
VCruiseHelper.v_cruise_cluster_kph ─→ carState.vCruiseCluster
      └─→ controlsd hudControl.setSpeed ─→ hyundai/carcontroller.py set_speed_in_units ─→ CLUSTER
```

Preconditions: `CarParams.pcmCruise == False` **and** `openpilotLongitudinalControl == True`, so
openpilot owns the set speed via `_update_v_cruise_non_pcm`. On a PCM car the set speed is read
off CAN every frame and any write would flap — **guard on `pcmCruise` even if your car isn't
one**, because `card.py` is shared by every port.

**Consequences for anything that writes the set speed:**
- Write **both** `v_cruise_kph` and `v_cruise_cluster_kph`. Upstream keeps them equal inside
  `update_v_cruise`; a hook running after it that sets only one desynchronises the cluster from
  the planner.
- **Frame counts are in `DT_CTRL`.** A 10 s window is 1000 frames here, not 200. Mixing up the
  two rates silently changes every timeout by 5×.
- Never compare set speeds with `==`. They have been through `round(v, 1)`, `+= 1.0` and
  `np.clip`. One `99.99999` and an ownership test fails forever — and the symptom reads as "the
  feature got annoying", not as a bug. Use an epsilon (`SPEED_EPS_KPH = 0.5`).

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

## 4.3 A fork-owned message between two openpilot processes

Needed when the state lives in one process and the consequence in another (the set-speed pending
state lives in `card`; only `selfdrived` can raise an alert). **Both ends are Python, so this
needs no C++ build** — `mapdIn` already proves openpilot Python can publish on a service absent
from the compiled `services.h`. Don't spend a device session testing that.

The pattern, which is the same one that made the mapd schema safe:

1. **Rename a `CustomReserved<N>` struct IN PLACE** in `custom.capnp` — keep the `@0x…` struct
   ID — and rename the matching `log.capnp` union member **keeping its `@N` ordinal**. Adding
   fields to a previously-empty reserved struct is free; nothing consumes it.
2. Inline a `services.py` entry (see §4.1 — it cannot import `openpilot.grt`). Small queue,
   modest rate, `should_log=False` (loggerd is inert anyway, §1.1).
3. Publish from the owning process **at a sane rate, not the loop rate**: `card` is 100 Hz, so
   publish on `frame % 5` for 20 Hz. Verified: 240 msgs in 12 s.
4. Subscribe in the consumer **with all three ignore lists** — and read §2.4 first.
5. **Assert the wire discriminants in a test**, not by hand. `grtSetSpeedState` occupies
   discriminant 140; mapd's must stay 141/142/143. `test_schema_conformance.py` now checks all
   four on every run, on the Pi5 *and* on the device against its own `log.capnp`.

17 reserved slots (`CustomReserved0`–`16`) remain, minus the one now used.

## 4.4 A driver-facing alert needs NO schema change

**I assumed this required a new `EventName` enumerant and planned an on-device experiment to see
whether one would render on a frozen-artifact branch. That was wrong, and reading the alert
path settled it in minutes.**

```
selfdrived.update_alerts:  AM.add_many(frame, alerts)   ← keys on alert.alert_type, a plain STRING
selfdrived.publish_selfdriveState:
    ss.alertText1 = AM.current_alert.alert_text_1       ← free-form Text
    ss.alertSound = AM.current_alert.audible_alert      ← existing AudibleAlert enum
ui / soundd consume selfdriveState — NOT onroadEvents.
```

So a fork alert is just an `Alert` object with a fork-owned `alert_type` string, added via one
line in `update_alerts`. No enumerant, no `EVENTS` entry, nothing to recompile.

- Use `event_type = ET.WARNING`: `update_alerts` only clears WARNING when the state machine is
  not warning-capable, i.e. when **not engaged** — which is also the only time these features
  run, so it survives exactly when you need it.
- **Corollary: an alert cannot be tested while parked**, because a disengaged car clears it.
  Plan for that being unverifiable until the first drive (§11.6).
- Respect `is_metric` for the text.

---

# 5. DEPLOYMENT PROCEDURE (what actually worked)

1. **Recon first, read-only.** Device branch/HEAD vs your base, `prebuilt` present?, tiles
   present?, disk free, `IsOffroad`, AGNOS `/VERSION` vs `launch_env.sh`.
2. **Transfer with a git bundle**, never raw file copies. Both deploys used a bundle; this is
   verbatim what was executed for the set-speed deploy:
   ```
   git bundle create setspeed.bundle dcb3550cac..nightly-dev   # base the DEVICE already has
   git bundle verify setspeed.bundle                           # names its prerequisite ref
   scp setspeed.bundle comma4:/data/
   ssh comma4 'cd /data/openpilot && git status --porcelain'   # must be empty; if not, STOP
   ssh comma4 'cd /data/openpilot && git fetch /data/setspeed.bundle nightly-dev &&
               git merge --ff-only FETCH_HEAD'
   ```
   Atomic, verifiable, no GitHub push, honours `CLAUDE.md`'s "never SCP cereal files", and
   `--ff-only` refuses rather than inventing a merge if the device has drifted. Delete the
   bundle from `/data` afterwards. Bundle the *whole* fork range, not just new commits — the
   device may be several commits behind (it was 8) and `git bundle verify` will tell you the
   one ref it needs.
3. **Tiles.** `<MAPD_ROOT>/offline/<band>/<lon>/…`.
   **BAND = `floor(lat/2)*2`, NOT the latitude.** Tiles for lat −34.x live in dir **−36**.
   Deploying `-34` produced `could not unmarshal offline data` and `tileLoaded=False`.
   Get the device's real position first (subscribe to `gpsLocationExternal`) and deploy that band.
   Use `rsync -aq` — **not** `--info=progress2`, which floods the transcript.
4. **Stop openpilot, then reboot to apply.**
   **The `pkill -f` rule, corrected — the earlier blanket ban was too strong.** A bare
   `pkill -f manager.py` sends its pattern through your own ssh command line, matches itself and
   kills the session (it did, twice, with silent empty output). The bracket form does **not**,
   because the literal text `[m]anager\.py` is not matched by the regex it compiles to:
   ```
   pkill -f "[m]anager\.py"     # works over ssh — used in both deploys
   ```
   `pkill -x manager.py` also avoids self-match but only matches on process *name*.
   Either way, keep the pattern string out of the rest of the command.
5. Verify §6, **then** enable the flag. **No second reboot is needed** — `update_params()`
   re-reads the flag every 3 s, so a running `card`/`plannerd` picks it up in place. (The
   2026-07-28 mapd deploy rebooted here; it was unnecessary.)
6. **The device may be asleep.** A parked comma powers down; ssh gives *"No route to host"* and
   100% ping loss. That is not a fault. Stage the bundle and the runbook, and retry — in this
   session it returned within minutes. Never auto-deploy on reconnect: it may have come back
   because someone started driving.
7. AGNOS: compare `AGNOS_VERSION` in `launch_env.sh` against `/VERSION` **before** rebooting. If
   they differ, the reboot will run the OS updater instead of manager, which waits for on-screen
   confirmation and looks exactly like a hang over ssh (see captains_log, 2026-07-28).

---

# 6. VERIFICATION — what is actually runnable

~~"Clean local `scons -j4`"~~ is impossible on the Pi5 (no scons/cmake/capnproto) **and wrong**
on the device (prebuilt). Replace with:

| # | Check | Where |
|---|---|---|
| 1 | **Schema conformance** — every cereal field the fork reads exists | Pi5 + device |
| 2 | Unit suites (`test_scc_map`, `test_hooks`, `test_set_speed`) | Pi5 + device |
| 2b | **Real-import gate** — `import` every fork module against the ACTUAL openpilot deps, build one of each fork message, construct the controllers | **device only** |
| 3 | **Wire compat**: union discriminants — mapd 141/142/143 and `grtSetSpeedState` 140 — pristine vs branch vs the Go schema; 24 fields; round-trip | Pi5, then device |
| 4 | `services.py` still runs standalone and emits `queue_size 2097152` for the mapd services | Pi5 |
| 5 | **Phase 0 gate**: run mapd by hand, `tileLoaded=True`, messages decode | device |
| 6 | Boot clean: manager + plannerd/controlsd/selfdrived/card/modeld/mapd up, `managerState` reports nothing `shouldBeRunning`-but-not-running, **`longitudinalPlan VALID=True`** | device |
| 6b | **ENGAGEMENT IS NOT BLOCKED** — `onroadEvents` contains only legitimate physical blockers (`wrongGear`, `seatbeltNotLatched`) and **no `commIssue`** | device |
| 7 | **Zero `grt:` exceptions since boot** — compare swaglog mtimes against `uptime -s`; do not count old files | device |
| 8 | **The feature's debug log is growing** — this is the proof the controller actually runs | device |
| 9 | Fork messages arriving **at the designed rate** (e.g. `grtSetSpeedState` 240 msgs / 12 s = 20 Hz) | device |
| 10 | Road test, then measure the log (§7) | car |

**Gate 7+8 together are the ones that would have caught the silent no-op.** A feature that
throws every frame looks *exactly* like a working one from the outside — except the debug log
is missing and swaglog is full.

**Gate 2b is not redundant with gate 2.** The unit suites run against `SimpleNamespace` stubs so
they work on a Pi5 that cannot import openpilot; that is precisely why they cannot catch an
import error, a missing service registration, or a wrong cereal name. Gate 2b is the cheapest
check that consults reality:

```
ssh comma4 'cd /data/openpilot && PYTHONPATH=/data/openpilot /usr/local/venv/bin/python -c "
from openpilot.grt import hooks, set_speed, registry
from openpilot.cereal.services import SERVICE_LIST
import openpilot.cereal.messaging as messaging
print(\"grtSetSpeedState registered:\", \"grtSetSpeedState\" in SERVICE_LIST)
messaging.new_message(\"grtSetSpeedState\")
print(\"tracker enabled:\", set_speed.SetSpeedLimitTracker().enabled)"'
```

**Gate 6b is the one that matters most after a `selfdrived` change** (§2.4). Run it *before*
enabling any flag — engagement can be broken by the plumbing alone, with the feature off.

**Run gates 1, 2b and 3 BEFORE the reboot.** They need only the updated files on disk, and
finding a schema or import problem while the old processes are still running is much cheaper
than finding it in a boot loop.

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

## 7.1 Instrument the NEGATIVE case — log why nothing happened

Decision-only logging is a trap. If a feature never fires on a road test, a decisions-only log
is **empty**, and every possible cause looks identical. Since `mapdOut` is not in rlog (§1.1),
there is no retrospective path either — you get nothing from a whole drive.

**Rule: emit a throttled heartbeat naming the gate that rejected**, alongside the decision
lines. `set_speed.log` carries `mapd_not_alive`, `no_tiles`, `way_fail`, `no_limit_posted`,
`implausible_limit`, `settling`, `already_handled`, `driver_busy`, `defer_up_on_predicted`, plus
the raw m/s value, `waySelectionType` and the candidate counter. Throttle it (2 s) — these loops
are 20–100 Hz — and let a real decision reset the throttle so decisions are never suppressed.

Two things this buys that nothing else does:

- It distinguishes *"the gate rejected"* from *"the code never ran"*, which is the §2.3 failure.
- It exposes gates that can **never** be satisfied. Example worth watching for: a
  `waySelectionType` flickering between `current` and `fail` faster than the 1 s stability
  window resets the candidate counter every time, so a limit never settles and **no decision is
  ever logged at all**. Only the heartbeat makes that visible.

**And make sure a benign-sounding reason is not hiding a dead feature** — `already_handled` was
exactly that in §2.6. When adding a reason string, ask what it would look like if it were the
*only* thing being logged for an hour.

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
- **`VCruiseHelper.update_v_cruise(CS, enabled, is_metric)` has no SubMaster**, and
  `selfdrive/car/tests/test_cruise_speed.py` calls it directly in **five** places. Changing that
  signature to pass map data breaks an upstream suite. Hook into **`card.py`** instead, which
  already owns both the helper and a SubMaster — `cruise.py` then needs no fork edit at all.
- The set-speed hook must sit **after** `initialize_v_cruise` (which resets `v_cruise` on the
  engage edge) and **before** the `CS.vCruise` assignment. Mirror upstream's own engage
  condition into a local rather than recomputing it differently.
- Upstream's `_update_v_cruise_non_pcm` acts on button **release**. A fork hook that runs after
  it and assigns an absolute value should match that edge, or an adopted value lands on
  `limit ± 1` from upstream's own bump.
- Hyundai RES/+ is `ButtonType.accelCruise` — verified statically in
  `opendbc/car/hyundai/carstate.py` `BUTTONS_DICT`, not assumed from a log.
- `V_CRUISE_INITIAL = 40`, `V_CRUISE_INITIAL_EXPERIMENTAL_MODE = 105`. With
  `ExperimentalMode=1` the set speed starts at **105** and, on this car, stays there all drive.
- `mapdOut.speedLimit` and its siblings are **m/s**; the driver-facing set speed is **km/h**.
  Convert once, and reject implausible values rather than clamping them — a rejected
  `[20, 145] km/h` band doubles as a units-error trap, whereas clamping launders a bad value
  into a legal set speed.
- `AlertManager.add_many` keys on `alert.alert_type` (a plain string) — see §4.4.
- `openpilot/selfdrive/ui/` is **Python** in this version (`ui.py`, `soundd.py`), which is why
  the whole alert path is reachable without a build.
- On device, `/usr/local/venv/bin/python` runs the grt suites and pycapnp fine; the Pi5 needs
  the repo-local `.venv` for pycapnp and plain `python3` for the stubbed suites (**no numpy** —
  keep fork modules numpy-free so they stay testable there).

---

# 9. RECIPE FOR THE NEXT FORK FEATURE

Ordered. Steps 1–3 are where the value is; skipping them is what produced every bug in §2.

### Phase A — before writing anything

1. **Read the prior art** in the order of §0.1. It has already answered something.
2. **Measure the operating point** (§0.4). Query the device's own logs for the variables your
   thresholds will be relative to. Two queries settled two design arguments here.
3. **Restate the operator's spec as testable predicates**, and put genuine ambiguities back to
   them as a choice (§0.5). Don't pick a reading when the readings produce different cars.
4. **Locate the injection points and read what surrounds them.** For every process you will
   touch, run the §2.4 grep and write down the blast radius *before* you decide to touch it.
5. **Get the design reviewed** (§0.3). Cheaper than the deploy that follows it.

### Phase B — build

6. **Fork-owned module in `openpilot/grt/`**; upstream files get one sentinel-wrapped line each.
   Record every touchpoint in `GRT_MODS.md` with its category and *why*.
7. **New flag, default OFF**, as a file under `/data/media/0/grt/` (§4.2), read through
   `get_bool_safe`. Register it in `grt_params_keys.inc` too, for the day the branch is buildable.
8. **Guard the hook** — construction *and* update, latched, with counted logging (§2.1).
   Add the `pcmCruise` bail if you touch the set speed (§3.2).
9. **Write the state machine with each fact single-writer** (§2.6), gates as `>=` with a
   separate decision flag (§2.5), and every skip labelled *deferral* or *decision*.
10. **Audit for terminal states**: for each, what re-opens it? (§2.6)
11. **Instrument the negative case** before the first drive, not after it (§7.1).
12. **Extend `test_schema_conformance.py`** with every new cereal field AND the wire
    discriminants. Make the stubs use the real names (§2.3).

### Phase C — verify and deploy

13. Run §6 gates 1, 2, 3 on the Pi5; **2b and 3 on the device before rebooting**.
14. Deploy per §5 — git bundle, `--ff-only`, AGNOS check, bracket-form `pkill`.
15. After the reboot: gates 6, 6b, 7, 8, 9 — **6b before enabling anything**.
16. Enable the flag. Confirm the feature reports itself enabled.
17. **Say plainly what could not be verified parked** and what to watch on the first drive.
18. Drive; measure from the log (§7); tune one constant at a time.

### The five rules worth memorising

> 1. A fork feature's failure mode is *disabled*, never *crash* — and its fail-safe must not
>    flood (§0.2, §2.1).
> 2. Passing tests mean your tests agree with your model (§0.3).
> 3. Any optional service in a SubMaster needs all three ignore lists — and know the blast
>    radius of that particular process (§2.2, §2.4).
> 4. Ceilings may step; approaches must be shaped by distance (§3.1).
> 5. When state variables overlap, the bugs live in the seams (§2.6).

---

# 10. CURRENT STATE (2026-07-29)

**Feature A — mapd longitudinal control.** Implemented, deployed, driven three times. Speed
ceiling **ON**. Approach profile validated on drive 3: median decel −0.51 against a −0.50
design target, overshoot ~1.0× (was 5.8–10×); operator called it "felt perfect".
**`APPROACH_DECEL` stays at 0.5 — do not touch it without new evidence.** Firm hazard accel
(Hook 2, `SmartCruiseControlMapHazardAccel`) is still **OFF** by design and has never been
enabled on the car.

**Feature B — set-speed tracking.** Implemented, deployed and **ON**. Preliminary road results
good; see §11.6 for the one thing still unverified.

Enable/disable without any rebuild, effective within ~3 s (param poll period):
```
echo 1 > /data/media/0/grt/SmartCruiseControlMap             # mapd control
echo 1 > /data/media/0/grt/SmartCruiseControlSetSpeed        # set-speed tracking
rm    /data/media/0/grt/SmartCruiseControlSetSpeed           # kill switch
```

---

# 11. FEATURE B — SET SPEED TRACKS THE POSTED LIMIT

## 11.1 The first design was nearly inert, and measurement is what exposed it

The first cut was the obvious one: adopt a new limit if it is within ±20 km/h of the current set
speed, otherwise ask. It passed 33 tests and would have done **almost nothing on these roads**.

`ExperimentalMode=1` ⇒ the set speed starts at `V_CRUISE_INITIAL_EXPERIMENTAL_MODE = 105`, and
the measurement in §0.4 showed it **stays** at 105 for essentially the whole drive. A ±20 band
off 105 reaches only `[85, 125]`, while the roads actually driven post 20/40/60/80/120. The
feature would have logged `ignore` and done nothing, and the drive would have "proved" nothing —
the exact shape of the §2.3 failure, arrived at by a different route.

**The lesson is §0.4:** a threshold is meaningless until you know the operating point. One query
against a log already on the device would have caught this before a line was written.

## 11.2 The rules that actually shipped (operator's spec)

1. **At engage, seed the set speed from the posted limit** — replacing upstream's
   `V_CRUISE_INITIAL*`. This wins even on a RES/resume engage (operator's explicit choice over
   upstream's restore-previous behaviour), and it is what makes the feature useful rather than
   inert. **With no map data the set speed is left exactly as upstream set it** — see §11.5.
2. **A later limit change is adopted silently iff BOTH of:**
   - **a.** the set speed is a **multiple of 10** — a non-round value is hand-tuned;
   - **b.** the change is **within ±20 km/h**.
3. **Otherwise it is offered as a PENDING prompt for 10 s**, adopted only on RES/+.
   SET/− declines (final); a limit change under it retires it as stale; an unanswered prompt is
   re-offered after 60 s (§2.6).

**Rule 2b is absolute** — it holds however the set speed got there. On roads that post
20 and 40, that means 120→80 and 60→20 both prompt. Prompts being common is the intent.

**Rule 2a carries the whole hand-tuned test.** ~~Originally there was a third condition: the feature had to still OWN the set speed.~~
**Removed 2026-08-06.** It made a *driver-set* 110 prompt for a 120 limit, which the operator
reported as a bug — they expect round numbers they dialled in themselves to keep tracking.
Roundness alone does the hand-tuned test: 103 and 116 still prompt, 110 does not.

It also closed a real seam. `tracking` was the only **time-varying** term in the auto test, and
the test is evaluated at two different moments — once for the UPCOMING limit (which unlocks the
approach ramp) and again when that limit becomes CURRENT (which decides prompt vs auto). If it
flipped in between, the car slowed while the display still waited for an answer. Roundness and
Δ cannot disagree that way. **Generalisation: if the same predicate is evaluated at two times,
every time-varying term in it is a bug waiting to happen.**

**What rule 2a buys the operator**, and it was the point of the whole redesign: dial in 103 in a
100 zone and the feature never touches it again — it only ever asks. Dial back to exactly 100
and tracking resumes automatically.

## 11.3 Structure

- `openpilot/grt/set_speed.py` — `SetSpeedLimitTracker`, the whole state machine.
- `hooks.track_set_speed(...)` — **hook 3**, one line in `card.py` (§8, §3.2).
- `hooks.set_speed_state_msg(...)` → `grtSetSpeedState` published from `card` at 20 Hz (§4.3).
- `hooks.set_speed_alerts(...)` — **hook 4**, one line in `selfdrived.update_alerts` (§4.4).
- Flag `SmartCruiseControlSetSpeed`, default OFF, **separate from `SmartCruiseControlMap`**:
  this is the only fork feature that can *raise* the set speed, i.e. **accelerate the car on OSM
  data**. That deserves its own switch.

## 11.4 Data-quality gates (all of them earned)

`mapdOut` alive+valid · `tileLoaded` · `waySelectionType ∈ {current, predicted, extended}` ·
limit stable for 1 s · limit within `[20, 145]` km/h · no cruise-button activity this frame.

Plus an asymmetry worth copying: **raising** the set speed additionally requires
`waySelectionType ∈ {current, extended}`. `predicted` is mapd guessing which way you will take
at a junction — acting on a guess to *slow down* is conservative; acting on it to *speed up* is
not.

## 11.5 Seeding: never invent a speed the road did not post

Engaging from standstill reports `waySelectionType=fail` (`vEgo=0`, `bearing=0` — §8), so at the
moment of engagement there is usually no usable limit yet. The seed therefore **waits
indefinitely for a real limit**, and upstream's own `V_CRUISE_INITIAL*` stands until one arrives.
If the driver touches the cruise buttons while it is waiting, the seed is abandoned — the speed
is theirs.

~~Originally the seed fell back to **60 km/h** after a 10 s timeout when no map data had
arrived.~~ **Removed 2026-08-03 as problematic.** "No fix yet" is the normal state when engaging,
and the persistent state anywhere off-tile — so the timeout fired routinely and forced the set
speed to 60 regardless of the road. Engaging at highway speed dropped it to 60 and the car braked
for a limit that was never posted.

**The general rule, and it generalises past this feature:** a map-driven feature may only ever
command a value the map actually supplied. A "sensible default" for missing data is an invented
measurement, and it will be wrong in exactly the situations where the data is missing. The
correct behaviour for absent data is to do *nothing* and leave the base system in charge.

## 11.6 STILL UNVERIFIED — does the confirmation alert render?

The prompt only fires while **engaged**, and `ET.WARNING` alerts are cleared when the state
machine is not warning-capable — so **a parked car cannot display one** (§4.4). This could not be
checked during deployment and is the one open question about feature B.

- **Watch for:** `Speed limit N km/h` / `Press RES/+ to accept`, plus a chime.
- **If it never appears**, the failure is benign but total: no prompt → no confirmation → the
  set speed simply never changes for out-of-band limits. That is the §2.3 silhouette again —
  a feature that looks fine from the driver's seat while doing nothing.
- **Evidence to pull either way:** `/data/media/0/grt/set_speed.log`. `pending` lines prove the
  tracker offered; the absence of a matching `confirm`/`expire` pattern, or of `pending` lines
  at all, tells you which half is broken.

Also still open, and free to check: `carState.cumLagMs` on a post-change segment against a
pre-change one. `card` is the 100 Hz CAN loop and now carries both a subscriber and a publisher.
