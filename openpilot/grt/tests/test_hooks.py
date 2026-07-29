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
_stub("openpilot.common.constants", CV=NS(KPH_TO_MS=1 / 3.6, KPH_TO_MPH=1 / 1.609344))


class FakeAlert:
  def __init__(self, t1, t2, status, size, priority, visual, audible, duration, creation_delay=0.):
    self.alert_text_1, self.alert_text_2 = t1, t2
    self.audible_alert = audible
    self.priority = priority
    self.alert_type = ""
    self.event_type = None


for _p in ("openpilot.selfdrive.selfdrived",):
  sys.modules.setdefault(_p, types.ModuleType(_p))
_stub("openpilot.selfdrive.selfdrived.events", Alert=FakeAlert,
      AlertStatus=NS(normal=0), AlertSize=NS(mid=2), Priority=NS(LOW=2),
      AudibleAlert=NS(prompt="prompt"), VisualAlert=NS(none=0))
_stub("openpilot.common.params", Params=FakeParams)
_stub("openpilot.common.realtime", DT_MDL=0.05, DT_CTRL=0.01)


class ButtonType:
  accelCruise = 1
  decelCruise = 2


_stub("openpilot.selfdrive.car.cruise", V_CRUISE_UNSET=255.0, V_CRUISE_MIN=8, V_CRUISE_MAX=145,
      ButtonType=ButtonType)
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


_load("openpilot.grt.registry", str(GRT / "registry.py"))
scc = _load("openpilot.grt.scc_map", str(GRT / "scc_map.py"))
scc._DEBUG_LOG = None
ss = _load("openpilot.grt.set_speed", str(GRT / "set_speed.py"))
ss._DEBUG_LOG = None
hooks = _load("openpilot.grt.hooks", str(GRT / "hooks.py"))

A_CRUISE_MIN = -1.2  # must track longitudinal_planner.A_CRUISE_MIN


def SM(curve=0., hz="", hzd=0., l1=None):
  # field names MUST match the real cereal schema (radarState.LeadData.present)
  lead = lambda d: NS(present=(d is not None), dRel=(d or 0.))
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


class FakeHelper:
  """Stand-in for VCruiseHelper — only what hook 3 touches."""

  def __init__(self, v_cruise=80.0, pcm=False):
    self.CP = NS(pcmCruise=pcm)
    self.v_cruise_kph = v_cruise
    self.v_cruise_cluster_kph = v_cruise


class SM3:
  """Minimal SubMaster carrying mapdOut (+ grtSetSpeedState for the alert hook)."""

  def __init__(self, limit_kph=60.0, way="current", alive=True, state=None):
    self.msg = NS(speedLimit=limit_kph / 3.6, tileLoaded=True, waySelectionType=way)
    self.state = state
    self.alive = {'mapdOut': alive, 'grtSetSpeedState': state is not None}
    self.valid = {'mapdOut': True, 'grtSetSpeedState': True}

  def __getitem__(self, k):
    return self.state if k == 'grtSetSpeedState' else self.msg


def _reset_hook3():
  hooks._set_speed = None
  hooks._set_speed_broken = False
  hooks._exc_counts.clear()


def test_hook3():
  """Hook 3 runs in `card`, which publishes carState — if it dies, everything downstream dies.
  Same guarantees as hooks 1/2, asserted on the shim rather than on the tracker."""
  saved = dict(FakeParams.vals)
  FakeParams.vals["SmartCruiseControlSetSpeed"] = True
  CS = NS(buttonEvents=[])

  # the engage seed writes BOTH fields — the one property the tracker's own tests cannot see
  _reset_hook3()
  h = FakeHelper(105.0)
  sm = SM3(60.0)
  hooks.track_set_speed(sm, CS, h, True, True)          # engage edge
  check("hook3 seeds and writes v_cruise_kph AND v_cruise_cluster_kph",
        h.v_cruise_kph == 60.0 and h.v_cruise_cluster_kph == 60.0)

  # no decision -> neither field touched
  _reset_hook3()
  h = FakeHelper(80.0)
  hooks.track_set_speed(SM3(60.0), CS, h, True)
  check("hook3 leaves both fields alone when there is no decision",
        h.v_cruise_kph == 80.0 and h.v_cruise_cluster_kph == 80.0)

  # a raising tracker must not propagate into card
  _reset_hook3()
  hooks._set_speed = NS(update=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
  h = FakeHelper(80.0)
  hooks.track_set_speed(SM3(60.0), CS, h, True)
  check("hook3 swallows a tracker exception (card must never die)",
        h.v_cruise_kph == 80.0 and h.v_cruise_cluster_kph == 80.0)
  check("hook3 counts the exception instead of logging every frame",
        hooks._exc_counts.get("set_speed update") == 1)
  for _ in range(50):
    hooks.track_set_speed(SM3(60.0), CS, h, True)
  check("hook3 exception logging is rate-limited (38,300-in-one-drive lesson)",
        hooks._exc_counts["set_speed update"] == 51)

  # hard construction failure must latch, exactly like hooks 1/2
  _reset_hook3()
  orig = ss.SetSpeedLimitTracker
  ss.SetSpeedLimitTracker = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
  h = FakeHelper(80.0)
  hooks.track_set_speed(SM3(60.0), CS, h, True)
  check("hook3 hard construction failure -> no-op, no raise", h.v_cruise_kph == 80.0)
  check("hook3 construction failure is latched (not retried every frame)",
        hooks._set_speed_broken is True)
  ss.SetSpeedLimitTracker = orig

  # PCM cars read the set speed off CAN; adopting there would flap
  _reset_hook3()
  h = FakeHelper(80.0, pcm=True)
  for _ in range(int(ss.LIMIT_STABLE_S / 0.01) + 2):
    hooks.track_set_speed(SM3(60.0), CS, h, True, True)
  check("hook3 is inert on a pcmCruise car", h.v_cruise_kph == 80.0)

  test_hook4()

  _reset_hook3()
  FakeParams.vals.clear()
  FakeParams.vals.update(saved)


def test_hook4():
  """The confirmation alert. Built as a plain Alert with a fork-owned alert_type string, so no
  EventName enumerant and no schema change — which is what makes it work on a prebuilt branch."""
  no_state = SM3(60.0)
  check("hook4 returns [] when card is not publishing the state",
        hooks.set_speed_alerts(no_state, True) == [])

  idle = SM3(60.0, state=NS(pending=False, pendingLimit=0.0, secondsLeft=0.0,
                            setSpeed=100.0, tracking=True))
  check("hook4 returns [] when nothing is pending", hooks.set_speed_alerts(idle, True) == [])

  pend = SM3(60.0, state=NS(pending=True, pendingLimit=80.0, secondsLeft=7.5,
                            setSpeed=120.0, tracking=True))
  alerts = hooks.set_speed_alerts(pend, True)
  check("hook4 emits exactly one alert while pending", len(alerts) == 1)
  a = alerts[0]
  check("hook4 alert names the limit in km/h when metric",
        "80" in a.alert_text_1 and "km/h" in a.alert_text_1)
  check("hook4 alert tells the driver what to press", "RES" in a.alert_text_2)
  check("hook4 alert makes a sound", a.audible_alert == "prompt")
  check("hook4 alert_type is fork-owned (AlertManager keys on it, not on an EventName)",
        a.alert_type == "grtSetSpeedPending")
  check("hook4 alert is ET.WARNING (survives clear_event_types while engaged)",
        a.event_type == "warning")

  imperial = hooks.set_speed_alerts(pend, False)
  check("hook4 converts to mph when the driver is imperial",
        "50" in imperial[0].alert_text_1 and "mph" in imperial[0].alert_text_1)

  broken = NS(alive={'grtSetSpeedState': True})
  check("hook4 cannot raise into selfdrived", hooks.set_speed_alerts(broken, True) == [])


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

  test_hook3()

  print(f"\n{sum(results)}/{len(results)} passed")
  return 0 if all(results) else 1


if __name__ == "__main__":
  sys.exit(main())
