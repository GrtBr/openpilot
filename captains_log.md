# Captain's Log — `nightly-dev`

Running record of code changes to **this checkout only** (`~/Comma/openpilot/nightly-dev`, branch
`nightly-dev`). Newest entry first. Each entry: what changed, why, how it was verified, and current
deploy status.

The sibling `~/Comma/openpilot/release-mici-staging/` checkout keeps its **own** `captains_log.md`.
The two branches diverge — changes logged here are not present there unless cherry-picked.

---

## 2026-08-05 — curve approach: rate-limit the ceiling descent to APPROACH_DECEL (fixes the harsh curve slow-down)

Operator: the slow-down *before* a curve is too rapid. Root-caused from the 11:28 drive, fixed
in `scc_map.py`, tests green, **DEPLOYED**.

### Getting the right drive out of the log (worth reusing)

`mapd_debug.log` `t` is `time.monotonic()` — boot-relative and reset every boot, so it cannot
locate a wall-clock time. **Device file mtimes are also useless**: the RTC is batteryless, so
segments carry pre-NTP stamps (`Jun 5 15:37`) corrected only later.

What works: each route's qlog `clocks` messages, whose *last* sample is post-sync. Route
`00000072` → boot_epoch `2026-08-05 09:27:06 UTC`, so the drive ran **11:27:42–11:51:38 SAST**
= mapd_debug session 56. Verified the session↔route pairing twice by matching qlog
`carState.vEgo` against `v_ego_kmh` over the same monotonic window (21.5 vs 21.5 km/h;
13.6 vs 13.5 on a second drive). Boot cycles map 1:1 to the day's routes by duration.

### The aggregate hid the defect

Over 11 curve episodes: mean a_ego **−0.35 m/s²**, median frames at/below −1.15 = **0%**.
Nothing looks wrong. The whole problem is the onset transient, visible only frame by frame:

```
t       v_ego  a_ego  jerk   mapCurve  v_target
970.0   71.2    0.07         75.5      75.5     <- ceiling above us, inert
972.0   72.7    0.21         57.2      57.2     <- STEP 85.2 -> 57.2 in ONE 0.5 s frame
972.5   72.2   -0.96  -2.33
975.0   61.4   -1.34                            <- saturated A_CRUISE_MIN
976.0   58.2   -0.42                            <- gentle tail
```

Ported `CalculateJerkLimitedDistance` from the Go source to get mapd's own planned decel:
**mapd sized the trigger for −0.26 m/s², the car used −1.24** — 4.5× overshoot, and 70% of
curve steps saturated `A_CRUISE_MIN`. Same pathology `approach_speed()` fixed for hazards and
limits on 2026-07-29; curves never got it.

### It was NOT caused by the 10% cut — but the cut is why it became noticeable

Clean before/after, because no driving happened between the 08-04 change and this drive. A
recurring corner pair reads **(52.9, 41.7) km/h** across 16 pre-change sessions and
**(47.7, 37.6)** today — both ratios 0.9017, exactly the √(2.025/2.5) applied. Same corners,
same κ, only `latA` moved.

| no-lead events | PRE (latA 2.5) | TODAY (latA 2.025) |
|---|---|---|
| median Δv to target | 2.9 km/h | **8.7 km/h** |
| **peak decel used** | **−1.23** | **−1.24 m/s²** |
| events saturating ≤−1.15 | 57% | **70%** |
| median time to target | 3.0 s | **5.0 s** |

Peak decel is identical, so the saturation is structural and pre-existing. What the cut did was
triple Δv per step, so the same hard braking lasts ~1.7× longer and fires more often. A −1.2
blip shedding 3 km/h is imperceptible; −1.2 for 5 s shedding 9 km/h is what the operator felt.

### The fix: rate-limit the DESCENT, not the distance

`_ramp_curve_ceiling()` in `scc_map.py`. `approach_speed()` cannot be reused because it needs a
distance and **mapdOut publishes none for curves** (only `nextSpeedLimitDistance`,
`nextHazardDistance`, `nextAdvisorySpeedDistance`). Shaping in the *time* domain needs no
distance and achieves the same thing: the commanded ceiling may fall no faster than
`APPROACH_DECEL = 0.5 m/s²`, so the planner sees a small error each frame instead of one big one.

Design points, each asserted by test:
- **Anchored at `v_ego`, never at the previous ceiling.** A ceiling above current speed is inert;
  ramping down from a stale 85 km/h would burn ~7 s doing nothing before braking began.
- **Rises are not limited** — curve ends, ceiling lifts immediately, can never hold the car back.
- **Self-escalating, so authority is never reduced.** The ceiling descends on its own clock
  whether or not the car keeps up; a closer-than-assumed curve grows the error and the planner
  brakes harder. Floor is still `A_CRUISE_MIN`.
- **Still lands in time.** mapd's trigger carries a `target_speed_time_offset = 4 s` margin;
  the ramp needs Δv/0.5 ≈ 4.8 s plus ~1 s to build tracking error, against mapd's planned 5.5 s.

`_dbg` now logs BOTH `map_curve_speed_kmh` (raw step) and `curve_ceiling_kmh` (ramped), so the
next drive shows the shaping directly.

### Replay of the 11 real measured events through the new limiter

| | before | after |
|---|---|---|
| commanded `a_cruise` peak (median) | −1.20 | **−0.50 m/s²** |
| onset jerk (median) | −2.81 | **−0.84 m/s³** |
| events saturating `A_CRUISE_MIN` | 100% | **0%** |

Every event lands on exactly −0.50, i.e. `APPROACH_DECEL`, which is the design intent.
Open-loop caveat: real `v_ego` would differ once braking changes, so this is first-order — but
the onset transient is precisely what the change targets.

### Tests

**42 scc_map** (13 new for the limiter), **44 hooks**, **62 set_speed**, **30/30 schema
conformance** against the real `log.capnp` including all four wire discriminants.

Two pre-existing scc_map tests asserted the curve target arrives *instantly* — the exact step
being removed. Rewritten to assert convergence rather than deleted, per §0.3: a test that
encodes the old behaviour is not evidence, it is the old behaviour.

### Deploy + on-device verification

Pi5 → GitHub (`ae8322a`) → comma4 `git pull --ff-only`, offroad, then reboot.

Ran the cheap gates ON THE DEVICE before rebooting: **42 scc_map / 44 hooks / 62 set_speed /
30 schema conformance**, all passing under `/usr/local/venv`. Plus the real-import gate the
stubs cannot cover: `grt.scc_map` imports with the actual openpilot deps, `APPROACH_DECEL = 0.5`,
`DT_MDL = 0.05`, `A_CRUISE_MIN` still −1.2, and `_ramp_curve_ceiling(15.9)` from `v_ego = 20.2`
returns 20.175 on the first frame and converges to exactly 15.9 within 10 s.

After the reboot: all processes up, `longitudinalPlan VALID=True`, zero `grt:` exceptions, and
`curve_ceiling_kmh` confirmed live in `mapd_debug.log`.

**`micd`/`soundd` were down again on the first reboot**, raising `processNotRunning` — which is
`ET.NO_ENTRY` and blocks engagement. Same boot-time audio-init transient as 2026-07-30; `mapd`
itself was running, so the fork is not implicated. A second reboot cleared it: `onroadEvents` is
now `seatbeltNotLatched` + `wrongGear` only, **no engagement blockers**. Worth noting this has
now happened on two consecutive deploys — if it ever fails to clear, engagement stays blocked.

**Do not read `tileLoaded=False` here as a tile problem.** The car is parked with **no GPS fix**
(`gpsLocation` not alive, `gpsLocationExternal` reporting lat/lon ≈ 0), so mapd has no position,
loads no tile, and `roadName` is empty. Today's drive produced 971 frames of curve data from
these same tiles, so they load fine when there is a fix.

**New gotcha:** `waySelectionType` reads **`current`** in this state, which looks like success but
is not — `current @0` is the enum's zero value, so with no fix the field is simply never set. The
2026-07-29 entry recorded `fail` when parked; that was with a stale position. Neither value proves
anything parked. Judge way selection only while moving.

### Still outstanding

`latA` stays at **2.025**, keeping both goals (slower corners AND a gentle approach). The next
drive judges whether the onset now feels right; instrument is `curve_ceiling_kmh` vs
`map_curve_speed_kmh` in `/data/media/0/mapd_debug.log` — the gap between them IS the shaping.
Expect onset decel ≈ −0.5 m/s² instead of −1.2. If braking now feels *late*, raise the descent
rate — but `APPROACH_DECEL` is shared with hazards and limits, both separately drive-validated,
so prefer a curve-specific constant over changing it.

## 2026-08-05 — lead-vehicle dash icon, Tier 2: ROAD TEST — plausible, good for now, one thing to watch

Operator drove and reports the distance/speed reading on the cluster "looks plausible — steady,
roughly matches what I expect for the gap." Closes the last open question from the offroad-only
verification (whether the lead-shown branch and the actual numbers render sanely — confirmed by
direct observation, same as Tier 1's road test).

**One thing flagged, not yet acted on:** "a little bit of jumping around" in the reading — operator
wants more drive time before drawing conclusions, and asked to leave it alone for now ("good for
now"). Recorded as a candidate follow-up, not a bug to fix reactively:

- Tier 2's design deliberately did NOT add hysteresis/smoothing to the numeric `dRel`/`vRel`
  values themselves (`PORT_LEAD_ICON_FROM_SUNNYPILOT.md` §4 point 3) — only the *presence* gate
  (`hud_control.leadVisible` AND `radarState.leadOne.present` agreeing) is debounced, via Tier 1's
  already-existing signal. The raw distance/speed numbers pass through radard's fused track
  unsmoothed on top of that gate.
- **CORRECTION (2026-08-11):** the candidate fix originally noted here — "the same pattern already
  proven elsewhere on this branch (the e2e acceleration filter, 2026-07-24 entry)" — pointed at
  code that no longer exists. That filter was DISCARDED in the 2026-07-28 hard reset to upstream
  (see that entry's "Discarded" table); confirmed directly against the current
  `longitudinal_planner.py:132`, which reads `output_a_target_e2e =
  sm['modelV2'].action.desiredAcceleration` raw, no filter. Do not cite it as available prior art.
- **Also corrected, from a deeper look at `radard.py` while chasing the above:** the Staria runs
  `radarUnavailable=True` (already noted in the 2026-07-24 entry above), so it has NO radar point
  tracks. `radard.get_lead()` therefore ALWAYS takes the vision-only branch
  (`get_RadarState_from_vision()`), where `vLeadK`/`aLeadK` are direct copies of the model's raw
  output — `"vLeadK": float(v_ego + lead_v_rel_pred)`, no Kalman filter applied. The `KF1D`/`Track`
  Kalman filter class exists in this file but only runs for radar-matched tracks, which never
  happen on this car. So "swap `vRel` for the already-Kalman-filtered `vLeadK`" (suggested in
  chat before this entry was corrected) is **not actually a free smoothing win on this vehicle** —
  `vLeadK` carries no more smoothing than `vRel` does here. If dRel/vRel jitter turns out to be a
  real problem, it needs an actual new filter (`FirstOrderFilter` or `_hysteresis_update`-style),
  not a field swap. Not built now — no evidence yet that it's needed (§0.3 of the mapd doc: don't
  fix a problem you haven't measured).

**Both tiers of this feature are now DONE and road-tested.** No code changes pending. Watching
item above is the only open thread, and it's explicitly deferred to more drive data at the
operator's instruction.

## 2026-08-05 — lead-vehicle dash icon, Tier 2: DEPLOYED to comma4, offroad checks PASS, road test outstanding

Deployed `00c38bd` (Tier 2). Note on advisor: Tier 1's plan got two successful advisor reviews;
Tier 2's design (both-sources-agree gate, `ACC_ObjRelSpd` omission) did **not** — both attempts
this session hit "temporarily overloaded". Operator made an informed call to deploy anyway, given
the offline verification already done (schema conformance 30/30, 5 behavioural cases against the
real packing logic). Recorded so it's clear this wasn't silently skipped.

**Deploy:** Pi5 → GitHub → comma4, same route as Tier 1. Fast-forward `405e1a8 → a6e183a`
(7 files, +344/−74 — includes `00c38bd` Tier 2 plus a docs-only commit and the micd diagnostic
entry). `prebuilt` marker untouched, tree clean before and after.

**Also resolved, incidentally:** `micd` (see the entry below, filed by the previous investigation)
was still down going into this deploy. This reboot cleared it — `running=True, exitCode=0` for
both `micd` and `soundd` post-reboot, matching the established precedent that this class of
boot-time audio-init race self-resolves on a subsequent boot. No separate action was taken on it.

**Post-reboot, offroad, all checks before the road test:**
- Device back in ~1 minute. `git log -1` = `a6e183a`, tree clean.
- `managerState`: nothing shouldBeRunning-but-not-running. All processes up including `micd`.
- `onroadEvents` = `[wrongGear, seatbeltNotLatched]` only — **the new `radarState` subscriber on
  `card` did not trip anything**, confirming the plan's prediction that card's checks being scoped
  to `all_checks(['carControl'])` makes this safe.
- swaglog since boot grepped for `card|hyundaicanfd|carcontroller|packer|SCC_Obj|ACC_Obj|
  KeyError|Traceback`: **empty.**
- `carState.cumLagMs` = **28.45 ms**, DOWN from the Tier-1 baseline of 36.86 ms — no lag
  regression from the new subscriber (the doc's required Tier-2-specific check).
- **Captured a real `SCC_CONTROL` frame off `sendcan` and decoded ALL three touched signals**
  (not just `SCC_ObjSta` this time):
  - `SCC_ObjSta=0` — Tier 1 unaffected.
  - `ACC_ObjDist=1.0m` — exactly the pre-existing no-lead constant, correctly preserved when
    `radarState.leadOne.present=False` (confirmed via the same `sendcan` capture:
    `(dRel=0.0, vRel=0.0, present=False)`).
  - `ACC_ObjRelSpd=-16.4 m/s` — this is the packer's default for an UNSET signal (raw 0 → physical
    `0×0.1 + (-16.4)`), confirming the field really was omitted from the packed dict as designed,
    not explicitly zeroed. This was the design decision under the most scrutiny (§4 of the plan
    doc) and it's now verified on the real compiled packer, not just the offline stub test.

**NOT YET PROVEN: the lead-shown branch with real numbers, or how the cluster renders a moving
distance/speed.** Everything above is reachable parked and disengaged with no lead present. Road
test outstanding — operator driving next.

## 2026-08-05 — DIAGNOSTIC ONLY: `micd` down after operator reboot — NOT fork-related, unfixed, handed off

Operator rebooted comma4 (unrelated to any deploy from this session — device came back online on
its own) and hit `processNotRunning` blocking engagement, reported as "micd soundd process not
running". Investigated; **no fix applied**, handed off to a separate agent per operator's
instruction. Recorded here so the next investigation doesn't repeat the same steps.

**Ruled out as fork-related:**
- Device is on `405e1a8` (Tier 1 only — `SCC_ObjSta`). Tier 2 (`00c38bddb5`) has **not** been
  pulled to the device; nothing from this session's card.py/carcontroller.py/hyundaicanfd.py work
  is on the car.
- `micd`/`soundd` are the microphone/audio subsystem — no code path shared with the CAN/dash-icon
  changes in either tier.
- Tier 1 already passed a full road test earlier in this same session, before this reboot.

**State found:**
- `soundd`: running fine (confirmed via `pgrep`).
- `micd`: `managerState` reports `shouldBeRunning=True, running=False, exitCode=1`, stale
  `pid=18707`. **Not self-recovering** — polled 4× over ~80s with `managerState`, identical
  `pid`/`exitCode` every time, so manager is not retrying it (differs from the closest documented
  precedent, the 2026-07-30 `soundd` boot crash, which self-recovered via manager's own restart).
- `onroadEvents` includes `processNotRunning` — confirmed this blocks engagement, matching the
  documented behaviour of that event type.
- Manually running `python3 -m openpilot.system.micd` on the device **succeeds cleanly right now**
  — "micd stream started" logged immediately, no error, blocks normally on the audio stream. Not a
  persistent/reproducible fault as of this investigation.

**Root cause of the ORIGINAL crash (exitCode=1 at boot) could not be determined — the traceback is
structurally unrecoverable on this device**: `system/manager/process.py:221-222`
(`PythonProcess.start()`) redirects every Python-process's `stdout`/`stderr` to `/dev/null`. Once
manager launches a process, any traceback it prints on crash is gone. This is stock/upstream
manager code, not fork-owned, so not something to patch as part of this feature — but worth knowing
for any future investigation on this device: a crash-at-boot diagnosis needs either a live
`journalctl`/dmesg capture from the actual boot window, or `stdout`/`stderr` redirected to a file
temporarily (e.g. a manual foreground run at the moment of boot), not swaglog after the fact.

**Not attempted:** a reboot, which is the only thing precedent shows reliably clears this class of
issue (2026-07-30 entry). Deliberately left to the operator/next agent rather than done unasked,
since a device reboot is a real action on a physical, possibly-driven car.

## 2026-08-05 — lead-vehicle dash icon, Tier 2 (distance/speed): IMPLEMENTED, offline-verified, NOT DEPLOYED (comma4 offline)

Real `ACC_ObjDist`/`ACC_ObjRelSpd` numbers on top of Tier 1's `SCC_ObjSta` icon. `comma4` was
offline for this entire session — nothing below has touched the device.

**Design change from the original plan (`PORT_LEAD_ICON_FROM_SUNNYPILOT.md` §4), found while
re-grounding on the current code before writing anything:** the plan called for porting
sunnypilot's separate `LeadDataCarController` hysteresis class to debounce lead *presence*. Didn't
build it. Tier 1 already has a working, road-tested presence signal — `hud_control.leadVisible` —
which itself derives from `radarState.leadOne.present` one hop upstream
(`longitudinal_planner.py: longitudinalPlan.hasLead = sm['radarState'].leadOne.present`, re-checked
directly, not assumed). Porting a SECOND, independent hysteresis on `radarState.leadOne.present`
would risk `SCC_ObjSta` (gated on `hud_control.leadVisible`) and the new numeric fields (gated on
the new hysteresis) disagreeing frame-to-frame — icon on with no number, or vice versa. Instead:
gate the numeric fields on `hud_control.leadVisible` AND card's own `radarState.leadOne.present`
BOTH agreeing. When they don't (a real but narrow window — the two views update on independent
cycles), the fail-safe direction is "no number" — same rule as the mapd port's 60 km/h fallback
removal: never command a value the data didn't actually, currently supply.

**Changes:**
- `openpilot/selfdrive/car/card.py` — `'radarState'` added to the base `SubMaster` list (NOT
  `GRT_SUB_CARD` — it's a stock always-published service, none of `mapdOut`'s ignore-list
  treatment applies). One hook: `self.CI.CS._grt_lead = (dRel, vRel, present)` stashed onto the
  `CarStateBase` instance right before `self.CI.apply(CC, now_nanos)` — works because `apply()`
  forwards `self.CS` unchanged into `CarController.update()`, and `self.CI.CS` is already reached
  this exact way at the `secoc_key` line a few lines up. No `interfaces.py` signature change, no
  other car brand touched.
- `opendbc_repo/opendbc/car/hyundai/carcontroller.py` — the `create_acc_control()` call site
  passes `lead=getattr(CS, '_grt_lead', None)`. `CS` already reaches this call unchanged; `getattr`
  with a `None` default so any `CS` this branch hasn't touched (replay, tests) degrades to Tier 1's
  behaviour instead of raising.
- `opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py` — `create_acc_control()` gained an optional
  `lead=None` param. Packs `ACC_ObjDist` (clipped 0–204.7 m) and `ACC_ObjRelSpd` (clipped
  −16.4–34.7 m/s) only when the gate above is true. **`ACC_ObjRelSpd` is omitted from the values
  dict entirely (not set to 0) when no lead is shown** — mainline never packed it either, and its
  DBC receiver is unconfirmed (`XXX`, not `CLU`); writing an explicit `0` physical value would
  silently change existing behaviour on a signal that might feed something else. `ACC_ObjDist`
  stays at the constant `1` in that case, matching mainline exactly (it was already always set).
- `openpilot/grt/tests/test_schema_conformance.py` — added `radarState.leadOne.vRel` (`.dRel`/
  `.present` were already asserted from the mapd port).

**§2.3 trap, checked explicitly because it bit the mapd port exactly this way:** re-grepped
`log.capnp`'s `LeadData` struct directly before writing any code — `dRel @0`, `vRel @2`,
`present @11`. Confirmed on THIS repo's actual schema. (Earlier in this session, before any code
was written, a `.status` field name from an unrelated earlier grep against a different repo —
sunnypilot's — nearly got carried into a design note; caught and corrected by re-grepping before
committing to it, not after.)

**Offline verification (comma4 unreachable all session):**
- `ast.parse` on all 3 modified Python files: clean.
- `test_schema_conformance.py`: **30/30**, including the new `vRel` assertion.
- Real import of the `opendbc`-side files (`uv run --no-project --with numpy`): clean.
  `card.py`'s own import could not be exercised — needs the on-device path layout for `opendbc`/
  `cereal` resource resolution, which the Pi5 doesn't replicate. Pre-existing limitation from
  every prior port on this branch, not new here.
- **5 behavioural cases against the real `create_acc_control()` packing logic**, via a stub
  packer that captures the `values` dict instead of encoding real bytes (the compiled `CANPacker`
  needs a build the Pi5 can't do — same class of gate as the mapd port's Verification 1):
  1. no lead at all (`lead=None`) → `ACC_ObjDist=1`, `ACC_ObjRelSpd` absent — **exact parity with
     pre-Tier-2 behaviour**, the most important case to get right.
  2. lead present + visible, in range → both fields pack correctly.
  3. `hud_control.leadVisible=True` but `radarState.leadOne.present=False` → falls back to
     no-number, confirming the fail-safe-on-disagreement gate actually works.
  4. out-of-range `dRel`/`vRel` (500 m, 100 m/s) → clipped to 204/34.7, not passed through raw
     (which would make the real packer choke on the car).
  5. `gas_override=True` with a lead shown → `SCC_ObjSta=1` (uncontrollable), confirming Tier 1's
     logic and Tier 2's numeric fields don't fight each other.
  All 5 pass.

**Advisor could not review this round's design** (overloaded both times called this session,
same as earlier for Tier 1's initial DBC pull). Proceeded on the same grounding discipline as
Tier 1 — every claim re-checked against this repo's live code — but this is flagged explicitly in
`PORT_LEAD_ICON_FROM_SUNNYPILOT.md` §7 as outstanding: get advisor's read on the
gate-on-both-sources-agreeing design and the `ACC_ObjRelSpd` omission before deploying.

**NOT ON THE DEVICE.** `comma4` was offline for this entire session. Next step: deploy via the
same Pi5 → GitHub → device route that worked for Tier 1 (see the entry below for the exact runbook
and the `pkill -x manager.py` correction), then the on-device block — engagement check and a
`cumLagMs` comparison against the 36.86 ms Tier-1 baseline are the two items Tier 2 specifically
needs (new `radarState` subscriber, unlike Tier 1) — then a road test for the actual numbers.

## 2026-08-05 — lead-vehicle dash icon, Tier 1: ROAD TEST PASSED

Operator drove behind traffic and confirmed the lead icon appears on the Staria's cluster and
"looks sane" — steady, not flickering, no reported oddity. This closes the one open question from
the offroad-only verification below (the DBC confirms `SCC_ObjSta` is routed to the cluster; it
never proved what the cluster's firmware does with each value — now confirmed by direct observation).

**Tier 1 is DONE.** No further action needed on it. Proceeding to Tier 2 (real distance/speed via
`radarState`) — see the entry above this one for its full design; `comma4 is offline` at the start
of this work, so Tier 2 will be implemented and offline-verified only, not deployed, until the
device is reachable again.

## 2026-08-05 — lead-vehicle dash icon, Tier 1: DEPLOYED to comma4, offroad checks PASS, road test outstanding

Deployed the change from the entry below. Device confirmed parked/ignition-off by the operator
before touching anything (see the process-state note further down — worth recording as a real
gotcha, not just a formality).

**Deploy:** Pi5 → GitHub → comma4 (the normal-case route per PROGRESS.md's 2026-08-04 note).
Committed the 4 files below as `405e1a84e7`, `git push origin nightly-dev`, device fast-forwarded
`09a174b → 405e1a8` (picked up one prior docs commit it hadn't pulled yet, plus this one — 5 files,
+418/−7). `prebuilt` marker untouched. No scons, no cereal SCP (this change touches zero cereal
files, so that class of risk was structurally not in play — see the entry below).

**`pkill -x manager.py` matched nothing** — both `manager.py` processes' actual `/proc/PID/comm` is
`python3` (invoked as `python3 ./manager.py`), so an exact-name match never fires. Not a blocker:
per the 2026-08-04 SYNC entry's own finding, a plain `git merge --ff-only` is safe with the old
processes still running (they just keep the old module in memory until restarted); the reboot is
what actually applies the change, not the pkill. Recorded as a correction to the `pkill -x`
guidance in the deploy runbook — it doesn't reliably match on this device's process tree.

**Also noteworthy: `IsOffroad=0` while the operator reported the car parked/off.** Caught this before
doing anything: `controlsd`/`selfdrived`/`card` were all live pre-reboot, which is the *onroad*
process set. Paused and asked the operator directly rather than trusting either signal alone —
confirmed parked. Given the operator's direct confirmation, proceeded, but this is worth watching
on a future deploy: either the param is stale after a non-clean stop, or the device's onroad
detection doesn't match the operator's notion of "off". Not resolved here; just flagged.

**Post-reboot, offroad, all checks before any road test:**
- Device back in ~60s. `git log -1` = `405e1a8`, tree clean.
- `managerState`: **nothing** shouldBeRunning-but-not-running. mapd/modeld/dmonitoringmodeld/
  controlsd/selfdrived/card/plannerd all up.
- `onroadEvents` = `[wrongGear, seatbeltNotLatched]` only — the same benign parked-car signature
  recorded on 2026-07-30. No `commIssue`. **Engagement is not blocked.**
- swaglog since boot grepped for `card|hyundaicanfd|carcontroller|packer|SCC_ObjSta|KeyError`:
  **empty.** The packer accepted the new DBC key without throwing.
- **Captured the real `SCC_CONTROL` (address 416) frame off `sendcan`** and hand-decoded byte 13
  bits 4-6 (the DBC's `108|3@1+` layout) against the raw bytes: `SCC_ObjSta = 0`. Correct — the car
  is disengaged (`selfdriveState.enabled=False`), and the formula collapses to 0 whenever `enabled`
  is false, independent of lead state. This is the expected value here, not an inert result; it
  also cross-validates the packer's encoding against independent hand bit-math, both agreeing.
- `carState.cumLagMs` = 36.86 ms. No pre/post comparison needed (unlike the set-speed feature):
  this change adds no new `SubMaster`/`PubMaster` traffic to `card`, only edits an
  already-running message builder inside the existing car-control path.

**NOT YET PROVEN: the `1`/`2` branches, or what the cluster actually renders for any of them.**
Everything above is reachable while parked and disengaged; the DBC confirms `SCC_ObjSta` is routed
to the cluster (`CLU` receiver tag), not what the cluster's firmware does with each value. That
needs the driver engaged with a real lead vehicle present — the road test in
`PORT_LEAD_ICON_FROM_SUNNYPILOT.md` §6, still outstanding.

## 2026-08-05 — lead-vehicle dash icon, Tier 1 (`SCC_ObjSta`): CODED, NOT YET DEPLOYED

New feature, unrelated to mapd/set-speed. sunnypilot's Hyundai `CarController` fills the dash's
lead-vehicle icon from real lead data; mainline (this branch's base) sends the same `SCC_CONTROL`
CAN message with those fields hardcoded static, so the Staria's cluster shows no dynamic lead icon
today. Full plan: `PORT_LEAD_ICON_FROM_SUNNYPILOT.md`.

**Why this is smaller than the mapd port:** the data source (`radarState`) is already a stock,
compiled service — no `CustomReserved` slot, no `services.py`/`custom.capnp`/`log.capnp` edit. This
is the first fork feature in this repo's history to touch zero cereal files, so the repo `CLAUDE.md`
rule against SCP'ing cereal files to the device cannot be triggered by it.

**DBC finding (`opendbc/dbc/generator/hyundai/hyundai_canfd.dbc:374-407`, `SCC_CONTROL` id 416):**
`SCC_ObjSta` is the only signal in the message with a documented comment + `VAL_` table
(0=no object, 1=uncontrollable, 2=controllable:longitudinal) AND the only one receiver-tagged `CLU`
(cluster). Every other candidate signal (`ObjValid`, `OBJ_STATUS`, `ACC_ObjDist`) shows unconfirmed
`XXX` receivers. sunnypilot's own `ObjValid` polarity differs between its CANFD and non-CANFD paths
with no documented reason — rather than guess it, this change avoids that signal entirely and
targets `SCC_ObjSta`, whose semantics are unambiguous from the DBC alone.

**Confirmed the Staria's `SCC_CONTROL` is authored by openpilot, not a live factory ECU:**
`create_acc_control()` only fires `if self.CP.openpilotLongitudinalControl` (`carcontroller.py:196`),
already confirmed `True` on this car (line 310 of this log, 2026-07-24 entry). The ADAS Driving ECU
is disabled and openpilot is the sole author of this message.

**Advisor review, two checks before coding, both cleared:**
1. `hyundaicanfd.py` has a second `SCC_CONTROL` builder, `create_acc_cancel()` — checked it only
   fires when `openpilotLongitudinalControl` is `False` (`carcontroller.py:205-217`), the opposite
   condition to `create_acc_control()`. Exactly one builder is ever active for this car; no risk of
   two conflicting writers alternating the field.
2. `gas_override` (the argument driving the 1-vs-2 distinction) is `CC.cruiseControl.override` at
   the call site — the driver-pedal-override signal, matching the DBC's "uncontrollable" semantics.

**Change:** `opendbc_repo/opendbc/car/hyundai/hyundaicanfd.py`, `create_acc_control()`, one dict
entry, GRT-MOD sentinel-wrapped:
```python
"SCC_ObjSta": 0 if not (enabled and hud_control.leadVisible) else (1 if gas_override else 2),
```
Uses `enabled`, `hud_control`, `gas_override` — all pre-existing function arguments. No new
SubMaster subscription, no signature change, no schema edit. `ObjValid`/`OBJ_STATUS` left at
mainline's hardcoded constants — their cluster relevance is unconfirmed by the DBC, so touching
them would add risk without a confirmed payoff.

**Deliberately NOT built in the same pass:** Tier 2 (real `ACC_ObjDist`/`ACC_ObjRelSpd` numbers via
`radarState`, hysteresis debounce) — advisor's instruction, followed: Tier 1 is an unproven probe
(no reference implementation ships icon-only; sunnypilot always drives `ObjValid`/`SCC_ObjSta`/
`ACC_ObjDist` together), so a road-test surprise with both changes present wouldn't be attributable
to either one. Tier 2's design is fully scoped in the plan doc but stays unstarted until Tier 1 is
driven.

**Verified so far (offline only — Pi5 cannot build the compiled `CANPacker`, same constraint as
every prior port on this branch):**
- `ast.parse` on the modified file: syntax OK.
- Real import: `from opendbc.car.hyundai import hyundaicanfd` succeeds (via `uv run --no-project
  --with numpy`, the same pattern used for the RHD fingerprint verification on 2026-07-28);
  `create_acc_control`'s signature unchanged.
- Packer-level round-trip (does the DBC-generated `CANPacker` actually accept `SCC_ObjSta` as a
  key) could NOT be run here — needs the compiled extension, which needs a build this branch's Pi5
  cannot do. Deferred to the device, same as the mapd port's Verification 1.

**NOT YET ON THE DEVICE.** Next step per the plan doc's §6: offroad on comma4, confirm engagement
still works (lowest risk here — no new SubMaster service, so none of the `all_checks()` blast-radius
concerns from the mapd port apply), then a `candump`-level check that `SCC_ObjSta` actually varies
with `hudControl.leadVisible`, then a road test to see what the cluster actually does with it — the
DBC confirms the signal is routed to the cluster, not what the cluster's firmware renders for each
of its three documented values. Do not run any of this unattended.

## 2026-08-04 — map curve speed cut 10% (`map_curve_target_lat_a` 2.5 → 2.025) + NATIONAL tile set, both DEPLOYED

Two changes, both on the car. Device on `09a174b`, offroad during the whole deploy, rebooted, back
in ~90 s.

### 1. Curve speed 10% slower — a setting, not a code path

Operator: curve entry slightly too fast.

mapd computes `v = sqrt(map_curve_target_lat_a / κ)` (`mapd_source/math.go` `GetTargetVelocities`),
and `UpdateCurveSpeed` takes the min over lookahead nodes. `scc_map.py:202` consumes
`mapdOut.mapCurveSpeed` **directly with no scaling** — and its own comment says to tune MapdSettings
rather than add a second python-side integrator. So the lever is the setting.

Speed scales with √latA, so a factor *f* on speed needs *f²* on latA: **2.5 × 0.9² = 2.025**.

**The key was ABSENT from `DEFAULT_MAPD_SETTINGS`**, so mapd was silently falling back to its own
embedded default of 2.5 (`mapd_source/settings/defaults.json`). Pinning it makes the tune explicit
and survives `clear_all()`, since `write_settings_file()` rewrites the blob before every mapd exec.

**Expect slightly MORE than 10%.** A lower target also lets more nodes pass `map_curve.go:58`'s
`tv.Velocity > VEgo + CURVE_CALC_OFFSET` filter and lengthens the jerk-limited trigger distance, so
braking starts marginally earlier too.

`APPROACH_DECEL = 0.5` deliberately **untouched** — it shapes the *approach*, latA sets the
*target*. The drive-3 validation stands; the complaint was corner speed, not braking feel.

**Verified:** ran `write_settings_file()` to a temp path **on device** → emits `2.025`; mapd's start
time is after the pull (uptime 7 min vs mapd elapsed 7:26, both post-reboot).

**Do NOT verify this by grepping `/data/params/d/MapdSettings`** — it is absent in steady state *by
design* (`clear_all()` deletes it; mapd holds the values in memory). Same trap recorded in the
2026-07-29 "one wrinkle understood and benign" entry. I hit it again and wasted a 5-minute poll on it.

Category A, fork-owned file → no `GRT_MODS.md` entry needed (checked).

### 2. National tile set replaces the two-band regional one

Tiles regenerated on another machine from the full South Africa PBF, using the reworked sunnypilot
generator (κ now suppressed at ≥3-way junctions and dead ends; chains held to a single highway tag).

Device: **376 files / 106 MB (bands -34, -36 only) → 2,046 files / 601 MB (bands -24 … -36).**

**The delivered tiles were verified before deploying, not taken on trust.**
`parity_check_ver2.py` gate 1a asserts *"≥3-way node ⇒ stored κ == 0.0"*. Run against the delivered
tiles with a Garden Route clip PBF: **4,468 junction nodes checked, 0 violations, PASS.** A stale
checkout on the build machine would have shown ~3,029 violations — that is measured, not assumed:
it is exactly what the pre-change generator produces on the same clip. So the build machine ran the
reviewed code.

Transfer: rsync → `offline.new`, verified 2,046 files + matching md5 on a spot tile, atomic swap.
`offline.old` kept briefly, then deleted at the operator's instruction — it was a *regional subset*,
not an equivalent fallback, so if the new tiles are ever wrong the fix is a rebuild, not a revert.

The Phase 1b band gotcha (`floor(lat/2)*2`) is now moot: the whole generated tree is deployed, so
every band is present rather than hand-picked.

### STILL OUTSTANDING — the drive

Every check confirms the **input** is 2.025. **Nothing confirms the output moved 10%.** Also, mapd
was never observed opening a tile: it has no open handles into `offline/` while parked and opens
tiles on demand near a fix, so that proves nothing either way.

Instrument is `/data/media/0/mapd_debug.log` → `map_curve_speed_kmh`, same corner before vs after.
`mapdOut` is still never in rlog on this prebuilt branch, so there is no retrospective path.

### Deploy method note

Used GitHub fetch → `git pull --ff-only` on device, matching the SYNC entry below. PROGRESS.md's
`DEPLOY RUNBOOK` still describes the older git-bundle route; marked superseded there rather than
deleted, since the bundle path is still correct when the device has no network.

## 2026-08-04 — SYNC: Pi5 → GitHub → comma4 (no code change; the 08-03 fallback removal is now ON the car)

Housekeeping entry. No source was modified today — this records where each copy of the code now
sits, and closes the "deploy tomorrow" left open by the 2026-08-03 entry below.

**GitHub (`GrtBr/openpilot`), both pushes fast-forward, no force:**

| Branch | Before | After | Note |
|---|---|---|---|
| `nightly-dev` | `dcb3550cac` | `005d003592` | 32 commits — the whole mapd + set-speed body of work |
| `release-mici-staging` | *(absent)* | `0af132822` | **new branch**; the RHD Staria FW commit was local-only until now |

Remote refs were a week stale (`FETCH_HEAD` dated Jul 28); re-fetched and re-checked
`rev-list --left-right` before pushing — `0 32`, a true fast-forward, not a divergence.

**comma4:** `git fetch origin nightly-dev && git merge --ff-only origin/nightly-dev`,
`19c3568 → 005d003` (2 commits: the fallback removal + one docs commit). `--ff-only` deliberately,
so it fails loudly rather than merging on the car.

**Verified on the device:** `HEAD == 005d003`, working tree clean and exactly level with
`origin/nightly-dev`, `prebuilt` sentinel still present (0 bytes, untouched — no scons was run and
none is implied: the diff is 3 markdown files + `set_speed.py` + its tests). `test_set_speed.py`
via `/usr/local/venv`: **42/42 passing on the car** — up from the 33 recorded on 2026-08-03,
which is the rewritten suite from `005d003` running green against the device's real fork-config.

**DEPLOY STATUS — read this before the next drive.** The car was *onroad* during the sync
(`IsOffroad=0`, 17 selfdrive processes up, not engaged). At the operator's instruction the pull was
done **without a reboot**: the new `set_speed.py` is on disk but the running processes still hold
the old module in memory. **The 60 km/h no-map fallback is therefore still live until the next
ignition cycle**, which will pick up the new code with no further action. First drive after that
cycle is the one that exercises the removal.

**Deliberately NOT committed** (local tooling, not code): `graphify-out/` (57 MB of generated
graph output), `CLAUDE.md`, `.graphifyignore`.

`CLAUDE.md` and `graphify-out/` were then added to `.gitignore` at the operator's instruction, in
the existing `# agents` block at the foot of the file. Both were already untracked, so the rules
hide nothing that was ever committed — verified with `git check-ignore -v` (both rules fire) and
`git ls-files | grep -cE 'CLAUDE\.md|graphify'` → `0`. This is a 2-line fork diff against upstream
in a file upstream rarely touches; if it ever conflicts on rebase, `.git/info/exclude` is the
zero-diff alternative. `.graphifyignore` was left untracked and un-ignored.

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

## 2026-07-29 — ROOT CAUSE of the failed test drive: radarState lead field name

Test drive: neither speed-limit nor curve slow-downs happened. Root cause found in swaglog.

`scc_map.update_calculations()` did `raw_has_lead = lead1.status or lead2.status`. openpilot's
`radarState.LeadData` has NO `status` field - it is `present` ("true if a lead is present").
sunnypilot's schema used `status`, and I ported the line verbatim without checking. Result:
AttributeError on EVERY frame - 38,300 of them in that drive. hooks.limit_v_cruise caught each
one and returned v_cruise unchanged, so the feature was a silent, complete no-op. The car drove
normally throughout, which is the fail-safe design working exactly as intended, but the feature
never ran. It also explains the missing mapd_debug.log: update_calculations() throws before
_write_debug() is ever reached.

Fixed .status -> .present in all four places (lead gate, both lead-past-hazard checks, debug log).

THE REAL LESSON - why the tests did not catch it: the unit tests stub cereal with
SimpleNamespace, and my stub used `status` too. The tests were validating my own assumption, not
reality, so 29/29 passed while the car raised on every frame. Two fixes:
  1. the stubs now use `present`, mirroring the real schema;
  2. new `openpilot/grt/tests/test_schema_conformance.py` loads the ACTUAL log.capnp via pycapnp
     and asserts all 14 cereal fields the fork reads really exist. Verified it FAILS on `status`
     (listing the available field names) and passes on `present`. Stubbed tests can no longer
     drift from the schema silently.

Note dRel was fine - it does exist. Only `status` was wrong.

## 2026-07-29 — test drive forensics + a second prebuilt-branch limitation

Drive identified as route 00000034--aa6306c38e (28 segments): 167,025 carState frames, 58%
moving, max 66 km/h, average 35 km/h while moving, and carControl.enabled for 60,543 frames -
so openpilot WAS engaged and the drive was a valid test. Route 00000035--da076d2e95 is just
post-drive idling (0 moving, 0 engaged). The feature failed purely because of the per-frame
AttributeError, now fixed.

SECOND PREBUILT LIMITATION FOUND: **mapdOut is never written to rlog on this device** - 0 frames
across the whole drive, while carState had 2,738 per segment. Cause: loggerd is C++ and reads
the COMPILED services.h, generated back on Jul 23, which has no mapdOut. `should_log=True` in
services.py only affects the python view. So the earlier claim that "mapdOut is logged so drives
can be replayed for tuning" is FALSE on a prebuilt branch, and drives cannot be retrospectively
analysed for mapd behaviour.

Consequence: `/data/media/0/mapd_debug.log` is now the ONLY instrument for tuning and
diagnosis. It was absent after the failed drive because update_calculations() threw before
_write_debug() could run; with the fix in, it will be written from the next drive onward.

Still unknown and only answerable by driving: whether mapd's way selection succeeds in motion.
Parked it reports waySelectionType=fail with an empty roadName because vEgo=0 and bearingDeg=0.

## 2026-07-29 — fix verified on device after reboot

- scc_map update failures since boot: **0** (was 38,300 in the failed drive).
- `/data/media/0/mapd_debug.log` is now being WRITTEN (78 KB and growing) - proof that
  update_calculations() completes every frame instead of throwing.
- Sample entry parked: v_cruise_kmh=145, v_ego 0, all mapd values 0 (expected while stationary).
- mapd running, plannerd running, longitudinalPlan VALID=True, mapdOut live tileLoaded=True.
- waySelectionType still `fail` - expected while parked; only a drive can settle it.

Ready for a second test drive. mapd_debug.log is the instrument: check map_curve_speed_kmh,
speed_limit_suggested_kmh, v_target_kmh, state and is_active while moving.

## 2026-07-29 — SECOND DRIVE WORKED; slow-downs far too aggressive -> approach profile

Second drive: the feature engaged (869 active frames; stop, T-Junction, mini_roundabout,
turning_circle all detected; speed limits and curve speeds all present). User reported the
slow-downs were much too aggressive and reached target speed well before the hazard.

MEASURED from /data/media/0/mapd_debug.log (2.5 MB, 4,067 frames):
  turning_circle 69->20 km/h over 586 m: needed -0.28, USED -1.64 m/s^2 (5.8x), target 47 m early
  stop           63->20 km/h over 830 m: needed -0.17, USED -1.69 m/s^2 (10x),  target 365 m EARLY
  decel while active: mean -0.48, p10 -1.28, hardest -1.69 m/s^2

ROOT CAUSE: v_target stepped straight to the FINAL target the moment a hazard/limit came into
range. The planner's P-controller, a_cruise = clip(v_cruise - v_ego, A_CRUISE_MIN=-1.2, ...),
then saturates at maximum braking to close a huge speed error immediately - so it brakes hard,
arrives at target far too early, and coasts the rest of the way.

FIX - distance-based approach profile. Command the speed the car should be at RIGHT NOW to
arrive at the target exactly AT the hazard:
    v_now = sqrt(v_target^2 + 2 * APPROACH_DECEL * distance)
APPROACH_DECEL = 0.5 m/s^2 (~3.3x gentler than the -1.64 measured). At distance 0 it equals the
target exactly; far away it is high and has no effect; if a hazard appears late the formula
self-escalates (small d -> low command -> harder braking), so safety is preserved.
Applied to hazards AND to upcoming lower speed limits (nextSpeedLimit/nextSpeedLimitDistance).
The hazard engagement distance now also uses APPROACH_DECEL so the latch fires at the right point.

Also set "slow_down_for_next_speed_limit": false in MapdSettings. mapd's own lookahead
(speed_limit.go:111) was stepping speedLimitSuggestedSpeed down to the upcoming limit while the
sign was still far away - the same harsh step, from the mapd side. Current-limit ceiling still
comes from speedLimitSuggestedSpeed; the APPROACH is now ours to shape. This deliberately
reverses the earlier Phase 6 "let mapd do the lookahead" decision, on drive evidence.

Validated against the real drive episodes: at 0.5 m/s^2 braking would start ~336 m out
(turning_circle) and ~275 m (stop), landing exactly on 20 km/h AT the hazard.

Tests 18/18 incl. profile maths: lands on target at 30/120/400 m, monotonic in distance,
implied decel == APPROACH_DECEL, and MIN_V floor still applies at the hazard.

TUNING KNOB: APPROACH_DECEL in openpilot/grt/scc_map.py. LOWER = gentler and starts earlier.

## 2026-07-29 — THIRD DRIVE: approach profile VALIDATED ("felt perfect")

Measured on the frames where OUR target was actually binding (157 frames), which is the honest
metric - raw a_ego includes lead-car and driver braking:

               drive 2 (step)      drive 3 (profile)     design target
  median          -0.23 (spikes to -1.69)   -0.51            -0.50
  mean            -0.48                      -0.53           -0.50
  p10             -1.28                      -0.77             -
  overshoot       5.8x - 10x                 ~1.0x            1.0x

Median -0.51 against a 0.50 target: the profile does exactly what it was designed to do.
Visible in the raw frames: v_target 45.6 km/h with the hazard still 130 m away (an intermediate
speed, not a step to 20), and v_target 60.3 with the next limit 2 m ahead - landing on the limit
right at the sign. APPROACH_DECEL stays at 0.5; do not touch it without new evidence.

(My episode detector found 0 episodes this drive because it keyed off a large speed error at the
FIRST frame, which the profile no longer produces. The binding-frames metric replaces it.)

## 2026-08-07 (later) — set-speed-is-final + 110 cap DEPLOYED to comma4

Device on `b500214`, AGNOS 18.7, clean tree, healthy. 12 KB bundle from the device's actual HEAD
(`277973d8e2..nightly-dev`, 3 commits), clean fast-forward, back up in ~75 s.

Verified BEFORE the reboot — schema 30/30, suites 43 scc_map / 44 hooks / 71 set_speed — and the
critical decoupling confirmed against the device's real modules:

```
V_CRUISE_MAX = 110  |  MAX_LIMIT_KPH = 145.0
120 limit recognised: True -> clamps to 110.0
```

That is the trap check: had `MAX_LIMIT_KPH` still tracked `V_CRUISE_MAX`, a real 120 limit would
have read as implausible and the feature would have gone silently inert on 120 roads.

Verified AFTER the reboot: all processes up incl. `soundd`/`micd` (no audio transient this time);
`managerState` reports nothing missing; `onroadEvents` only `wrongGear`/`doorOpen`/
`seatbeltNotLatched` — **no `commIssue`, no `processNotRunning`**, engagement not blocked;
`longitudinalPlan VALID=True`; `grtSetSpeedState` at 20 Hz with `active=True`; **zero `grt:`
exceptions**.

**What to check on the next drive**, in order of what would tell us most:
1. **The reported case is fixed** — in a zone the map gets wrong, raise the set speed and confirm
   the car actually holds it after you lift off. That is the whole point of this change.
2. **The 110 cap** behaves as intended, including on a 120 road (set speed should sit at 110, and
   there should be no repeating prompt — the `at_limit`-on-clamped-value fix).
3. **The ramp trade-off**: after any manual set-speed change, pre-sign shaping stays off until the
   feature re-takes ownership. If in-band sign approaches now feel abrupt after a manual nudge,
   that is this, and it is tunable.

## 2026-08-07 — the driver's set speed is now the FINAL authority + V_CRUISE_MAX 145 → 110

Drive report, 14:45–15:00: map wrongly showed 60, driver set cruise to 100, **both the Staria
cluster and the comma MAX displayed 100** — and the car still dropped back to 60 the instant they
lifted off the throttle.

**This also answers the open question from 08-06.** MAX was never "out of sync" in the sense I
assumed: the display was right and the *car* was wrong. The set speed followed the driver
correctly; the physical behaviour was pinned by a stale map authorisation.

**Cause:** `scc_map`'s steady-state ceiling used `authorisedLimit`, which held the last authorised
limit (60). **Nothing revoked it when the driver raised the set speed**, so hook 1 pinned the
planner's `v_cruise` at 60 permanently. The driver could not overrule the map by any means.

**Fix:** the steady-state posted-limit ceiling is REMOVED while the set-speed feature is active.
The argument generalises — *a ceiling only ever does anything when it is below the set speed*, so
"the map may never hold the car below the set speed" and "there is no ceiling" are the same
statement. It was redundant anyway: with feature B the limit already reaches the car through the
set speed. One authority, not two.

Untouched: curve braking, hazard braking, the pre-sign ramp, and the whole fail-open path. **All
three FAILS-OPEN tests pass unchanged** — that is how I know the removal did not leak outside
`if gated:`. Two gated-ceiling tests were replaced, one of them asserting the reported case
verbatim (stale 60 authorisation + driver-set 100 → no ceiling).

Pre-authorisation now also requires the set speed to still be **ours**, so the map cannot pull the
driver below a number they dialled in even transiently. **Cost, stated rather than discovered:**
after any manual set-speed change the ramp stays off until the feature re-takes ownership.

### V_CRUISE_MAX 145 → 110, and the trap in it

Sentinel-wrapped in `cruise.py`, with a `GRT_MODS.md` row recording that 110 is a *preference*,
not a technical limit — so a future rebase does not "restore" 145 as a bugfix.

**The trap, caught before it shipped:** `set_speed.py MAX_LIMIT_KPH` was `float(V_CRUISE_MAX)`.
At 110 a real **120 limit would read as implausible**, and the feature would go inert on exactly
the roads it matters most — while the heartbeat logged a reassuring `implausible_limit`. It is now
a literal 145: a 120 limit is recognised and then clamped to 110. `at_limit` also compares the
**clamped** limit, or a 120 road would re-decide every `REOFFER_S` forever. Both covered by tests.

### The pattern this is the third instance of — now §0.6 of the plan

| # | Defect | What the driver could not do |
|---|---|---|
| 1 | 60 km/h no-map fallback | stop the fork inventing a limit the road never posted |
| 2 | ownership term in the auto rule | keep a round set speed they dialled in themselves |
| 3 | un-revoked `authorisedLimit` ceiling | overrule a *wrong* map limit by raising the set speed |

> Every value the fork commands must be one the driver can override by an ordinary control input,
> immediately, with the override sticking. If a fork feature can hold a value against the driver's
> own most recent instruction, that is a bug regardless of how correct the value is.

Each looked reasonable in isolation; each was only visible from the driver's seat, never from a
test. Map data being wrong or stale is the normal condition, not an edge case — which is why the
driver has to win by construction.

Tests: 43 scc_map, 44 hooks, 71 set_speed, 30/30 schema. **NOT YET DEPLOYED.**

## 2026-08-07 — auto-rule fix DEPLOYED to comma4

Device on `277973d`, AGNOS 18.7, clean tree, healthy. **One commit** — the 2026-08-03 fallback
removal (`005d0035`) had already gone out in the 08-04 sync, so only the auto-rule fix was
outstanding. 6.4 KB bundle, clean fast-forward from `fe51b09`, back up in ~75 s.

Worth noting for the runbook: my first instinct was to bundle from the old `dcb3550cac` base and
I listed 23 "missing" commits — but the device HEAD `fe51b09` was *itself in that list*. Checking
`git merge-base --is-ancestor <device HEAD> nightly-dev` and bundling `fe51b09..nightly-dev`
turned a 6 MB transfer into 6.4 KB and made the real delta obvious. **Always derive the range
from the device's actual HEAD, not from a remembered base.**

Verified BEFORE the reboot — schema 30/30, suites 42 scc_map / 44 hooks / 67 set_speed, real
imports — and the reported case evaluated against the shipped rule **on the device**:

| set speed | new limit | Δ | result |
|---|---|---|---|
| 110 | 120 | +10 | **AUTO** (was ASK — the reported bug) |
| 116 | 120 | +4 | ASK — not a multiple of 10 |
| 103 | 90 | −13 | ASK — original hand-tuned protection intact |
| 100 | 60 | −40 | ASK — the >20 km/h safety rule intact |

`_why_not_auto(116, 4)` returns `set_speed_not_multiple_of_10`, i.e. the misleading
`driver_owns_set_speed` string is gone from the logs.

Verified AFTER the reboot: all processes up incl. `soundd`/`micd` (no audio transient this time);
`managerState` reports nothing missing; `onroadEvents` only `wrongGear`/`doorOpen`/
`seatbeltNotLatched` — **no `commIssue`, no `processNotRunning`**, so engagement is not blocked;
`longitudinalPlan VALID=True`; `grtSetSpeedState` at 20 Hz with `active=True`; **zero `grt:`
exceptions**.

**On the next drive, the one open question from 08-06:** if MAX ever jumps while a prompt is
open, look for a `seed_from_map` line in `/data/media/0/grt/set_speed.log` at that moment. That
is the discriminator for the engage-seed path, which is the only route that can write the set
speed while a prompt is pending and which I could not rule out statically.

## 2026-08-06 — auto-adopt rule corrected: ownership dropped; nothing pre-authorised while asking

Two issues from the drive. **Local only — comma4 offline.**

### Issue 2 (the clear one): a driver-set round speed must keep auto-tracking

Reported verbatim: set 110 by hand, new limit 120, and it asked for confirmation. Expected: auto
change. And 116 should still ask, because it is not a multiple of 10.

My auto test had **three** conditions where the operator had specified two — I had added
`self.tracking` (the feature still owns the set speed). A driver-set 110 fails it, so it
prompted. **Removed.** Roundness alone is the hand-tuned test and it does the job: 103 and 116
still prompt, 110 does not. The rule is now exactly:

> auto iff `set speed is a multiple of 10` AND `|Δ| ≤ 20 km/h`; otherwise ask.

`tracking`, `_owned_kph` and `_in_force_kph` are KEPT — they still drive `at_limit`, the
heartbeat and `grtSetSpeedState`. They are instrumentation now, not decision inputs.
`_why_not_auto` no longer returns `driver_owns_set_speed`, which would have actively misled the
next diagnosis.

### Issue 1: same root cause, plus an invariant

`tracking` was the **only time-varying term** in the auto test, and that test is evaluated at two
different moments: once for the UPCOMING limit (which pre-authorises it and unlocks the approach
ramp) and again when the limit becomes CURRENT (which decides prompt vs auto). If it flipped in
between, **the car slowed while the display waited** — exactly "Max speed is out of sync". Δ and
roundness cannot disagree that way, so dropping `tracking` removes the disagreement entirely.

Belt and braces, because the operator must be able to rely on "nothing changes until I answer":
**nothing is pre-authorised while a prompt is open.** Early return in `_preauthorise_upcoming`,
plus an explicit revoke on the frame a prompt is created — that method runs *before* the pending
branch, so it had a one-frame window where both could be set.

**NOT fully explained, and I am not claiming otherwise.** The above explains the car slowing. It
does not explain MAX itself moving, because `update()` cannot write the set speed while a prompt
is open. The one path that bypasses the pending branch is the **engage seed**: `engage_edge`
calls `_reset()` (clearing the prompt) and then seeds from the map. `grt_engage_edge` is
byte-identical to upstream's own condition on the next line — the one gating
`initialize_v_cruise` — so a momentary `carControl.enabled` glitch resets the set speed with or
without the fork; we only change what it resets *to*. **Cannot be ruled out statically.**
Discriminator for the next drive: a `seed_from_map` line in `set_speed.log` at the moment MAX
jumped. It is already logged — just look for it.

### The generalisation worth keeping

> If the same predicate is evaluated at two different times, every time-varying term in it is a
> bug waiting to happen. Here it produced a feature that acted on the car and not on the display.

Tests: 67 set_speed (3 new — the reported 110→120 case verbatim, the 116 counter-case, and one
asserting `authorisedNextLimit` stays 0 for **every** frame a prompt is open), 42 scc_map, 44
hooks, 30/30 schema. Plan doc §11.2 updated: rule 2a struck through with the reason kept.

## 2026-08-03 — REMOVED the 60 km/h no-map fallback (operator: problematic)

The engage seed used to fall back to **60 km/h** when no trusted limit had arrived within 10 s.
Removed entirely at the operator's instruction. **Local only — comma4 is offline; deploy
tomorrow.**

**Why it was wrong.** "No fix yet" is not an exceptional state — it is the *normal* state at the
moment of engagement (parked: `vEgo=0`, `bearing=0`, so `waySelectionType=fail`), and the
persistent state anywhere off-tile. So the timeout fired routinely and forced the set speed to 60
regardless of the road. Engage at highway speed, wait 10 s, and the set speed dropped to 60 — the
car then braking for a limit that was never posted anywhere.

**What happens now instead:**
- the seed **waits indefinitely** for a real posted limit; upstream's own `V_CRUISE_INITIAL*`
  stands until one arrives, and if none ever does the feature stays out of the way for the whole
  drive (owns nothing, authorises nothing, so `scc_map` also fails open);
- if the driver touches the cruise buttons while it is still waiting, the seed is **abandoned** —
  the speed is theirs, and a limit arriving later will not overwrite it. New `seed_abandoned`
  line in `set_speed.log`.

Deleted: `NO_MAP_DEFAULT_KPH`, `SEED_TIMEOUT_S`, and the whole `seed_no_map` path. `_seed()` is
now only ever called with a limit that passed `_read_limit`, which simplifies it — seeding is
unambiguously an adoption, so the limit is in force, decided and authorised in one place.

**The general rule, now written into the plan (§11.5) because it outlives this feature:**

> A map-driven feature may only ever command a value the map actually supplied. A "sensible
> default" for missing data is an invented measurement, and it will be wrong in exactly the
> situations where the data is missing. The correct behaviour for absent data is to do *nothing*
> and leave the base system in charge.

That is the same prime directive as §0.2, in a form I had not applied to *data* — only to
crashes. Worth remembering: the fallback did not fail safe, it failed *confidently*.

Docs updated: `PORT_MAPD_FROM_SUNNYPILOT.md` §11.2 (rule 1 restated) and §11.5 (rewritten, with
the old behaviour struck through and the reason kept, per this doc's convention), plus the §2.6
illustration which had used the 60 seed as its example. `PROGRESS.md` status + next step.

Tests: 62 set_speed (3 seed tests rewritten, incl. a REGRESSION test asserting no fallback value
is ever forced over 30 s without a fix), 31 scc_map, 44 hooks, 29/29 schema conformance.

## 2026-07-30 — ramp + coast-down DEPLOYED to comma4

Device on `19c3568`, AGNOS 18.7, clean tree, healthy. Fast-forward from `4a9305f`, 11 files,
+273/-13. Back up in ~75 s; no AGNOS update needed.

Verified BEFORE the reboot (cheapest place to find a problem):
- schema conformance **29/29** against the device's own log.capnp, including all four wire
  discriminants — mapd still 141/142/143, `grtSetSpeedState` 140 after adding `authorisedNextLimit`.
- unit suites on device: 31 scc_map, 44 hooks, 59 set_speed.
- real-import gate: `longitudinal_planner` imports with the new hook, `COAST_DECEL = 0.5`,
  `soften_cruise_decel(-1.2) -> -0.5`, `authorisedNextLimit` round-trips.

Verified AFTER the reboot:
- all processes up; `managerState` reports **nothing** shouldBeRunning-but-not-running.
- `onroadEvents` = `wrongGear` + `seatbeltNotLatched` only. **No `commIssue`, no
  `processNotRunning`** — engagement is not blocked.
- `longitudinalPlan VALID=True`; `grtSetSpeedState` at exactly 20 Hz (201 msgs / 10 s), `active=True`.
- **Zero `grt:` exceptions.**

**soundd, again — worth watching.** It crashed once during startup (`soundd_thread()` audio-stream
init) and manager restarted it successfully; it is running and stable. The other tracebacks in the
log are `athenad`/urllib3 network retries, which are routine. This is an audio-init race at boot,
not fork-related — but it matters because if soundd ever fails to come back, `processNotRunning`
is `ET.NO_ENTRY` and **blocks engagement**, which is exactly the state the operator hit and cleared
with a manual reboot. If the car ever refuses to engage, check `micd`/`soundd` first.

**Next drive judges two things:** whether the pre-sign ramp now feels right for in-band limit
changes (it should ease down at ~0.5 m/s² and land on the limit AT the sign, not step at it), and
whether the coast-down after an overtake feels natural. Both instruments are in place:
`/data/media/0/grt/set_speed.log` and `/data/media/0/mapd_debug.log`.

## 2026-07-30 — ramp restored when no confirmation is needed + gentle coast-down to set speed

Operator review of the authorisation gate, two requirements.

### 1. The pre-sign ramp must be ON whenever confirmation is not required

The gate had switched the approach ramp off wholesale, because an UPCOMING limit cannot be
authorised before it becomes current. Correct observation from the operator: when the auto rules
already say no confirmation is needed, there is nothing to wait for.

`set_speed` now PRE-AUTHORISES an upcoming limit that passes the **same three rules** as a normal
auto-adopt (feature owns the set speed AND it is a multiple of 10 AND |Δ| ≤ 20), and `scc_map`
lets the ramp shape the run-up at `APPROACH_DECEL = 0.5 m/s²` again. A change that WOULD prompt
is still not pre-authorised, so the confirmation flow is untouched.

**Published in its own field, `authorisedNextLimit`, deliberately NOT reusing `authorisedLimit`.**
That separation is the entire point: if the CEILING saw the upcoming value, the target would drop
to the new limit while the sign was still hundreds of metres away — exactly the harsh step the
ramp exists to prevent. There is a test asserting the pre-authorised value is not used as a
ceiling.

### 2. Gentle coast-down to the set speed (hook 5, new)

Operator's example: driving 110 to overtake with cruise set to 100, lift off the throttle, and
the car should ease back to 100 rather than braking hard. Stock clips `a_cruise` at
`A_CRUISE_MIN = -1.2 m/s²`, and a 10 km/h error saturates it, so it braked at the full −1.2.

`COAST_DECEL = 0.5` raises that floor for PLAIN overspeed only. One line in
`longitudinal_planner`, right after `get_cruise_accel`.

**Why this cannot make the car less able to stop** (each asserted by test):
- it only ever RAISES `a_cruise`, and `a_cruise` is one candidate in the planner's `min()` — with
  a lead the MPC candidate is harder and wins; with a hazard, hook 2's candidate wins. So it can
  only bind when the cruise branch is the sole reason for braking, i.e. plain overspeed on a
  clear road;
- skipped entirely when hook 1 lowered `v_cruise`, so the map approach profile keeps full
  authority and its late-hazard self-escalation still works;
- skipped when `v_cruise ≈ 0`, which is how `forceDecel` demands a stop.

`COAST_DECEL = 1.2` restores stock behaviour exactly. Trade-off worth knowing: the car now spends
longer above its set speed after an overtake.

### One spec ambiguity resolved by precedent, not by asking again

The operator's parenthetical read "auto change if <=20 km/h **OR** cruise set speed is a factor
of 10". The earlier explicit statement was AND — *"auto-adopt only changes within ±20 km/h if set
max speed is a factor of 10"* — and AND is what is implemented. OR would let a hand-tuned 103 be
overwritten silently, which rule 2a exists to prevent. Flagged rather than silently chosen.

Tests: 31 scc_map, 44 hooks, 59 set_speed, 29/29 schema conformance. **NOT YET DEPLOYED.**

### Device note

The `micd`/`soundd` processes were down after the deploy reboot, raising `processNotRunning`
(which is `ET.NO_ENTRY` and blocks engagement). The operator rebooted and it cleared — a boot-time
audio-init transient, not caused by the fork (neither file is touched by it). **Correction to what
I said at the time:** I claimed the qlog proved this pre-existed the deploy, but the route I
checked (`00000043`) was created at 08:50, *after* the 08:48 reboot — so it proved nothing. The
claim was unsupported; the reboot resolved it either way.

## 2026-07-30 — drive 2 of set-speed: two issues root-caused from the logs and FIXED (not deployed)

**The alert DOES render** — the operator saw the confirmation box, which closes the one open
question from yesterday. `set_speed.log`: 782 lines, 12 engagements all seeded from the map,
**544 heartbeats reporting `at_limit`** (set speed sitting exactly on the posted limit), 2 clean
auto-adopts (40→20, 60→80). The core works.

### ISSUE 1 — the car obeyed the posted limit while the display waited for confirmation

Root cause is **feature A, not feature B**: `scc_map` applies the posted limit as a ceiling inside
the planner and knew nothing about feature B's confirmation state. Two independent notions of
"the limit". Measured in `mapd_debug.log` (34,001 frames):

| binding source | frames |
|---|---|
| current-limit **ceiling** (`speedLimitSuggestedSpeed`) | **1,069** |
| upcoming-limit **approach ramp** (`nextSpeedLimit`) | **95** |

Example frames: set speed 105, no hazard, commanded target pinned at the posted 40. And the ramp
at 80 m from a 20 zone commanding 37.9 — literally "it slowed down before the sign".

**Fix (operator chose "confirmation gates behaviour"):** card publishes `authorisedLimit` +
`active` on `grtSetSpeedState`; plannerd subscribes via `GRT_SUB` (already passed as all three
ignore lists — plannerd calls `all_checks()` UNSCOPED, §2.2 of the plan); `scc_map` obeys only
authorised limits. **Fails OPEN** to mapd's own value when the feature is inactive or the message
is missing/stale — infrastructure failure must never silently stop the car obeying limits.
Curve and hazard braking are deliberately **not** gated; they are not speed limits.

**⚠️ Accepted consequence, stated for the record:** a declined or unanswered limit change means
the car does **not** slow for that sign. openpilot stops being an automatic speed-limit follower
and becomes one the driver authorises.

**⚠️ Documented trade-off:** the pre-sign approach ramp is now OFF while gated, because it acts on
the *upcoming* limit, which by definition cannot be authorised yet — the driver is only asked once
it becomes current. Slow-downs therefore happen AT the sign via the ceiling, at the planner's
`A_CRUISE_MIN = -1.2 m/s²` floor instead of the validated 0.5 m/s² ramp. **If that feels abrupt,
the follow-up is two-stage pre-authorisation: authorise the upcoming limit early, move the set
speed at the sign.** Deliberately not built blind — it needs a drive to know if it is warranted.

### ISSUE 2 — the prompt vanished before it could be answered. NOT the timeout.

The operator was right to push back on my first read. The 10 s window did expire twice, but the
prompts that could not be reacted to died in **0.2–0.3 s**:

```
-5.80  settling  limit=120                 ← 120 has held ~1 s
-5.55  pending   120  way=extended
-5.22  stale     current reads 60          ← retired 0.33 s later
-1.22  way_possible raw=120 way=possible
-0.20  pending   120  way=current
+0.00  stale     current reads 60          ← retired 0.20 s later
```

`mapdOut.speedLimit` was **alternating 60↔120 every 1–2 s** while `waySelectionType` churned
`current`→`possible`→`extended`: a spot where a freeway and a parallel service road overlap. Both
values passed the 1 s stability gate, so the feature offered 120 — **the wrong road** — and the
next flip killed it. Both of those prompts were spurious.

A stability requirement on the *retirement* cannot fix this: the 60 is equally stable. Two fixes:
- `LIMIT_STABLE_S` **1.0 → 3.0 s**, so neither value in a flip-flop qualifies at all;
- an offer is retired as stale **only once a DIFFERENT limit has become established in its own
  right**. Retiring on a single differing frame WAS the 0.2 s bug. This required moving the
  stability tracker so it keeps running while a prompt is open — it previously stopped, which is
  why the new test could not otherwise fire.

`PENDING_TIMEOUT_S` stays at **10 s** at the operator's instruction.

### Also per operator spec: DIRECTION-MATCHED confirmation

"Push the switch the way the speed is going." A **higher** pending limit is accepted with RES/+
and declined with SET/−; a **lower** one is accepted with SET/− and declined with RES/+. The
direction is captured when the offer is MADE, so a set-speed nudge mid-window cannot invert the
buttons under the driver. The alert text names the correct button.

This replaces "RES/+ accepts, SET/− declines", which in this drive read a routine **81→80 km/h
nudge as a decline** — visible in the log as `decline` 6.9 s after the offer.

New state stays single-writer, per the rule that earned itself last session: `_established_kph`
(the debounced limit) and `_authorised_kph` (what the driver accepted).

Tests: 28 scc_map (9 new, covering fail-open AND fail-closed), 35 hooks, 55 set_speed, 28/28
schema conformance including the four wire discriminants. **NOT YET DEPLOYED.**

## 2026-07-29 — set-speed test drive: GOOD PRELIMINARY RESULTS; plan doc consolidated

Operator reports **good preliminary results** from the first drive with set-speed tracking live.
Not yet a validation — see the open item below.

**STILL UNVERIFIED, and it is the one thing that decides whether half the feature works:**
whether the confirmation prompt actually RENDERS. It only fires while engaged, so it could not
be checked during deployment. If it never appears, the failure is benign but total — no prompt
means no confirmation means the set speed simply never moves for any out-of-band limit change,
which is the same silhouette as the 38,300-exception drive: fine from the driver's seat, doing
nothing. `/data/media/0/grt/set_speed.log` settles it: `pending` lines prove the tracker offered;
a `confirm` following one proves the driver could see and answer it. **Offered to pull and
analyse that log — not yet done.**

### `PORT_MAPD_FROM_SUNNYPILOT.md` rewritten as a two-feature record + reusable recipe

Merged everything learned from the set-speed work into the main plan rather than starting a
second document, at the operator's request. The doc now covers feature A (mapd control) and
feature B (set-speed tracking) and shares every constraint between them.

What is new in it:
- **§0.3** — the session's most important lesson: *a passing suite is evidence your tests agree
  with your model, not that your model is right.* 104 tests passed over two real defects, and
  one test actively asserted a bug was correct. Every set-speed defect was caught by review.
- **§0.4** — settle design questions with device data before choosing thresholds, with the two
  one-line queries that did so here (set speed sits at 105; limits are only ever multiples of 10).
- **§0.5** — restate an operator's prose spec as testable predicates and put genuine ambiguity
  back as a choice. Two of three answers changed the design.
- **§2 is now SIX bugs, not three** — added §2.4 (the SubMaster mistake in a process where it
  blocks ENGAGEMENT, with the per-process blast-radius table and the grep that finds it),
  §2.5 (an exact-frame `==` gate drops the event permanently; deferral vs decision), §2.6 (a
  terminal "handled" state killed the feature for a drive; single-writer-per-fact). Log flooding
  folded into §2.1 as the second half of the same rule rather than a seventh entry.
- **§3.2** — the two speed variables, the full chain from `v_cruise_kph` to the Staria cluster,
  and why `DT_CTRL` vs `DT_MDL` silently changes every timeout by 5×.
- **§4.3/§4.4** — the reusable patterns: a fork-owned message on a renamed `CustomReserved` slot,
  and the finding that a driver-facing alert needs **no schema change at all**.
- **§5** corrected on two counts I had recorded wrongly: `pkill -f "[m]anager\.py"` (bracket
  form) DOES work over ssh — only the bare form self-matches — and the flag needs no second
  reboot, since `update_params()` re-reads every 3 s.
- **§6** — added the real-import gate (2b) and the engagement gate (6b), and the instruction to
  run the cheap gates BEFORE the reboot.
- **§7.1** — instrument the negative case; a benign-sounding heartbeat reason can hide a dead
  feature.
- **§9** — an ordered recipe for the next fork feature, plus five rules worth memorising.
- **§11** — the full feature-B record, including why the first ±20-only design was nearly inert.

## 2026-07-29 — set-speed tracking DEPLOYED to comma4 and ENABLED

Device is on `dc6e2b5`, AGNOS 18.7, clean tree, feature ON. No reboot loop, no errors.

Sequence: git bundle `dcb3550cac..nightly-dev` (5.8 MB) → scp → `pkill -f "[m]anager\.py"` →
`git fetch <bundle> && git merge --ff-only` → reboot. Device was 8 commits behind at `cbe0818`;
fast-forward was clean, 16 files / +2044 −352. No scons, no cereal SCP. Bundle deleted after.

Device came back in ~120 s. AGNOS_VERSION 18.7 == /VERSION 18.7, so no OS update was triggered
this time (unlike the Jul 28 deploy).

**Verified ON DEVICE, before the reboot:**
- `test_schema_conformance.py` against the device's own log.capnp: **25/25**, including the four
  wire discriminants — mapdExtendedOut/mapdIn/mapdOut still **141/142/143**, so renaming
  `customReserved16` did not move mapd's slots. `grtSetSpeedState` = 140.
- Real imports with the actual openpilot deps (not the test stubs — the class of check the
  `.status` bug proved is necessary): `grt.hooks`, `grt.set_speed`, `grt.registry` all import,
  `grtSetSpeedState` is in `SERVICE_LIST`, `messaging.new_message('grtSetSpeedState')` builds,
  and `SetSpeedLimitTracker()` constructs reporting `enabled = False` (flag absent = safe
  default).

**Verified AFTER the reboot:**
- manager, card, plannerd, controlsd, selfdrived, modeld and mapd all up; managerState reports
  **nothing shouldBeRunning-but-not-running**; 0 Traceback/CRITICAL in the newest swaglog.
- `msgq_grtSetSpeedState` exists and `grtSetSpeedState` arrives at exactly **20 Hz** (240 msgs
  in 12 s) — card's `frame % 5` publish is behaving.
- **THE CRITICAL ONE — engagement is not blocked.** `onroadEvents` carries only `wrongGear` and
  `seatbeltNotLatched`, i.e. the legitimate physical blockers for a parked car. **No
  `commIssue`, no `commIssueAvgFreq`** — so adding `grtSetSpeedState` to selfdrived's SubMaster
  did not trip its unscoped `all_checks()` at `:381`/`:469`. That was the single highest-risk
  edit in this change and it is clean.
- `carState.vCruise` 255 (UNSET, not engaged); `grtSetSpeedState` pending=False tracking=False.

Then enabled: `echo 1 > /data/media/0/grt/SmartCruiseControlSetSpeed`, confirmed the tracker
reads `enabled = True`. No restart needed — `update_params()` re-reads every 3 s.

**WHAT COULD NOT BE VERIFIED PARKED, and must be watched on the first drive:**
1. **Does the alert actually render?** The prompt only fires while ENGAGED, and `ET.WARNING`
   alerts are cleared when the state machine is not in a warning-capable state — so a parked
   car cannot show one. The failure mode is benign (no visible prompt → no confirmation → the
   set speed simply does not change), but it would make every out-of-band limit change a silent
   no-op. **Watch for the text "Speed limit N km/h / Press RES/+ to accept" plus a chime.**
2. The set speed at engage being the posted limit (or 60) rather than 105.
3. `carState.cumLagMs` vs a pre-change segment — card now carries a subscriber AND a publisher.

Disable at any time with `rm /data/media/0/grt/SmartCruiseControlSetSpeed` (takes effect within
3 s, no restart). The device is one docs-only commit behind the Pi5 (`08aedfa`, PROGRESS.md).

## 2026-07-29 — set-speed tracking REDESIGNED to the user's spec, and part (b) BUILT

The user replaced the ±20-only design below after seeing the measurement that the set speed
sits at 105 km/h all drive (ExperimentalMode initial), which made part (a) nearly inert on the
roads actually driven. The new rules fix the cause rather than the symptom.

**AGREED RULES (this supersedes the "AGREED BEHAVIOUR" further down):**

1. **At engage, seed the set speed from the posted limit**, or **60 km/h if there is no map
   data** — replacing upstream's fixed `V_CRUISE_INITIAL` (40) / `V_CRUISE_INITIAL_EXPERIMENTAL_
   MODE` (105). The user chose that this wins even on a RES/resume engage, which upstream would
   otherwise answer with the previous set speed.
2. **A later limit change is adopted automatically only if ALL of:**
   a. the feature still OWNS the set speed — it equals the limit in force, or the value we
      ourselves last wrote;
   b. the set speed is a **multiple of 10** (60, 70, ... 120) — a non-round value is hand-tuned;
   c. the change is **within ±20 km/h**.
3. **Otherwise the new limit is offered as a PENDING prompt for 10 s**, adopted only on a RES/+
   tap. SET/− declines it; a limit change under it retires it as stale.

The >20 km/h rule is absolute — it applies even while the feature owns the set speed. On these
roads limits of 20 and 40 exist, so 120→80 and 60→20 both prompt. That is the user's explicit
safety call, and it means prompts will be common; that is intended, not a defect.

Rule 2a is what makes "set your own speed and keep it" work: dial in 103 in a 100 zone and the
feature never touches it again — it only asks. Dial back to exactly 100 and tracking resumes.

**THE ALERT COST NOTHING IN THE END.** `AlertManager.add_many(frame, alerts)` keys on
`alert.alert_type`, a plain string, and `selfdriveState.alertText1` is free-form Text with
`alertSound` reusing the existing `AudibleAlert` enum. So the prompt is a plain `Alert` object
with a fork-owned `alert_type` — **no `EventName` enumerant, no schema addition, nothing to
recompile**. That kills the on-device experiment the previous entry called for. `ET.WARNING` is
the right event type: `update_alerts` only clears WARNING when not engaged, and this feature
only runs engaged.

**THE REAL WORK WAS THE CHANNEL.** The pending state lives in `card`; only `selfdrived` can
raise an alert. Added a fork-owned message on reserved slot 16 — `CustomReserved16` →
`GrtSetSpeedState` renamed IN PLACE (struct ID kept, `log.capnp` ordinal `@142` kept), so the
wire discriminant stays 140 and **mapd's 141/142/143 do not move**. Now asserted permanently by
`test_schema_conformance.py` rather than checked by hand. card publishes at 20 Hz (not 100 — it
must not spend its CAN budget on a status message); selfdrived subscribes.

**THE DANGEROUS BIT, caught before writing it.** selfdrived calls `sm.all_checks()` **unscoped**
at `:381` and again at `:469` where it gates `self.initialized`. Adding `grtSetSpeedState` to
its SubMaster without the `ignore` list would have **blocked engagement entirely** on any device
where card doesn't publish it — strictly worse than the `longitudinalPlan` invalidation bug from
earlier today, and the third instance of the same class. The service is in selfdrived's `ignore`
list, and GRT_MODS.md now flags that row as the most safety-critical in the table.

Also settled empirically, not assumed:
- 11 MB of on-device `mapd_debug.log`: every limit seen is 20/40/60/80/120 — all multiples of
  10 — so rule 2b cannot latch the feature into permanent-prompt mode by itself.
- `mapdIn` already proves Python can publish on a service absent from the compiled
  `services.h`, so `grtSetSpeedState` needs no device experiment.
- Float comparisons use a 0.5 km/h epsilon. An `==` on values that have been through
  `round()`, `+=` and `clip()` would end tracking permanently after one wobble, and the symptom
  would read as "the feature got annoying", not as a bug.

Seeding detail worth knowing: engaging from standstill reports `waySelectionType=fail`, so the
seed WAITS up to 10 s for a first fix before falling back to 60. Without the wait every drive
would start on 60 and immediately prompt to move to the real limit. Upstream's own value stands
during the window.

**TWO BUGS IN THE OWNERSHIP LOGIC, both found in review, both in the seams between state
variables that were maintained on some code paths and not others:**

1. **An ignored prompt killed the feature for the rest of the drive.** Seeding 60 with no map,
   then meeting a 100 zone, gives |Δ|=40 → prompt. If the driver missed it, the limit was
   marked handled *permanently*: no further prompt, no adopt, set speed stranded at 60 in a
   100 zone — with the heartbeat reporting a benign-looking `already_handled` forever. Fixed
   with `REOFFER_S = 60 s`: an UNANSWERED prompt is offered again while the mismatch persists.
   A deliberate SET/− decline is never re-offered, because that was an answer.
2. **A stale prompt left residue.** When the road changed under an open prompt, the offer was
   marked "acted", so returning to that limit later skipped the decision entirely. A stale
   offer was never decided and is now not recorded as one.

The fix was structural, not three patches: `_owned_kph` ("the set speed WE established") and
`_in_force_kph` ("the posted limit in force") each now have exactly ONE writer, and the limit
in force is recorded at a single point — right after the stability gate, as a fact about the
road, independent of what we decide about it. The old `_prev_limit_kph` juggling, including a
save/restore around prompt creation, is gone.

Tests: 51 set_speed (rewritten — the ±20-only assertions were wrong, not failing), 35 hooks
(hook 3 + the new hook 4 alert), 18 scc_map, 25/25 schema conformance including the four wire
discriminants. NOT YET ON THE CAR.

## 2026-07-29 — set speed tracks the posted limit: adoption core IMPLEMENTED (part a, SUPERSEDED ABOVE)

Implemented the auto-adopt half of the feature designed below. **Part (b), the >20 km/h
PENDING + RES/+ confirmation, is coded but shipped DISABLED** — see "what is deliberately not
shipped" at the end.

New (fork-owned): `openpilot/grt/set_speed.py` — `SetSpeedLimitTracker`, plus
`grt/tests/test_set_speed.py` (27 tests).

Upstream hook (category C, sentinel-wrapped) — `selfdrive/car/card.py`, +12 lines:
- `SubMaster` gains `mapdOut` via `GRT_SUB_CARD`, **with `ignore_alive`/`ignore_valid`/
  `ignore_avg_freq`**, exactly as plannerd needed. card's own checks are scoped to
  `all_checks(['carControl'])` / `all_alive(['carControl'])`, verified, so `mapdOut` cannot
  invalidate `carOutput` — the ignores are belt-and-braces for the same bug class.
- One line `grt_hooks.track_set_speed(...)` after `initialize_v_cruise` and before the
  `CS.vCruise` assignment.

DEVIATION FROM THE DESIGN BELOW, deliberate: the design said "ONE sentinel-wrapped line in
cruise.py's non-pcm path". The hook is in card.py instead. `VCruiseHelper.update_v_cruise` has
no access to a SubMaster, and `selfdrive/car/tests/test_cruise_speed.py` calls
`update_v_cruise(CS, enabled, is_metric)` directly in five places — changing that signature
would break an upstream test suite. card.py already owns both the helper and a SubMaster, so
`cruise.py` is now untouched by the fork.

BEHAVIOUR (what actually ships):
- Edge-triggered on a CHANGE of `mapdOut.speedLimit`, never continuous. One decision per limit
  VALUE. A driver who overrides an adopted speed is never overridden back.
- |new limit − set speed| ≤ 20 km/h → adopt automatically, up AND down. Writes BOTH
  `v_cruise_kph` and `v_cruise_cluster_kph` (upstream keeps them equal in the non-pcm path and
  we run after it), so the comma UI MAX and the Staria cluster both follow.
- |difference| > 20 km/h → currently IGNORED, logged as `ignore`.
- Gates: feature flag on, openpilot engaged, set speed initialised, `mapdOut` alive+valid,
  `tileLoaded`, `waySelectionType` ∈ {current, predicted, extended} (parked reports `fail`),
  limit stable for 1.0 s, limit within [20, 145] km/h, and no cruise-button activity that frame.
- **Raising** the set speed additionally requires `waySelectionType` ∈ {current, extended}.
  `predicted` is mapd guessing which way we will take at a junction; acting on a guess to slow
  down is conservative, acting on it to speed up is not. Slowing down still honours `predicted`.
- Result clamped to upstream's `[V_CRUISE_MIN, V_CRUISE_MAX]`.

Both "not now" gates (driver on the buttons, upward-on-`predicted`) are DEFERRALS: they return
before `_acted_limit_kph` is set, so the limit is reconsidered on the next clear frame. The
stability counter is compared with `>=`, not `==`. This was a real bug in the first cut — with
an exact `==`, a button press landing on the single frame the gate fired dropped that limit
**permanently and silently**, because the counter kept incrementing and `==` was never true
again. The original test asserted the no-change and blessed it. Caught in review; the test now
asserts adoption on the following clear frame.

Two design points worth keeping:
- The [20, 145] km/h band REJECTS rather than clamps. Clamping would launder a garbage value
  into a legal set speed, and the band doubles as a units-error trap: mapd publishes m/s, so a
  km/h value leaking through reads ~3.6× high and gets rejected. Confirmed with pycapnp that
  `speedLimit` 16.667 → 60 km/h and that `str(waySelectionType)` yields the enumerant name.
- Confirmation/adoption acts on button RELEASE, the same edge upstream's `+1` bump uses. Our
  hook runs after it and assigns an absolute value, so the adopted limit wins cleanly instead
  of landing on limit+1. Staria RES/+ → `ButtonType.accelCruise`, verified statically in
  `opendbc/car/hyundai/carstate.py` `BUTTONS_DICT`, not assumed.

Enable on device (no rebuild): `echo 1 > /data/media/0/grt/SmartCruiseControlSetSpeed`.
Default OFF, and separate from `SmartCruiseControlMap` because this is the first fork feature
that can ACCELERATE the car on OSM data.

Instrumentation: `/data/media/0/grt/set_speed.log` — one line per DECISION **plus a 2 s
heartbeat naming the gate that rejected** (`mapd_not_alive`, `no_tiles`, `way_fail`,
`no_limit_posted`, `implausible_limit`, `settling`, `already_handled`, `driver_busy`,
`defer_up_on_predicted`), with the raw m/s limit, `waySelectionType` and the candidate counter.
Decision-only logging was not enough: if the feature never fires on the road the log would be
empty and the five possible causes indistinguishable — and `mapdOut` is still not in rlog on a
prebuilt branch, so there is no retrospective path. One failure mode to look for specifically:
`_read_limit` returning None resets the stability counter, so a `waySelectionType` flickering
between `current` and `fail` faster than 1 s means a limit never clears the gate at all.
Unlike the mapd controller, the OUTCOME here is retrospectively analysable: `carState.vCruise`
/ `vCruiseCluster` are in rlog.

Also registered `SmartCruiseControlSetSpeed` in `grt_params_keys.inc` for consistency with the
other two fork keys. It has no effect on this prebuilt branch (never compiled in) — the file
fallback is what actually works — but the pattern stays uniform.

AFTER DEPLOY, one free check: card is the 100 Hz realtime CAN loop and now carries an extra
subscriber. `carState.cumLagMs` IS in rlog — compare a post-change segment against a pre-change
one. plannerd's precedent does not cover this risk.

HOW OFTEN WILL IT ACTUALLY FIRE? Measured, not guessed. `ExperimentalMode=1` on the device, so
`V_CRUISE_INITIAL_EXPERIMENTAL_MODE = 105` is the set speed at engage — and from
`mapd_debug.log`, while moving the set speed was **105 km/h for 1,918 frames and 145 for 26**.
The driver essentially never changes it. With a ±20 band off 105 that adopts limits in
**[85, 125]**: 100 and 120 zones fire, 80 and 60 zones do not.

Drive 3 was urban (max 66 km/h, mean 35 while moving), so on the roads actually driven,
**part (a) alone will mostly log `ignore` and do nothing.** It is not inert in principle — the
band applies to the CURRENT set speed, so a route that steps down gradually ratchets:
105 → 100 (adopt) → 80 (adopt) → 60 (adopt). But a direct 105-to-60 transition is out of band.

Two consequences: (1) design the first road test around a 100 or 120 zone, or a route with
graduated limits, otherwise "nothing happened" is the expected result and proves nothing;
(2) part (b) is the MAJORITY case on this car's roads, not an edge case — which raises its
priority above where the original design put it. Widening the band is the alternative, but that
trades away exactly the confirmation step the >20 rule exists to provide.

WHAT IS DELIBERATELY NOT SHIPPED — part (b), the pending/confirm alert. The state machine and
its tests exist and pass, but `PENDING_ENABLED = False`. A pending limit the driver cannot
perceive is worse than not offering one, and the alert needs an event that this PREBUILT branch
can actually render. Adding an `EventName` enumerant is NOT the same claim as the in-place
struct renames we proved wire-neutral — those kept struct IDs and ordinals; a new enumerant is
a genuine schema addition on a device whose compiled artefacts are frozen. The discriminating
check, to run on the car before building anything: **can a Python-published `onroadEvent`
carrying an EventName the compiled side does not know about reach the UI and soundd?** If not,
the cheaper route is reusing an existing enumerant the Staria can never raise and overriding its
text in the Python `EVENTS` dict — zero schema change. Nothing about part (b) should be built
until that question is answered on device.

FOLLOW-UP FINDING (local, reduces that risk a lot): the alert the driver actually sees and hears
does NOT travel as an EventName. `selfdrived.publish_selfdriveState` puts
`AM.current_alert.alert_text_1` into `selfdriveState.alertText1` (plain Text) and
`audible_alert` into `selfdriveState.alertSound`, an **existing** `AudibleAlert` enum whose
`prompt` enumerant already maps to `warning.wav` in `ui/soundd.py`. The UI and soundd consume
`selfdriveState`, not `onroadEvents`. So the text is free-form and the sound can reuse an
existing enumerant — **no schema addition is needed for rendering**. An `EventName` is only the
dict key that selects the Alert inside selfdrived, and that whole path (events.py, selfdrived,
ui, soundd) is Python on this branch.

THE REAL REMAINING PROBLEM for part (b) is therefore not the alert — it is the CHANNEL. The
pending state lives in `card`; the alert must be raised in `selfdrived`, which does not
subscribe to `mapdOut` and must not (it gates engagement — worse blast radius than the
`longitudinalPlan` invalidation bug). Options, in rough order of preference:
  1. a fork-owned message on one of the 17 free `CustomReserved0..16` slots (rename in place,
     struct ID and ordinal unchanged — the pattern already proven wire-neutral for mapd),
     published by card, subscribed by selfdrived WITH ignore lists;
  2. move the whole pending state machine into selfdrived and have it drive card — worse, splits
     ownership of the set speed;
  3. a file in `/data/media/0/grt` written only on state edges — crude but zero schema risk.
Pick one deliberately; do not let it default.

Tests: 33/33 set_speed, 18/18 scc_map, 17/17 hooks, 20/20 schema conformance (now covering
`mapdOut.speedLimit`/`tileLoaded`/`waySelectionType` and `carState.buttonEvents`/`vCruise`/
`vCruiseCluster`). `cruise.py` untouched, so `test_cruise_speed.py` is unaffected; it needs the
full openpilot venv and was not run on the Pi5.

NOT YET ON THE CAR. Deploy + road test outstanding.

## FEATURE DESIGN (part a now implemented above): set speed tracks the speed limit

User asked for the comma 4 "MAX" and the Staria cluster to follow posted limits. Today we only
lower v_cruise INSIDE longitudinal_planner - a local planning variable that never reaches
carState.vCruise, so no display changes. Expected, not a bug.

FEASIBILITY - both displays are achievable (verified on device):
- CarParams.pcmCruise = False and openpilotLongitudinalControl = True on the Staria, so
  openpilot owns the set speed via VCruiseHelper._update_v_cruise_non_pcm (cruise.py).
- VCruiseHelper.v_cruise_kph / v_cruise_cluster_kph -> carState.vCruise / vCruiseCluster
  -> comma UI hud_renderer AND controlsd.py:166 hudControl.setSpeed
  -> hyundai/carcontroller.py:86 set_speed_in_units -> the CLUSTER. So the car's dash follows.

AGREED BEHAVIOUR:
- |new limit - current set speed| <= 20 km/h : adopt automatically, both up AND down.
- |difference| > 20 km/h : do NOT adopt. Show the new limit as PENDING for 10 s with an alert
  sound; adopt only if the driver taps RES/+ (ButtonType.accelCruise) within that window.

IMPLEMENTATION NOTES for whoever builds it:
- New fork module (e.g. openpilot/grt/set_speed.py) owning the pending state machine and the
  10 s timer. Upstream touch must stay ONE sentinel-wrapped line in cruise.py's non-pcm path.
- Button press: cruise.py already parses CS.buttonEvents for accelCruise/decelCruise - reuse
  that, do not add a second parser.
- Alert/sound: needs an event; check what is available without adding to the compiled alert
  tables (this is a PREBUILT branch - see the constraints note).
- SAFETY: this is the first change that lets the feature ACCELERATE the car on OSM data. It must
  respect V_CRUISE_MIN/MAX clamping, must not fight the driver's own button presses, and must be
  behind its own flag (/data/media/0/grt/SmartCruiseControlSetSpeed) default OFF.
- The speed-limit VALUE should come from mapdOut (current limit), not from our v_target, which
  is an approach profile and deliberately transient.
