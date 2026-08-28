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
(fixed by HOT_CLOSING_RATE on both arm and release -- see "FOURTH BUG").
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


def close(hook, dRel0, v_ego, closing_rate, frames, relaxed=True, long_active=True,
          driver_input=False, stock_min=1.0):
  """Drive `frames` ticks with dRel decreasing at closing_rate (m/s), true vRel == closing_rate
  (noiseless -- these tests check the state machine's logic, not the filter's noise rejection;
  that is covered separately by the replay bar in section 10, run against the real log)."""
  out = []
  dRel = dRel0
  for _ in range(frames):
    out = hook.step(True, dRel, closing_rate, v_ego, relaxed, long_active, driver_input,
                    stock_min)
    dRel = max(0.0, dRel + closing_rate * DT_MDL)
  return out, dRel


def arm(hook, dRel0=120.0, v_ego=30.6, closing_rate=-8.0, max_frames=60):
  """Drive until armed or max_frames elapse. Returns (out, dRel) at the arming frame."""
  dRel = dRel0
  for _ in range(max_frames):
    out = hook.step(True, dRel, closing_rate, v_ego, True, True, False, 1.0)
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

  # relaxed, 120 m, closing hard, after persistence -> one candidate, a <= -0.40
  h = new_hook()
  out, dRel_end = arm(h, 120.0, 30.6, -8.0)
  check("relaxed, 120 m, hard closing -> arms with one candidate", len(out) == 1)
  check("candidate a <= -0.40 (floor)", len(out) == 1 and out[0][0] <= -0.40)
  check("candidate a >= -1.2 (cap)", len(out) == 1 and out[0][0] >= -1.2)
  check("candidate source is lead0", len(out) == 1 and out[0][1] == fl.LongitudinalPlanSource.lead0)
  check("arms while dRel still > 80 m", dRel_end > 80.0)

  # 110 vs 100 (a_req~0.14 at the true, instantaneous state) -> must not arm within a realistic
  # evaluation window. NOTE: because a_req grows as dRel shrinks, ANY sustained nonzero closing
  # rate eventually crosses the threshold given enough time/distance -- that is correct physics,
  # not a bug. This checks it does not hair-trigger on an ordinary overtaking-speed gap.
  h = new_hook()
  out, dRel_end = close(h, 150.0, 30.6, -2.78, 30)  # 1.5 s
  check("110 vs 100 at 150 m -> [] within 1.5 s (a_req~0.14, not hot)", out == [] and not h.armed)

  # 110 vs 0 (fully stopped lead) at 120 m -- the canonical worst-case synthetic (spec section 3).
  # A naive "check dRel at gate-completion" implementation FAILED this (dRel had already crossed
  # under threshold by the time the 0.8 s gate cleared). v1 fixed it by anchoring at first
  # presence; v2 anchors at hot-streak-start instead (see module docstring, "THIRD BUG") -- must
  # still arm here, and harder than the slow-pack case, with dRel_at_hot_start comfortably clear
  # of ARM_MIN_DIST (80 m).
  h_slow = new_hook()
  out_slow, _ = arm(h_slow, 120.0, 30.6, -8.0)
  h_stop = new_hook()
  out_stop, dRel_stop = arm(h_stop, 120.0, 30.6, -30.6)
  check("110 vs 0 at 120 m arms (dRel_at_hot_start, not live dRel, gates this)", len(out_stop) == 1)
  check("110 vs 0 dRel_at_hot_start clears ARM_MIN_DIST with margin",
        h_stop.dRel_at_hot_start is not None and h_stop.dRel_at_hot_start > fl.ARM_MIN_DIST + 20.0)
  check("110 vs 0 candidate at least as hard as the slow-pack case",
        len(out_stop) == 1 and len(out_slow) == 1 and out_stop[0][0] <= out_slow[0][0])
  check("110 vs 0 candidate still >= -1.2 (CAP)", len(out_stop) == 1 and out_stop[0][0] >= -1.2)

  # KNOWN LIMITATION (documented in module docstring): a fully-stopped lead first detected
  # already inside ~87 m never arms, because dRel_at_hot_start freezes below ARM_MIN_DIST on the
  # very first hot frame and is never re-anchored. Not a regression vs v1 (which has the
  # analogous failure for a lead first sighted already inside 100 m) and outside this hook's
  # declared envelope (sub-3-second emergency stop, not long-range complacency correction) --
  # documented here so a future reader does not "fix" this file back into the v1 absence-gate
  # bug while chasing it.
  h_close = new_hook()
  out_close, _ = arm(h_close, 86.0, 30.6, -30.6)
  check("KNOWN LIMIT: stopped lead first seen at 86 m never arms (see docstring)", out_close == [])

  # hot-streak anchor is distinct from first-presence: a lead present and steady (non-closing)
  # for a while, THEN starts closing hard, must anchor dRel_at_hot_start at the moment it goes
  # hot -- not at first sight -- and still arm if that's above ARM_MIN_DIST.
  h = new_hook()
  for _ in range(80):  # 4 s steady presence, not closing yet
    h.step(True, 95.0, 0.0, 30.6, True, True, False, 1.0)
  out, dRel_end = arm(h, 95.0, 30.6, -8.0)
  check("steady-then-closing at 95 m: hot-start anchor arms once it goes hot",
        len(out) == 1 and h.dRel_at_hot_start is not None and h.dRel_at_hot_start > fl.ARM_MIN_DIST)

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

  # armed, stock stuck near 0 all the way down to the 20 m backstop -> still supplies a candidate
  h = new_hook()
  arm(h, 120.0, 30.6, -8.0)
  out = h.step(True, 21.0, -8.0, 30.6, True, True, False, 0.0)
  check("stock stuck near 0 at 21 m -> hook still supplies a candidate", len(out) == 1)

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

  # candidate a > -0.20 must never happen -- hook 10 C's ABANDON would eat it
  h = new_hook()
  out, _ = arm(h, 120.0, 30.6, -8.0)
  check("candidate a <= -0.20 always (hook 10 C floor)", all(c[0] <= -0.20 for c in out))

  # FOURTH BUG regression: a_req alone can clear HOT_A_REQ on a tiny closing rate at close-ish
  # range -- must NOT arm without also clearing HOT_CLOSING_RATE. At 90 m, v_ego=30.6,
  # closing at -1.0 m/s (3.6 km/h, well under the 10 km/h gate): a_req ~ 0.35, clears HOT_A_REQ,
  # but must not arm.
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
  for _ in range(60):  # 3 s at the slow rate -- must release well before this elapses
    out = h.step(True, dRel, -1.5, 30.6, True, True, False, 1.0)
    dRel = max(0.0, dRel - 1.5 * DT_MDL)
    if out == [] and not h.armed:
      break
  check("FOURTH BUG: closing rate decays to -1.5 m/s (<10km/h) -> released, not held",
        out == [] and not h.armed)

  print(f"\n{sum(results)}/{len(results)} passed")
  return 0 if all(results) else 1


if __name__ == "__main__":
  sys.exit(main())
