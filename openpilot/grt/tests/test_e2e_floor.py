#!/usr/bin/env python3
"""Tests for hook 6, the e2e acceleration floor (openpilot/grt/e2e_floor.py).

Runs with STUBBED openpilot deps so it works on a dev box that cannot import openpilot.

    python3 openpilot/grt/tests/test_e2e_floor.py

The safety-relevant properties asserted here:
  * the hook is INERT outside aggressive personality, and booting in aggressive is not
    treated as a driver request,
  * a personality merely PASSED THROUGH while cycling the wheel button does not arm it
    (the 2026-08-15 fault: it armed and released within 39 ms),
  * a SUSTAINED model objection releases it and it then LATCHES OUT until a fresh
    strong-acceleration episode or a fresh deliberate selection,
  * a transient dip in the model's output does NOT release it (the other 08-15 fault:
    every false release tripped at -0.050..-0.057 against a -0.05 threshold),
  * a negative model command is never lifted,
  * the floor is jerk-limited and capped.
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[3]   # repo root, so `import openpilot.*` works
sys.path.insert(0, str(ROOT))
_rt = types.ModuleType('openpilot.common.realtime')
_rt.DT_MDL = 0.05
sys.modules['openpilot.common.realtime'] = _rt

from openpilot.grt.e2e_floor import E2EAccelFloor          # noqa: E402

OPEN = dict(v_ego=25.0, v_cruise=33.0, lead=False, throttle_prob=0.9, curvature=0.0001,
            long_pid=True, driver_input=False, experimental=True)


def run(fl, n, a_e2e=0.0, aggressive=True, **kw):
  p = dict(OPEN)
  p.update(kw)
  return [fl.update(a_e2e=a_e2e, aggressive=aggressive, **p) for _ in range(n)]


def main():
  ok = True

  def check(name, cond):
    nonlocal ok
    print(('  PASS  ' if cond else '  FAIL  ') + name)
    ok = ok and bool(cond)

  # --- arm triggers ---------------------------------------------------------------
  fl = E2EAccelFloor()
  run(fl, 40, aggressive=True)
  check("boot in aggressive is not a request",
        fl.stats["personality_edge"] == 0 and fl.state == 0)

  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 12, aggressive=True)                       # 0.60 s
  check("relaxed -> aggressive HELD 0.6 s arms",
        fl.stats["personality_edge"] == 1 and fl.state == 1)

  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 5, aggressive=True)                        # 0.25 s < 0.40 s debounce
  check("aggressive held only 0.25 s does NOT arm",
        fl.stats["personality_edge"] == 0 and fl.state == 0)

  # the 2026-08-15 fault: cycling the wheel button THROUGH aggressive
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  for _ in range(3):
    run(fl, 2, aggressive=True)                      # 0.10 s passing through
    run(fl, 3, aggressive=False)
  check("cycling THROUGH aggressive (3x 0.10 s) does not arm  [08-15 regression]",
        fl.stats["personality_edge"] == 0 and fl.state == 0)
  run(fl, 12, aggressive=True)
  check("...but settling on it afterwards does arm",
        fl.stats["personality_edge"] == 1 and fl.state == 1)

  # --- release debounce -----------------------------------------------------------
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 30, aggressive=True)
  run(fl, 1, a_e2e=-0.055)
  check("a -0.055 blip does NOT release  [08-15 regression]", fl.state == 1)
  run(fl, 4, a_e2e=-0.30)                            # 0.20 s < 0.30 s debounce
  check("a 0.20 s objection at -0.30 does NOT release", fl.state == 1)
  run(fl, 3, a_e2e=-0.30)                            # 0.35 s total
  check("a 0.35 s objection at -0.30 DOES release", fl.state == 0)

  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 4, a_e2e=-0.30, aggressive=True)
  run(fl, 12, a_e2e=0.0, aggressive=True)
  check("stale objection credit does not kill a fresh arm",
        fl.state == 1 and fl.object_t == 0.0)

  # --- named preconditions --------------------------------------------------------
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 12, aggressive=True)
  run(fl, 1, driver_input=True)
  check(f"precondition release names the cause (got '{fl.last_reason}')",
        fl.state == 0 and fl.last_reason == "precondition: driver input")

  # --- floor shape ----------------------------------------------------------------
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  o = run(fl, 60, aggressive=True)
  steps = [abs(o[i] - o[i - 1]) for i in range(1, len(o))]
  check(f"floor jerk-limited (max step {max(steps):.4f}) and capped ({max(o):.3f})",
        max(steps) <= 0.0151 and max(o) <= 0.4001)

  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 12, aggressive=True)
  o = run(fl, 1, a_e2e=-0.03)
  check(f"negative raw passes through unlifted (got {o[0]:+.3f})", o[0] == -0.03)

  # --- situational gates ----------------------------------------------------------
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 80, aggressive=True, lead=True)
  check("request expires if the gate never opens",
        fl.state == 0 and fl.stats["personality_expired"] == 1)

  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 80, aggressive=True, curvature=0.006)
  check("mid-curve request refused (load-bearing on the personality path)",
        fl.state == 0 and fl.stats["armed"] == 0)

  # --- latch-out ------------------------------------------------------------------
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 30, aggressive=True)
  run(fl, 8, a_e2e=-0.40)
  released = fl.state == 0
  run(fl, 200, a_e2e=0.0)
  check("release latches out through 10 s of quiet", released and fl.state == 0)
  run(fl, 20, a_e2e=0.60)
  run(fl, 30, a_e2e=0.02)
  check("a fresh strong->taper re-arms afterwards", fl.state == 1)

  print("\nALL PASS" if ok else "\nFAILURES PRESENT")
  return 0 if ok else 1


if __name__ == "__main__":
  sys.exit(main())
