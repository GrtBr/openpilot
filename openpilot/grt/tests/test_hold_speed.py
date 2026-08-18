#!/usr/bin/env python3
"""Tests for hook 8, the under-delivery servo (openpilot/grt/hold_speed.py), and for its
interaction with hook 6 -- the two can be active at the same instant.

Runs with STUBBED openpilot deps so it works on a dev box that cannot import openpilot.

    python3 openpilot/grt/tests/test_hold_speed.py

The safety-relevant properties asserted here:
  * the correction is POSITIVE-ONLY and capped, so it can only ever raise the e2e candidate,
    and `min()` still hands control to cruise or a lead branch whenever either wants less,
  * while the model asks for DECELERATION the output is capped at 0 -- it can undo
    over-braking down to coasting but never accelerates against a deceleration request,
  * the lag reference is the ACTUAL commanded accel, so a lead branch's braking is NOT
    misread as the plant under-delivering,
  * it BACKS OFF on its own as the car reaches what was asked (proportional, no flip-off),
  * it is inert outside aggressive, below the minimum speed, at the set speed, and on
    driver input.
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
_rt = types.ModuleType('openpilot.common.realtime')
_rt.DT_MDL = 0.05
sys.modules['openpilot.common.realtime'] = _rt

from openpilot.grt.hold_speed import (HoldSpeed, _HS_GAIN, _HS_DEAD,   # noqa: E402
                                      _HS_CAP, _LAG_N, _SMOOTH_N)
from openpilot.grt.e2e_floor import E2EAccelFloor                      # noqa: E402

OK = dict(v_ego=25.0, v_cruise=33.0, aggressive=True, long_pid=True,
          driver_input=False, experimental=True)
SETTLE = _LAG_N + _SMOOTH_N + 2       # frames before the servo has enough history


def run(hs, n, a_e2e=0.0, a_commanded=None, a_ego=0.0, **kw):
  """a_commanded defaults to a_e2e, i.e. the e2e branch won min()."""
  p = dict(OK)
  p.update(kw)
  return [hs.update(a_e2e=a_e2e, a_commanded=a_e2e if a_commanded is None else a_commanded,
                    a_ego=a_ego, **p) for _ in range(n)]


def main():
  ok = True

  def check(name, cond):
    nonlocal ok
    print(('  PASS  ' if cond else '  FAIL  ') + name)
    ok = ok and bool(cond)

  # --- no correction when the plant delivers ---------------------------------------
  hs = HoldSpeed()
  o = run(hs, SETTLE, a_e2e=0.10, a_ego=0.10)
  check(f"plant delivering -> no correction (last {o[-1]:+.3f})", o[-1] == 0.10)

  # --- corrects when under-delivering ----------------------------------------------
  hs = HoldSpeed()
  o = run(hs, SETTLE, a_e2e=0.10, a_ego=-0.10)      # u = +0.20
  expect = 0.10 + min(_HS_CAP, _HS_GAIN * (0.20 - _HS_DEAD))
  check(f"under-delivery of 0.20 -> corrects (got {o[-1]:+.3f}, expect {expect:+.3f})",
        abs(o[-1] - expect) < 1e-9)

  hs = HoldSpeed()
  o = run(hs, SETTLE, a_e2e=0.10, a_ego=-1.00)
  check(f"correction is capped at _HS_CAP (got {o[-1]:+.3f})",
        abs(o[-1] - (0.10 + _HS_CAP)) < 1e-9)

  hs = HoldSpeed()
  o = run(hs, SETTLE, a_e2e=0.10, a_ego=0.13)       # OVER-delivering
  check(f"over-delivery -> no correction (got {o[-1]:+.3f})", o[-1] == 0.10)

  hs = HoldSpeed()
  o = run(hs, SETTLE, a_e2e=0.10, a_ego=0.10 - _HS_DEAD * 0.5)
  check(f"under-delivery below the deadband is ignored (got {o[-1]:+.3f})", o[-1] == 0.10)

  # --- THE REFERENCE IS THE ACTUAL COMMAND -----------------------------------------
  # A lead branch commands -1.0 and the car achieves -1.0. Measuring against a_e2e (~0)
  # would read u = +1.0 and demand a nonsense correction.
  hs = HoldSpeed()
  o = run(hs, SETTLE, a_e2e=0.0, a_commanded=-1.00, a_ego=-1.00)
  check(f"a lead branch braking is NOT misread as under-delivery (got {o[-1]:+.3f})",
        o[-1] == 0.0)

  # --- capped at zero while the model asks to slow ---------------------------------
  hs = HoldSpeed()
  o = run(hs, SETTLE, a_e2e=-0.10, a_ego=-0.40)     # u = +0.30
  check(f"model asking to slow: output never rises above 0 (got {o[-1]:+.3f})",
        o[-1] <= 1e-12)
  check(f"...but it DOES undo over-braking (got {o[-1]:+.3f} vs raw -0.100)", o[-1] > -0.10)

  hs = HoldSpeed()
  o = run(hs, SETTLE, a_e2e=-0.60, a_ego=-0.70)
  check(f"a deep braking request is only eased, never inverted (got {o[-1]:+.3f})",
        -0.60 <= o[-1] <= 0.0)

  # --- positive-only ---------------------------------------------------------------
  worst = 0.0
  for a in (-0.30, -0.10, 0.0, 0.10, 0.30):
    hs = HoldSpeed()
    o = run(hs, SETTLE, a_e2e=a, a_ego=a - 0.20)
    worst = min(worst, o[-1] - a)
  check(f"the correction is never negative (min {worst:+.4f})", worst >= -1e-12)

  # --- it backs off by itself ------------------------------------------------------
  hs = HoldSpeed()
  o1 = run(hs, SETTLE, a_e2e=0.10, a_ego=-0.10)
  c_big = o1[-1] - 0.10
  o2 = run(hs, SETTLE, a_e2e=0.10, a_ego=0.05)      # car catching up
  c_small = o2[-1] - 0.10
  check(f"correction shrinks as the car catches up ({c_big:+.3f} -> {c_small:+.3f}); "
        f"no flip-off needed", c_small < c_big and c_small >= 0.0)

  # --- inert where it should be ----------------------------------------------------
  for lbl, kw in (("outside aggressive personality", dict(aggressive=False)),
                  ("below the minimum speed", dict(v_ego=5.0)),
                  ("at the set speed -- cruise owns it", dict(v_cruise=25.05)),
                  ("on driver input", dict(driver_input=True))):
    hs = HoldSpeed()
    o = run(hs, SETTLE, a_e2e=0.10, a_ego=-0.10, **kw)
    check(f"inert {lbl} (last {o[-1]:+.3f})", o[-1] == 0.10)

  # --- hook 6 / hook 8 interaction -------------------------------------------------
  fl = E2EAccelFloor()
  hs = HoldSpeed()
  base = dict(v_ego=25.0, v_cruise=33.0, lead=False, throttle_prob=0.9, curvature=0.0001,
              long_pid=True, driver_input=False, experimental=True)
  for _ in range(20):
    fl.update(a_e2e=0.02, aggressive=False, **base)
  out = []
  for i in range(300):
    a = 0.60 if i < 20 else 0.02
    af = fl.update(a_e2e=a, aggressive=True, **base)
    ah = hs.update(a_e2e=a, a_commanded=a, a_ego=a - 0.15, v_ego=25.0, v_cruise=33.0,
                   aggressive=True, long_pid=True, driver_input=False, experimental=True)
    out.append(max(af, ah))
  check(f"both active together; combined output never below the raw request "
        f"(min {min(out):+.3f})", min(out) >= 0.02 - 1e-9)
  check(f"hook 6 armed ({fl.stats['armed']}) and hook 8 corrected "
        f"({hs.stats['frames_correcting']}) in the same run",
        fl.stats['armed'] >= 1 and hs.stats['frames_correcting'] >= 1)
  check(f"combined output stays bounded (max {max(out):.3f})", max(out) <= 1.0)

  print("\nALL PASS" if ok else "\nFAILURES PRESENT")
  return 0 if ok else 1


if __name__ == "__main__":
  sys.exit(main())
