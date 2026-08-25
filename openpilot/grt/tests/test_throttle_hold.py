#!/usr/bin/env python3
"""Tests for hook 10 (openpilot/grt/throttle_hold.py) -- layers A, B and C.

Runs with STUBBED openpilot deps so it works on a dev box that cannot import openpilot.

    python3 openpilot/grt/tests/test_throttle_hold.py

The safety-relevant properties asserted here:
  * a request at or beyond ABANDON (-0.20) is NEVER delayed, clipped or held, in any layer,
    on any frame -- that is the one invariant the whole hook rests on,
  * layer A holds the PRE-GLITCH command through a short sign flip, and never emits 0 while
    holding throttle on (0 is the SCC deadband -- clipping to it is the bug, not the fix),
  * layer B ONLY clips a sub-BAND ripple just over the set speed, and never touches the
    candidate below the set speed -- the approach taper must survive (regression, 2026-08-25),
  * layer C refuses a mild coast only while real headroom is unused, and adds no acceleration.
"""
import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
_rt = types.ModuleType('openpilot.common.realtime')
_rt.DT_MDL = 0.05
sys.modules['openpilot.common.realtime'] = _rt

from openpilot.grt.throttle_hold import (ThrottleHold, EPSILON, BAND,      # noqa: E402
                                         T_HOLD, ABANDON, MIN_HEADROOM,
                                         MIN_V_CRUISE)

DT = 0.05
HOLD_N = int(round(T_HOLD / DT))          # frames the debounce holds -- 6


def main():
  ok = True

  def check(name, cond):
    nonlocal ok
    print(('  PASS  ' if cond else '  FAIL  ') + name)
    ok = ok and bool(cond)

  # ================= LAYER B =========================================================
  # The set-speed RELEASE was reverted on 2026-08-25 after one drive: it removed the cruise
  # approach taper and produced a ~1 km/h bang-bang limit cycle across the set speed (time
  # above set 23% -> 54%, reversals at 0.10 up 18.9 -> 22.7/min). It was also unnecessary --
  # 5159 of 5182 measured veto frames were within 1 km/h of the set speed. All that remains
  # is the SCC ripple clip.
  th = ThrottleHold()
  B = th.deadband_cruise_accel
  V = 30.0

  check(f"REGRESSION: at the set speed the candidate is NOT released ({B(0.0, V, V):+.3f})",
        B(0.0, V, V) == 0.0)
  check(f"REGRESSION: below set the P-term is left ALONE, so the approach still tapers "
        f"({B(0.28, V - 0.28, V):+.3f})", B(0.28, V - 0.28, V) == 0.28)
  check(f"REGRESSION: a small negative below set is untouched ({B(-0.30, V - 1.0, V):+.3f})",
        B(-0.30, V - 1.0, V) == -0.30)

  over = V + 1.0 / 3.6
  check(f"over set by 1 km/h, -0.28 passes through unchanged ({B(-0.28, over, V):+.3f})",
        B(-0.28, over, V) == -0.28)
  check(f"the SCC ripple clip survives: -0.05 -> 0 ({B(-0.05, over, V):+.3f})",
        B(-0.05, over, V) == 0.0)
  check(f"-{BAND + 0.01:.2f} is beyond the ripple band and is NOT clipped",
        B(-(BAND + 0.01), over, V) == -(BAND + 0.01))
  check(f"the clip can withhold at most BAND ({BAND}) of braking, and only just over set",
        abs(B(-BAND + 1e-9, over, V) - 0.0) < 1e-6)

  check(f"forceDecel (v_cruise ~0): -1.20 stays -1.20 ({B(-1.20, 10.0, 0.0):+.3f})",
        B(-1.20, 10.0, 0.0) == -1.20)
  check(f"forceDecel: the ripple clip does NOT apply ({B(-0.05, 0.5, 0.0):+.3f})",
        B(-0.05, 0.5, 0.0) == -0.05)

  # ================= LAYER A =========================================================
  # no headroom in these cases, so layer C stays out of the way
  NC = dict(v_ego=30.0, v_cruise=30.0)

  th = ThrottleHold()
  check(f"first active frame ADOPTS the command ({th.update(0.07, **NC, long_active=True):+.3f})",
        th.update(0.07, **{**NC, 'long_active': True}) == 0.07)

  th = ThrottleHold()
  th.update(0.10, **NC, long_active=True)
  th.update(0.10, **NC, long_active=False)
  check("inactive drops state", th.last_sign == 0 and th.pending_t == 0.0)
  check("inactive is a pass-through", th.update(-0.9, **NC, long_active=False) == -0.9)

  # --- the 14:35 chatter: +0.05 held, 0.25 s of -0.02, back to +0.05 ------------------
  th = ThrottleHold()
  out = [th.update(0.05, **NC, long_active=True) for _ in range(20)]     # 1 s of throttle on
  glitch = [th.update(-0.02, **NC, long_active=True) for _ in range(5)]  # 0.25 s dip
  after = [th.update(0.05, **NC, long_active=True) for _ in range(5)]
  check(f"chatter: output never <= 0 through the dip (min {min(glitch):+.3f})",
        min(glitch) > 0.0)
  check(f"chatter: the dip holds the PRE-GLITCH value, not 0 "
        f"(held {glitch[0]:+.3f} vs pre {out[-1]:+.3f})",
        all(abs(g - out[-1]) < 1e-12 for g in glitch))
  check(f"chatter: recovers cleanly afterwards ({after[-1]:+.3f})", after[-1] == 0.05)

  # --- a HELD negative is believed after T_HOLD ---------------------------------------
  th = ThrottleHold()
  th.update(0.05, **NC, long_active=True)
  held = [th.update(-0.09, **NC, long_active=True) for _ in range(8)]
  check(f"held negative: still positive through frame {HOLD_N} "
        f"({[f'{h:+.2f}' for h in held[:HOLD_N]]})",
        all(h > 0.0 for h in held[:HOLD_N - 1]))
  check(f"held negative: accepted from frame {HOLD_N + 1} on ({held[-1]:+.3f})",
        held[-1] == -0.09)

  # --- ABANDON is never delayed --------------------------------------------------------
  th = ThrottleHold()
  th.update(0.10, **NC, long_active=True)
  check("a = -0.50 the frame after +0.10 is -0.50 THAT FRAME",
        th.update(-0.50, **NC, long_active=True) == -0.50)

  th = ThrottleHold()
  th.update(0.40, **NC, long_active=True)
  check(f"exactly ABANDON ({ABANDON}) is also immediate",
        th.update(ABANDON, **NC, long_active=True) == ABANDON)

  # --- never emit 0 / sub-epsilon while holding throttle on ----------------------------
  th = ThrottleHold()
  th.update(0.20, **NC, long_active=True)
  check(f"while last_sign is +, a = +0.01 emits >= EPSILON "
        f"({th.update(0.01, **NC, long_active=True):+.3f})",
        th.update(0.01, **NC, long_active=True) >= EPSILON - 1e-12)

  th = ThrottleHold()
  th.update(0.20, **NC, long_active=True)
  zeros = [th.update(0.0, **NC, long_active=True) for _ in range(HOLD_N + 3)]
  check(f"a = 0.0 is debounced like a glitch, not taken as coast immediately "
        f"(first {zeros[0]:+.3f})", zeros[0] == 0.20)
  check(f"...and after T_HOLD it IS accepted as coast ({zeros[-1]:+.3f})", zeros[-1] == 0.0)

  # ================= LAYER C =========================================================
  HR = dict(v_ego=62 / 3.6, v_cruise=110 / 3.6)     # ~13 m/s of headroom
  th = ThrottleHold()
  out = [th.update(-0.13, **HR, long_active=True) for _ in range(10)]
  check(f"C: mild -0.13 with big headroom never goes negative (min {min(out):+.3f})",
        min(out) >= 0.0)

  th = ThrottleHold()
  check(f"C: -0.50 with big headroom is still -0.50 this frame",
        th.update(-0.50, **HR, long_active=True) == -0.50)

  # headroom below the threshold -> C stays out; A still debounces off the first frame
  NEAR = dict(v_ego=110 / 3.6 - (MIN_HEADROOM - 0.2), v_cruise=110 / 3.6)
  th = ThrottleHold()
  first = th.update(-0.13, **NEAR, long_active=True)
  check(f"C: below {MIN_HEADROOM * 3.6:.0f} km/h headroom, -0.13 is NOT clamped by C "
        f"({first:+.3f})", first == -0.13)

  th = ThrottleHold()
  check(f"C: does not fire when v_cruise ~0 (forceDecel) "
        f"({th.update(-0.13, v_ego=0.5, v_cruise=0.0, long_active=True):+.3f})",
        th.update(-0.13, v_ego=0.5, v_cruise=0.0, long_active=True) == -0.13)

  # C must not ADD acceleration -- only decline to cut it
  th = ThrottleHold()
  th.update(0.5, **HR, long_active=True)
  o = th.update(0.5, **HR, long_active=True)
  check(f"C never raises an already-positive request ({o:+.3f} == +0.500)", o == 0.5)

  # ================= REPLAY-SHAPED ===================================================
  # 15:28:39 (relaxed, uphill): +0.07, 4 frames of -0.017, then +0.14
  th = ThrottleHold()
  UP = dict(v_ego=70 / 3.6, v_cruise=110 / 3.6)
  seq = [0.066] + [-0.015] * 4 + [0.025, 0.116]
  o = [th.update(a, **UP, long_active=True) for a in seq]
  check(f"15:28:39 replay: no sign change reaches the SCC (min {min(o):+.3f})", min(o) >= 0.0)

  # 14:35:50 (aggressive, at set): a -0.03 cruise ripple vs e2e +0.07. The ripple is inside
  # BAND so B clips it to 0; min() is then 0, and LAYER A is what stops the throttle clicking.
  th = ThrottleHold()
  vset = 110 / 3.6
  over = vset + 0.1 / 3.6
  cru = [th.deadband_cruise_accel(-0.03, over, vset) for _ in range(5)]
  check(f"14:35:50 replay: the ripple is clipped to 0, not passed as negative ({cru[0]:+.3f})",
        all(c == 0.0 for c in cru))
  th.update(0.07, v_ego=vset, v_cruise=vset, long_active=True)       # throttle on first
  o = [th.update(min(c, 0.07), v_ego=vset, v_cruise=vset, long_active=True) for c in cru]
  check(f"14:35:50 replay: layer A holds the throttle through it (min {min(o):+.3f})",
        min(o) > 0.0)

  print("\nALL PASS" if ok else "\nFAILURES PRESENT")
  return 0 if ok else 1


if __name__ == "__main__":
  sys.exit(main())
