#!/usr/bin/env python3
"""Tests for hook 8, the hold-speed servo (openpilot/grt/hold_speed.py), and for its
interaction with hook 6 -- the two can be active at the same instant.

Runs with STUBBED openpilot deps so it works on a dev box that cannot import openpilot.

    python3 openpilot/grt/tests/test_hold_speed.py

The safety-relevant properties asserted here:
  * the correction is POSITIVE-ONLY, so applied to the e2e candidate it can never make
    braking weaker -- cruise and the MPC lead branches still bind through min(),
  * it is INERT outside aggressive personality and above the band,
  * the anchor is dropped AT ONCE when the model asks to slow (holding speed would fight
    it), but SURVIVES an upward excursion and ratchets up to what the model achieved,
  * a stale anchor beyond _HS_MAX_ERR stands the servo down rather than commanding,
  * it never pushes past the set speed,
  * hook 6 and hook 8 combine by max() and neither is suppressed by the other.
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
_rt = types.ModuleType('openpilot.common.realtime')
_rt.DT_MDL = 0.05
sys.modules['openpilot.common.realtime'] = _rt

from openpilot.grt.hold_speed import (HoldSpeed, _HS_BAND, _HS_GAIN,   # noqa: E402
                                      _HS_MAX, _HS_MAX_ERR, _HS_LATCH_T)
from openpilot.grt.e2e_floor import E2EAccelFloor                      # noqa: E402

OK = dict(v_cruise=33.0, aggressive=True, long_pid=True, driver_input=False, experimental=True)


def run(hs, n, a_e2e=0.0, v_ego=25.0, **kw):
  p = dict(OK)
  p.update(kw)
  return [hs.update(a_e2e=a_e2e, v_ego=v_ego, **p) for _ in range(n)]


def main():
  ok = True

  def check(name, cond):
    nonlocal ok
    print(('  PASS  ' if cond else '  FAIL  ') + name)
    ok = ok and bool(cond)

  # --- latching --------------------------------------------------------------------
  hs = HoldSpeed()
  run(hs, 4, a_e2e=0.0, v_ego=25.0)                       # 0.20 s < _HS_LATCH_T
  check(f"does not latch before {_HS_LATCH_T}s of quiet", hs.anchor is None)
  run(hs, 4, a_e2e=0.0, v_ego=25.0)
  check(f"latches after {_HS_LATCH_T}s (anchor {hs.anchor})", hs.anchor == 25.0)

  # --- the core behaviour ----------------------------------------------------------
  hs = HoldSpeed()
  run(hs, 10, a_e2e=0.0, v_ego=25.0)                      # latch at 25.0
  o = run(hs, 1, a_e2e=0.0, v_ego=24.5)[0]                # lost 0.5 m/s
  check(f"corrects when speed is lost (got {o:+.3f}, expected {_HS_GAIN * 0.5:+.3f})",
        abs(o - _HS_GAIN * 0.5) < 1e-9)
  o = run(hs, 1, a_e2e=0.0, v_ego=25.5)[0]                # ABOVE the anchor
  check(f"does NOT correct when speed is above the anchor (got {o:+.3f})", o == 0.0)

  hs = HoldSpeed()
  run(hs, 10, a_e2e=0.0, v_ego=25.0)
  o = run(hs, 1, a_e2e=0.0, v_ego=20.0)[0]                # huge error -> capped by _HS_MAX
  check(f"correction is capped at _HS_MAX (got {o:+.3f})", o <= _HS_MAX + 1e-9)

  # --- anchor hygiene --------------------------------------------------------------
  # ASYMMETRIC: downward exit conflicts with holding speed, upward exit does not.
  hs = HoldSpeed()
  run(hs, 10, a_e2e=0.0, v_ego=25.0)
  run(hs, 1, a_e2e=-0.30, v_ego=25.0)                     # model wants to SLOW
  check("anchor dropped at once when the model asks to slow", hs.anchor is None)
  run(hs, 10, a_e2e=0.0, v_ego=22.0)
  check(f"and a FRESH anchor is latched at the new speed ({hs.anchor})", hs.anchor == 22.0)

  hs = HoldSpeed()
  run(hs, 10, a_e2e=0.0, v_ego=25.0)
  run(hs, 6, a_e2e=+0.30, v_ego=25.0)                     # model wants to GO
  check(f"anchor SURVIVES an upward exit (anchor {hs.anchor})", hs.anchor == 25.0)
  run(hs, 6, a_e2e=+0.30, v_ego=27.0)                     # and the car goes faster
  check(f"anchor ratchets UP to what the model achieved ({hs.anchor})", hs.anchor == 27.0)
  run(hs, 6, a_e2e=+0.30, v_ego=26.0)                     # dips again while still pushing
  check(f"anchor never ratchets DOWN ({hs.anchor})", hs.anchor == 27.0)

  hs = HoldSpeed()
  run(hs, 10, a_e2e=0.0, v_ego=25.0)
  o = run(hs, 1, a_e2e=0.0, v_ego=25.0 - _HS_MAX_ERR - 0.1)[0]
  check(f"a STALE anchor (> {_HS_MAX_ERR:.2f} m/s error) stands down rather than commanding "
        f"(got {o:+.3f}, anchor {hs.anchor})", o == 0.0 and hs.anchor is None)

  # --- inert where it should be ----------------------------------------------------
  hs = HoldSpeed()
  o = run(hs, 20, a_e2e=0.0, v_ego=25.0, aggressive=False)
  check("inert outside aggressive personality", all(x == 0.0 for x in o) and hs.anchor is None)

  hs = HoldSpeed()
  run(hs, 10, a_e2e=0.0, v_ego=25.0)
  o = run(hs, 1, a_e2e=0.0, v_ego=24.0, v_cruise=24.1)[0]
  check(f"never pushes past the set speed (got {o:+.3f})", o == 0.0)

  hs = HoldSpeed()
  o = run(hs, 20, a_e2e=0.0, v_ego=5.0)
  check("inert below the minimum speed", all(x == 0.0 for x in o) and hs.anchor is None)

  # --- POSITIVE-ONLY: the safety claim ---------------------------------------------
  hs = HoldSpeed()
  run(hs, 10, a_e2e=0.0, v_ego=25.0)
  worst = 0.0
  for a in (-0.04, -0.02, 0.0, 0.02, 0.04):
    o = run(hs, 1, a_e2e=a, v_ego=24.0)[0]
    worst = min(worst, o - a)
  check(f"correction is never negative -> cannot make braking weaker (min {worst:+.4f})",
        worst >= -1e-12)

  # --- hook 6 / hook 8 interaction -------------------------------------------------
  # They can be active at the same instant: hook 6's taper settles into `quiet`, which is
  # inside hook 8's band. Combined with max() in the shim.
  fl = E2EAccelFloor()
  hs = HoldSpeed()
  base = dict(v_ego=25.0, v_cruise=33.0, lead=False, throttle_prob=0.9, curvature=0.0001,
              long_pid=True, driver_input=False, experimental=True)
  for _ in range(20):
    fl.update(a_e2e=0.02, aggressive=False, **base)
  out = []
  for i in range(200):
    a = 0.60 if i < 20 else 0.02              # a strong push, then settle -> taper arms
    v = 25.0 - min(i, 60) * 0.01              # and the car slowly loses speed
    af = fl.update(a_e2e=a, aggressive=True, **{**base, 'v_ego': v})
    ah = hs.update(a_e2e=a, v_ego=v, v_cruise=33.0, aggressive=True, long_pid=True,
                   driver_input=False, experimental=True)
    out.append(max(af, ah))
  check(f"both can arm together; combined output never below the raw request "
        f"(min {min(out):+.3f})", min(out) >= 0.02 - 1e-9)
  check(f"hook 6 armed ({fl.stats['armed']}) and hook 8 latched ({hs.stats['latched']}) "
        f"in the same run", fl.stats['armed'] >= 1 and hs.stats['latched'] >= 1)
  check(f"combined output stays bounded (max {max(out):.3f})", max(out) <= 0.95)

  print("\nALL PASS" if ok else "\nFAILURES PRESENT")
  return 0 if ok else 1


if __name__ == "__main__":
  sys.exit(main())
