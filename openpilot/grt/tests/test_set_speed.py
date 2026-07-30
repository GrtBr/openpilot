#!/usr/bin/env python3
"""Tests for the set-speed tracker (openpilot/grt/set_speed.py).

    python3 openpilot/grt/tests/test_set_speed.py

Runs with STUBBED openpilot deps so it works on a dev box that cannot import openpilot.
Field names in the stubs MIRROR THE REAL SCHEMA — see tests/test_schema_conformance.py, which
checks that claim against the actual log.capnp. (A stub that invented `lead.status` is exactly
how the mapd controller shipped a silent 38,300-exception no-op to the car.)

The behaviour under test (user spec, 2026-07-29):
  * at engage, seed the set speed from the posted limit, else 60 km/h if no map data;
  * afterwards adopt a limit change automatically ONLY if the feature still owns the set speed
    AND that speed is a multiple of 10 AND the change is within ±20 km/h;
  * otherwise offer it for 10 s and adopt only on RES/+.

Safety-relevant properties asserted here:
  * INERT by default, and an unregistered param degrades to off rather than raising;
  * a driver-set speed is NEVER silently overwritten;
  * any change beyond ±20 km/h prompts, even while the feature owns the set speed;
  * float comparisons tolerate rounding (an == would end tracking permanently).
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


_reg = _load("openpilot.grt.registry", str(GRT / "registry.py"))
# Point the fork config dir at an empty temp dir. get_bool_safe() falls back to a FILE under
# GRT_CONFIG_DIR when Params raises, and on the device that directory holds the live feature
# flags — so without this the suite reads production config and "unregistered param" tests see
# an ENABLED feature. Found by running these tests on the car, not on the dev box.
import tempfile as _tf  # noqa: E402
_reg.GRT_CONFIG_DIR = _tf.mkdtemp(prefix="grt-test-")
_load("openpilot.grt.scc_map", str(GRT / "scc_map.py"))
ss = _load("openpilot.grt.set_speed", str(GRT / "set_speed.py"))

ss._DEBUG_LOG = None          # no file writes during tests
STABLE = int(ss.LIMIT_STABLE_S / 0.01)
SEED_TIMEOUT = int(ss.SEED_TIMEOUT_S / 0.01)
PENDING = int(ss.PENDING_TIMEOUT_S / 0.01)


# ------------------------------------------------------------------------------------------
# fakes


class FakeSM:
  """Minimal SubMaster: mapdOut only, with the real field names."""

  def __init__(self, speed_limit_kph=0.0, tile_loaded=True, way="current", alive=True):
    self.msg = NS(speedLimit=speed_limit_kph / 3.6, tileLoaded=tile_loaded, waySelectionType=way,
                  nextSpeedLimit=0.0, nextSpeedLimitDistance=0.0)
    self.alive = {'mapdOut': alive}
    self.valid = {'mapdOut': True}

  def set_limit(self, kph):
    self.msg.speedLimit = kph / 3.6

  def set_next(self, kph, dist_m=200.0):
    self.msg.nextSpeedLimit = kph / 3.6
    self.msg.nextSpeedLimitDistance = dist_m

  def __getitem__(self, k):
    assert k == 'mapdOut'
    return self.msg


def button(btn_type, pressed=False):
  return NS(type=NS(raw=btn_type), pressed=pressed)


def fake_cs(buttons=()):
  return NS(buttonEvents=list(buttons))


def make_tracker(enabled=True):
  FakeParams.vals = {"SmartCruiseControlSetSpeed": enabled}
  return ss.SetSpeedLimitTracker()


def run(tracker, sm, v_cruise, frames, CS=None, enabled=True, engage=False):
  out = v_cruise
  for i in range(frames):
    out = tracker.update(sm, CS if CS is not None else fake_cs(), out, enabled,
                         engage and i == 0)
  return out


def engaged_at(tracker, sm, limit_kph, start=105.0):
  """Engage and let the seed settle. Returns the seeded set speed."""
  sm.set_limit(limit_kph)
  return run(tracker, sm, start, STABLE + 5, engage=True)


# ------------------------------------------------------------------------------------------

results = []


def check(name, cond, detail=""):
  results.append((name, bool(cond), detail))
  print(f"  {'PASS' if cond else '**FAIL**':9s} {name}" + (f"   {detail}" if detail else ""))


# --- inertness ----------------------------------------------------------------------------

def test_disabled_by_default():
  FakeParams.vals = {}                      # every key raises, as on the prebuilt device
  t = ss.SetSpeedLimitTracker()
  out = run(t, FakeSM(60), 105.0, STABLE + 10, engage=True)
  check("inert when the param is unregistered (degrades to off, no raise)",
        out == 105.0, f"got {out}")


def test_disabled_flag():
  t = make_tracker(enabled=False)
  out = run(t, FakeSM(60), 105.0, STABLE + 10, engage=True)
  check("inert when the flag is off", out == 105.0, f"got {out}")


def test_not_engaged():
  t = make_tracker()
  out = run(t, FakeSM(60), 105.0, STABLE + 10, enabled=False)
  check("inert while openpilot is not engaged", out == 105.0, f"got {out}")


# --- engage seeding -----------------------------------------------------------------------

def test_seed_from_map():
  t = make_tracker()
  out = engaged_at(t, FakeSM(), 100.0)
  check("engage seeds the set speed from the posted limit (not 105)", out == 100.0, f"got {out}")


def test_seed_no_map_falls_back_to_60():
  t = make_tracker()
  sm = FakeSM(100, way="fail")              # parked: way selection fails
  out = run(t, sm, 105.0, SEED_TIMEOUT + 5, engage=True)
  check("no map data at engage -> seeds 60 after the timeout", out == 60.0, f"got {out}")


def test_seed_waits_before_falling_back():
  """Engaging from standstill reports way=fail; without the wait every drive would start on 60
  and immediately prompt to move to the real limit."""
  t = make_tracker()
  sm = FakeSM(100, way="fail")
  out = run(t, sm, 105.0, SEED_TIMEOUT - 20, engage=True)
  check("upstream's value stands while waiting for a first fix", out == 105.0, f"got {out}")
  sm.msg.waySelectionType = "current"
  out = run(t, sm, out, 2)
  check("...and the real limit wins if it arrives inside the window", out == 100.0, f"got {out}")


def test_seed_overrides_resume():
  """User chose 'OSM limit always wins' over upstream's resume-previous-speed behaviour."""
  t = make_tracker()
  sm = FakeSM(80)
  out = run(t, sm, 120.0, STABLE + 5, CS=fake_cs([button(ButtonType.accelCruise)]), engage=True)
  check("a RES/resume engage still seeds from the map", out == 80.0, f"got {out}")


# --- auto-adopt while the feature owns the set speed --------------------------------------

def test_auto_adopt_in_band():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.set_limit(80.0)
  out = run(t, sm, 100.0, STABLE + 2)
  check("tracking + round + within 20 -> auto-adopts 100 -> 80", out == 80.0, f"got {out}")


def test_auto_adopt_upward():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.set_limit(120.0)
  out = run(t, sm, 100.0, STABLE + 2)
  check("adopts upward 100 -> 120 while tracking", out == 120.0, f"got {out}")


def test_chained_adoptions():
  t = make_tracker()
  sm = FakeSM()
  out = engaged_at(t, sm, 120.0)
  for nxt in (100.0, 80.0, 60.0):
    sm.set_limit(nxt)
    out = run(t, sm, out, STABLE + 2)
  check("tracks a graduated route 120 -> 100 -> 80 -> 60", out == 60.0, f"got {out}")


def test_big_drop_prompts_even_while_tracking():
  """The user's absolute safety rule: >20 km/h always asks, tracking or not."""
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 120.0)
  sm.set_limit(80.0)
  out = run(t, sm, 120.0, STABLE + 2)
  check("120 -> 80 (delta 40) does NOT auto-adopt", out == 120.0, f"got {out}")
  check("...it prompts for confirmation instead",
        t.pending_limit_kph == 80.0 and t.last_action == "pending", t.last_action)
  check("...and says why", t.tracking is True)


def test_confirm_restores_tracking():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 120.0)
  sm.set_limit(80.0)
  out = run(t, sm, 120.0, STABLE + 2)
  out = t.update(sm, fake_cs([button(ButtonType.decelCruise)]), out, True)
  check("SET/- confirms a DOWNWARD pending limit (direction-matched)", out == 80.0, f"got {out}")
  sm.set_limit(60.0)
  out = run(t, sm, out, STABLE + 2)
  check("...and the next in-band change auto-adopts again (tracking restored)",
        out == 60.0, f"got {out}")


# --- driver ownership ----------------------------------------------------------------------

def test_driver_set_speed_is_never_overwritten():
  """The user's example: 103 in a 100 zone, then the limit moves. Must ask, never act."""
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  out = 103.0                               # driver dials in their own number
  sm.set_limit(90.0)
  out = run(t, sm, out, STABLE + 2)
  check("a driver-set 103 is not overwritten when the limit drops to 90",
        out == 103.0, f"got {out}")
  check("...it prompts instead", t.pending_limit_kph == 90.0, str(t.pending_limit_kph))
  check("...for the right reason", t.tracking is False)


def test_driver_set_speed_up_also_prompts():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.set_limit(110.0)
  out = run(t, sm, 103.0, STABLE + 2)
  check("a driver-set 103 is not raised to 110 either", out == 103.0, f"got {out}")
  check("...it prompts", t.pending_limit_kph == 110.0, str(t.pending_limit_kph))


def test_non_round_set_speed_prompts_even_if_owned():
  """set % 10 != 0 means hand-tuned, so no silent change even within band."""
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  t._owned_kph = 105.0                      # pretend we own a non-round value
  t._prev_limit_kph = 105.0
  sm.set_limit(110.0)
  out = run(t, sm, 105.0, STABLE + 2)
  check("a non-multiple-of-10 set speed prompts even when owned and in band",
        out == 105.0 and t.pending_limit_kph == 110.0, f"out {out} pending {t.pending_limit_kph}")


def test_driver_matching_the_limit_resumes_tracking():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  out = 103.0
  sm.set_limit(100.0)
  out = run(t, sm, out, STABLE + 2)         # limit unchanged; driver still owns
  out = 100.0                               # driver dials back to the posted limit
  out = run(t, sm, out, STABLE + 2)
  check("dialling the posted limit by hand re-establishes tracking", t.tracking is True)
  sm.set_limit(90.0)
  out = run(t, sm, out, STABLE + 2)
  check("...so the next in-band change auto-adopts", out == 90.0, f"got {out}")


def test_float_drift_does_not_end_tracking():
  """An == comparison here would silently end tracking forever after one rounding wobble."""
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  out = 100.0 - 1e-9
  sm.set_limit(90.0)
  out = run(t, sm, out, STABLE + 2)
  check("a float wobble in the set speed does not end tracking", out == 90.0, f"got {out}")


# --- pending window -------------------------------------------------------------------------

def test_upward_prompt_is_confirmed_with_res():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 60.0)
  out = 60.0
  sm.set_limit(120.0)                        # +60: out of band, so it prompts
  out = run(t, sm, out, STABLE + 2)
  assert t.pending_limit_kph == 120.0, t.pending_limit_kph
  check("an UPWARD offer is declined by SET/-",
        t.update(sm, fake_cs([button(ButtonType.decelCruise)]), out, True) == 60.0)
  t2 = make_tracker(); sm2 = FakeSM(); engaged_at(t2, sm2, 60.0)
  sm2.set_limit(120.0)
  out2 = run(t2, sm2, 60.0, STABLE + 2)
  out2 = t2.update(sm2, fake_cs([button(ButtonType.accelCruise)]), out2, True)
  check("...and confirmed by RES/+", out2 == 120.0, f"got {out2}")


def test_upcoming_limit_preauthorised_when_it_would_auto_adopt():
  """Operator, 2026-07-30: the pre-sign ramp must be ON whenever no confirmation is needed, so
  the run-up is shaped at APPROACH_DECEL instead of stepping at the sign."""
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)                   # tracking, set speed 100 (round)
  sm.set_next(80.0, 250.0)                   # -20: inside the band -> would auto-adopt
  run(t, sm, 100.0, 5)
  check("an upcoming in-band limit is PRE-AUTHORISED for the ramp",
        t.authorised_next_limit_kph == 80.0, str(t.authorised_next_limit_kph))
  check("...and the CEILING authorisation is untouched (still the current limit)",
        t.authorised_limit_kph == 100.0, str(t.authorised_limit_kph))


def test_upcoming_limit_not_preauthorised_when_it_would_prompt():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.set_next(60.0, 250.0)                   # -40: out of band -> needs confirmation
  run(t, sm, 100.0, 5)
  check("an out-of-band upcoming limit is NOT pre-authorised",
        t.authorised_next_limit_kph == 0.0, str(t.authorised_next_limit_kph))


def test_no_preauthorisation_when_driver_owns_set_speed():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.set_next(90.0, 250.0)                   # in band, but the driver owns a non-round 103
  run(t, sm, 103.0, 5)
  check("no pre-authorisation while the driver owns the set speed",
        t.authorised_next_limit_kph == 0.0, str(t.authorised_next_limit_kph))


def test_pending_expires():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 120.0)
  sm.set_limit(60.0)
  out = run(t, sm, 120.0, STABLE + 2)
  assert t.pending_limit_kph == 60.0
  out = run(t, sm, out, PENDING + 5)
  check("an unconfirmed prompt expires without acting",
        out == 120.0 and t.pending_limit_kph is None and t.last_action == "expire",
        f"out {out} action {t.last_action}")


def test_pending_declined_by_set_button():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 120.0)
  sm.set_limit(60.0)
  out = run(t, sm, 120.0, STABLE + 2)
  out = t.update(sm, fake_cs([button(ButtonType.accelCruise)]), out, True)
  check("RES/+ DECLINES a downward prompt (wrong direction = no)",
        out == 120.0 and t.pending_limit_kph is None and t.last_action == "decline",
        f"out {out} action {t.last_action}")


def test_pending_goes_stale_on_a_new_limit():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 120.0)
  sm.set_limit(60.0)
  out = run(t, sm, 120.0, STABLE + 2)
  assert t.pending_limit_kph == 60.0
  sm.set_limit(100.0)                       # road changed under us
  out = run(t, sm, out, 2)
  check("one differing frame does NOT retire the offer", t.pending_limit_kph == 60.0)
  out = run(t, sm, out, STABLE + 2)         # 100 now becomes ESTABLISHED
  # The offer is retired, and the newly established limit is then decided on its own merits in
  # the same pass (120 -> 100 is in band, so it auto-adopts) — hence last_action is "adopt".
  check("a prompt is retired once a DIFFERENT limit becomes established",
        t.pending_limit_kph is None and out == 100.0, f"out {out} action {t.last_action}")
  out = run(t, sm, out, STABLE + 2)
  check("...and the new limit is then decided on its own merits", out == 100.0, f"got {out}")


def test_expired_prompt_is_reoffered():
  """Regression: an ignored prompt used to mark the limit `already_handled` FOREVER — leaving
  the set speed stranded (60 in a 100 zone) with the heartbeat looking benign."""
  t = make_tracker()
  sm = FakeSM(100, way="fail")
  out = run(t, sm, 105.0, SEED_TIMEOUT + 5, engage=True)
  assert out == 60.0, out
  sm.msg.waySelectionType = "current"       # map arrives: 100 in a 60-seeded drive
  out = run(t, sm, out, STABLE + 2)
  check("first real limit after a no-map seed is offered, not silently taken",
        out == 60.0 and t.pending_limit_kph == 100.0, f"out {out} pending {t.pending_limit_kph}")
  out = run(t, sm, out, PENDING + 5)        # driver misses it
  assert t.pending_limit_kph is None
  out = run(t, sm, out, 500)
  check("...not re-offered immediately (no nagging)", t.pending_limit_kph is None)
  out = run(t, sm, out, int(ss.REOFFER_S / 0.01) + STABLE)
  check("...but re-offered after the cooldown, so the feature is not dead for the drive",
        t.pending_limit_kph == 100.0, str(t.pending_limit_kph))


def test_declined_prompt_is_not_reoffered():
  """A SET/- decline is an answer, not a missed prompt."""
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 120.0)
  sm.set_limit(60.0)
  out = run(t, sm, 120.0, STABLE + 2)
  out = t.update(sm, fake_cs([button(ButtonType.decelCruise)]), out, True)
  assert t.pending_limit_kph is None
  out = run(t, sm, out, int(ss.REOFFER_S / 0.01) + STABLE + 10)
  check("a declined limit is never re-offered", t.pending_limit_kph is None, t.last_action)


def test_stale_pending_leaves_no_residue():
  """Regression: a stale offer used to be marked 'acted', so returning to that limit later
  skipped the decision entirely."""
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 120.0)
  sm.set_limit(60.0)
  out = run(t, sm, 120.0, STABLE + 2)
  assert t.pending_limit_kph == 60.0
  sm.set_limit(100.0)                       # road changes under the offer
  out = run(t, sm, out, 2)
  check("a single differing frame does NOT kill the offer (the 0.2 s bug)",
        t.pending_limit_kph == 60.0, str(t.pending_limit_kph))
  out = run(t, sm, out, STABLE + 2)
  assert out == 100.0, out                  # 120 -> 100 is in band, auto-adopted
  sm.set_limit(60.0)                        # back to the limit that went stale
  out = run(t, sm, out, STABLE + 2)
  check("a limit that went stale is still decided properly when it returns",
        t.pending_limit_kph == 60.0 and out == 100.0,
        f"out {out} pending {t.pending_limit_kph}")


def test_reengage_with_stale_set_speed():
  """On the car, upstream's initialize_v_cruise runs first and may restore the previous set
  speed — the seed must override whatever it left behind."""
  t = make_tracker()
  sm = FakeSM()
  out = engaged_at(t, sm, 100.0)
  run(t, sm, out, 5, enabled=False)
  sm.set_limit(60.0)
  out = run(t, sm, 100.0, STABLE + 5, engage=True)   # stale 100 still in v_cruise_kph
  check("re-engage seeds from the map over a stale restored set speed", out == 60.0, f"got {out}")


def test_pending_seconds_left():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 120.0)
  sm.set_limit(60.0)
  run(t, sm, 120.0, STABLE + 2)
  full = t.pending_seconds_left
  run(t, sm, 120.0, 200)
  check("pending_seconds_left counts down for the UI",
        abs(full - ss.PENDING_TIMEOUT_S) < 0.1 and t.pending_seconds_left < full,
        f"{full} -> {t.pending_seconds_left}")


# --- data-quality gates ---------------------------------------------------------------------

def test_way_selection_fail():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.set_limit(80.0)
  sm.msg.waySelectionType = "fail"
  out = run(t, sm, 100.0, STABLE + 5)
  check("ignores a limit from a FAILED way selection", out == 100.0, f"got {out}")


def test_tiles_not_loaded():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.set_limit(80.0)
  sm.msg.tileLoaded = False
  out = run(t, sm, 100.0, STABLE + 5)
  check("ignores a limit when tiles are not loaded", out == 100.0, f"got {out}")


def test_mapd_not_alive():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.set_limit(80.0)
  sm.alive['mapdOut'] = False
  out = run(t, sm, 100.0, STABLE + 5)
  check("ignores mapdOut when the service is not alive (mapd absent)", out == 100.0, f"got {out}")


def test_implausible_limit_units_trap():
  """A km/h value leaking into an m/s field reads ~3.6x high — reject, do not clamp."""
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.msg.speedLimit = 60.0                  # 60 m/s == 216 km/h
  out = run(t, sm, 100.0, STABLE + 5)
  check("rejects an implausible limit outright (units-error trap)", out == 100.0, f"got {out}")


def test_losing_a_limit_does_not_trigger():
  t = make_tracker()
  sm = FakeSM()
  out = engaged_at(t, sm, 100.0)
  sm.set_limit(0.0)
  out = run(t, sm, out, 300)
  check("X -> 0 (limit lost) changes nothing", out == 100.0, f"got {out}")


def test_flapping_limit_never_settles():
  t = make_tracker()
  sm = FakeSM()
  out = engaged_at(t, sm, 100.0)
  for i in range(STABLE * 4):
    sm.set_limit(80.0 if i % 2 else 90.0)
    out = t.update(sm, fake_cs(), out, True)
  check("a flapping limit never reaches the stability gate", out == 100.0, f"got {out}")


def test_up_on_predicted_way_is_deferred():
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.set_limit(110.0)
  sm.msg.waySelectionType = "predicted"
  out = run(t, sm, 100.0, STABLE + 5)
  check("does NOT raise the set speed off a merely PREDICTED way", out == 100.0, f"got {out}")
  sm.msg.waySelectionType = "current"
  out = run(t, sm, out, 3)
  check("...and adopts it once the way selection becomes `current`", out == 110.0, f"got {out}")


def test_button_frame_is_deferred_not_dropped():
  """Regression: an exact `_cand_frames == STABLE` gate dropped the limit PERMANENTLY when a
  button landed on that one frame."""
  t = make_tracker()
  sm = FakeSM()
  engaged_at(t, sm, 100.0)
  sm.set_limit(80.0)
  out = 100.0
  for i in range(STABLE + 3):
    cs = fake_cs([button(ButtonType.decelCruise)]) if i >= STABLE - 1 else fake_cs()
    out = t.update(sm, cs, out, True)
  check("stays out of the way while the driver is on the buttons", out == 100.0, f"got {out}")
  out = run(t, sm, out, 2)
  check("adopts on the first clear frame afterwards (deferral, not a silent drop)",
        out == 80.0, f"got {out}")


def test_clamped_to_upstream_limits():
  t = make_tracker()
  sm = FakeSM()
  out = engaged_at(t, sm, 144.0)
  check("seeded value stays within [V_CRUISE_MIN, V_CRUISE_MAX]",
        ss.V_CRUISE_MIN <= out <= ss.V_CRUISE_MAX, f"got {out}")


def test_mph_limit_is_rounded():
  t = make_tracker()
  sm = FakeSM()
  out = engaged_at(t, sm, 96.56)            # 60 mph
  check("an mph-derived limit lands on a whole km/h", out == 97.0, f"got {out}")


def test_disengage_resets():
  t = make_tracker()
  sm = FakeSM()
  out = engaged_at(t, sm, 100.0)
  run(t, sm, out, 5, enabled=False)
  sm.set_limit(60.0)
  out = engaged_at(t, sm, 60.0, start=105.0)
  check("re-engaging seeds afresh from the current limit", out == 60.0, f"got {out}")


def test_heartbeat_records_rejection_reason():
  written = []
  real_write = ss.SetSpeedLimitTracker._write
  ss.SetSpeedLimitTracker._write = staticmethod(written.append)
  old = ss._DEBUG_LOG
  ss._DEBUG_LOG = "/dev/null"
  try:
    t = make_tracker()
    sm = FakeSM()
    engaged_at(t, sm, 100.0)
    sm.set_limit(80.0)
    sm.msg.waySelectionType = "fail"
    written.clear()
    run(t, sm, 100.0, int(ss.HEARTBEAT_S / 0.01) * 3)
    reasons = {r.get("reason") for r in written}
    check("heartbeat names the gate that rejected", "way_fail" in reasons, str(reasons))
    check("heartbeat is throttled, not per-frame", len(written) <= 4, f"{len(written)} lines")
  finally:
    ss._DEBUG_LOG = old
    ss.SetSpeedLimitTracker._write = staticmethod(real_write)


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
