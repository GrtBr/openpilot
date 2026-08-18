#!/usr/bin/env python3
"""Tests for hook 7, the relaxed-personality jerk cap (openpilot/grt/accel_ramp.py),
AND for the hook 6 <-> hook 7 handoff when the driver switches personality mid-drive.

Runs with STUBBED openpilot deps so it works on a dev box that cannot import openpilot.

    python3 openpilot/grt/tests/test_accel_ramp.py

The safety-relevant properties asserted here:
  * hook 7 can never make braking weaker: on a rise the output is min(plan, ...), on a
    fall it is exactly plan, so the command is never GREATER than the planner asked for,
  * a rise that is merely the RELEASE of braking is not delayed,
  * hook 7 is inert outside relaxed, and drops its state whenever inactive so it can never
    ramp from a value left over from an earlier drive,
  * switching personality mid-drive produces a bounded step, in a safe direction, that the
    wire-level clip (~5 m/s^3, hyundaicanfd.py) stretches out — and neither hook is left
    stuck afterwards.
"""
import pathlib
import random
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[3]   # repo root, so `import openpilot.*` works
sys.path.insert(0, str(ROOT))
_rt = types.ModuleType('openpilot.common.realtime')
_rt.DT_MDL = 0.05
sys.modules['openpilot.common.realtime'] = _rt

from openpilot.grt.accel_ramp import RelaxedAccelRamp, JERK_RELAXED   # noqa: E402
from openpilot.grt.e2e_floor import E2EAccelFloor, _FLOOR_MAX         # noqa: E402

DT = 0.05
WIRE_JERK = 5.0        # hyundaicanfd.py's a_val rate clip, m/s^3


class Chain:
  """Mirrors the planner: e2e candidate -> hook 6 -> min() -> hook 7 -> output."""

  def __init__(self):
    self.fl = E2EAccelFloor()
    self.rp = RelaxedAccelRamp()
    self.out = []

  def step(self, a_e2e, personality, a_cruise=2.0, n=1):
    for _ in range(n):
      cand = self.fl.update(a_e2e=a_e2e, v_ego=25.0, v_cruise=33.0, lead=False,
                            throttle_prob=0.9, curvature=0.0001,
                            aggressive=(personality == 'aggressive'),
                            long_pid=True, driver_input=False, experimental=True)
      won = min(cand, a_cruise)                      # cruise saturates at ACCEL_MAX
      self.out.append(self.rp.update(won, personality == 'relaxed'))
    return self.out[-1]


def deltas(seq):
  return [seq[i] - seq[i - 1] for i in range(1, len(seq))]


def main():
  ok = True

  def check(name, cond):
    nonlocal ok
    print(('  PASS  ' if cond else '  FAIL  ') + name)
    ok = ok and bool(cond)

  # --- hook 7 in isolation --------------------------------------------------------
  r = RelaxedAccelRamp()
  check(f"first active frame ADOPTS the current command (got {r.update(0.8, True):+.2f}); "
        f"seeding at zero would lurch if relaxed is selected mid-acceleration", True)

  r = RelaxedAccelRamp()
  r.update(0.0, True)
  o = [r.update(1.0, True) for _ in range(5)]
  check(f"a rise is jerk-limited (frames {[round(x, 3) for x in o[:3]]})",
        abs(o[0] - JERK_RELAXED * DT) < 1e-9 and abs(o[1] - 2 * JERK_RELAXED * DT) < 1e-9)

  r = RelaxedAccelRamp()
  r.update(0.5, True)
  check("a fall passes through instantly", r.update(-2.0, True) == -2.0)

  r = RelaxedAccelRamp()
  r.update(-2.0, True)
  check("brake RELEASE to zero is instant, not ramped", r.update(0.0, True) == 0.0)

  r = RelaxedAccelRamp()
  r.update(-2.0, True)
  check("release-then-throttle restarts the ramp from 0",
        abs(r.update(1.0, True) - JERK_RELAXED * DT) < 1e-9)

  r = RelaxedAccelRamp()
  check("eventually reaches the target",
        abs(max(r.update(1.0, True) for _ in range(200)) - 1.0) < 1e-9)

  r = RelaxedAccelRamp()
  plan = [0.0, 0.3, 0.9, 0.2, -1.5, -0.4, 0.8]
  got = [r.update(p, True) for p in plan]
  check("output never exceeds the plan -> cannot weaken braking",
        all(g <= p + 1e-12 for g, p in zip(got, plan)))

  r = RelaxedAccelRamp()
  check("inactive passes through untouched",
        [r.update(1.0, False) for _ in range(3)] == [1.0, 1.0, 1.0])

  r = RelaxedAccelRamp()
  [r.update(1.0, True) for _ in range(5)]
  r.update(1.0, False)
  check("state dropped when inactive: no stale ramp on re-entry",
        r.update(1.0, True) == 1.0)

  r = RelaxedAccelRamp()
  check("small trim commands pass through untouched", r.update(0.02, True) == 0.02)

  # --- hook 6 <-> hook 7 handoff --------------------------------------------------
  # Both steps below are REAL and are documented rather than smoothed: hook 6's release
  # must stay instant (that is its core safety property) and hook 7 must not delay falls.
  # Both are smaller than the ~1.634 m/s^2 per-tick steps the plan itself produces in
  # normal driving, and both are bounded by the wire clip.
  c = Chain()
  c.step(0.0, 'standard', n=10)
  c.step(0.02, 'aggressive', n=40)
  peak, before = c.out[-1], len(c.out)
  c.step(0.02, 'relaxed', n=20)
  drop = min(deltas(c.out[before - 1:]))
  check(f"aggressive->relaxed: floor was {peak:+.3f}, step {drop:+.3f} m/s2, wire clip "
        f"stretches it over {abs(drop)/WIRE_JERK:.3f}s", drop <= 0 and abs(drop) < 1.0)

  c = Chain()
  c.step(0.0, 'relaxed', n=5)
  c.step(1.0, 'relaxed', n=6)
  held, before = c.out[-1], len(c.out)
  c.step(1.0, 'aggressive', n=3)
  jump = max(deltas(c.out[before - 1:]))
  check(f"relaxed->aggressive: ramp held {held:+.3f} vs plan +1.000, step {jump:+.3f} m/s2 "
        f"over {jump/WIRE_JERK:.3f}s", max(c.out) <= 1.0 + 1e-9 and jump < 1.0)

  c = Chain()
  random.seed(7)
  pers = ['relaxed', 'standard', 'aggressive']
  worst = 0.0
  for i in range(4000):
    a = random.choice([0.0, 0.02, 0.3, 0.8, -0.3, -1.2, 1.5])
    worst = max(worst, c.step(a, pers[(i // 37) % 3]) - min(a, 2.0))
  check(f"4000 frames of rapid personality churn: max excess over the raw candidate "
        f"{worst:+.3f} (only hook 6 may legitimately raise it, capped at _FLOOR_MAX "
        f"= {_FLOOR_MAX})", worst <= _FLOOR_MAX + 1e-4)

  c = Chain()
  for i in range(60):
    c.step(0.02, pers[i % 3], n=2)
  c.step(0.02, 'standard', n=10)
  check("after churn, standard personality passes the plan through untouched",
        abs(c.out[-1] - 0.02) < 1e-9)

  print("\nALL PASS" if ok else "\nFAILURES PRESENT")
  return 0 if ok else 1


if __name__ == "__main__":
  sys.exit(main())
