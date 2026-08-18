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

from openpilot.grt.e2e_floor import (E2EAccelFloor, _FLOOR_FALL_JERK,   # noqa: E402
                                     _DECAY_DEADBAND, _FLOOR_MAX, _FLOOR_JERK)

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

  # 2026-08-18: a mere TRANSIT through another personality must not kill the session.
  # Three of ten sessions on 08-18 died this way (1.1 s and 0.05 s). Exit is debounced by
  # _PERSONALITY_EXIT_T; a real switch away still disables promptly.
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 20, aggressive=True, a_e2e=0.05)
  armed = fl.state == 1
  run(fl, 4, aggressive=False, a_e2e=0.05)          # 0.20 s transit, under the 0.30 s exit
  survived = fl.state == 1
  run(fl, 12, aggressive=True, a_e2e=0.05)
  check(f"a 0.20 s transit through another personality does not kill the session "
        f"(armed {armed}, survived {survived})", armed and survived and fl.state == 1)
  run(fl, 10, aggressive=False, a_e2e=0.05)         # 0.50 s: a real switch away
  check(f"a real switch away still releases (got '{fl.last_reason}')",
        fl.state == 0 and fl.last_reason == "precondition: not aggressive")

  # --- floor shape ----------------------------------------------------------------
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  o = run(fl, 120, aggressive=True)
  steps = [abs(o[i] - o[i - 1]) for i in range(1, len(o))]
  check(f"floor jerk-limited (max step {max(steps):.4f}) and capped at _FLOOR_MAX "
        f"({max(o):.3f})",
        max(steps) <= _FLOOR_JERK * 0.05 + 1e-9 and max(o) <= _FLOOR_MAX + 1e-4)

  # 2026-08-16/18: the model's output wanders across zero constantly. A hard branch here
  # made the command alternate between floor and raw (felt as stutter). A fast asymmetric
  # withdrawal then produced half-a-m/s^2 round trips every 2-5 s (felt as a slow stutter).
  # The floor must withdraw SLOWLY and SYMMETRICALLY, and only for a meaningful, persistent
  # negative. Built from a LOW positive so the fall does not also trip the drop detector.
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 120, aggressive=True, a_e2e=0.05)        # long enough to SATURATE at the cap
  peak = fl.floor
  o = run(fl, 10, a_e2e=-0.03)                      # inside the deadband
  check(f"a -0.03 request is INSIDE the deadband: floor unmoved (was {peak:.3f}, "
        f"now {fl.floor:.3f})", abs(fl.floor - peak) < 1e-9)
  o = run(fl, 60, a_e2e=-0.15)                      # past the deadband, short of abandon
  steps = [abs(o[i] - o[i - 1]) for i in range(1, len(o))]
  check(f"a -0.15 request withdraws it smoothly, max step {max(steps):.4f}",
        max(steps) <= _FLOOR_FALL_JERK * 0.05 + 1e-9 and fl.state == 1)
  check(f"...and converges on the model's own value (got {o[-1]:+.3f})",
        abs(o[-1] - (-0.15)) < 1e-9)
  o2 = run(fl, 3, a_e2e=0.05)
  check(f"...and does not snap back up when the model returns positive ({o2[0]:+.3f})",
        o2[0] <= 0.05 + 1e-9)

  # 2026-08-17 regression: a NOISE-level touch of zero must not move the floor. Two such
  # touches (-0.001 and -0.007, ~0.1 s each) were felt as stutter on the 08-17 drive.
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 40, aggressive=True, a_e2e=0.30)
  held = fl.floor
  o = run(fl, 2, a_e2e=-0.001)
  check(f"a -0.001 touch does NOT move the floor (was {held:.3f}, now {fl.floor:.3f})",
        abs(fl.floor - held) < 1e-9 and o[0] == held)
  o = run(fl, 6, a_e2e=-0.15)
  check(f"a sustained -0.15 DOES withdraw it (floor {fl.floor:+.3f})", fl.floor < held)

  # --- situational gates ----------------------------------------------------------
  # 2026-08-17: lead PRESENCE is no longer a gate. min() hands control to the MPC lead
  # branch whenever a lead genuinely binds (100% of frames under 25 m, measured), so the
  # gate was redundant when it fired and blocked 31% of arm-eligible time when it did not.
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 12, aggressive=True, lead=True)
  check("a distant lead no longer blocks arming (min() covers a binding lead)",
        fl.state == 1 and fl.stats["lead_present_at_arm"] >= 1)

  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 80, aggressive=True, curvature=0.006)
  check("request still expires if a REAL gate never opens",
        fl.state == 0 and fl.stats["personality_expired"] == 1)

  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 80, aggressive=True, curvature=0.006)
  check("mid-curve request refused (load-bearing on the personality path)",
        fl.state == 0 and fl.stats["armed"] == 0)

  # 2026-08-18: do not arm while the model is already asking for less. Two sessions that
  # day armed at raw -0.011 and -0.092 and were wasted.
  fl = E2EAccelFloor()
  run(fl, 20, aggressive=False)
  run(fl, 20, aggressive=True, a_e2e=-0.12)
  check(f"will not arm while raw is below the deadband (state {fl.state}, "
        f"gate_raw_negative {fl.stats['gate_raw_negative']})", fl.state == 0)
  run(fl, 5, aggressive=True, a_e2e=0.05)
  check("...and arms as soon as the model returns to neutral, within the 3 s window",
        fl.state == 1)

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
