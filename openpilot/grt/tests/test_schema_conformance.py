#!/usr/bin/env python3
"""Assert every cereal field the fork reads ACTUALLY EXISTS in the real schema.

    python3 openpilot/grt/tests/test_schema_conformance.py

Why this exists
---------------
The other test suites stub cereal with SimpleNamespace, so they validate control logic against
*our own assumptions* about field names. That is a blind spot: scc_map.py was ported from
sunnypilot and used `radarState.leadOne.status`, which does not exist in this openpilot (it is
`.present`). Every stubbed test passed, and on the car the controller raised AttributeError on
every single frame - 38,300 exceptions in one drive. The fail-safe caught it, so the car drove
normally, but the feature was silently a complete no-op.

This test loads the actual log.capnp with pycapnp and checks the field names for real, so that
class of bug cannot reach the car again. It needs pycapnp (dev-only on the Pi5; already present
on device).
"""
import pathlib
import sys

REPO = pathlib.Path(__file__).resolve().parents[3]
CEREAL = REPO / "openpilot" / "cereal"

# Every cereal field openpilot/grt reads, as (message, dotted path).
# Keep this in sync with scc_map.update_calculations() and set_speed.SetSpeedLimitTracker.
REQUIRED = [
  ("mapdOut", "mapCurveSpeed"),
  ("mapdOut", "speedLimitSuggestedSpeed"),
  ("mapdOut", "nextSpeedLimit"),
  ("mapdOut", "nextSpeedLimitDistance"),
  ("mapdOut", "nextHazard"),
  ("mapdOut", "nextHazardDistance"),
  ("radarState", "leadOne.present"),
  ("radarState", "leadOne.dRel"),
  ("radarState", "leadTwo.present"),
  ("radarState", "leadTwo.dRel"),
  ("carState", "aEgo"),
  ("carState", "vEgo"),
  ("carControl", "enabled"),
  ("carControl", "cruiseControl.override"),
  # set_speed.py (hook 3, runs in card)
  ("mapdOut", "speedLimit"),
  ("mapdOut", "tileLoaded"),
  ("mapdOut", "waySelectionType"),
  ("carState", "buttonEvents"),
  ("carState", "vCruise"),
  ("carState", "vCruiseCluster"),
  # grtSetSpeedState — the fork's own card -> selfdrived channel (hooks.set_speed_alerts)
  ("grtSetSpeedState", "pending"),
  ("grtSetSpeedState", "pendingLimit"),
  ("grtSetSpeedState", "secondsLeft"),
  ("grtSetSpeedState", "setSpeed"),
  ("grtSetSpeedState", "tracking"),
]

# Union discriminants are POSITIONAL, not the @N ordinal, and they are what actually goes on the
# wire. The prebuilt Go mapd binary has these compiled in, so they must never move — renaming
# customReserved16 in place for grtSetSpeedState must not disturb them.
REQUIRED_DISCRIMINANTS = {
  "mapdExtendedOut": 141,
  "mapdIn": 142,
  "mapdOut": 143,
  "grtSetSpeedState": 140,
}


def resolve(schema, dotted: str):
  """Walk a dotted field path through a capnp schema, returning (ok, detail).

  Only descends into intermediate parts: calling .schema on a leaf (Float32, Bool, ...) raises
  "Schema type is unknown", so the final component is checked by membership alone.
  """
  parts = dotted.split(".")
  cur = schema
  for i, part in enumerate(parts):
    fields = getattr(cur, "fields", None)
    if fields is None or part not in fields:
      have = sorted(cur.fieldnames) if hasattr(cur, "fieldnames") else []
      return False, f"missing {part!r}; available: {', '.join(have[:14])}"
    if i < len(parts) - 1:          # only descend for intermediate structs
      cur = fields[part].schema
  return True, "ok"


def main() -> int:
  try:
    import capnp
  except ImportError:
    print("pycapnp not installed - cannot verify schema conformance")
    return 1

  capnp.remove_import_hook()
  log = capnp.load(str(CEREAL / "log.capnp"),
                   imports=[str(REPO / "opendbc_repo" / "opendbc" / "car"),
                            str(CEREAL), str(REPO)])
  event = log.Event.schema

  failures = []
  for msg, path in REQUIRED:
    if msg not in event.fields:
      failures.append((msg, path, f"Event has no union member {msg!r}"))
      continue
    ok, detail = resolve(event.fields[msg].schema, path)
    status = "PASS" if ok else "**FAIL**"
    print(f"  {status:9s} {msg}.{path}")
    if not ok:
      failures.append((msg, path, detail))

  for name, want in REQUIRED_DISCRIMINANTS.items():
    got = event.fields[name].proto.discriminantValue if name in event.fields else None
    ok = got == want
    print(f"  {'PASS' if ok else '**FAIL**':9s} {name} wire discriminant == {want}")
    if not ok:
      failures.append((name, "discriminant", f"expected {want}, got {got}"))

  print()
  if failures:
    print(f"{len(failures)} FIELD(S) DO NOT EXIST — this WILL raise on the car:")
    for msg, path, detail in failures:
      print(f"  {msg}.{path}: {detail}")
    return 1
  print(f"{len(REQUIRED)}/{len(REQUIRED)} fields exist in the real schema")
  return 0


if __name__ == "__main__":
  sys.exit(main())
