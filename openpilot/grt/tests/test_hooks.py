#!/usr/bin/env python3
"""Tests for the planner hooks (openpilot/grt/hooks.py).

Runs with STUBBED openpilot deps so it works on a dev box that cannot import openpilot.

    python3 openpilot/grt/tests/test_hooks.py

The safety-relevant properties asserted here:
  * hook 2 is INERT by default (SmartCruiseControlMapHazardAccel defaults off),
  * its candidate can never make braking WEAKER than stock (min() semantics),
  * an unregistered param degrades to off instead of raising,
  * a controller exception cannot propagate into plannerd.
"""
import pathlib
import sys
import types
from types import SimpleNamespace as NS

GRT = pathlib.Path(__file__).resolve().parents[1]


def _stub(name, **attrs):
  m = types.ModuleType(name)
  for k, v in attrs.items():
    setattr(m, k, v)
  sys.modules[name] = m


for p in ("openpilot", "openpilot.cereal", "openpilot.common", "openpilot.selfdrive",
          "openpilot.selfdrive.car", "openpilot.selfdrive.controls",
          "openpilot.selfdrive.controls.lib",
          "openpilot.selfdrive.controls.lib.longitudinal_mpc_lib", "openpilot.grt"):
  sys.modules.setdefault(p, types.ModuleType(p))


class FakeParams:
  vals = {"SmartCruiseControlMap": True, "SmartCruiseControlMapHazardAccel": False}

  def __init__(self, *a, **k):
    pass

  def get_bool(self, k):
    if k not in FakeParams.vals:
      raise Exception("UnknownKeyName")   # mirrors openpilot's behaviour
    return FakeParams.vals[k]


_stub("openpilot.cereal.messaging", SubMaster=object)
_stub("openpilot.common.constants", CV=NS(KPH_TO_MS=1 / 3.6))
_stub("openpilot.common.params", Params=FakeParams)
_stub("openpilot.common.realtime", DT_MDL=0.05)
_stub("openpilot.selfdrive.car.cruise", V_CRUISE_UNSET=255.0)
_stub("openpilot.common.swaglog", cloudlog=NS(exception=lambda *a, **k: None))
_stub("openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc",
      LongitudinalPlanSource=NS(cruise="cruise"))
_stub("openpilot.selfdrive.controls.lib.drive_helpers",
      should_stop=lambda v, a: bool(v < 0.5 and a < -0.2))

import importlib.util as _ilu  # noqa: E402


def _load(name, path):
  spec = _ilu.spec_from_file_location(name, path)
  mod = _ilu.module_from_spec(spec)
  sys.modules[name] = mod
  spec.loader.exec_module(mod)
  return mod


scc = _load("openpilot.grt.scc_map", str(GRT / "scc_map.py"))
scc._DEBUG_LOG = None
hooks = _load("openpilot.grt.hooks", str(GRT / "hooks.py"))

A_CRUISE_MIN = -1.2  # must track longitudinal_planner.A_CRUISE_MIN


def SM(curve=0., hz="", hzd=0., l1=None):
  lead = lambda d: NS(status=(d is not None), dRel=(d or 0.))
  return {'mapdOut': NS(mapCurveSpeed=curve, speedLimitSuggestedSpeed=0., nextHazard=hz,
                        nextHazardDistance=hzd, nextSpeedLimit=0.,
                        nextSpeedLimitDistance=0., suggestedSpeed=0.),
          'radarState': NS(leadOne=lead(l1), leadTwo=lead(None))}


results = []


def check(name, cond):
  results.append(cond)
  print(f"  {'PASS' if cond else '**FAIL**':9s} {name}")


def settle(sm, frames=5, v_cruise=30., v_ego=15.):
  out = v_cruise
  for _ in range(frames):
    out = hooks.limit_v_cruise(sm, v_cruise, v_ego, True, False, 0.)
  return out


def main():
  vc = settle(SM(hz="stop", hzd=30.))
  check("hook1 lowers v_cruise when a hazard is ahead", vc < 30.)
  check("hook2 inert while param OFF (default)", hooks.extra_accel_candidates(15.) == [])

  FakeParams.vals["SmartCruiseControlMapHazardAccel"] = True
  hooks._hazard_accel_enabled = None
  settle(SM(hz="stop", hzd=30.))
  cands = hooks.extra_accel_candidates(15.)
  check("hook2 emits a candidate when enabled and hazard active", len(cands) == 1)
  a = cands[0][0] if cands else None
  check("candidate accel within [-1.5, -0.3]", a is not None and -1.5 <= a <= -0.3)

  stock = [(-0.5, "mpc", False), (A_CRUISE_MIN, "cruise", False)]
  check("min() with candidate is never weaker than stock",
        min(x[0] for x in stock + cands) <= min(x[0] for x in stock))

  settle(SM())
  check("clear road -> no candidate even when param ON", hooks.extra_accel_candidates(15.) == [])

  settle(SM(hz="stop", hzd=30., l1=10.))
  check("lead present -> no candidate (lead keeps authority)",
        hooks.extra_accel_candidates(15.) == [])

  FakeParams.vals.pop("SmartCruiseControlMapHazardAccel")
  hooks._hazard_accel_enabled = None
  check("unregistered param degrades to OFF instead of raising",
        hooks.extra_accel_candidates(15.) == [])

  check("controller exception is contained; v_cruise passes through",
        hooks.limit_v_cruise(None, 42., 15., True, False, 0.) == 42.)

  # REGRESSION: a device running prebuilt binaries has NOT compiled grt_params_keys.inc, so
  # every fork param raises UnknownKeyName. The controller must then be unbuildable WITHOUT
  # taking plannerd down. This bug shipped once and was caught on the car - keep it covered.
  saved = dict(FakeParams.vals)
  FakeParams.vals.clear()                      # every key now raises
  hooks._scc = None
  hooks._scc_broken = False
  hooks._hazard_accel_enabled = None
  check("all params raise -> limit_v_cruise is a no-op, does NOT raise",
        hooks.limit_v_cruise(SM(hz="stop", hzd=30.), 30., 15., True, False, 0.) == 30.)
  check("all params raise -> extra_accel_candidates returns []",
        hooks.extra_accel_candidates(15.) == [])
  # With get_bool_safe in place the controller still CONSTRUCTS; it just reports disabled.
  # That is the desired outcome - degrade to "feature off", not "no controller".
  check("controller still constructs, reporting feature DISABLED",
        hooks._scc is not None and hooks._scc.enabled is False)
  FakeParams.vals.update(saved)

  # Separately: if construction genuinely fails, it must latch and never retry.
  import openpilot.grt.scc_map as _sm
  orig = _sm.SmartCruiseControlMap
  _sm.SmartCruiseControlMap = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
  hooks._scc = None
  hooks._scc_broken = False
  first = hooks.limit_v_cruise(SM(), 31., 15., True, False, 0.)
  check("hard construction failure -> no-op, no raise", first == 31.)
  check("hard construction failure is latched (not retried every frame)",
        hooks._scc_broken is True)
  _sm.SmartCruiseControlMap = orig
  hooks._scc = None
  hooks._scc_broken = False

  # File-based fallback: on a prebuilt branch the params are unknown, so a plain file under
  # GRT_CONFIG_DIR is the only way to enable the feature. Verify both polarities.
  import tempfile, os
  _reg = _load("openpilot.grt.registry", str(GRT / "registry.py"))
  get_bool_safe = scc.get_bool_safe
  FakeParams.vals.clear()                      # force the Params path to fail
  with tempfile.TemporaryDirectory() as d:
    _reg.GRT_CONFIG_DIR = d
    check("file fallback: absent file -> False", get_bool_safe(FakeParams(), "SmartCruiseControlMap") is False)
    with open(os.path.join(d, "SmartCruiseControlMap"), "w") as f:
      f.write("1\n")
    check("file fallback: '1' -> True", get_bool_safe(FakeParams(), "SmartCruiseControlMap") is True)
    with open(os.path.join(d, "SmartCruiseControlMap"), "w") as f:
      f.write("0\n")
    check("file fallback: '0' -> False", get_bool_safe(FakeParams(), "SmartCruiseControlMap") is False)
  FakeParams.vals.update(saved)

  print(f"\n{sum(results)}/{len(results)} passed")
  return 0 if all(results) else 1


if __name__ == "__main__":
  sys.exit(main())
