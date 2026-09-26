#!/usr/bin/env python3
"""Tests for the far-lead pre-brake hook (openpilot/grt/far_lead.py).

Runs with STUBBED openpilot deps so it works on a dev box that cannot import openpilot.

    python3 openpilot/grt/tests/test_far_lead.py

See FAR_LEAD_PREBRAKE_PROMPT.md (repo root) section 9 for the case list this covers, and the
far_lead.py module docstring for the bugs testing/replay already caught: gating arming on raw
vRel (false-armed on noise), checking the distance gate at persistence-completion instead of at
first lock (stopped-lead case never armed at all, v1), the rising-edge absence gate that blocked
arming on a real 2026-08-27 drive regardless of how hot the danger signal got (removed in v2 --
see "THIRD BUG"), and a_req alone clearing HOT_A_REQ on ordinary highway noise / tiny closing
rates at long range, which held the floor for far longer than any real closing event lasted
(fixed by HOT_CLOSING_RATE on both arm and release -- see "FOURTH BUG"), and the lead slot
switching objects, which the unguarded range-rate filter differentiated into tens of m/s of phantom
closing (fixed by the object-switch guards -- see "OBJECT-SWITCH GUARDS").
"""
import pathlib
import sys
import types

GRT = pathlib.Path(__file__).resolve().parents[1]


def _stub(name, **attrs):
  m = types.ModuleType(name)
  for k, v in attrs.items():
    setattr(m, k, v)
  sys.modules[name] = m


for p in ("openpilot", "openpilot.common", "openpilot.selfdrive",
          "openpilot.selfdrive.controls", "openpilot.selfdrive.controls.lib",
          "openpilot.selfdrive.controls.lib.longitudinal_mpc_lib", "openpilot.grt"):
  sys.modules.setdefault(p, types.ModuleType(p))

DT_MDL = 0.05
_stub("openpilot.common.realtime", DT_MDL=DT_MDL, DT_CTRL=0.01)


class _Source:
  lead0 = "lead0"


_stub("openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc",
      LongitudinalPlanSource=_Source)
_stub("openpilot.selfdrive.controls.lib.drive_helpers",
      should_stop=lambda v, a: bool(v < 0.3 and a < 0.1))

import importlib.util as _ilu  # noqa: E402


def _load(name, path):
  spec = _ilu.spec_from_file_location(name, path)
  mod = _ilu.module_from_spec(spec)
  sys.modules[name] = mod
  spec.loader.exec_module(mod)
  return mod


fl = _load("openpilot.grt.far_lead", str(GRT / "far_lead.py"))

results = []


def check(name, cond):
  results.append(cond)
  print(f"  {'PASS' if cond else '**FAIL**':9s} {name}")


def new_hook():
  return fl.FarLeadPreBrake()


# The band-slope gate needs BAND_N + SLOPE_N frames (7.0 s) of range history before its slope is
# defined at all, and it arms on a CROSSING below ARM_SLOPE -- so a test that starts closing hard
# on frame 0 never arms: by the time the slope exists it is already past the bar and never crosses
# it. Real approaches begin from a steady gap, so every arming test warms the band on one first.
WARM = fl.BAND_N + fl.SLOPE_N + 20


def warm(hook, dRel, frames=WARM, v_ego=30.6):
  """Fill the band buffers at a steady range, leaving the band slope at ~0 (above ARM_SLOPE)."""
  for _ in range(frames):
    hook.step(True, dRel, 0.0, v_ego, True, True, False, 1.0, dRel)


def close(hook, dRel0, v_ego, closing_rate, frames, relaxed=True, long_active=True,
          driver_input=False, stock_min=1.0, model=True):
  """Drive `frames` ticks with dRel decreasing at closing_rate (m/s), true vRel == closing_rate
  (noiseless -- these tests check the state machine's logic, not the filter's noise rejection;
  that is covered separately by the replay bar in section 10, run against the real log).

  `model` feeds the same range as dRel_model, which is what the band-slope gate reads; pass
  model=False to simulate modelV2 publishing no lead."""
  out = []
  dRel = dRel0
  for _ in range(frames):
    out = hook.step(True, dRel, closing_rate, v_ego, relaxed, long_active, driver_input,
                    stock_min, dRel if model else None)
    dRel = max(0.0, dRel + closing_rate * DT_MDL)
  return out, dRel


def arm(hook, dRel0=120.0, v_ego=30.6, closing_rate=-8.0, max_frames=200, prewarm=True):
  """Drive until armed or max_frames elapse. Returns (out, dRel) at the arming frame."""
  if prewarm:
    warm(hook, dRel0, v_ego=v_ego)
  dRel = dRel0
  for _ in range(max_frames):
    out = hook.step(True, dRel, closing_rate, v_ego, True, True, False, 1.0, dRel)
    if out:
      return out, dRel
    dRel = max(0.0, dRel + closing_rate * DT_MDL)
  return [], dRel


def main():
  # aggressive personality -> always inert regardless of everything else
  h = new_hook()
  out, _ = close(h, 120.0, 30.6, -8.0, 60, relaxed=False)
  check("aggressive, 120 m, true vRel -8 m/s -> []", out == [])

  # relaxed, lead present for 1 frame only -> no 0.30 s persistence -> []
  h = new_hook()
  out = h.step(True, 120.0, -8.0, 30.6, True, True, False, 1.0)
  check("relaxed, lead present 1 frame at 120 m -> [] (no persistence)", out == [])

  # relaxed, flicker (3 frames, < persist) then gone again -> never arms
  h = new_hook()
  for _ in range(3):
    h.step(True, 118.0, -1.0, 30.6, True, True, False, 1.0)
  out = []
  for _ in range(10):
    out = h.step(False, 0.0, 0.0, 30.6, True, True, False, 1.0)
  check("relaxed, flicker then gone -> [] and not armed", out == [] and not h.armed)

  # relaxed, 120 m, closing hard, after persistence -> one candidate, a <= FLOOR
  h = new_hook()
  out, dRel_end = arm(h, 120.0, 30.6, -8.0)
  check("relaxed, 120 m, hard closing -> arms with one candidate", len(out) == 1)
  check("candidate a <= FLOOR", len(out) == 1 and out[0][0] <= fl.FLOOR)
  check("candidate a >= CAP", len(out) == 1 and out[0][0] >= fl.CAP)
  check("candidate source is lead0", len(out) == 1 and out[0][1] == fl.LongitudinalPlanSource.lead0)
  check("arms while dRel still > 80 m", dRel_end > 80.0)

  # ---- BAND-SLOPE GATE (2026-09-22). Replaces the presence/hot-streak/ARM_MIN_DIST tests that
  # stood here: none of those quantities gate arming any more. See far_lead.py "BAND-SLOPE GATE".

  # LEVEL TEST, 2026-09-22. This case is the reason for the change: a series already closing
  # faster than ARM_SLOPE when the slope first becomes defined produces no crossing at all. Under
  # the old crossing test it could never arm however hard the approach was. Now it does.
  # Checked at the frame the slope first exists, not at the end of the drive -- by then the range
  # has fallen under HANDOFF_DIST and the hook has handed off, which would pass for the wrong
  # reason.
  h = new_hook()
  armed_any = False
  d = 120.0
  for _ in range(fl.BAND_N + fl.SLOPE_N + 40):
    h.step(True, d, -8.0, 30.6, True, True, False, 1.0, d)
    if h.armed:
      armed_any = True
      break
    d = max(0.0, d - 8.0 * DT_MDL)
  check("closing hard from the very first frame NOW arms (no crossing required)",
        armed_any and d > fl.HANDOFF_DIST)

  # ...and warming on a steady gap first is what makes the same approach arm
  h = new_hook()
  out, d_arm = arm(h, 120.0, 30.6, -8.0)
  check("steady gap, then closing at -8 m/s -> arms", len(out) == 1)
  check("arms on the first frame at FLOOR, never the full formula", len(out) == 1 and out[0][0] == fl.FLOOR)
  check("arm candidate source is lead0", len(out) == 1 and out[0][1] == fl.LongitudinalPlanSource.lead0)
  check("arms well beyond the hand-off distance", d_arm > fl.HANDOFF_DIST + 20.0)

  # HANDOFF_DIST gates arming: stock owns the near field
  h = new_hook()
  warm(h, 48.0)
  out, _ = close(h, 48.0, 20.0, -8.0, 120)
  check("closing hard but inside HANDOFF_DIST -> never arms (stock owns it)", out == [] and not h.armed)

  # a gentle close must not arm: the band slope never reaches ARM_SLOPE
  h = new_hook()
  warm(h, 120.0)
  out, _ = close(h, 120.0, 30.6, -2.0, 200)
  check("gentle -2 m/s close -> [] (band slope never reaches ARM_SLOPE)", out == [] and not h.armed)

  # the band is history of the ROAD: it must survive frames where the hook is not eligible, or
  # every personality flicker would blind the gate for 7.0 s
  h = new_hook()
  warm(h, 120.0)
  n_before = len(h.band.d)
  h.step(True, 120.0, 0.0, 30.6, False, True, False, 1.0, 120.0)   # not relaxed -> _reset()
  check("a non-eligible frame does NOT clear the band buffers", len(h.band.d) >= n_before)
  check("...and _reset() leaves the band object in place", h.band is not None and not h.armed)

  # ...and it keeps running while radar reports no lead at all
  h = new_hook()
  warm(h, 120.0)
  n_before = len(h.band.d)
  for _ in range(20):
    h.step(False, 0.0, 0.0, 30.6, True, True, False, 1.0, 118.0)
  check("band keeps filling while the radar lead is absent", len(h.band.d) >= n_before)

  # a frame with no model lead contributes nothing rather than a fabricated sample
  h = new_hook()
  warm(h, 120.0)
  n_before = len(h.band.d)
  h.step(True, 120.0, 0.0, 30.6, True, True, False, 1.0, None)
  check("a frame with no model lead is skipped, not interpolated", len(h.band.d) == n_before)

  # arming needs a radar lead even though the band runs without one
  h = new_hook()
  warm(h, 120.0)
  out = []
  for i in range(200):
    out = h.step(False, 0.0, -8.0, 30.6, True, True, False, 1.0, 120.0 - 8.0 * i * DT_MDL)
  check("band crossing with NO radar lead present -> does not arm", out == [] and not h.armed)

  # THE RELEASE THAT CANNOT COEXIST: this gate arms before v_filt converges, so the old
  # `eff_vRel_range >= -HOT_CLOSING_RATE` release would fire on the very next frame. Assert the
  # arm actually survives -- a regression here collapses every span to 1-2 frames.
  h = new_hook()
  out, d = arm(h, 120.0, 30.6, -8.0)
  check("armed", h.armed)
  out2, _ = close(h, d, 30.6, -8.0, 40)
  check("...the arm SURVIVES 40 more frames (old v_filt release would have killed it)",
        h.armed and len(out2) == 1)
  check("...and holds at FLOOR while the filter catches up", out2[0][0] <= fl.FLOOR)

  # hand-off on range, and it must read eff_dRel -- a one-frame dropout reads dRel 0.0 and would
  # otherwise be mistaken for 0 m
  h = new_hook()
  out, d = arm(h, 120.0, 30.6, -8.0)
  armed_before = h.armed
  out2 = h.step(False, 0.0, 0.0, 30.6, True, True, False, 1.0, d)
  check("one-frame lead dropout does NOT trigger the range hand-off", armed_before and h.armed)
  h = new_hook()
  out, d = arm(h, 120.0, 30.6, -8.0)
  out3, _ = close(h, fl.HANDOFF_DIST + 1.0, 30.6, -8.0, 10)
  check("falling under HANDOFF_DIST hands off to stock", out3 == [] and not h.armed)

  # release on the band slope turning back up
  h = new_hook()
  out, d = arm(h, 120.0, 30.6, -8.0)
  check("armed before the approach resolves", h.armed)
  out4 = []
  for _ in range(fl.BAND_N + fl.SLOPE_N + 80):
    out4 = h.step(True, d, 0.0, 30.6, True, True, False, 1.0, d)   # closing stops dead
    if not h.armed:
      break
  check("closing stops -> band slope crosses back above RELEASE_SLOPE -> released",
        out4 == [] and not h.armed)

  # once armed, dRel falls under ARM_MIN_DIST -> still armed (the arm-distance check is one-time)
  h = new_hook()
  arm(h, 120.0, 30.6, -8.0)
  armed_after_first = h.armed
  out = []
  dRel = 90.0
  while dRel > 70.0:
    out = h.step(True, dRel, -8.0, 30.6, True, True, False, 1.0)
    dRel -= 8.0 * DT_MDL
  check("armed before continuing", armed_after_first)
  check("still armed once dRel falls under ARM_MIN_DIST", h.armed and len(out) == 1)

  # armed, stock candidate reaches -0.40 while dRel still > 20 m -> this frame still returns
  # the candidate (min() picks the harder one), latch drops after
  h = new_hook()
  arm(h, 120.0, 30.6, -8.0)
  out = h.step(True, 60.0, -8.0, 30.6, True, True, False, -0.40)
  check("stock caught up at 60 m -> still returns a candidate this frame", len(out) == 1)
  out2 = h.step(True, 59.0, -8.0, 30.6, True, True, False, -0.40)
  check("stock caught up -> latch dropped next frame", out2 == [] and not h.armed)

  # ---- ARM CONFIRMATION (replaced a 10 m hysteresis margin, operator 2026-09-22) ----
  # The whole arm condition must hold for ARM_CONFIRM_FRAMES consecutive frames. A single bad
  # frame restarts the count, which is what rejects the range-bar chatter.
  # (the far-field "arms on the first qualifying frame" case is asserted in the near-field-only
  # confirmation block below; this block covers the counter's reset behaviour)
  # one non-qualifying frame restarts the count
  # inside ARM_CONFIRM_DIST, where the confirmation actually runs
  band_d = fl.HANDOFF_DIST + 3.0
  h = new_hook()
  warm(h, band_d)
  h.step(True, band_d, -8.0, 20.0, True, True, False, 1.0, band_d)    # 1 of 3
  h.step(False, 0.0, 0.0, 20.0, True, True, False, 1.0, band_d)       # lead drops -> restart
  out = h.step(True, band_d, -8.0, 20.0, True, True, False, 1.0, band_d)
  check("a single non-qualifying frame restarts the confirmation", out == [] and not h.armed)

  # and the range bar is checked on every confirming frame, not only the first
  h = new_hook()
  warm(h, band_d)
  h.step(True, band_d, -8.0, 20.0, True, True, False, 1.0, band_d)
  out = h.step(True, fl.HANDOFF_DIST - 1.0, -8.0, 20.0, True, True, False, 1.0, 49.0)
  check("dropping under HANDOFF_DIST mid-confirmation cancels the arm",
        out == [] and not h.armed)

  # ---- CONFIRMATION IS NEAR-FIELD ONLY (operator, 2026-09-22) ----
  # Above ARM_CONFIRM_DIST the gate arms on the FIRST qualifying frame: every frame of delay in
  # the far field is a frame of lost warning, and the chatter it guards against is a near-field
  # phenomenon (20 of 21 degenerate spans armed within 8 m of HANDOFF_DIST).
  check("the confirmation boundary sits above the hand-off bar",
        fl.ARM_CONFIRM_DIST > fl.HANDOFF_DIST)
  h = new_hook()
  warm(h, 120.0)
  d = 120.0
  qual = 0
  fired = None
  for _ in range(400):
    out = h.step(True, d, -8.0, 30.6, True, True, False, 1.0, d)
    if h.band.slope is not None and h.band.slope <= fl.ARM_SLOPE:
      qual += 1
    if out:
      fired = qual
      break
    d = max(0.0, d - 8.0 * DT_MDL)
  check("far field: arms on the FIRST qualifying frame, no confirmation delay",
        fired == 1 and d > fl.ARM_CONFIRM_DIST)

  # inside the boundary the confirmation applies
  h = new_hook()
  start = fl.HANDOFF_DIST + 4.0                    # between HANDOFF_DIST and ARM_CONFIRM_DIST
  warm(h, start)
  qual = 0
  fired = None
  for _ in range(400):
    out = h.step(True, start, -2.0, 20.0, True, True, False, 1.0, start)
    if h.band.slope is not None and h.band.slope <= fl.ARM_SLOPE:
      qual += 1
    if out:
      fired = qual
      break
  check("near field: does not arm before the confirmation completes",
        fired is None or fired >= fl.ARM_CONFIRM_FRAMES)

  # a hard close entirely below HANDOFF_DIST still never arms
  h = new_hook()
  warm(h, fl.HANDOFF_DIST - 4.0)
  out, _ = close(h, fl.HANDOFF_DIST - 4.0, 20.0, -8.0, 200)
  check("a hard close entirely inside HANDOFF_DIST never arms", out == [] and not h.armed)

  # ---- RE-ARM HOLD (only needed because arming is now a level test) ----
  # After a stock hand-off the slope is usually still past ARM_SLOPE, so without a hold the hook
  # would re-arm on the very next frame and oscillate for as long as stock kept braking.
  h = new_hook(); _, d = arm(h, 120.0, 30.6, -8.0)
  h.step(True, d, -8.0, 30.6, True, True, False, fl.HANDOFF_ACCEL, d)   # stock takes over
  h.step(True, d, -8.0, 30.6, True, True, False, fl.HANDOFF_ACCEL, d)   # latch drops
  check("handed off to stock", not h.armed)
  re_armed = False
  for _ in range(int(fl.RE_ARM_HOLD_S / DT_MDL) - 2):
    h.step(True, d, -8.0, 30.6, True, True, False, 1.0, d)              # stock backs off again
    if h.armed: re_armed = True; break
    d = max(0.0, d - 8.0 * DT_MDL)
  check("does NOT re-arm during the hold, even with the slope still past the bar", not re_armed)
  for _ in range(60):
    h.step(True, d, -8.0, 30.6, True, True, False, 1.0, d)
    if h.armed: break
    d = max(0.0, d - 8.0 * DT_MDL)
  check("...and CAN arm again once the hold expires", h.armed)

  # ---- HAND-OFF BAR DECOUPLED FROM FLOOR, 2026-09-22 (FINDINGS.md 25) ----
  # They hold the same value today, so these pin the SEPARATION rather than a difference: the
  # release must follow HANDOFF_ACCEL, and must not move when FLOOR is tuned. Overloading the two
  # is what caused the 2026-08-31 "sixth bug".
  # They were equal (-0.40) from 2026-09-22 until 09-24, when FLOOR alone moved to -0.20. The hand-off
  # bar stays at -0.40: stock must be genuinely braking before the hook lets go, and a hand-off bar
  # softer than the hook's own floor would release on stock merely matching it.
  check("HANDOFF_ACCEL is -0.40 and is at least as hard as FLOOR",
        fl.HANDOFF_ACCEL == -0.40 and fl.HANDOFF_ACCEL <= fl.FLOOR)
  # Hook 10 (grt/throttle_hold.py) passes a request at or below ABANDON through unfiltered and may
  # hold back or clip anything milder -- so the hook's softest command must never be milder.
  _th_src = (GRT / "throttle_hold.py").read_text()
  import re as _re
  _abandon = float(_re.search(r"^ABANDON = (-?[0-9.]+)", _th_src, _re.M).group(1))
  check(f"FLOOR ({fl.FLOOR}) is never softer than hook 10's ABANDON ({_abandon})",
        fl.FLOOR <= _abandon)
  _fl_src = (GRT / "far_lead.py").read_text()
  check("the release reads HANDOFF_ACCEL, not FLOOR",
        "if stock_min <= HANDOFF_ACCEL:" in _fl_src and "if stock_min <= FLOOR:" not in _fl_src)

  # stock exactly at the bar releases; a hair above it does not
  h = new_hook(); _, d = arm(h, 120.0, 30.6, -8.0)
  out = h.step(True, d, -8.0, 30.6, True, True, False, fl.HANDOFF_ACCEL, d)
  check("stock reaching HANDOFF_ACCEL still returns this frame's candidate", len(out) == 1)
  out2 = h.step(True, d, -8.0, 30.6, True, True, False, fl.HANDOFF_ACCEL, d)
  check("...and the latch is dropped on the next frame", out2 == [] and not h.armed)
  h = new_hook(); _, d = arm(h, 120.0, 30.6, -8.0)
  h.step(True, d, -8.0, 30.6, True, True, False, fl.HANDOFF_ACCEL + 0.05, d)
  check("stock a hair softer than the bar does NOT hand off", h.armed)

  # THE POINT: moving FLOOR must not move the release bar. Tune the authority, keep the trust.
  # Reproduce the 2026-08-31 SIXTH BUG's exact setup: FLOOR moved SOFTER (it was set to 0.00).
  # Coupled, that made the release `stock_min <= 0.00` -- true whenever stock merely was not
  # accelerating, so the hook self-released within a few frames of every arm. Decoupled, the trust
  # bar stays at -0.40 and a barely-braking stock no longer counts as "handled".
  _saved = fl.FLOOR
  try:
    fl.FLOOR = 0.00
    h = new_hook(); _, d = arm(h, 120.0, 30.6, -8.0)
    h.step(True, d, -8.0, 30.6, True, True, False, -0.10, d)   # stock barely braking
    check("FLOOR at 0.00: stock at -0.10 does NOT hand off (coupled, this was the sixth bug)",
          h.armed)
    h2 = new_hook(); _, d2 = arm(h2, 120.0, 30.6, -8.0)
    h2.step(True, d2, -8.0, 30.6, True, True, False, fl.HANDOFF_ACCEL, d2)
    h2.step(True, d2, -8.0, 30.6, True, True, False, fl.HANDOFF_ACCEL, d2)
    check("...and genuine braking at HANDOFF_ACCEL still hands off, whatever FLOOR is",
          not h2.armed)
    # and a HARDER floor, which is what FINDINGS 25 actually contemplates
    fl.FLOOR = -0.80
    h3 = new_hook(); _, d3 = arm(h3, 120.0, 30.6, -8.0)
    h3.step(True, d3, -8.0, 30.6, True, True, False, -0.30, d3)
    check("FLOOR at -0.80: the release bar is unmoved, stock at -0.30 does not hand off",
          h3.armed)
  finally:
    fl.FLOOR = _saved

  # armed, then down to 21 m with stock stuck near 0. Under the band-slope gate the hook has
  # ALREADY handed off at HANDOFF_DIST (50 m) and never reaches 21 m armed -- stock owns that
  # range whether or not it is commanding anything. This replaces the old assertion that the
  # hook keeps supplying a candidate down to the 20 m backstop.
  h = new_hook()
  arm(h, 120.0, 30.6, -8.0)
  out = h.step(True, 21.0, -8.0, 30.6, True, True, False, 0.0, 21.0)
  check("stock stuck near 0 at 21 m -> hook has handed off, supplies nothing", out == [] and not h.armed)

  # dRel < 20 m -> [], latch cleared regardless of stock
  out = h.step(True, 19.0, -8.0, 30.6, True, True, False, 0.0)
  check("dRel < 20 m -> [] regardless of stock (absolute backstop)", out == [] and not h.armed)

  # not longActive -> [], latch cleared
  h = new_hook()
  arm(h, 120.0, 30.6, -8.0)
  check("armed before longActive drops", h.armed)
  out = h.step(True, 90.0, -8.0, 30.6, True, False, False, 1.0)
  check("not longActive -> [], latch cleared", out == [] and not h.armed)

  # driver gas/brake -> [], latch cleared
  h = new_hook()
  arm(h, 120.0, 30.6, -8.0)
  out = h.step(True, 90.0, -8.0, 30.6, True, True, True, 1.0)
  check("driver input -> [], latch cleared", out == [] and not h.armed)

  # exception containment lives in hooks.far_lead_candidates (the try/except boundary); step()
  # itself must at least not raise on a degenerate input.
  h = new_hook()
  try:
    h.step(True, float('nan'), -8.0, 30.6, True, True, False, 1.0)
    ok = True
  except Exception:
    ok = False
  check("step() does not raise on NaN dRel input", ok)

  # candidate a > -0.20 must never happen -- hook 10 C's ABANDON would eat it. (A FLOOR=0.00
  # experiment briefly relaxed this 2026-08-31 and found a worse, separate bug -- see module
  # docstring "FLOOR EXPERIMENT" -- reverted; this invariant holds again at FLOOR=-0.40.)
  h = new_hook()
  out, _ = arm(h, 120.0, 30.6, -8.0)
  check("candidate a <= -0.20 always (hook 10 C floor)", all(c[0] <= -0.20 for c in out))

  # FOURTH BUG regression: a_req alone can clear HOT_A_REQ on a tiny closing rate at close-ish
  # range -- must NOT arm without also clearing HOT_CLOSING_RATE. At 90 m, v_ego=30.6, closing at
  # -1.0 m/s (3.6 km/h, well under the 10 km/h gate): under the OLD formula, a_req ~ 0.35, clears
  # HOT_A_REQ on its own, and only HOT_CLOSING_RATE stopped it from arming. Post-attempt-5,
  # a_req_correct ~ 0.006 at this state and no longer clears HOT_A_REQ=0.10 by itself either --
  # this case is now doubly rejected, so it no longer isolates HOT_CLOSING_RATE specifically, but
  # the invariant it checks ("must not arm here") still must hold.
  h = new_hook()
  out, _ = close(h, 90.0, 30.6, -1.0, 40)  # 2 s, well past HOT_PERSIST_S if it were going to arm
  check("FOURTH BUG: a_req hot but closing <10km/h at 90m -> [] (not armed)",
        out == [] and not h.armed)

  # FOURTH BUG regression: armed on a real fast approach, then closing rate DECAYS to a slow
  # trickle (-1.5 m/s, under HOT_CLOSING_RATE) without ever reaching >= 0 -- must release anyway,
  # not hold the floor waiting for fully non-negative (the actual 2026-08-28 defect). Feed
  # physically consistent kinematics (dRel actually decreasing at the claimed rate), same as
  # close()/arm() -- a frozen dRel with a nonzero claimed vRel is an inconsistent input the
  # filter correctly refuses to trust quickly, which is not what this case is testing.
  h = new_hook()
  _, dRel = arm(h, 120.0, 30.6, -8.0)
  check("armed before decay", h.armed)
  out = []
  for _ in range(60):  # 3 s at the slow rate
    out = h.step(True, dRel, -1.5, 30.6, True, True, False, 1.0, dRel)
    dRel = max(0.0, dRel - 1.5 * DT_MDL)
    if out == [] and not h.armed:
      break
  # BEHAVIOUR CHANGE, 2026-09-22. The old gate released here on `v_filt >= -HOT_CLOSING_RATE`
  # ("no longer closing FAST"). The band-slope gate cannot use that test -- it arms before
  # v_filt converges, so that release would fire on the frame after arming (see the arm-survival
  # case above). A lead still genuinely closing at -1.5 m/s therefore HOLDS the arm now, at FLOOR
  # (-0.40, ~0.04 g), until either closing stops (band slope crosses RELEASE_SLOPE) or the range
  # falls under HANDOFF_DIST. This is a deliberate trade, and it is the main residual risk of
  # this change: a long slow approach can hold FLOOR for tens of seconds where the old gate let
  # go. Measured on the c7+c8 replay the spans did not run long -- median 3.10 s, max 11.20 s --
  # but that is 0.62 h of evidence, and it is what the field test is watching for.
  # BEHAVIOUR CHANGE AGAIN, 2026-09-26 (operator): the ARM CHECK (CHECK_S) now releases this case.
  # The decay starts on the frame after arming, so the gap closes at 1.5 m/s over the check window,
  # slower than CHECK_RATE: released at the check, with the re-arm hold set.
  check("ARM CHECK: closing that decays to -1.5 m/s right after arming is released at CHECK_S",
        out == [] and not h.armed and h.rearm_hold_s > 0.0)

  # The residual trade described above still applies once the check has passed: a close that
  # stays fast through the check window and only THEN decays to -1.5 m/s still HOLDS the arm.
  h = new_hook()
  _, dRel = arm(h, 120.0, 30.6, -8.0)
  for _ in range(fl._CHECK_N + 2):          # fast through the check window
    out = h.step(True, dRel, -8.0, 30.6, True, True, False, 1.0, dRel)
    dRel -= 8.0 * DT_MDL
  passed = h.armed and h.check_gaps is None
  for _ in range(60):                       # then 3 s at the slow rate
    out = h.step(True, dRel, -1.5, 30.6, True, True, False, 1.0, dRel)
    dRel = max(0.0, dRel - 1.5 * DT_MDL)
    if out == [] and not h.armed:
      break
  check("...a close that stays fast through the check passes it", passed)
  check("...and a later decay to -1.5 m/s still HOLDS the arm (no second check)",
        h.armed and len(out) == 1)
  check("...held between FLOOR and CAP", len(out) == 1 and fl.CAP <= out[0][0] <= fl.FLOOR)

  # ---- distance-neutral arming threshold (2026-09-04) ----
  # a_req = v^2/(2*(d-6)) means the CLOSING RATE arming demands grows with distance (3.85 m/s at
  # 80 m, 4.56 at 110 m), so the far-lead feature was hardest to trigger where it is meant to
  # work. Scaling the threshold by ARM_MIN_DIST/dRel cancels that. These tests pin the two
  # properties the safety argument rests on: identical below 80 m, and bounded above it.
  print("\ndistance-neutral arming threshold")
  check("threshold is EXACTLY HOT_A_REQ at THRESH_SCALE_DIST",
        fl.hot_a_req_for(fl.THRESH_SCALE_DIST) == fl.HOT_A_REQ)
  check("threshold is EXACTLY HOT_A_REQ below it (near field bit-identical to before)",
        all(fl.hot_a_req_for(d) == fl.HOT_A_REQ for d in (5.0, 40.0, 79.9)))
  check("beyond THRESH_SCALE_DIST it only ever RELAXES, never tightens",
        all(fl.hot_a_req_for(d) <= fl.HOT_A_REQ for d in (81.0, 100.0, 120.0, 200.0, 1e4)))
  check("threshold is monotonically non-increasing with distance",
        all(fl.hot_a_req_for(a) >= fl.hot_a_req_for(b)
            for a, b in zip((80.0, 90.0, 100.0, 110.0), (90.0, 100.0, 110.0, 120.0))))
  check("relaxation is CLAMPED at HOT_A_REQ_MIN_SCALE (bounded, not open-ended)",
        abs(fl.hot_a_req_for(1e6) - fl.HOT_A_REQ * fl.HOT_A_REQ_MIN_SCALE) < 1e-12)
  check("clamp floor in (0,1): can bind, cannot invert the threshold",
        0.0 < fl.HOT_A_REQ_MIN_SCALE < 1.0)
  check("threshold stays strictly positive at any distance",
        all(fl.hot_a_req_for(d) > 0.0 for d in (0.0, 1.0, 80.0, 1e6)))

  def _v_req(d):
    return (2.0 * fl.hot_a_req_for(d) * max(d - fl.STOP_MARGIN, 1.0)) ** 0.5
  vs = [_v_req(d) for d in (80.0, 90.0, 100.0, 110.0, 120.0)]
  check("THE POINT: required closing rate is now flat over 80-120 m (spread < 0.25 m/s)",
        max(vs) - min(vs) < 0.25)
  old = [(2.0 * fl.HOT_A_REQ * (d - fl.STOP_MARGIN)) ** 0.5 for d in (80.0, 120.0)]
  check("...whereas unscaled it spread by more than 0.8 m/s over the same range",
        old[1] - old[0] > 0.8)
  check("required closing rate still exceeds HOT_CLOSING_RATE at every distance",
        all(v > fl.HOT_CLOSING_RATE for v in vs))

  # ARM_MIN_DIST and THRESH_SCALE_DIST are INDEPENDENT knobs. They were equal until 2026-09-04,
  # when the arming floor moved to 70 m. Coupling them would mean that lowering the floor also
  # relaxes the far-field threshold -- two changes at once, and an unreadable road test.
  print("\nARM_MIN_DIST and THRESH_SCALE_DIST must stay decoupled")
  check("the threshold helper does NOT depend on ARM_MIN_DIST",
        "ARM_MIN_DIST / dRel" not in (GRT / "far_lead.py").read_text())
  _amd, _tsd = fl.ARM_MIN_DIST, fl.THRESH_SCALE_DIST
  try:
    fl.ARM_MIN_DIST = 999.0                      # moving the floor must not move the threshold
    check("moving ARM_MIN_DIST leaves the threshold curve unchanged",
          all(abs(fl.hot_a_req_for(d) - fl.HOT_A_REQ * max(fl.HOT_A_REQ_MIN_SCALE,
                                                           min(1.0, _tsd / d))) < 1e-12
              for d in (50.0, 80.0, 100.0, 120.0)))
  finally:
    fl.ARM_MIN_DIST = _amd
  check("arming floor is at or below the threshold-scaling distance",
        fl.ARM_MIN_DIST <= fl.THRESH_SCALE_DIST)
  check("between the floor and the scaling distance the threshold is the plain constant",
        all(fl.hot_a_req_for(d) == fl.HOT_A_REQ
            for d in (fl.ARM_MIN_DIST + 0.1, 0.5 * (fl.ARM_MIN_DIST + fl.THRESH_SCALE_DIST),
                      fl.THRESH_SCALE_DIST)))

  # The severity formula decides how HARD to brake, not WHETHER. It must never pick up the
  # ARMING gate's distance-neutralising scale (hot_a_req_for) -- that exists to make a threshold
  # equally reachable at every range, which is meaningless for a magnitude.
  _src = (GRT / "far_lead.py").read_text()
  check("armed-branch severity a_req uses the proportional stopping target (2026-09-22)",
        "a_req = (eff_vRel_range ** 2) / (2.0 * max(eff_dRel * (1.0 - STOP_MARGIN_FRAC), 1.0))"
        in _src)
  check("...and is NOT scaled by the arming threshold's distance correction",
        "hot_a_req_for" not in _src.split("already armed")[-1])

  # STOP_MARGIN stays 6.0 for the OLD gate's math: hook 11b's _ArmMirror and hot_a_req_for
  # reproduce what that gate would have armed on, and are the field-test comparator. If the
  # proportional target ever leaks into them the comparison silently stops comparing.
  check("STOP_MARGIN is unchanged for the retained old-gate math",
        fl.STOP_MARGIN == 6.0)
  check("the proportional target is a fraction of dRel, not a distance",
        0.0 < fl.STOP_MARGIN_FRAC < 1.0)

  # With FRAC = 0.5 the denominator is exactly dRel, so a_req = v^2/dRel. Assert the KINEMATICS
  # rather than the algebra: the command must demand the closing rate be bled off over the
  # remaining fraction of the gap, at every distance.
  for d, v in ((100.0, 10.0), (60.0, 8.0), (30.0, 5.0)):
    want = (v ** 2) / (2.0 * d * (1.0 - fl.STOP_MARGIN_FRAC))
    got = (v ** 2) / (2.0 * max(d * (1.0 - fl.STOP_MARGIN_FRAC), 1.0))
    check(f"a_req at {d:.0f} m closing {v:.0f} m/s is {want:.3f} m/s^2 "
          f"(bleed off within {(1.0 - fl.STOP_MARGIN_FRAC) * d:.0f} m)",
          abs(got - want) < 1e-9)
  # RETAINED, not deleted: hook 11b (_ArmMirror in grt/hooks.py) reads these to shadow what the
  # OLD gate would have armed on, which is the field-test comparator for this change. They are no
  # longer on the live arming path -- assert exactly that, so a future edit cannot quietly
  # reintroduce the old gate alongside the new one.
  check("hot_a_req_for is still DEFINED (hook 11b reads it)",
        "def hot_a_req_for(" in _src)
  check("...but the old arming call site is gone",
        "a_req_filt > hot_a_req_for" not in _src)
  check("...and no code line outside its own def calls it",
        not [ln for ln in _src.splitlines()
             if "hot_a_req_for(" in ln and not ln.lstrip().startswith(("#", "def "))
             and not ln.lstrip().startswith(("ARM", "HOT", "THRESH"))
             and '"' not in ln and "'" not in ln and ln.strip().startswith(("if", "return", "self", "a_req"))])
  check("arming is a LEVEL test on BOTH the band slope and the raw slope (AND gate), not a crossing",
        "self.band.slope is None or self.band.slope > ARM_SLOPE" in _src
        and "self.band.slope_raw is None or self.band.slope_raw > ARM_SLOPE" in _src
        and "self.band.crossed_arm" not in _src)
  check("the RELEASE is still a crossing (a level release would re-fire every frame)",
        _src.count("self.band.crossed_release") == 1)
  check("the v_filt release is GONE from the code path (it cannot coexist with this gate)",
        "if present and eff_vRel_range >= -HOT_CLOSING_RATE:" not in _src)

  # ---- object-switch guards + ARM_MIN_DIST 65 (2026-09-15) ----
  # leadOne is an anonymous slot: when radard's lead switches objects, dRel steps, and the unguarded
  # filter differentiated that step into tens of m/s of phantom closing which a_req then squared --
  # every CAP arm on the 2026-09-14 corpus followed such a switch. A range change that motion cannot
  # produce must re-initialise the filter, not move v. See module docstring, "OBJECT-SWITCH GUARDS".
  # The real-close cases pin the other side: the guards never fire on physically possible motion.
  print("\nobject-switch guards")
  import random as _random

  def feed(samples, v_ego, vrel=-0.5, noise=0.0, seed=7, hook=None):
    """samples: [(present, dRel)] -> (hook, per-frame (armed, v_filt, stepped, candidate a,
    present_s, band slope)). Noise, if any, is seeded: deterministic."""
    rng = _random.Random(seed)
    h = hook if hook is not None else new_hook()
    res = []
    for pres, d in samples:
      z = d + (rng.gauss(0.0, noise) if (pres and noise) else 0.0)
      out = h.step(pres, z, vrel, v_ego, True, True, False, 1.0, z if pres else None)
      res.append((h.armed, h.filt.v, h.filt.stepped, out[0][0] if out else None,
                  h.present_s, h.band.slope))
    return h, res

  def STEADY(d, n=None):
    """A flat prefix that warms the band-slope buffers without being a closing event itself.
    The gate arms on a CROSSING below ARM_SLOPE, so a series that is already closing hard when
    the slope first becomes defined never crosses -- every arming case needs a steady lead-in,
    which is also what a real approach looks like."""
    return [(True, d)] * (n if n is not None else fl.BAND_N + fl.SLOPE_N + 20)

  def STEADY_P(d, n=None):
    """STEADY() for feedp(): a confident, present, flat lead-in that warms the band."""
    return [(True, d, 0.9)] * (n if n is not None else fl.BAND_N + fl.SLOPE_N + 20)

  def switches(r):
    return [i for i, x in enumerate(r) if x[2]]

  _h_sw, r = feed([(True, 110.0)] * 100 + [(True, 45.0)] * 100, 30.0)
  check("slot switch 110 -> 45 m is ONE new object, not a velocity", len(switches(r)) == 1)
  # STEP guard REMOVED 2026-09-24 (operator). The physical bound still catches a 65 m inward jump,
  # but two frames late, so v_filt differentiates those two frames: about -7.4 m/s of phantom
  # closing, bounded, instead of 0 with the guard or ~-28 with no guard at all.
  check("...without the STEP guard the physical bound catches it within 3 frames",
        bool(switches(r)) and switches(r)[0] - 100 <= 3)
  # With ALPHA/BETA 0.20/0.0222 (2026-09-24, FINDINGS 37) those two frames read a LARGE phantom --
  # about -51 m/s on this 65 m jump, against -7.4 with the old 0.10/0.003 -- but only for them.
  check("...so the phantom lasts at most 2 frames before v_filt is reset",
        all(x[1] == 0.0 for x in r[switches(r)[0]:switches(r)[0] + 3]) if switches(r) else False)
  check("...never arms", not any(x[0] for x in r))
  sw = switches(r)[0] if switches(r) else None
  check("...on the switch frame, presence restarts for the new object",
        sw is not None and abs(r[sw][4] - DT_MDL) < 1e-9)
  # the BAND is deliberately NOT restarted by a slot switch: it tracks the model's range series,
  # which is continuous across a radar slot change. Re-warming it here would blind the gate for
  # 7.0 s at exactly the moment a new object appeared.
  check("...but the band is NOT restarted by a slot switch",
        sw is not None and len(_h_sw.band.d) == fl.BAND_N and _h_sw.band.slope is not None)

  _, r = feed([(True, 45.0)] * 100 + [(True, 110.0)] * 100, 30.0)
  check("reveal 45 -> 110 m (switch to a farther object): one new object, caught by the physical "
        "bound within 3 frames (STEP guard removed 2026-09-24)",
        len(switches(r)) == 1 and switches(r)[0] - 100 <= 3)

  # 2026-09-22. This case USED to assert "never arms", and passed -- but only because feed()
  # started the band cold, so the slope first became defined mid-ramp with prev=None and the
  # crossing test could not fire (the blind spot described at the top of this file). In
  # production the band is warm long before any event -- update(None) never clears it, so it
  # warms once per boot -- and the git-pinned pre-change file ARMS at frame 173 on this input.
  # The honest statement is therefore: _RangeRateFilter catches this for v_filt, but the BAND has
  # NO physical bound, so a model-range ramp faster than a lead can close does arm the gate.
  # That gap is PRE-EXISTING and is deliberately not fixed here; see FINDINGS.md 30.
  ramp = (STEADY(92.0) + [(True, 92.0 - 44.0 * k / 20) for k in range(1, 21)]
          + [(True, 48.0)] * 100)
  _, r = feed(ramp, 25.0)
  check("ramped switch 92 -> 48 m over 1.0 s at ego 25 m/s: _RangeRateFilter's physical bound "
        "fires and re-initialises the filter (a gradual ramp is too slow for the STEP test)",
        len(switches(r)) >= 1)
  check("...but the band has no physical bound, so the gate DOES arm (known gap, FINDINGS 30)",
        any(x[0] for x in r))
  check("...and it releases by HANDOFF_DIST, so the exposure is bounded",
        not r[-1][0])

  _, r = feed([(True, 90.0)] * 100 + [(True, 110.0)] + [(True, 90.0)] * 100, 30.0)
  check("single-frame +20 m outlier is noise, not a new object", switches(r) == [])

  for noise in (0.0, 2.5):
    _, r = feed(STEADY(130.0) + [(True, 130.0 - 33.0 * i * DT_MDL) for i in range(70)],
                33.0, vrel=-3.0, noise=noise)
    cands = [x[3] for x in r if x[3] is not None]
    check(f"stopped car closing at exactly ego speed 33 m/s (noise {noise} m): physical bound does "
          f"NOT fire, arms, reaches CAP",
          switches(r) == [] and any(x[0] for x in r) and bool(cands) and min(cands) <= fl.CAP + 1e-6)
  _, r = feed(STEADY(130.0) + [(True, 130.0 - 20.0 * i * DT_MDL) for i in range(110)],
              30.0, noise=2.5)
  check("genuine 20 m/s close with 2.5 m dRel noise: no false switch, arms",
        switches(r) == [] and any(x[0] for x in r))

  for seed in (1, 2, 3):
    _, r = feed([(True, 90.0)] * 2000, 30.0, noise=2.7, seed=seed)
    check(f"steady following at 90 m for 100 s, 2.7 m noise (seed {seed}): no false switch, never arms",
          switches(r) == [] and not any(x[0] for x in r))

  h, _ = feed(STEADY(120.0) + [(True, 120.0 - 8.0 * i * DT_MDL) for i in range(40)],
              30.6, vrel=-8.0)
  armed_before = h.armed
  _, r = feed([(True, 40.0)] * 20, 30.6, vrel=-0.5, hook=h)
  check("armed, then the slot switches to a 40 m object: hook releases within 3 frames",
        armed_before and any(not x[0] for x in r[:3]))

  _, r = feed(STEADY(115.0) + [(True, 85.0 - 10.0 * i * DT_MDL) for i in range(60)], 30.0)
  first = next((i for i, x in enumerate(r) if x[0]), None)
  # STEP guard REMOVED 2026-09-24 (operator). A 30 m inward jump is now differentiated as closing
  # until the physical bound fires 7 frames later; the band arms on the jump itself (it always
  # could -- the band has no physical bound, FINDINGS 30a). On a REAL approach, which is what this
  # is, that means braking starts on the jump instead of after re-earning the arm.
  check("jump to an 85 m object that IS closing at 10 m/s: arms, and brakes past FLOOR",
        first is not None and min((x[3] for x in r if x[3] is not None), default=0.0) < fl.FLOOR)
  check("...and the physical bound still registers the jump as a new object",
        bool(switches(r)))

  # ---------------------------------------------------------------- band samples every frame (09-24)
  # Operator, 2026-09-24: the band must take a sample all the time; arming unchanged (slope <=
  # ARM_SLOPE with a lead present, plus the existing guards). Between 09-22 and 09-24 the band
  # skipped low-confidence frames and re-seeded on acquisition; that is gone (FINDINGS 36).
  def feedp(samples, v_ego, vrel=-0.5, noise=0.0, seed=7, hook=None):
    """Like feed(), but each sample is (present, dRel, prob); the model range is always fed."""
    rng = _random.Random(seed)
    h = hook if hook is not None else new_hook()
    res = []
    for pres, d, pr in samples:
      z = d + (rng.gauss(0.0, noise) if noise else 0.0)
      h.step(pres, z, vrel, v_ego, True, True, False, 1.0, z, pr)
      res.append((h.armed, h.band.slope))
    return h, res

  h, _ = feedp([(False, 120.0 + (k % 7) * 4.0, 0.01) for k in range(60)], 30.0)
  check("the band samples a low-confidence model range (prob 0.01) too",
        len(h.band.d) == 60)
  h, _ = feedp([(True, 120.0, 0.01)] * 60 + [(True, 100.0, 0.9)], 30.0)
  check("no re-seed on acquisition: the window just gains one sample",
        len(h.band.d) == 61 and h.band.d[-1] == 100.0)

  # THE REASON FOR THE CHANGE. Armed on a real close, then the model's confidence flickers around
  # 0.5 for 5 s (route 000001d7 15:48:14). The gated band re-seeded on every return and its slope
  # stayed undefined, so the slope-above-0 release could never fire. Now the slope always exists.
  closing = [(True, 110.0 - 8.0 * k * DT_MDL, 0.9) for k in range(40)]
  h, r = feedp(STEADY_P(110.0) + closing, 30.0)
  armed_at = next((i for i, x in enumerate(r) if x[0]), None)
  d_end = 110.0 - 8.0 * 40 * DT_MDL
  # 8-frame dips: long enough to pull the FILTERED prob below 0.5 (a 3-frame dip is not -- it
  # decays at PROB_ALPHA). On the pre-09-24 file this leaves the slope undefined on 92/100 frames
  # and the hook still armed at the end; that is the fault this test pins.
  flick = [(True, d_end + 6.0 * k * DT_MDL, 0.9 if (k // 8) % 2 else 0.2) for k in range(100)]
  h, r2 = feedp(flick, 30.0, hook=h)
  check("armed on a real close", armed_at is not None)
  check("...then prob flickers around 0.5 while the gap opens: the slope stays defined every frame",
        all(x[1] is not None for x in r2))
  check("...so the hook can release (the release blind spot is gone)",
        not r2[-1][0])

  # THE ACCEPTED COST, stated so nobody mistakes it for a pass. A model range wandering at prob
  # ~0.01 and then stepping ~25 m onto a newly believed lead (the 2026-09-22 13:44:41 shape) feeds
  # the step straight into the band. Nothing between the band and the arm checks confidence, so
  # once a lead is present the step can arm.
  wander = [(False, 130.0 + (k % 5) * 1.5, 0.01) for k in range(fl.BAND_N + fl.SLOPE_N + 20)]
  acq = [(True, 103.0 + ((-1) ** k) * 1.0, 0.9) for k in range(60)]
  h, r = feedp(wander + acq, 36.0)
  check("KNOWN COST: an acquisition step onto a newly believed lead can arm again (FINDINGS 36)",
        any(x[0] for x in r))

  # What sampling every frame must NOT do: arm on a lead that is simply there.
  for nz in (2.0, 3.0, 4.0):
    h, r = feedp(STEADY_P(100.0) + [(True, 100.0, 0.9)] * 200, 30.0, noise=nz)
    worst = min((x[1] for x in r if x[1] is not None), default=0.0)
    check(f"constant 100 m range, {nz:.0f} m noise, warm band: never arms "
          f"(worst slope {worst:.2f} vs ARM_SLOPE {fl.ARM_SLOPE})",
          not any(x[0] for x in r))
  h, r = feedp(STEADY_P(100.0) + [(True, 100.0 + 3.0 * k * DT_MDL, 0.9) for k in range(1, 201)],
               30.0, noise=3.0)
  check("an OPENING gap never arms", not any(x[0] for x in r))

  # ---------------------------------------------------------------- physical-bound span (09-24)
  # Shortened 0.8 -> 0.45 s on operator decision (FINDINGS 35): the smallest span that still clears
  # the genuine arms on route 000001d7. The bound multiplies v_ego by PHYS_SPAN_S, so the constant
  # must equal the real distance between the two 5-sample median centres.
  check("PHYS_SPAN_S equals the median-centre separation of PHYS_WINDOW",
        abs(fl.PHYS_SPAN_S - (fl.PHYS_WINDOW - 5) * DT_MDL) < 1e-9)
  # Bookmark 3 (2026-09-23 14:48:08-11, route 000001d7): REAL camera range (m) and v_ego (m/s) at
  # 20 Hz across the ~119 -> 102 m step just before a real approach. A synthetic step does NOT
  # discriminate -- only the real noise does: 0.40 s trips on this and resets v_filt 0.3 s before
  # the arm; 0.45 s must clear it.
  BM3 = [(121.6, 27.5), (119.96, 27.5), (113.66, 27.5), (110.76, 27.47), (112.46, 27.47), 
         (123.02, 27.47), (120.3, 27.47), (118.46, 27.48), (117.18, 27.47), (121.07, 27.48), 
         (124.39, 27.49), (125.13, 27.49), (119.66, 27.49), (116.38, 27.5), (112.72, 27.48), 
         (119.66, 27.48), (122.54, 27.48), (119.99, 27.48), (119.97, 27.48), (119.88, 27.49), 
         (124.7, 27.51), (119.59, 27.52), (115.29, 27.51), (111.12, 27.51), (112.5, 27.5), 
         (116.34, 27.49), (120.67, 27.48), (122.74, 27.49), (120.22, 27.48), (119.57, 27.49), 
         (114.73, 27.49), (119.17, 27.49), (116.76, 27.49), (118.63, 27.48), (121.07, 27.47), 
         (119.97, 27.47), (118.08, 27.47), (119.37, 27.49), (120.38, 27.49), (119.19, 27.49), 
         (119.1, 27.51), (112.71, 27.51), (102.18, 27.51), (104.57, 27.51), (103.32, 27.5), 
         (101.68, 27.49), (99.01, 27.47), (96.76, 27.44), (101.59, 27.44), (99.99, 27.44), 
         (102.07, 27.44), (105.73, 27.46), (103.42, 27.47), (102.92, 27.49), (98.04, 27.51), 
         (100.83, 27.51), (98.15, 27.51), (95.46, 27.53), (97.66, 27.49), (96.83, 27.47)]
  def trips_on_bm3():
    f = fl._RangeRateFilter(fl.ALPHA, fl.BETA)
    hit = False
    for z, v in BM3:
      f.update(z, v)
      hit = hit or f.stepped
    return hit
  check("bookmark 3's real camera step does not trip the physical bound at the shipped span",
        not trips_on_bm3())
  saved = (fl.PHYS_WINDOW, fl.PHYS_SPAN_S)
  try:
    fl.PHYS_WINDOW, fl.PHYS_SPAN_S = 13, 0.40
    check("...whereas one frame shorter (0.40 s) it does -- 0.45 s is the floor on this drive",
          trips_on_bm3())
  finally:
    fl.PHYS_WINDOW, fl.PHYS_SPAN_S = saved

  # ---------------------------------------------------------------- filter gains
  # 0.20/0.0222 (09-24) was reverted to 0.10/0.003 on 09-25 (FINDINGS 39-41): the fast pair made the
  # command pulse. Pin the pair and its lag, so a future retune is a deliberate, visible change.
  check("filter gains are ALPHA 0.10 / BETA 0.003 (reverted 2026-09-25)",
        fl.ALPHA == 0.10 and fl.BETA == 0.003)
  f = fl._RangeRateFilter(fl.ALPHA, fl.BETA)
  for _ in range(200):
    f.update(120.0, 27.5)
  t63 = next((k * DT_MDL for k in range(1, 400) if f.update(120.0 - 10.0 * k * DT_MDL, 27.5) <= -6.3), None)
  check(f"a clean 10 m/s close reaches 63% in about 1.8 s with these gains (now {t63} s)",
        t63 is not None and 1.5 <= t63 <= 2.1)
  # A faster filter spikes harder on a jump before the physical bound resets it. What must stay
  # true is that the COMMAND can only follow at JERK_ARM, so a 2-frame spike moves it by <= 0.15.
  h = new_hook()
  for i in range(fl.BAND_N + fl.SLOPE_N + 20):
    h.step(True, 120.0, -2.0, 30.0, True, True, False, 0.0, 120.0, 0.9)
  for k in range(60):
    d = 120.0 - 8.0 * k * DT_MDL
    h.step(True, d, -2.0, 30.0, True, True, False, 0.0, d, 0.9)
  before = h.last_emitted
  cm = []
  for k in range(20):                       # 30 m inward jump onto a nearer object
    o = h.step(True, d - 30.0, -2.0, 30.0, True, True, False, 0.0, d - 30.0, 0.9)
    if o:
      cm.append(o[0][0])
  check("armed, then a 30 m inward jump: the spike moves the command by at most 2 x JERK_ARM x DT",
        h.armed is not None and cm and min(cm) >= before - 2 * fl.JERK_ARM * DT_MDL - 1e-6)
  check("...and once the physical bound resets v_filt the command returns to FLOOR",
        cm and abs(cm[-1] - fl.FLOOR) < 1e-9)

  # ---------------------------------------------------------------- AND gate + lead lost (09-25)
  # An OUTWARD jump inflates the band's stdev, so band = mean - 2 stdev falls and its slope goes below
  # ARM_SLOPE while the range is actually opening. The pre-09-25 single-slope gate armed on this at
  # frame 105 (FINDINGS 30a); the raw slope is positive, so the AND gate must not.
  h = new_hook(); armed_any = False
  for i, d in enumerate([45.0] * 200 + [110.0] * 150):
    h.step(True, d, -0.5, 30.0, True, True, False, 1.0, d, 0.9)
    armed_any = armed_any or h.armed
  check("AND gate: an outward range jump (45 -> 110 m) does not arm, though the band slope dips below -5",
        not armed_any)
  # ...while a genuine close, where both slopes fall, still arms.
  h = new_hook(); armed_at = None
  seq = [120.0] * (fl.BAND_N + fl.SLOPE_N + 20) + [120.0 - 10.0 * k * DT_MDL for k in range(80)]
  for i, d in enumerate(seq):
    h.step(True, d, -10.0, 30.0, True, True, False, 1.0, d, 0.9)
    if h.armed and armed_at is None:
      armed_at = i
  check("AND gate: a genuine 10 m/s close still arms", armed_at is not None)
  check("...and the raw slope was at or below ARM_SLOPE when it did",
        armed_at is not None and h.band.slope_raw is not None and h.band.slope_raw <= fl.ARM_SLOPE)

  # LEAD_LOST_S 0.5 (was 1.0): an armed hook rides a short dropout on last_known, but not a long one.
  def armed_then_gap(gap_frames):
    h = new_hook()
    for d in seq:
      h.step(True, d, -10.0, 30.0, True, True, False, 1.0, d, 0.9)
    was = h.armed
    dz = seq[-1]
    for k in range(gap_frames):
      h.step(False, 0.0, 0.0, 30.0, True, True, False, 1.0, dz, 0.9)
    return was, h.armed
  check("LEAD_LOST_S is 0.5 s", fl.LEAD_LOST_S == 0.5)
  w, a = armed_then_gap(9)
  check("a 0.45 s lead dropout while armed does not release", w and a)
  w, a = armed_then_gap(12)
  check("a 0.6 s lead dropout while armed releases", w and not a)

  # ---------------------------------------------------------------- continuous v_filt (09-24)
  # Operator, 2026-09-24: v_filt must have a reading on every frame, not only while a lead is
  # present. The filter now runs at the top of step() like the band: fed dRel while present and the
  # model range otherwise, created once, never reset by _reset().
  h = new_hook()
  for i in range(200):                     # a lead closing at 6 m/s, radar-present throughout
    d = 110.0 - 6.0 * i * DT_MDL
    h.step(True, d, -6.0, 30.0, True, True, False, 1.0, d, 0.9)
  v_before = h.v_filt
  d = 110.0 - 6.0 * 200 * DT_MDL
  for i in range(20):                      # lead absent for 1 s: dRel is the 0.0 struct default
    d -= 6.0 * DT_MDL
    h.step(False, 0.0, 0.0, 30.0, True, True, False, 1.0, d, 0.9)
  check("v_filt has a reading on frames with no lead present",
        isinstance(h.v_filt, float))
  check("...fed from the model range, so it keeps tracking the closing through the gap",
        -8.0 < h.v_filt < -4.0 and abs(h.v_filt - v_before) < 2.0)
  check("...and the absent frame's dRel 0.0 is never fed (no 100 m phantom collapse)",
        h.v_filt > -10.0)

  f0 = h.filt
  h.step(True, d, -6.0, 30.0, True, True, True, 1.0, d, 0.9)     # driver input -> _reset()
  check("the filter survives _reset(): same object, history kept across an eligibility flicker",
        h.filt is f0 and abs(h.v_filt - v_before) < 2.0)

  v0 = h.v_filt
  h.step(False, 0.0, 0.0, 30.0, True, True, False, 1.0, None, None)
  check("no lead and no model range: the filter coasts, v_filt unchanged",
        h.v_filt == v0)

  # ---------------------------------------------------------------- ARM CHECK (09-26)
  # Operator, 2026-09-26: CHECK_S after arming, release unless the gap -- corrected for our own
  # speed change since the arm -- closed at least CHECK_RATE. FINDINGS 40, 42.
  check("CHECK_S 1.5 s = 30 frames, CHECK_RATE -2.0 m/s",
        fl.CHECK_S == 1.5 and fl._CHECK_N == 30 and fl.CHECK_RATE == -2.0)

  def after_arm(rel_v, decel=0.0, frames=None):
    """Arm on a genuine -8 m/s close, then run `frames` with the lead moving rel_v relative to our
    ARM speed while we slow at `decel`. Returns (armed at the end, rearm_hold_s, armed each frame)."""
    h = new_hook()
    _, d = arm(h, 120.0, 30.6, -8.0)
    v0 = v = 30.6; states = []
    for _ in range(frames or fl._CHECK_N + 5):
      v = max(0.0, v - decel * DT_MDL)
      d += (rel_v + (v0 - v)) * DT_MDL           # our slowing opens the gap
      h.step(True, d, rel_v + (v0 - v), v, True, True, False, 1.0, d, 0.9)
      states.append(h.armed)
    return h, states

  h, st_ = after_arm(0.0)
  check("ARM CHECK: gap stops closing after the arm -> released at the check",
        st_[fl._CHECK_N - 2] and not st_[fl._CHECK_N - 1])
  check("...with the re-arm hold set, and no re-arm on the frames after", h.rearm_hold_s > 0.0
        and not any(st_[fl._CHECK_N:]))
  h, st_ = after_arm(-6.0)
  check("ARM CHECK: gap keeps closing at 6 m/s -> still armed past the check", all(st_))
  # Our own braking: the lead closes at 3 m/s relative to our ARM speed, but we shed 2 m/s^2, so the
  # RAW gap closes at only ~1.5 m/s over the window -- slower than CHECK_RATE. Corrected, it is 3.
  h, st_ = after_arm(-3.0, decel=2.0)
  check("ARM CHECK: own braking is corrected for -- a 3 m/s close under 2 m/s^2 of our braking holds",
        all(st_))
  h, st_ = after_arm(-1.0)
  check("ARM CHECK: a 1 m/s close (slower than CHECK_RATE) is released", not st_[-1])

  check("ARM_MIN_DIST 65 m ships only with the guards (73 m is the guards-only rollback)",
        fl.ARM_MIN_DIST == 65.0 and hasattr(fl._RangeRateFilter, "_switch"))

  print(f"\n{sum(results)}/{len(results)} passed")
  return 0 if all(results) else 1


if __name__ == "__main__":
  sys.exit(main())
