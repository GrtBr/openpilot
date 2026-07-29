#!/usr/bin/env python3
"""Tests for the set-speed tracker (openpilot/grt/set_speed.py).

    python3 openpilot/grt/tests/test_set_speed.py

Runs with STUBBED openpilot deps so it works on a dev box that cannot import openpilot.
Field names in the stubs MIRROR THE REAL SCHEMA — see tests/test_schema_conformance.py, which
checks that claim against the actual log.capnp. (A stub that invented `lead.status` is exactly
how the mapd controller shipped a silent 38,300-exception no-op to the car.)

Safety-relevant properties asserted here:
  * INERT by default — SmartCruiseControlSetSpeed defaults off, and an unregistered param
    degrades to off rather than raising;
  * never acts while disengaged, while the set speed is UNSET, or on an untrusted mapd fix
    (tiles not loaded / waySelectionType fail);
  * edge-triggered: one decision per limit VALUE, so a driver override is never re-adopted;
  * a limit outside the plausible band, or lost entirely, changes nothing;
  * the result is always inside upstream's [V_CRUISE_MIN, V_CRUISE_MAX].
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
          "openpilot.selfdrive.car", "openpilot.grt"):
  sys.modules.setdefault(p, types.ModuleType(p))


class FakeParams:
  vals: dict = {}

  def __init__(self, *a, **k):
    pass

  def get_bool(self, k):
    if k not in FakeParams.vals:
      raise Exception("UnknownKeyName")   # mirrors openpilot's behaviour on a prebuilt branch
    return FakeParams.vals[k]


class ButtonType:
  accelCruise = 1
  decelCruise = 2
  cancel = 3


_stub("openpilot.cereal.messaging", SubMaster=object)
_stub("openpilot.common.constants", CV=NS(KPH_TO_MS=1 / 3.6))
_stub("openpilot.common.params", Params=FakeParams)
_stub("openpilot.common.realtime", DT_MDL=0.05, DT_CTRL=0.01)
_stub("openpilot.common.swaglog", cloudlog=NS(exception=lambda *a, **k: None))
_stub("openpilot.selfdrive.car.cruise", V_CRUISE_UNSET=255.0, V_CRUISE_MIN=8, V_CRUISE_MAX=145,
      ButtonType=ButtonType)

import importlib.util as _ilu  # noqa: E402


def _load(name, path):
  spec = _ilu.spec_from_file_location(name, path)
  mod = _ilu.module_from_spec(spec)
  sys.modules[name] = mod
  spec.loader.exec_module(mod)
  return mod


_load("openpilot.grt.registry", str(GRT / "registry.py"))
_load("openpilot.grt.scc_map", str(GRT / "scc_map.py"))
ss = _load("openpilot.grt.set_speed", str(GRT / "set_speed.py"))

ss._DEBUG_LOG = None          # no file writes during tests
STABLE = int(ss.LIMIT_STABLE_S / 0.01)


# ------------------------------------------------------------------------------------------
# fakes


class FakeSM:
  """Minimal SubMaster: mapdOut only, with the real field names."""

  def __init__(self, speed_limit_ms=0.0, tile_loaded=True, way="current", alive=True):
    self.msg = NS(speedLimit=speed_limit_ms, tileLoaded=tile_loaded, waySelectionType=way)
    self.alive = {'mapdOut': alive}
    self.valid = {'mapdOut': True}

  def __getitem__(self, k):
    assert k == 'mapdOut'
    return self.msg


def button(btn_type, pressed):
  return NS(type=NS(raw=btn_type), pressed=pressed)


def fake_cs(buttons=()):
  return NS(buttonEvents=list(buttons))


def kph_to_ms(kph):
  return kph / 3.6


def make_tracker(enabled=True):
  FakeParams.vals = {"SmartCruiseControlSetSpeed": enabled} if enabled is not None else {}
  t = ss.SetSpeedLimitTracker()
  return t


def run(tracker, sm, v_cruise, frames, CS=None, enabled=True):
  """Drive the tracker `frames` times; return the final set speed."""
  out = v_cruise
  for _ in range(frames):
    out = tracker.update(sm, CS if CS is not None else fake_cs(), out, enabled)
  return out


# ------------------------------------------------------------------------------------------
# tests

results = []


def check(name, cond, detail=""):
  results.append((name, bool(cond), detail))
  print(f"  {'PASS' if cond else '**FAIL**':9s} {name}" + (f"   {detail}" if detail else ""))


def test_disabled_by_default():
  FakeParams.vals = {}                      # every key raises, as on the prebuilt device
  t = ss.SetSpeedLimitTracker()
  sm = FakeSM(kph_to_ms(60))
  out = run(t, sm, 80.0, STABLE + 10)
  check("inert when the param is unregistered (degrades to off, no raise)", out == 80.0, f"got {out}")


def test_disabled_flag():
  t = make_tracker(enabled=False)
  sm = FakeSM(kph_to_ms(60))
  out = run(t, sm, 80.0, STABLE + 10)
  check("inert when the flag is off", out == 80.0, f"got {out}")


def test_adopt_within_band_down():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  out = run(t, sm, 80.0, STABLE + 2)
  check("adopts 80 -> 60 (delta 20, on the band edge)", out == 60.0, f"got {out}")


def test_adopt_within_band_up():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(100))
  out = run(t, sm, 90.0, STABLE + 2)
  check("adopts upward 90 -> 100", out == 100.0, f"got {out}")


def test_reject_outside_band():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  out = run(t, sm, 120.0, STABLE + 5)
  check("does NOT adopt a >20 km/h drop (120 -> 60)", out == 120.0, f"got {out}")
  check("...and records it as ignored while PENDING_ENABLED is False",
        t.last_action == "ignore" and t.pending_limit_kph is None, t.last_action)


def test_one_shot_then_driver_override():
  """The whole point of edge-triggering: we must not fight the driver."""
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  out = run(t, sm, 80.0, STABLE + 2)
  assert out == 60.0
  out = 75.0                                # driver raises the set speed manually
  out = run(t, sm, out, 500)                # same limit still posted, for 5 seconds
  check("does not re-adopt after a driver override (one decision per limit value)",
        out == 75.0, f"got {out}")


def test_new_limit_after_override_is_adopted():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  run(t, sm, 80.0, STABLE + 2)
  sm.msg.speedLimit = kph_to_ms(80)         # genuinely new posted limit
  out = run(t, sm, 75.0, STABLE + 2)
  check("a NEW limit value is still adopted after an override", out == 80.0, f"got {out}")


def test_not_engaged():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  out = run(t, sm, 80.0, STABLE + 5, enabled=False)
  check("inert while openpilot is not engaged", out == 80.0, f"got {out}")


def test_v_cruise_unset():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  out = run(t, sm, 255.0, STABLE + 5)
  check("inert while the set speed is UNSET", out == 255.0, f"got {out}")


def test_way_selection_fail():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60), way="fail")
  out = run(t, sm, 80.0, STABLE + 5)
  check("ignores a limit from a FAILED way selection (what parking reports)",
        out == 80.0, f"got {out}")


def test_tiles_not_loaded():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60), tile_loaded=False)
  out = run(t, sm, 80.0, STABLE + 5)
  check("ignores a limit when tiles are not loaded", out == 80.0, f"got {out}")


def test_mapd_not_alive():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60), alive=False)
  out = run(t, sm, 80.0, STABLE + 5)
  check("ignores mapdOut when the service is not alive (mapd absent)", out == 80.0, f"got {out}")


def test_implausible_limit_units_trap():
  """A km/h value leaking into an m/s field reads ~3.6x high — must be rejected, not clamped."""
  t = make_tracker()
  sm = FakeSM(60.0)                          # 60 m/s == 216 km/h
  out = run(t, sm, 120.0, STABLE + 5)
  check("rejects an implausible limit outright (units-error trap)", out == 120.0, f"got {out}")


def test_zero_limit_does_nothing():
  t = make_tracker()
  sm = FakeSM(0.0)
  out = run(t, sm, 80.0, STABLE + 5)
  check("no limit posted -> no change", out == 80.0, f"got {out}")


def test_losing_a_limit_does_not_trigger():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  out = run(t, sm, 80.0, STABLE + 2)
  assert out == 60.0
  sm.msg.speedLimit = 0.0                    # limit disappears
  out = run(t, sm, out, 300)
  check("X -> 0 (limit lost) changes nothing", out == 60.0, f"got {out}")


def test_flapping_limit_never_settles():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  out = 100.0
  for i in range(STABLE * 4):
    sm.msg.speedLimit = kph_to_ms(60 if i % 2 else 80)
    out = t.update(sm, fake_cs(), out, True)
  check("a flapping limit never reaches the stability gate", out == 100.0, f"got {out}")


def test_stability_gate_timing():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  out = run(t, sm, 80.0, STABLE - 1)
  check("does not act before LIMIT_STABLE_S has elapsed", out == 80.0, f"got {out}")
  out = run(t, sm, out, 2)
  check("acts once the limit has held for LIMIT_STABLE_S", out == 60.0, f"got {out}")


def test_button_frame_is_skipped():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  out = 80.0
  for i in range(STABLE + 3):
    # driver is on the buttons exactly when the gate would fire
    cs = fake_cs([button(ButtonType.decelCruise, False)]) if i >= STABLE else fake_cs()
    out = t.update(sm, cs, out, True)
  check("stays out of the way on a frame with cruise-button activity", out == 80.0, f"got {out}")


def test_clamped_to_upstream_limits():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(144))
  out = run(t, sm, 145.0, STABLE + 2)
  check("adopted value stays within [V_CRUISE_MIN, V_CRUISE_MAX]",
        ss.V_CRUISE_MIN <= out <= ss.V_CRUISE_MAX, f"got {out}")


def test_mph_limit_is_rounded():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(96.56))              # 60 mph
  out = run(t, sm, 100.0, STABLE + 2)
  check("an mph-derived limit lands on a whole km/h", out == 97.0, f"got {out}")


def test_disengage_resets():
  t = make_tracker()
  sm = FakeSM(kph_to_ms(60))
  out = run(t, sm, 80.0, STABLE + 2)
  assert out == 60.0
  run(t, sm, out, 5, enabled=False)          # disengage
  out = run(t, sm, 80.0, STABLE + 2)         # re-engage with a stale set speed
  check("re-engaging takes a fresh look at the current limit", out == 60.0, f"got {out}")


def test_pending_flow_when_enabled():
  """PENDING_ENABLED is False in production; exercise the machinery anyway so it is not rotten."""
  ss.PENDING_ENABLED = True
  try:
    t = make_tracker()
    sm = FakeSM(kph_to_ms(60))
    out = run(t, sm, 120.0, STABLE + 2)
    check("a >band change goes PENDING, not adopted",
          out == 120.0 and t.pending_limit_kph == 60.0, f"out {out} pending {t.pending_limit_kph}")
    out = t.update(sm, fake_cs([button(ButtonType.accelCruise, False)]), out, True)
    check("RES/+ within the window confirms the pending limit", out == 60.0, f"got {out}")

    t2 = make_tracker()
    sm2 = FakeSM(kph_to_ms(60))
    out2 = run(t2, sm2, 120.0, STABLE + 2)
    out2 = run(t2, sm2, out2, int(ss.PENDING_TIMEOUT_S / 0.01) + 5)
    check("an unconfirmed pending limit expires without acting",
          out2 == 120.0 and t2.pending_limit_kph is None and t2.last_action == "expire",
          f"out {out2} action {t2.last_action}")
  finally:
    ss.PENDING_ENABLED = False


def test_pending_disabled_in_production():
  check("PENDING_ENABLED ships False (no alert mechanism verified yet)",
        ss.PENDING_ENABLED is False)


def main():
  for fn in [v for k, v in sorted(globals().items()) if k.startswith("test_")]:
    fn()
  failed = [n for n, ok, _ in results if not ok]
  print()
  print(f"{len(results) - len(failed)}/{len(results)} passed")
  if failed:
    for n in failed:
      print(f"  FAILED: {n}")
    return 1
  return 0


if __name__ == "__main__":
  sys.exit(main())
