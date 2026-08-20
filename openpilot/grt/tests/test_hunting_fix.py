#!/usr/bin/env python3
"""Tests for the 2026-08-20 hunting fix:
  * hook 8's EMA on the correction target (openpilot/grt/hold_speed.py)
  * hook 9, the aggressive candidate rising-edge jerk cap (openpilot/grt/accel_ramp.py)

    python3 openpilot/grt/tests/test_hunting_fix.py

The properties asserted are the ones the fix rests on -- the EMA must cut REVERSALS while
preserving the correction's MEAN and PEAK, and hook 9 must never be able to raise the candidate.
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
_rt = types.ModuleType('openpilot.common.realtime')
_rt.DT_MDL = 0.05
sys.modules['openpilot.common.realtime'] = _rt

from openpilot.grt.hold_speed import (HoldSpeed, _HS_CAP, _HS_TAU,        # noqa: E402
                                      _HS_ALPHA, _HS_CORR_JERK, _LAG_N, _SMOOTH_N)
from openpilot.grt.accel_ramp import (AggressiveCandidateRamp,            # noqa: E402
                                      JERK_AGGRESSIVE, RelaxedAccelRamp, JERK_RELAXED)

DT = 0.05
OK = dict(v_ego=25.0, v_cruise=33.0, aggressive=True, long_pid=True,
          driver_input=False, experimental=True)
SETTLE = _LAG_N + _SMOOTH_N + 2


def reversals(seq, amp):
  """Same zigzag counter used for the sizing measurement."""
  n = 0
  direc = 0
  hi = lo = seq[0]
  for x in seq[1:]:
    hi = max(hi, x)
    lo = min(lo, x)
    if direc >= 0 and x <= hi - amp:
      if direc > 0:
        n += 1
      direc = -1
      hi = lo = x
    elif direc <= 0 and x >= lo + amp:
      if direc < 0:
        n += 1
      direc = 1
      hi = lo = x
  return n


def run(hs, n, a_e2e=0.0, a_ego=0.0, **kw):
  p = dict(OK)
  p.update(kw)
  return [hs.update(a_e2e=a_e2e, a_commanded=a_e2e, a_ego=a_ego, **p) for _ in range(n)]


def main():
  ok = True

  def check(name, cond):
    nonlocal ok
    print(('  PASS  ' if cond else '  FAIL  ') + name)
    ok = ok and bool(cond)

  # --- the counter itself, before anything depends on it ---------------------------
  sq = []
  for _ in range(10):
    sq += [0.0] * 5 + [0.2] * 5
  check(f"reversal counter: 10 square cycles -> {reversals(sq, 0.05)} (expect 18-19)",
        18 <= reversals(sq, 0.05) <= 19)
  check("reversal counter: monotonic ramp -> 0",
        reversals([0.01 * i for i in range(200)], 0.05) == 0)
  check("reversal counter: sub-ruler dither -> 0",
        reversals([0.0, 0.02, -0.02] * 50, 0.05) == 0)

  # --- THE POINT OF THE FIX: fewer reversals on a wandering error ------------------
  # An under-delivery that oscillates slowly is exactly what made the servo wander.
  class NoEMA(HoldSpeed):
    """hook 8 exactly as it was before this change, for an A/B on the same input."""
    def _smooth(self, target):
      return target

  def wander(cls, half):
    """error swinging across the deadband with a `half`-frame half-period"""
    hs = cls()
    run(hs, SETTLE, a_e2e=0.10, a_ego=0.10)
    out = []
    for _ in range(40):
      out += run(hs, half, a_e2e=0.10, a_ego=-0.20)   # under-delivering
      out += run(hs, half, a_e2e=0.10, a_ego=0.12)    # over-delivering
    return out

  # Sweep the half-period: the rate limiter alone lets the correction swing the full width at
  # these rates, the EMA holds the swing under the ruler. Summed so the result does not hinge
  # on one period lining up with one time constant.
  r_s = sum(reversals(wander(HoldSpeed, h), 0.05) for h in (6, 8, 10, 12))
  r_r = sum(reversals(wander(NoEMA, h), 0.05) for h in (6, 8, 10, 12))
  check(f"EMA cuts reversals on a wandering error ({r_r} -> {r_s}, summed over 4 periods)",
        r_s < r_r)
  smoothed = wander(HoldSpeed, 10)
  raw = wander(NoEMA, 10)
  m_s = sum(smoothed) / len(smoothed)
  m_r = sum(raw) / len(raw)
  check(f"...while preserving the MEAN correction ({m_r:+.4f} -> {m_s:+.4f}, "
        f"{abs(m_s - m_r) / max(abs(m_r), 1e-9):.1%} change)",
        abs(m_s - m_r) < 0.10 * max(abs(m_r), 1e-9) + 1e-6)

  # --- peak authority survives -----------------------------------------------------
  hs = HoldSpeed()
  o = run(hs, SETTLE + int(20 / DT), a_e2e=0.10, a_ego=-1.00)   # sustained full-scale demand
  check(f"a SUSTAINED demand still reaches the cap ({o[-1] - 0.10:+.3f} of {_HS_CAP:.3f})",
        abs((o[-1] - 0.10) - _HS_CAP) < 0.01)

  # --- the EMA is a real time constant ---------------------------------------------
  hs = HoldSpeed()
  o = run(hs, SETTLE + int(_HS_TAU / DT), a_e2e=0.0, a_ego=-1.00)
  frac = o[-1] / _HS_CAP
  check(f"after one tau the correction is part-way, not complete ({frac:.0%} of cap)",
        0.10 < frac < 0.95)
  check(f"_HS_ALPHA matches _HS_TAU ({_HS_ALPHA:.5f} == {DT / (_HS_TAU + DT):.5f})",
        abs(_HS_ALPHA - DT / (_HS_TAU + DT)) < 1e-12)

  # --- the rate limiter is still downstream and still binds ------------------------
  hs = HoldSpeed()
  o = run(hs, SETTLE + 400, a_e2e=0.10, a_ego=-1.00)
  steps = [abs(o[n] - o[n - 1]) for n in range(1, len(o))]
  check(f"rate limiter still caps the slope (max step {max(steps):.4f} <= "
        f"{_HS_CORR_JERK * DT:.4f})", max(steps) <= _HS_CORR_JERK * DT + 1e-9)

  # --- filter state must not go stale ----------------------------------------------
  hs = HoldSpeed()
  run(hs, SETTLE + 200, a_e2e=0.10, a_ego=-1.00)          # wind the filter up
  o = run(hs, 200, a_e2e=0.10, a_ego=0.10, v_cruise=25.05)  # no headroom -> must decay out
  # The EMA decays asymptotically, so the correction approaches zero without reaching it:
  # measured residual 1.7e-5 after 10 s, 5.8e-14 after 30 s. Physically irrelevant, but the
  # tolerance has to admit it rather than demand an exact zero.
  check(f"no-headroom path decays through the EMA, no step "
        f"(residual {o[-1] - 0.10:.1e})", abs(o[-1] - 0.10) < 1e-3)
  steps = [abs(o[n] - o[n - 1]) for n in range(1, len(o))]
  check(f"...and does so without stepping (max {max(steps):.4f})",
        max(steps) <= _HS_CORR_JERK * DT + 1e-9)

  hs = HoldSpeed()
  run(hs, SETTLE + 200, a_e2e=0.10, a_ego=-1.00)
  o = run(hs, 5, a_e2e=0.10, a_ego=-1.00, aggressive=False)   # hard release
  check(f"hard release zeroes the EMA state (tgt_f {hs.tgt_f:.4f})", hs.tgt_f == 0.0)

  # ================= HOOK 9 =========================================================
  print()
  rp = AggressiveCandidateRamp()
  rp.update(0.0, True)          # prime: the FIRST frame is a pass-through by design (hook 7
                                # does the same), so a step must be measured from frame 2 on
  o = [rp.update(1.0, True) for _ in range(20)]
  check(f"hook 9 caps a step rise (first {o[0]:.3f} <= {JERK_AGGRESSIVE * DT:.3f})",
        o[0] <= JERK_AGGRESSIVE * DT + 1e-9)
  steps = [o[n] - o[n - 1] for n in range(1, len(o))]
  check(f"hook 9 rise rate == JERK_AGGRESSIVE (max {max(steps):.4f})",
        max(steps) <= JERK_AGGRESSIVE * DT + 1e-9)

  rp = AggressiveCandidateRamp()
  rp.update(0.5, True)
  check("hook 9 passes a FALL through untouched (never delays braking)",
        rp.update(-1.5, True) == -1.5)

  rp = AggressiveCandidateRamp()
  worst = 0.0
  seq = [0.0, 0.4, -0.3, 0.9, -1.2, 0.2, 0.0, 0.7]
  for a in seq * 20:
    worst = max(worst, rp.update(a, True) - a)
  check(f"hook 9 can only LOWER the candidate, never raise it (max rise {worst:+.6f})",
        worst <= 1e-12)

  rp = AggressiveCandidateRamp()
  rp.update(-0.5, True)
  o = rp.update(0.5, True)
  check(f"brake RELEASE is instant, ramp restarts from zero ({o:.3f} == "
        f"{JERK_AGGRESSIVE * DT:.3f})", abs(o - JERK_AGGRESSIVE * DT) < 1e-9)

  rp = AggressiveCandidateRamp()
  for _ in range(10):
    rp.update(1.0, True)
  hot = rp.prev
  rp.update(1.0, False)
  check(f"hook 9 drops state when inactive (was {hot:.3f}, now {rp.prev})", rp.prev is None)
  check("hook 9 is a pass-through when inactive", rp.update(2.0, False) == 2.0)

  check(f"hook 9 is gentler than the relaxed cap ({JERK_AGGRESSIVE} < {JERK_RELAXED})? "
        f"NO -- and that is intended: it shapes a CANDIDATE, not the final command",
        JERK_AGGRESSIVE <= JERK_RELAXED)
  r7 = RelaxedAccelRamp()
  r7.update(0.0, True)          # prime, same first-frame rule
  check("hook 7 is untouched by this change",
        r7.update(5.0, True) <= JERK_RELAXED * DT + 1e-9)

  print("\nALL PASS" if ok else "\nFAILURES PRESENT")
  return 0 if ok else 1


if __name__ == "__main__":
  sys.exit(main())
