#!/usr/bin/env python3
"""Behavioural tests for SmartCruiseControlMap (openpilot/grt/scc_map.py).

Runs with STUBBED openpilot deps so it can execute on a dev box that cannot import
openpilot (the Pi5 has no opendbc/capnp/setproctitle). That is deliberate: these tests
cover this fork's control logic, not openpilot's plumbing.

    python3 openpilot/grt/tests/test_scc_map.py

Covers the behaviours whose comments in scc_map.py record real on-car tuning history:
the lead gate applies to the RISING EDGE ONLY (a lead appearing mid-approach must not
cancel an engaged slow-down), a lead already past the hazard does not block, the MIN_V
floor, and full inertness when the feature param is off.
"""
import sys
import types
from types import SimpleNamespace as NS

def stub(name, **attrs):
    m = types.ModuleType(name); [setattr(m,k,v) for k,v in attrs.items()]; sys.modules[name]=m; return m
for pkg in ("openpilot","openpilot.cereal","openpilot.common","openpilot.selfdrive",
            "openpilot.selfdrive.car","openpilot.grt"):
    sys.modules.setdefault(pkg, types.ModuleType(pkg))
stub("openpilot.cereal.messaging", SubMaster=object)
stub("openpilot.common.constants", CV=NS(KPH_TO_MS=1/3.6))
class P:
    vals={"SmartCruiseControlMap":True}
    def __init__(self,*a,**k): pass
    def get_bool(self,k): return P.vals.get(k,False)
stub("openpilot.common.params", Params=P)
stub("openpilot.common.realtime", DT_MDL=0.05)
stub("openpilot.selfdrive.car.cruise", V_CRUISE_UNSET=255.0)

import importlib.util
spec=importlib.util.spec_from_file_location("scc_map", str(__import__("pathlib").Path(__file__).resolve().parents[1] / "scc_map.py"))
scc_map=importlib.util.module_from_spec(spec); spec.loader.exec_module(scc_map)
scc_map._DEBUG_LOG=None  # don't write logs during tests

def SM(curve=0.,sl=0.,hz="",hzd=0.,l1=None,l2=None,nsl=0.,nsld=0.):
    lead=lambda d:NS(status=(d is not None),dRel=(d or 0.))
    return {'mapdOut':NS(mapCurveSpeed=curve,speedLimitSuggestedSpeed=sl,nextHazard=hz,
                         nextHazardDistance=hzd,nextSpeedLimit=nsl,nextSpeedLimitDistance=nsld,
                         suggestedSpeed=99.),
            'radarState':NS(leadOne=lead(l1),leadTwo=lead(l2))}

UNSET=255.0; ok=lambda c: "PASS" if c else "**FAIL**"
res=[]
def check(name,cond): res.append((name,cond)); print(f"  {ok(cond):9s} {name}")

# 1 curve speed becomes the ceiling
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(SM(curve=15.0),True,False,25.0,0.0,30.0)
check("curve speed -> v_target=15, active, ceiling applied", abs(c.v_target-15.0)<1e-6 and c.is_active and abs(c.output_v_target-15.0)<1e-6)

# 2 speed limit (Phase 6) lowers target, and is taken over a higher curve speed
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(SM(curve=25.0,sl=13.9),True,False,30.0,0.0,33.0)
check("speedLimitSuggestedSpeed wins when lower than curve", abs(c.v_target-13.9)<1e-6)

# 3 hazard engages at close range with no lead, and yields firm decel
c=scc_map.SmartCruiseControlMap()
for _ in range(5): c.update(SM(hz="stop",hzd=30.0),True,False,15.0,0.0,25.0)
check("hazard 'stop' engages -> v_target 5.55, hazard_active", abs(c.v_target-5.55)<1e-6 and c.hazard_active)
check("hazard yields output_hazard_accel (adaptive, in [-1.5,-0.3])",
      c.output_hazard_accel is not None and -1.5<=c.output_hazard_accel<=-0.3)
check("MIN_V floor applied to ceiling (20km/h)", abs(c.output_v_target-scc_map.MIN_V)<1e-6)

# 4 lead blocks the RISING EDGE only
c=scc_map.SmartCruiseControlMap()
for _ in range(5): c.update(SM(hz="stop",hzd=30.0,l1=10.0),True,False,15.0,0.0,25.0)
check("lead in front blocks hazard engagement", not c.hazard_active and c.output_hazard_accel is None)

# 5 lead already PAST the hazard does not block
c=scc_map.SmartCruiseControlMap()
for _ in range(5): c.update(SM(hz="stop",hzd=30.0,l1=200.0),True,False,15.0,0.0,25.0)
check("lead far past hazard does NOT block engagement", c.hazard_active)

# 6 sticky latch: lead appearing AFTER engagement must not cancel
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(SM(hz="stop",hzd=30.0),True,False,15.0,0.0,25.0)
for _ in range(3): c.update(SM(hz="stop",hzd=20.0,l1=8.0),True,False,14.0,0.0,25.0)
check("lead appearing after engage does NOT cancel (sticky latch)", c.hazard_active)

# 7 param off -> fully inert
P.vals["SmartCruiseControlMap"]=False
c=scc_map.SmartCruiseControlMap()
for _ in range(80): c.update(SM(curve=10.0,hz="stop",hzd=20.0),True,False,25.0,0.0,30.0)
check("param off -> V_CRUISE_UNSET + no hazard accel", c.output_v_target==UNSET and c.output_hazard_accel is None)
P.vals["SmartCruiseControlMap"]=True

# 8 long disabled -> inert
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(SM(curve=10.0),False,False,25.0,0.0,30.0)
check("long_enabled False -> UNSET", c.output_v_target==UNSET)

# 9 override -> not active
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(SM(curve=10.0),True,True,25.0,0.0,30.0)
check("long_override -> overriding, not active", c.state==scc_map.MapState.overriding and c.output_v_target==UNSET)

# 10 nothing ahead -> inert
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(SM(),True,False,25.0,0.0,30.0)
check("clear road -> UNSET (no-op ceiling)", c.output_v_target==UNSET)

print(f"\n{sum(1 for _,c_ in res if c_)}/{len(res)} passed")
sys.exit(0 if all(c_ for _,c_ in res) else 1)
