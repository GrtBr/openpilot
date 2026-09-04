#!/usr/bin/env python3
"""Tests for the vision-lead flicker filter (openpilot/grt/lead_filter.py).

    python3 openpilot/grt/tests/test_lead_filter.py

lead_filter.py deliberately has NO openpilot imports at module level (the UI process must be able
to import it without dragging in plannerd's dependency graph), so unlike the other suites this
one needs no stubbing at all.

Covers the two wiring bugs caught while integrating, both of which passed a naive smoke test:
leadOne and leadTwo sharing one filter instance, and the filter never seeing `present=False` so
it smoothed across a dropout instead of resetting.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[3]))
from openpilot.grt.lead_filter import (  # noqa: E402
    DT, Hampel, LeadFilter, RangeRate, filtered_dRel, _display,
    HAMPEL_N, HAMPEL_K, HAMPEL_FLOOR, ALPHA, BETA)

_fail = 0


def check(label, cond):
  global _fail
  print(f"  {'PASS' if cond else 'FAIL'}  {label}")
  if not cond:
    _fail += 1


print("constants match the validated tuning (captains_log 2026-09-03)")
check("hampel window 7", HAMPEL_N == 7)
check("hampel k 3.0", HAMPEL_K == 3.0)
check("hampel floor 1.5 m (above the 1.77 m/frame fastest real motion)", HAMPEL_FLOOR == 1.5)
check("alpha 0.20", ALPHA == 0.20)
check("beta 0.008", BETA == 0.008)
check("DT matches DT_MDL", DT == 0.05)

print("\nHampel rejects impulses and passes clean samples untouched")
h = Hampel()
for z in (100.0, 100.2, 99.8, 100.1, 99.9, 100.3, 100.0):
  h.update(z)
check("a clean sample is returned unchanged", h.update(100.15) == 100.15)
out = h.update(70.0)
check("a 30 m impulse is replaced by the local median", abs(out - 100.0) < 1.5)
check("...and the impulse never enters the output", out != 70.0)

print("\nsustained step is followed (a real lead change must NOT be rejected forever)")
h2 = Hampel()
for z in (100.0,) * 7:
  h2.update(z)
seen = [h2.update(70.0) for _ in range(7)]
check("after a sustained step the filter tracks to the new level", abs(seen[-1] - 70.0) < 1.5)

print("\nLeadFilter position/velocity behaviour")
f = LeadFilter()
x0, v0 = f.update(True, 100.0)
check("first sample seeds position at the measurement", x0 == 100.0)
check("first sample seeds velocity at zero", v0 == 0.0)
for _ in range(40):
  x, v = f.update(True, 100.0)
check("steady lead -> velocity stays ~0", abs(v) < 0.5)
prev = x
for k in range(1, 41):                       # true 10 m/s closure
  x, v = f.update(True, 100.0 - 10.0 * k * DT)
check("10 m/s closure -> negative (closing) velocity", v < -5.0)
check("10 m/s closure -> position follows down", x < 90.0)

print("\nabsence resets, so a reacquired lead is never smoothed from a stale one")
xa, va = f.update(False, 0.0)
check("absent -> position None", xa is None)
check("absent -> velocity 0.0", va == 0.0)
xb, _ = f.update(True, 40.0)
check("reacquired far from the old value -> seeds at the new measurement", xb == 40.0)

print("\nfiltered_dRel: per-lead isolation and raw fallback")
_display.clear()
filtered_dRel(0, True, 100.0)
filtered_dRel(1, True, 40.0)
check("leadOne and leadTwo keep separate state",
      abs(filtered_dRel(0, True, 100.0) - 100.0) < 1.0 and abs(filtered_dRel(1, True, 40.0) - 40.0) < 1.0)
check("absent lead returns the raw value it was handed", filtered_dRel(0, False, 0.0) == 0.0)
check("bad input degrades to raw rather than raising", filtered_dRel(0, True, float("nan")) is not None)

print("\nlast_dRel: second consumer reads the frame's value WITHOUT advancing the filter")
_display.clear()
from openpilot.grt.lead_filter import last_dRel
a1 = filtered_dRel(0, True, 100.0)
r1 = last_dRel(0, 0.0)
r2 = last_dRel(0, 0.0)
check("last_dRel returns what filtered_dRel just produced", r1 == a1)
check("repeated last_dRel is stable (does not advance)", r2 == r1)
# advancing once vs twice must differ -- proves last_dRel is genuinely not advancing
fa_ = LeadFilter(); fb = LeadFilter()
for z in (100.0, 90.0, 80.0):
  fa_.update(True, z)
  fb.update(True, z); fb.update(True, z)          # double-stepped
check("double-advancing a filter really does change it (guard is meaningful)",
      abs(fa_.rr.x - fb.rr.x) > 1e-9)
check("last_dRel falls back when nothing cached", last_dRel(7, 42.0) == 42.0)

print("\nthe filter must never invent motion the raw signal does not have")
f2 = LeadFilter()
xs = [f2.update(True, 80.0)[0] for _ in range(60)]
check("constant input -> constant output (no drift)", abs(xs[-1] - 80.0) < 0.01)

print("\nRangeRate is independent of far_lead's deployed filter")
rr = RangeRate()
check("defaults are the new tuning, not stock's (0.10, 0.003)", rr.alpha == 0.20 and rr.beta == 0.008)

print(f"\n{'ALL PASS' if _fail == 0 else str(_fail) + ' FAILURES'}")
sys.exit(1 if _fail else 0)
