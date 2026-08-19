#!/usr/bin/env python3
"""Tests for the TEMPORARY cruise diagnostic recorder (openpilot/grt/cruise_log.py).

    python3 openpilot/grt/tests/test_cruise_log.py

This runs inside plannerd at 20 Hz, so the properties that matter are not about the data:
  * it NEVER raises, whatever the filesystem does,
  * it latches OFF permanently on any failure rather than retrying every frame,
  * it is buffered, so the 20 Hz loop does not wait on the filesystem each tick,
  * it stops at a size cap rather than filling the device.
"""
import pathlib
import sys
import tempfile
import types

ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

import openpilot.grt.cruise_log as CL   # noqa: E402


def main():
  ok = True

  def check(name, cond):
    nonlocal ok
    print(('  PASS  ' if cond else '  FAIL  ') + name)
    ok = ok and bool(cond)

  with tempfile.TemporaryDirectory() as d:
    reg = types.ModuleType('openpilot.grt.registry')
    reg.GRT_CONFIG_DIR = d
    sys.modules['openpilot.grt.registry'] = reg

    c = CL.CruiseLog()
    check("constructs and creates the file", c.path is not None and not c.dead)

    for i in range(CL._FLUSH_N - 1):
      c.record(30.0, 29.5, 0.5)
    size_before = pathlib.Path(c.path).stat().st_size
    check(f"buffers rather than writing every tick ({len(c.buf)} rows held)",
          len(c.buf) == CL._FLUSH_N - 1)
    c.record(30.0, 29.5, 0.5)
    check("flushes on the Nth row", len(c.buf) == 0 and
          pathlib.Path(c.path).stat().st_size > size_before)

    body = pathlib.Path(c.path).read_text().strip().split('\n')
    check(f"header + {CL._FLUSH_N} rows written ({len(body)} lines)",
          len(body) == CL._FLUSH_N + 1 and body[0].startswith('wall_time'))
    check(f"row shape is right ({body[1]})", len(body[1].split(',')) == 4)

    # a filesystem that refuses writes must not propagate
    c2 = CL.CruiseLog()
    c2.path = '/proc/definitely/not/writable/x.csv'
    try:
      for _ in range(CL._FLUSH_N + 5):
        c2.record(1.0, 2.0, 3.0)
      raised = False
    except Exception:
      raised = True
    check("an unwritable path never raises into plannerd", not raised)
    check("...and latches off instead of retrying every frame", c2.dead)

    # size cap
    c3 = CL.CruiseLog()
    c3.bytes = CL._MAX_BYTES
    for _ in range(CL._FLUSH_N):
      c3.record(1.0, 2.0, 3.0)
    check("stops at the size cap rather than filling the device", c3.dead)

    # a dead recorder is inert
    n = len(c3.buf)
    c3.record(1.0, 2.0, 3.0)
    check("a dead recorder does nothing", len(c3.buf) == n)

  print("\nALL PASS" if ok else "\nFAILURES PRESENT")
  return 0 if ok else 1


if __name__ == "__main__":
  sys.exit(main())
