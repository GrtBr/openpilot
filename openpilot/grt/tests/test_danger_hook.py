#!/usr/bin/env python3
"""Tests for hook 12, danger_hook (openpilot/grt/danger_hook.py). DANGER_HOOK_HANDOFF.md section 7.

Runs with STUBBED openpilot deps so it works on a dev box that cannot import openpilot.

    python3 openpilot/grt/tests/test_danger_hook.py

Tests 9, 10 and 11b (planner min(), post-min() layers, hook 11 pedal records) need hooks.py's stubs and live in
test_hooks.py.
"""
import csv
import json
import os
import pathlib
import sys
import tempfile
import types
from types import SimpleNamespace as NS

GRT = pathlib.Path(__file__).resolve().parents[1]
FIX = pathlib.Path(__file__).resolve().parent / "fixtures"


def _stub(name, **attrs):
  m = types.ModuleType(name)
  for k, v in attrs.items():
    setattr(m, k, v)
  sys.modules[name] = m


for p in ("openpilot", "openpilot.common", "openpilot.selfdrive",
          "openpilot.selfdrive.controls", "openpilot.selfdrive.controls.lib",
          "openpilot.selfdrive.controls.lib.longitudinal_mpc_lib", "openpilot.grt"):
  sys.modules.setdefault(p, types.ModuleType(p))

DT = 0.05
_stub("openpilot.common.realtime", DT_MDL=DT, DT_CTRL=0.01)


class _Source:
  lead0 = "lead0"


_stub("openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc", LongitudinalPlanSource=_Source)
_stub("openpilot.selfdrive.controls.lib.drive_helpers", should_stop=lambda v, a: bool(v < 0.3 and a < 0.1))

import importlib.util as _ilu  # noqa: E402


def _load(name, path):
  spec = _ilu.spec_from_file_location(name, path)
  mod = _ilu.module_from_spec(spec)
  sys.modules[name] = mod
  spec.loader.exec_module(mod)
  return mod


fl = _load("openpilot.grt.far_lead", str(GRT / "far_lead.py"))
dh = _load("openpilot.grt.danger_hook", str(GRT / "danger_hook.py"))

results = []


def check(name, cond):
  results.append(bool(cond))
  print(f"  {'PASS' if cond else '**FAIL**':9s} {name}")


# ------------------------------------------------------------------------------------------------ helpers
GOOD = dict(p6=0.10, present=True, dRel=120.0, z=120.0, v_ego=25.0, long_active=True, gas=False, brake=False,
            personality="relaxed")


class Rec:
  """A DangerHook with an in-memory log."""
  def __init__(self):
    self.h = dh.DangerHook()
    self.recs = []
    self.h.log.write = self.recs.append
    self.t = 100.0
    self.outs = []

  def frame(self, **kw):
    a = dict(GOOD); a.update(kw)
    o = self.h.step(self.t, a["p6"], a["present"], a["dRel"], a["z"], a["v_ego"], a["long_active"], a["gas"],
                    a["brake"], a["personality"])
    self.t += DT
    self.outs.append(o)
    return o

  def ev(self, kind):
    return [r for r in self.recs if r.get("ev") == kind]


def triggered(**kw):
  """A hook that has just triggered on the GOOD gate (10 frames)."""
  r = Rec()
  for _ in range(dh.GATE_HOLD):
    r.frame(**kw)
  return r


def compress(tr):
  """The camera's far compression (FINDINGS 44c): true range -> camera range."""
  return tr if tr <= dh.KNEE_M else dh.KNEE_M + (tr - dh.KNEE_M) * dh.KNEE_SLOPE


# ------------------------------------------------------------------------------------------------ 1. gate
def test_gate():
  print("\n1. gate")
  r = Rec()
  for _ in range(dh.GATE_HOLD - 1):
    r.frame()
  check("9 gate frames: not yet triggered", r.h.ctl is None and all(o == [] for o in r.outs))
  o = r.frame()
  check("the 10th consecutive gate frame triggers and brakes on that same frame", r.h.ctl is not None and len(o) == 1)
  for name, bad in (("p6 below P6_GATE", dict(p6=0.045)), ("p6 unavailable", dict(p6=None)),
                    ("v_ego not above V_GATE", dict(v_ego=21.0)), ("no lead published", dict(present=False)),
                    ("dRel below D_GATE", dict(dRel=99.9)), ("longActive off", dict(long_active=False)),
                    ("gas", dict(gas=True)), ("brake", dict(brake=True)), ("not relaxed", dict(personality="standard")),
                    ("no camera range", dict(z=None))):
    r = Rec()
    for _ in range(dh.GATE_HOLD + 5):
      r.frame(**bad)
    check(f"inert with {name}", r.h.ctl is None and all(o == [] for o in r.outs))
  r = Rec()
  for _ in range(dh.GATE_HOLD - 1):
    r.frame()
  r.frame(p6=0.0)
  for _ in range(dh.GATE_HOLD - 1):
    r.frame()
  check("a one-frame gap restarts the 10-frame count", r.h.ctl is None)
  r.frame()
  check("...and 10 more consecutive frames trigger", r.h.ctl is not None)
  check("trigger record: t, v_ego, dRel, z0, zmed, p6, personality",
        len(r.ev("trigger")) == 1 and all(k in r.ev("trigger")[0] for k in ("t", "v_ego", "dRel", "z0", "zmed", "p6",
                                                                             "personality")))


# ------------------------------------------------------------------------------------------------ 2. bad inputs
class _Raise:
  def __getitem__(self, k):
    raise RuntimeError("boom")


def sm_of(ts=(2.0, 4.0, 6.0, 8.0, 10.0), probs=(0.1, 0.1, 0.1, 0.1, 0.1), leads=True, **kw):
  a = dict(GOOD); a.update(kw)
  l3 = [NS(x=[a["z"] + fl.MODEL_RANGE_OFFSET])] if leads else []
  m = {"selfdriveState": NS(personality=a["personality"]), "carControl": NS(longActive=a["long_active"]),
       "carState": NS(gasPressed=a["gas"], brakePressed=a["brake"]),
       "radarState": NS(leadOne=NS(present=a["present"], dRel=a["dRel"])),
       "modelV2": NS(meta=NS(disengagePredictions=NS(t=list(ts), brakeDisengageProbs=list(probs))), leadsV3=l3)}
  return m


def test_inputs():
  print("\n2. inputs: inert and no exception on missing data")
  for name, kw in (("empty disengagePredictions", dict(ts=(), probs=())), ("t lacks 6.0", dict(ts=(2.0, 4.0, 8.0))),
                   ("empty leadsV3", dict(leads=False))):
    h = dh.DangerHook(); outs = []
    try:
      for i in range(dh.GATE_HOLD + 5):
        outs.append(h.step_sm(sm_of(**kw), 25.0, 100.0 + i * DT))
      ok = h.ctl is None and all(o == [] for o in outs)
    except Exception:
      ok = False
    check(f"{name}: inert, no exception", ok)
  h = dh.DangerHook(); outs = []
  for i in range(dh.GATE_HOLD):
    outs.append(h.step_sm(sm_of(), 25.0, 100.0 + i * DT))
  check("a well-formed sm triggers through step_sm (index of 6.0 looked up from t)", h.ctl is not None and outs[-1])
  h2 = dh.DangerHook(); outs = []
  for i in range(dh.GATE_HOLD):
    outs.append(h2.step_sm(sm_of(ts=(6.0, 2.0, 4.0), probs=(0.1, 0.0, 0.0)), 25.0, 100.0 + i * DT))
  check("p6 is found by t, not by a fixed index", h2.ctl is not None)
  errs = []
  h3 = dh.DangerHook(log_exception=errs.append)
  try:
    o = h3.step_sm(_Raise(), 25.0, 100.0); ok = o == []
  except Exception:
    ok = False
  check("a read that raises: [] and the exception is reported, not raised", ok and errs)
  o = h.step_sm(_Raise(), 25.0, 200.0)
  check("a read that raises while TRIGGERED returns [] (section 0.5; a hold would have no release path)", o == [])
  o = h.step(200.05, 0.1, True, 120.0, None, 25.0, True, False, False, "relaxed")
  check("...whereas a triggered frame with no camera range holds the previous command", len(o) == 1 and o[0][0] < 0)


# ------------------------------------------------------------------------------------------------ 3. stage 1
def test_stage1():
  print("\n3. stage 1")
  r = triggered()
  first = r.outs[-1][0][0]
  check("first output is the jerk-limited step toward -A1", abs(first - (-dh.JERK * DT)) < 1e-9)
  for _ in range(30):
    r.frame()
  cmds = [o[0][0] for o in r.outs if o]
  check("ramps at JERK and settles at -A1 (1.0, operator D1)",
        abs(cmds[-1] + dh.A1) < 1e-9 and all(b >= a - dh.JERK * DT - 1e-9 for a, b in zip(cmds, cmds[1:])))
  c = dh.DangerController(z0=40.0, TEST=False)
  out = [c.step(i * DT, 30.0, 40.0 - 0.5 * i, 1.5 * i) for i in range(80)]
  check("never harder than -CAP (2.5), even when the stopped-car need is far larger", min(out) >= -dh.CAP - 1e-9
        and min(out) <= -dh.CAP + 1e-9)


# ------------------------------------------------------------------------------------------------ 4. 2 s test
def run_ctl(lead_speed, v=25.0, tr0=150.0, frames=60, TEST=True):
  z0 = compress(tr0)
  c = dh.DangerController(z0=z0, TEST=TEST)
  x = 0.0; tr = tr0; out = []
  for i in range(frames):
    out.append(c.step(i * DT, v, compress(tr), x))
    x += v * DT; tr -= (v - lead_speed) * DT
    if c.rel:
      break
  return c, out


def test_two_second_test():
  print("\n4. 2 s stopped-or-moving test (synthetic camera with the section-3 compression)")
  c, out = run_ctl(0.0)
  check("stopped lead -> verdict 'stopped'", c.verdict == "stopped" and not c.rel and c.f >= dh.F_STOP)
  k = next(i for i in range(len(out)) if i * DT >= dh.TEST_S)
  check("...and the command escalates past -A1 after the verdict", min(out[k:]) < -dh.A1 - 0.1)
  c, out = run_ctl(0.8 * 25.0)
  check("lead at 0.8 x our speed -> 'moving' and release", c.verdict == "moving" and c.rel and c.reason == "test_moving"
        and c.f < dh.F_STOP)
  # section 3: at 27 m/s the stopped-car target saturates at CAP (intended). Start far enough out (true 200 m, camera
  # ~137 m) that stage 1 lasts until ~4.3 s, so the jerk-limited ramp can reach CAP before stage 2 takes over.
  c, out = run_ctl(0.0, v=27.0, tr0=200.0, frames=84)
  k = next(i for i in range(len(out)) if i * DT >= dh.TEST_S)
  check("27 m/s stopped lead: the stopped-car target saturates at -CAP in stage 1 (intended), never beyond",
        c.verdict == "stopped" and c.stage == 1 and abs(min(out[k:]) + dh.CAP) < 1e-9)
  c = dh.DangerController(z0=compress(150.0))
  for i in range(60):
    c.step(i * DT, 25.0, compress(150.0), 0.0)              # we do not move: predicted drop 0
  check("ds too small -> no verdict, stage 1 continues", c.verdict is None and not c.rel and c.stage == 1)


# ------------------------------------------------------------------------------------------------ 5. stage-2 entry
def test_stage2_entry():
  print("\n5. stage-2 entry needs the 0.5 s median")
  c = dh.DangerController(z0=100.0, TEST=False)
  for i in range(30):
    c.step(i * DT, 25.0, 80.0 if i == 20 else 100.0, 1.25 * i)
  check("one noisy 80 m frame inside a 100 m series does NOT enter stage 2", c.stage == 1)
  c = dh.DangerController(z0=100.0, TEST=False)
  for i in range(30):
    c.step(i * DT, 25.0, 80.0 if i >= 20 else 100.0, 1.25 * i)
  check("...a sustained 80 m (median below 85) does", c.stage == 2)


# ------------------------------------------------------------------------------------------------ 6. A1 floor
def test_floor():
  print("\n6. the A1 floor holds while the median is at or above TRUST_M")
  c = dh.DangerController(z0=80.0, TEST=False)
  out = []
  for i in range(80):                                        # z steady at 70 while we drive at 20 m/s: the fresh
    out.append(c.step(i * DT, 20.0, 70.0, 20.0 * DT * i))    # estimate reads the lead at our own speed (c = 0)
  check("stage 2 with a lead read as fast as us, median 70 m (>= 60): still -A1, not released",
        c.stage == 2 and not c.rel and abs(out[-1] + dh.A1) < 1e-9)


# ------------------------------------------------------------------------------------------------ 7. releases
def released_then_empty(r, n=3):
  return r.outs[-1] == [] and all(r.frame() == [] for _ in range(n))


def test_releases():
  print("\n7. every release reason, and [] from that frame on")
  c, _ = run_ctl(0.8 * 25.0)
  check("test_moving (controller)", c.rel and c.reason == "test_moving")
  c = dh.DangerController(z0=100.0, TEST=False)
  for i in range(200):
    c.step(i * DT, 25.0, 100.0, 1.25 * i)
    if c.rel:
      break
  check("timeout_far: 5 s in stage 1 with the median beyond 90 m", c.rel and c.reason == "timeout_far"
        and c.step(99.0, 25.0, 100.0, 0.0) == 0.0)
  c = dh.DangerController(z0=100.0, TEST=False)
  for i in range(200):
    c.step(i * DT, 25.0, 100.0 + 0.5 * i, 1.25 * i)        # camera range grows while we drive
    if c.rel:
      break
  check("pulling_away: camera range growing against our travel for 1 s", c.rel and c.reason == "pulling_away")
  c = dh.DangerController(z0=80.0, TEST=False)
  for i in range(200):
    c.step(i * DT, 10.0, 40.0, 10.0 * DT * i)               # 40 m, lead matches our 10 m/s: closing 0
    if c.rel:
      break
  check("closing_ended: stage 2, median inside 60 m, closing <= 0.5 m/s for 1 s", c.rel and c.reason == "closing_ended")
  for reason, kw in (("disengaged", dict(long_active=False)), ("personality", dict(personality="standard"))):
    r = triggered()
    r.frame(**kw)
    check(f"{reason}: released on that frame, [] after", r.h.ctl is None and released_then_empty(r))
  r = triggered()
  for _ in range(10):
    r.frame(present=False)
  check("lead absent 10 frames (0.5 s): still triggered", r.h.ctl is not None)
  r.frame(present=False)
  check("lead_lost after > 0.5 s, [] after", r.h.ctl is None and released_then_empty(r))
  for _ in range(6):
    r.frame(p6=0.0)
  check("release records carry reason, outcome ok, dur_s, speeds, min_dRel, hardest_cmd, stage, verdict, f",
        r.ev("release") and r.ev("release")[-1]["reason"] == "lead_lost" and r.ev("release")[-1]["outcome"] == "ok"
        and all(k in r.ev("release")[-1] for k in ("dur_s", "v_at_trigger", "v_at_release", "min_dRel",
                                                    "hardest_cmd", "stage", "verdict", "f")))


# ------------------------------------------------------------------------------------------------ 8. re-trigger
def test_retrigger():
  print("\n8. re-trigger hold")
  r = triggered()
  r.frame(long_active=False)                                 # release
  for _ in range(40):
    r.frame()                                                # gate True throughout: the hold never counts
  check("gate True continuously after a release: no re-trigger", r.h.ctl is None)
  for _ in range(19):
    r.frame(p6=0.0)
  for _ in range(dh.GATE_HOLD + 3):
    r.frame()
  check("19 gate-False frames then gate True: still blocked (the hold restarts on a gate-True frame)", r.h.ctl is None)
  for _ in range(20):
    r.frame(p6=0.0)
  for _ in range(dh.GATE_HOLD - 1):
    r.frame()
  check("20 gate-False frames, then 9 gate frames: not yet", r.h.ctl is None)
  r.frame()
  check("...the 10th gate frame re-triggers", r.h.ctl is not None)


# ------------------------------------------------------------------------------------------------ 11. log
def test_log():
  print("\n11. log")
  with tempfile.TemporaryDirectory() as d:
    path = os.path.join(d, "grt", "danger_hook.log")
    h = dh.DangerHook(log_path=path)
    t = 100.0; z = compress(150.0); tr = 150.0; x = 0.0
    for i in range(80):                                      # a stopped lead: trigger, verdict, then release by pedal
      h.step(t, 0.1, True, 120.0, compress(tr), 25.0, True, False, i == 79, "relaxed")
      t += DT; tr -= 25.0 * DT
    recs = [json.loads(l) for l in open(path)]
    kinds = [r["ev"] for r in recs]
    check("one trigger, one verdict, one release for the event (plus heartbeats)",
          kinds.count("trigger") == 1 and kinds.count("verdict") == 1 and kinds.count("release") == 1 and "hb" in kinds)
    v = [r for r in recs if r["ev"] == "verdict"][0]
    check("verdict record: t, f, verdict, ds, pred, zmed", all(k in v for k in ("t", "f", "verdict", "ds", "pred", "zmed")))
    rel = [r for r in recs if r["ev"] == "release"][0]
    check("release record carries the verdict and f", rel["verdict"] == "stopped" and rel["f"] == v["f"])
    lg = dh._RotatingLog(path)
    with open(path, "a") as f:
      f.write("x" * dh.LOG_MAX_BYTES)
    lg.write({"ev": "hb"})
    check("rotation: the full file moves to .1 and the new one starts with a 'rotated' record",
          os.path.exists(path + ".1") and json.loads(open(path).readline())["ev"] == "rotated")
  errs = []
  bad = dh._RotatingLog("/proc/definitely/not/writable/danger_hook.log", errs.append)
  try:
    bad.write({"ev": "hb"}); bad.write({"ev": "hb"}); ok = True
  except Exception:
    ok = False
  check("a write failure does not raise, latches logging off and reports once", ok and bad.dead and len(errs) == 1)


# ------------------------------------------------------------------------------------------------ 11a. pedals
def test_pedals():
  print("\n11a. D4/D5 for hook 12")
  for name, kw, reason, outcome in (("gas", dict(gas=True), "driver_gas", "false"),
                                    ("brake", dict(brake=True), "driver_brake", "fail"),
                                    ("both", dict(gas=True, brake=True), "driver_brake", "fail")):
    r = triggered()
    o = r.frame(**kw)
    nxt = r.frame()
    rel = r.ev("release")
    check(f"{name} while triggered: released, [] now and next frame, one record {reason}/{outcome}",
          o == [] and nxt == [] and len(rel) == 1 and rel[0]["reason"] == reason and rel[0]["outcome"] == outcome)
  r = Rec()
  for _ in range(5):
    r.frame(p6=0.0, gas=True); r.frame(p6=0.0, brake=True)
  check("pedal while idle: no record", r.ev("release") == [])
  r = triggered()
  r.frame(gas=True, long_active=False)
  check("gas AND longActive off on the same frame -> driver_gas, not disengaged",
        len(r.ev("release")) == 1 and r.ev("release")[0]["reason"] == "driver_gas")
  r = triggered()
  r.frame(long_active=False)
  r.frame(long_active=False); r.frame(long_active=False)
  r.frame(long_active=False, brake=True)
  for _ in range(8):
    r.frame(long_active=False, brake=True)
  rel = r.ev("release")
  check("longActive off, brake 3 frames later: exactly one record, driver_brake / fail, with t_pedal",
        len(rel) == 1 and rel[0]["reason"] == "driver_brake" and rel[0]["outcome"] == "fail"
        and rel[0]["t_pedal"] > rel[0]["t"])
  r = triggered()
  r.frame(long_active=False)
  for _ in range(5):
    r.frame(long_active=False)
  r.frame(long_active=False, brake=True)
  rel = r.ev("release")
  check("brake 6 frames later: the disengaged / ok record stands, nothing else",
        len(rel) == 1 and rel[0]["reason"] == "disengaged" and rel[0]["outcome"] == "ok")


# ------------------------------------------------------------------------------------------------ 12. fixtures
EXPECTED = [("danger_000001f1_335.csv", 339.63, "stopped", 0.97), ("danger_000001f3_1114.csv", 1120.46, "moving", 0.17),
            ("danger_000001fc_1070.csv", 1074.66, "stopped", 0.77), ("danger_000001de_810.csv", 814.60, "moving", 0.30)]


def test_fixtures():
  print("\n12. real-trace fixtures, open loop on the logged inputs")
  for name, t_trig, verdict, f in EXPECTED:
    h = dh.DangerHook(); recs = []; h.log.write = recs.append
    for row in csv.DictReader(open(FIX / name)):
      h.step(float(row["t"]), float(row["p6"]), row["present"] == "1", float(row["dRel"]),
             float(row["lead_x0"]) - fl.MODEL_RANGE_OFFSET, float(row["v_ego"]), row["long_active"] == "1", False,
             row["brake"] == "1", "relaxed")
    tr = [r for r in recs if r["ev"] == "trigger"]; vd = [r for r in recs if r["ev"] == "verdict"]
    ok = (len(tr) == 1 and abs(tr[0]["t"] - t_trig) <= 0.1 and vd and vd[0]["verdict"] == verdict
          and abs(vd[0]["f"] - f) <= 0.05)
    got = f"trigger {tr[0]['t'] if tr else None}, {vd[0]['verdict'] if vd else None} f {vd[0]['f'] if vd else None}"
    check(f"{name[7:15]}: trigger {t_trig} +-0.1, {verdict}, f {f} +-0.05  ({got})", ok)
    if verdict == "moving":
      rel = [r for r in recs if r["ev"] == "release"]
      check(f"{name[7:15]}: 'moving' releases (test_moving)", rel and rel[0]["reason"] == "test_moving")


def main():
  test_gate()
  test_inputs()
  test_stage1()
  test_two_second_test()
  test_stage2_entry()
  test_floor()
  test_releases()
  test_retrigger()
  test_log()
  test_pedals()
  test_fixtures()
  check("DangerController keeps EarlyMode's keyword names (the replays import it as EarlyMode)",
        all(k in dh.DangerController.__init__.__code__.co_varnames
            for k in ("A1", "CAP", "WAIT", "JERK", "ENTRY", "TRUST", "DT", "TEST", "F_STOP", "z0")))
  check("operator decisions D1-D3: A1 1.0, CAP 2.5, relaxed only", dh.A1 == 1.0 and dh.CAP == 2.5
        and "personality == \"relaxed\"" in (GRT / "danger_hook.py").read_text())
  print(f"\n{sum(results)}/{len(results)} passed")
  return 0 if all(results) else 1


if __name__ == "__main__":
  sys.exit(main())
