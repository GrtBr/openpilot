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
    # field names MUST match the real cereal schema (radarState.LeadData.present)
    lead=lambda d:NS(present=(d is not None),dRel=(d or 0.))
    return {'mapdOut':NS(mapCurveSpeed=curve,speedLimitSuggestedSpeed=sl,nextHazard=hz,
                         nextHazardDistance=hzd,nextSpeedLimit=nsl,nextSpeedLimitDistance=nsld,
                         suggestedSpeed=99.),
            'radarState':NS(leadOne=lead(l1),leadTwo=lead(l2))}

UNSET=255.0; ok=lambda c: "PASS" if c else "**FAIL**"
res=[]
def check(name,cond): res.append((name,cond)); print(f"  {ok(cond):9s} {name}")

# 1 curve speed becomes the ceiling. The ceiling is RATE-LIMITED downward at APPROACH_DECEL
# (see _ramp_curve_ceiling), so it starts at v_ego and converges on the curve speed rather
# than stepping to it — hold v_ego and let it run to convergence.
c=scc_map.SmartCruiseControlMap()
for _ in range(600): c.update(SM(curve=15.0),True,False,25.0,0.0,30.0)
check("curve speed -> v_target converges to 15, active, ceiling applied",
      abs(c.v_target-15.0)<1e-6 and c.is_active and abs(c.output_v_target-15.0)<1e-6)

# 2 speed limit (Phase 6) lowers target, and is taken over a higher curve speed
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(SM(curve=25.0,sl=13.9),True,False,30.0,0.0,33.0)
check("speedLimitSuggestedSpeed wins when lower than curve", abs(c.v_target-13.9)<1e-6)

# 3 hazard engages at close range with no lead, and yields firm decel
c=scc_map.SmartCruiseControlMap()
for _ in range(5): c.update(SM(hz="stop",hzd=30.0),True,False,15.0,0.0,25.0)
# v_target is now the APPROACH PROFILE, not the raw target: at 30 m from a 5.55 m/s hazard
# it should be sqrt(5.55^2 + 2*0.5*30) ~ 7.80 m/s, i.e. ABOVE the final target.
_expect = scc_map.approach_speed(5.55, 30.0)
check("hazard engages -> v_target is the approach profile (not a step to target)",
      c.hazard_active and abs(c.v_target-_expect)<1e-6 and c.v_target > 5.55)
check("hazard yields output_hazard_accel (adaptive, in [-1.5,-0.3])",
      c.output_hazard_accel is not None and -1.5<=c.output_hazard_accel<=-0.3)
# At the hazard itself (distance ~0) the profile collapses to the raw target, and the MIN_V
# floor then applies.
c2=scc_map.SmartCruiseControlMap()
for _ in range(5): c2.update(SM(hz="stop",hzd=0.4),True,False,15.0,0.0,25.0)
check("at the hazard, profile -> target and MIN_V floor applies",
      abs(c2.output_v_target-scc_map.MIN_V)<1e-6)

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

# --- approach profile maths (the fix for "too aggressive / reaches target too early") ---
ap=scc_map.approach_speed
check("profile at distance 0 == target exactly", abs(ap(5.55,0.0)-5.55)<1e-9)
check("profile is monotonically increasing with distance", ap(5.55,10)<ap(5.55,50)<ap(5.55,200))
# decelerating at APPROACH_DECEL from the profile speed must land ON target at the hazard
import math as _m
for d in (30.0,120.0,400.0):
    v=ap(5.55,d)
    landed=_m.sqrt(max(0.0,v*v-2*scc_map.APPROACH_DECEL*d))
    check(f"profile from {d:.0f}m lands on target at the hazard", abs(landed-5.55)<1e-6)
check("implied decel equals APPROACH_DECEL (gentle, ~3x softer than the first drive)",
      abs(((ap(5.55,200)**2-5.55**2)/(2*200))-scc_map.APPROACH_DECEL)<1e-9)


# --- AUTHORISATION GATE (2026-07-30) -------------------------------------------------------
# scc_map must obey only limits the set-speed feature has AUTHORISED, so a limit the driver
# declined or never answered is not acted on physically either. Measured cause of the reported
# issue: 1,069 frames of posted-limit ceiling + 95 frames of pre-sign ramp with the set speed
# still at 105 awaiting confirmation.
class GSM(dict):
    """SubMaster stub that also carries grtSetSpeedState."""
    def __init__(self, base, authorised_kph=None, active=True, alive=True, next_kph=None):
        super().__init__(base)
        self['grtSetSpeedState'] = NS(authorisedLimit=(authorised_kph or 0.0), active=active,
                                     pending=False, pendingLimit=0.0, secondsLeft=0.0,
                                     setSpeed=0.0, tracking=True, pendingIsIncrease=False,
                                     authorisedNextLimit=(next_kph or 0.0))
        self.alive = {'mapdOut': True, 'grtSetSpeedState': alive}
        self.valid = {'mapdOut': True, 'grtSetSpeedState': True}

# fail OPEN when the message is absent -- infrastructure failure must not stop limit compliance
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(SM(sl=13.9),True,False,30.0,0.0,33.0)
check("no grtSetSpeedState -> FAILS OPEN, obeys mapd as before", abs(c.v_target-13.9)<1e-6)

c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(GSM(SM(sl=13.9),authorised_kph=50.0,alive=False),True,False,30.0,0.0,33.0)
check("stale grtSetSpeedState -> FAILS OPEN", abs(c.v_target-13.9)<1e-6)

c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(GSM(SM(sl=13.9),authorised_kph=50.0,active=False),True,False,30.0,0.0,33.0)
check("feature not active -> FAILS OPEN", abs(c.v_target-13.9)<1e-6)

# gated: the steady-state posted-limit ceiling is GONE. The limit reaches the car through the
# SET SPEED instead, so this branch must never pull v_target down (2026-08-07).
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(GSM(SM(sl=13.9),authorised_kph=50.0),True,False,30.0,0.0,33.0)
check("gated -> no steady-state posted-limit ceiling, even for an AUTHORISED limit",
      c.v_target==0.0 and c.output_v_target==UNSET)

# THE REPORTED BUG, 2026-08-07: map says 60, driver sets cruise to 100, car was dragged back to
# 60 because the stale authorisation pinned v_target. v_cruise here is 100 km/h = 27.8 m/s.
c=scc_map.SmartCruiseControlMap()
for _ in range(5): c.update(GSM(SM(sl=60/3.6),authorised_kph=60.0),True,False,27.8,0.0,27.8)
check("a stale 60 authorisation can NO LONGER hold the car below a driver-set 100",
      c.v_target==0.0 and c.output_v_target==UNSET)

# gated with NOTHING authorised: the limit must not be acted on at all
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(GSM(SM(sl=13.9),authorised_kph=0.0),True,False,30.0,0.0,33.0)
check("gated + nothing authorised -> limit NOT obeyed (the reported bug)",
      c.v_target==0.0 and c.output_v_target==UNSET)

# curve/hazard braking must be entirely unaffected by the ceiling removal -- they are not
# speed limits. (Asserted again below with authorised_kph=0.)
# (the curve ceiling is rate-limited downward, so let it converge -- as test 1 does)
c=scc_map.SmartCruiseControlMap()
for _ in range(600): c.update(GSM(SM(curve=15.0,sl=13.9),authorised_kph=50.0),True,False,25.0,0.0,30.0)
check("gated -> a CURVE still lowers the target (only the limit ceiling went)",
      abs(c.v_target-15.0)<0.05)

# the pre-sign approach ramp is off while gated (it acts on the UPCOMING limit, which cannot
# have been authorised yet -- documented trade-off in scc_map)
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(GSM(SM(nsl=5.55,nsld=80.0),authorised_kph=0.0),True,False,20.0,0.0,30.0)
check("gated -> the pre-sign approach ramp does not fire", c.v_target==0.0)
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(SM(nsl=5.55,nsld=80.0),True,False,20.0,0.0,30.0)
check("ungated -> the approach ramp still fires (unchanged when the feature is off)",
      c.v_target>5.55 and c.v_target<20.0)

# curve and hazard braking are NOT gated -- they are not speed limits
c=scc_map.SmartCruiseControlMap()
for _ in range(600): c.update(GSM(SM(curve=15.0),authorised_kph=0.0),True,False,25.0,0.0,30.0)
check("curve braking is NOT gated by authorisation", abs(c.v_target-15.0)<1e-6)
c=scc_map.SmartCruiseControlMap()
for _ in range(5): c.update(GSM(SM(hz="stop",hzd=30.0),authorised_kph=0.0),True,False,15.0,0.0,25.0)
check("hazard braking is NOT gated by authorisation", c.hazard_active)


# The ramp comes BACK when the upcoming limit is pre-authorised (it would auto-adopt anyway).
# 5.55 m/s == 20 km/h.
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(GSM(SM(nsl=5.55,nsld=80.0),authorised_kph=0.0,next_kph=20.0),True,False,20.0,0.0,30.0)
check("gated + PRE-AUTHORISED upcoming limit -> the ramp fires again",
      c.v_target>5.55 and c.v_target<20.0)
# the pre-authorised value must NOT act as a ceiling -- that would be the harsh step
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(GSM(SM(sl=5.55),authorised_kph=0.0,next_kph=20.0),True,False,20.0,0.0,30.0)
check("a PRE-authorised upcoming limit is NOT used as a ceiling", c.v_target==0.0)
# a pre-authorisation for a DIFFERENT value must not unlock the ramp
c=scc_map.SmartCruiseControlMap()
for _ in range(3): c.update(GSM(SM(nsl=5.55,nsld=80.0),authorised_kph=0.0,next_kph=60.0),True,False,20.0,0.0,30.0)
check("a pre-authorisation for a different limit does not unlock the ramp", c.v_target==0.0)

# ---------------------------------------------------------------------------------------
# Curve-ceiling descent rate limit (_ramp_curve_ceiling). Added 2026-08-05 from drive data:
# mapd steps mapCurveSpeed (measured 85.2 -> 57.2 km/h in one frame at 72.7 km/h), which
# saturated A_CRUISE_MIN on 70% of curve events. Each assertion below mirrors a property
# claimed in the method's docstring.
DEC = scc_map.APPROACH_DECEL
DT = 0.05

# anchored at v_ego, NOT at the raw curve speed: no instantaneous step
c=scc_map.SmartCruiseControlMap()
c.update(SM(curve=15.9),True,False,20.2,0.0,30.0)          # the measured 72.7 -> 57.2 km/h case
check("first frame anchors at v_ego, not at the curve speed",
      abs(c.v_target-(20.2-DEC*DT))<1e-6)

# the descent rate IS APPROACH_DECEL
c=scc_map.SmartCruiseControlMap()
seq=[]
for _ in range(20):
    c.update(SM(curve=15.9),True,False,20.2,0.0,30.0); seq.append(c.v_target)
steps=[seq[i-1]-seq[i] for i in range(1,len(seq))]
check("ceiling descends at exactly APPROACH_DECEL",
      all(abs(s-DEC*DT)<1e-9 for s in steps))

# never overshoots below the raw target
c=scc_map.SmartCruiseControlMap()
for _ in range(2000): c.update(SM(curve=15.9),True,False,20.2,0.0,30.0)
check("ceiling converges to the raw curve speed and never undershoots it",
      abs(c.v_target-15.9)<1e-9)

# RISES are not limited -- curve ending must lift the ceiling immediately
c=scc_map.SmartCruiseControlMap()
for _ in range(40): c.update(SM(curve=15.9),True,False,20.2,0.0,30.0)
low=c.v_target
c.update(SM(curve=30.0),True,False,20.2,0.0,30.0)
check("a rising curve speed is applied immediately (never holds the car back)",
      c.v_target>low and abs(c.v_target-30.0)<1e-9)

# no curve -> ceiling resets, so the next curve re-anchors at v_ego rather than resuming
c=scc_map.SmartCruiseControlMap()
for _ in range(40): c.update(SM(curve=15.9),True,False,20.2,0.0,30.0)
c.update(SM(curve=0.0),True,False,20.2,0.0,30.0)
check("no curve -> v_target 0 and the ramp state resets", c.v_target==0.0 and c._curve_ceiling==0.0)
c.update(SM(curve=15.9),True,False,20.2,0.0,30.0)
check("next curve re-anchors at v_ego (no stale ceiling carried over)",
      abs(c.v_target-(20.2-DEC*DT))<1e-6)

# a stale HIGH ceiling must not delay braking: ceiling above v_ego is inert, so when the raw
# value drops below v_ego the ramp starts from v_ego, not from the old high value
c=scc_map.SmartCruiseControlMap()
for _ in range(20): c.update(SM(curve=23.7),True,False,20.2,0.0,30.0)   # 85.2 km/h, above v_ego
check("a ceiling above v_ego is passed through untouched", abs(c.v_target-23.7)<1e-9)
c.update(SM(curve=15.9),True,False,20.2,0.0,30.0)
check("step down from an above-v_ego ceiling still starts the ramp at v_ego",
      abs(c.v_target-(20.2-DEC*DT))<1e-6)

# self-escalation: the ceiling keeps descending even if the car does not slow, so the
# v_cruise - v_ego error grows and the planner brakes harder. Authority is never reduced.
c=scc_map.SmartCruiseControlMap()
errs=[]
for _ in range(60):
    c.update(SM(curve=8.0),True,False,20.2,0.0,30.0)   # car stubbornly holds 20.2 m/s
    errs.append(c.v_target-20.2)
check("ceiling keeps descending when the car does not slow (self-escalating)",
      errs[-1]<errs[0]<0 and all(errs[i]<errs[i-1] for i in range(1,len(errs))))

# a lower speed limit still wins over the ramped curve ceiling
c=scc_map.SmartCruiseControlMap()
for _ in range(10): c.update(SM(curve=15.9,sl=10.0),True,False,20.2,0.0,30.0)
check("a lower speed limit still beats the ramping curve ceiling", abs(c.v_target-10.0)<1e-9)

# time to converge is bounded by dv / APPROACH_DECEL -- it must land inside mapd's own
# trigger margin (target_speed_time_offset = 4 s on top of the jerk-limited distance)
c=scc_map.SmartCruiseControlMap()
n=0
while c.v_target != 15.9 and n < 10000:
    c.update(SM(curve=15.9),True,False,20.2,0.0,30.0); n+=1
t_conv=n*DT
expect=(20.2-15.9)/DEC
check(f"convergence time {t_conv:.1f}s == dv/APPROACH_DECEL ({expect:.1f}s)",
      abs(t_conv-expect)<0.1)

# ---------------------------------------------------------------------------------------
# T-junction validation from mapd path geometry (path_turn_deg / _update_t_junction_gate).
# Fixtures are the REAL bearings of two SA nodes, read from the PBF and cross-checked
# against the deployed tiles (both carry a T-Junction hazard today):
#
#   n36316730   -33.8306696, 20.0808104   crossbar R62(76.6) <-> R60-primary(260.3) = 176.3deg
#                                         stem R60-secondary(153.1). Hazard is INVERTED onto R62.
#   n5999476430 -34.0309952, 20.4399907   N2(235.4) + NR2/4(52.2) + NR2/4(60.8); the two
#                                         carriageways are 8.6deg apart. Not a junction at all.
import math as _mm

def _pt(node, bearing_deg, dist_m):
    """Point `dist_m` from `node` along `bearing_deg` (flat projection; fine under ~200 m)."""
    b = _mm.radians(bearing_deg)
    lat = node[0] + (dist_m * _mm.cos(b)) / 111320.0
    lon = node[1] + (dist_m * _mm.sin(b)) / (111320.0 * _mm.cos(_mm.radians(node[0])))
    return (lat, lon)

def _path(node, arrive_leg_bearing, depart_leg_bearing, span=120.0, step=10.0):
    """mapd-style path: CurrentWay nodes arriving along one leg, then NextWay nodes
    departing along another. Travel order, node shared once."""
    n = int(span // step)
    before = [_pt(node, arrive_leg_bearing, d) for d in range(int(span), 0, -int(step))]
    after = [_pt(node, depart_leg_bearing, d) for d in range(int(step), int(span) + 1, int(step))]
    return before + [node] + after

N_T = (-33.8306696, 20.0808104)     # n36316730
N_Y = (-34.0309952, 20.4399907)     # n5999476430

# --- pure geometry -----------------------------------------------------------------
# straight through the crossbar: arrive along R60-primary(260.3), depart along R62(76.6)
coords = _path(N_T, 260.3, 76.6)
car = _pt(N_T, 260.3, 80.0)
turn = scc_map.path_turn_deg(coords, car, 80.0)
check(f"n36316730 straight through crossbar -> {turn:.1f}deg (expect ~3.7)",
      turn is not None and turn < 8.0)

# on the stem: arrive along R60-secondary(153.1), depart along R62(76.6)
coords = _path(N_T, 153.1, 76.6)
car = _pt(N_T, 153.1, 80.0)
turn_stem = scc_map.path_turn_deg(coords, car, 80.0)
check(f"n36316730 stem -> crossbar -> {turn_stem:.1f}deg (expect ~103.5)",
      turn_stem is not None and turn_stem > 90.0)

# the Y: arrive along N2(235.4), depart along either NR2/4 carriageway
for leg, name in ((52.2, "636180741"), (60.8, "649706189")):
    coords = _path(N_Y, 235.4, leg)
    car = _pt(N_Y, 235.4, 80.0)
    t = scc_map.path_turn_deg(coords, car, 80.0)
    check(f"n5999476430 N2 -> carriageway {name} -> {t:.1f}deg (expect <6)",
          t is not None and t < 8.0)

check("honour threshold separates the real T from both false positives",
      turn < scc_map.TURN_HONOUR_DEG < turn_stem)

# --- fail-open paths ---------------------------------------------------------------
check("empty path -> None (honour)", scc_map.path_turn_deg([], car, 80.0) is None)
check("path shorter than the minimum -> None",
      scc_map.path_turn_deg([N_T, N_T], car, 80.0) is None)
check("car far off the published path -> None",
      scc_map.path_turn_deg(_path(N_T, 260.3, 76.6), (-30.0, 25.0), 80.0) is None)
check("hazard beyond the end of the path -> None",
      scc_map.path_turn_deg(_path(N_T, 260.3, 76.6), _pt(N_T, 260.3, 80.0), 5000.0) is None)

# --- end to end through the controller ---------------------------------------------
def SMT(hazard, hzd, coords, car, curve=0.0):
    base = SM(hz=hazard, hzd=hzd, curve=curve)
    base['mapdExtendedOut'] = NS(path=[NS(latitude=a, longitude=b) for a, b in coords])
    base['gpsLocationExternal'] = NS(latitude=car[0], longitude=car[1])
    return base

# straight through -> suppressed, no hazard braking
c = scc_map.SmartCruiseControlMap()
sm_ = SMT("T-Junction", 80.0, _path(N_T, 260.3, 76.6), _pt(N_T, 260.3, 80.0))
for _ in range(5): c.update(sm_, True, False, 25.0, 0.0, 30.0)
check("n36316730 driving straight through -> T-Junction SUPPRESSED",
      c._tj_suppressed and not c.hazard_active and c.output_hazard_accel is None)

# on the stem -> honoured, hazard engages
c = scc_map.SmartCruiseControlMap()
sm_ = SMT("T-Junction", 30.0, _path(N_T, 153.1, 76.6), _pt(N_T, 153.1, 30.0))
for _ in range(5): c.update(sm_, True, False, 15.0, 0.0, 25.0)
check("n36316730 approaching on the stem -> T-Junction HONOURED",
      not c._tj_suppressed and c.hazard_active)

# the Y -> suppressed
c = scc_map.SmartCruiseControlMap()
sm_ = SMT("T-Junction", 80.0, _path(N_Y, 235.4, 52.2), _pt(N_Y, 235.4, 80.0))
for _ in range(5): c.update(sm_, True, False, 25.0, 0.0, 30.0)
check("n5999476430 carriageway split -> T-Junction SUPPRESSED", c._tj_suppressed)

# a REAL surveyed hazard is never gated by geometry, even going straight
c = scc_map.SmartCruiseControlMap()
sm_ = SMT("stop", 30.0, _path(N_T, 260.3, 76.6), _pt(N_T, 260.3, 30.0))
for _ in range(5): c.update(sm_, True, False, 15.0, 0.0, 25.0)
check("a 'stop' hazard is NOT gated by the turn test", c.hazard_active and not c._tj_suppressed)

# no GPS fix -> fail open (honour), and the decision is not latched
c = scc_map.SmartCruiseControlMap()
sm_ = SMT("T-Junction", 30.0, _path(N_T, 260.3, 76.6), (0.0, 0.0))
for _ in range(5): c.update(sm_, True, False, 15.0, 0.0, 25.0)
check("no GPS fix -> hazard honoured (fails open)",
      not c._tj_suppressed and c.hazard_active and c._tj_turn_deg is None)

# missing mapdExtendedOut entirely -> fail open, counted, never raises
c = scc_map.SmartCruiseControlMap()
for _ in range(5): c.update(SM(hz="T-Junction", hzd=30.0), True, False, 15.0, 0.0, 25.0)
check("missing mapdExtendedOut -> honoured, error counted, no raise",
      not c._tj_suppressed and c.hazard_active and c._tj_errors > 0)

# latch clears when the announcement ends, so the next junction is judged afresh
c = scc_map.SmartCruiseControlMap()
sm_ = SMT("T-Junction", 80.0, _path(N_T, 260.3, 76.6), _pt(N_T, 260.3, 80.0))
for _ in range(3): c.update(sm_, True, False, 25.0, 0.0, 30.0)
assert c._tj_suppressed
c.update(SMT("", 0.0, _path(N_T, 260.3, 76.6), _pt(N_T, 260.3, 80.0)), True, False, 25.0, 0.0, 30.0)
check("latch resets when the T-Junction announcement ends",
      not c._tj_active and c._tj_turn_deg is None and not c._tj_suppressed)

print(f"\n{sum(1 for _,c_ in res if c_)}/{len(res)} passed")
sys.exit(0 if all(c_ for _,c_ in res) else 1)
