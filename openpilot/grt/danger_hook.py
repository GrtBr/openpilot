"""
Hook 12 -- `danger_hook`: early braking for a stopped or slow lead that first appears far away.

Hand-off and evidence: DANGER_HOOK_HANDOFF.md (repo root); analysis/lead_filter/tracker/FINDINGS.md sections
48-57 (the hook-12 series). Operator decisions D1-D5, 2026-09-30.

WHAT IT IS FOR. On this camera-only Staria a stopped or slow vehicle is first published at roughly 130-160 m true
range while we travel at 20-30 m/s. Beyond ~90 m the camera range is COMPRESSED (it moves ~0.43 m per true metre; a
stopped car at 151 m read 110 m) and the camera's lead speed is wrong (a stopped car read 65 km/h). Hook 11 arms in
time but its closing-speed estimate lags the compressed range, so it brakes softly (-0.2..-0.6) and stock ends up
braking at -3.5 m/s^2 near the car (27 Sep bookmark 1: -3.71 peak, 2.7 m final gap). Hook 12 triggers on a signal
that does not come from the lead range -- the driving model's own brake prediction, with speed and distance --
brakes firmly at once, decides after 2 s whether the lead is really stopped/slow, then escalates or releases.

WHAT IT IS NOT. It does not detect stopped leads earlier than hook 11: it triggers ~1 s after the lead is published
(bm1 0.95 s, 1fc 1.05 s) and hook 11 arms at about the same moment (bm1 0.15 s later, 1fc 1.1 s earlier). The gain
is braking level and a stopped-or-moving verdict, not detection time. Timing: trigger 0.5 s after the gate first
holds; the command ramps to -A1 at JERK (reaches -1.0 about 0.7 s after trigger); the verdict comes 2.0 s after
trigger. With A1 1.0 (D1, the evaluation-period setting) hook 12 cannot stop a bm1-type approach on its own -- it
relies on stock, which is always in the planner's min(), to finish (replay: gap 2.7 -> 4.8 m, peak -3.71 -> -3.48).

GATE (all, GATE_HOLD consecutive frames): brake-disengage probability at t = 6 s >= P6_GATE, v_ego > V_GATE, a lead
published with dRel >= D_GATE, longActive, no pedal, personality relaxed (D3), and a camera range available.

STAGE 1 -A1 (jerk-limited). 2 s TEST from trigger + TEST_S: predict where a STOPPED lead's camera range would be given
our wheel distance and the far compression (KNEE_M, KNEE_SLOPE); f = observed camera drop / predicted drop.
f >= F_STOP -> 'stopped': target the stopped-car need at the camera range, which saturates at CAP above ~25 m/s
(intended: the camera reads short far out, so this errs toward more braking). f < F_STOP -> 'moving': release.
The compression constants are a hypothesis test only; they never "correct" a range that is then braked on.
STAGE 2 when the 0.5 s median camera range < ENTRY (never on one frame): stopped-car-first until WAIT fresh samples,
then the lead's ground speed from the slope of (camera range + our distance) over 1 s, the lowest of the last second;
need = c^2 / (2 (z - (1.75 v_lead + 6))), never softer than -A1 while the median is >= TRUST or still closing.
RELEASES: lead pulling away (camera range growing against our travel, held D_NEG_S); closing ended in stage 2; 5 s in
stage 1 without the lead inside 90 m; 2 s test 'moving'; lead lost > LEAD_LOST_S; pedal; longActive off; personality.
PEDALS FIRST (D4/D5): throttle -> release, outcome "false"; brake -> release, outcome "fail" (both -> "fail"); checked
before longActive and personality every frame, and a pedal within 5 frames of any other release claims that release.
These are the operator's review labels, not a verdict on the hook.

Output: [(cmd, LongitudinalPlanSource.lead0, should_stop(v_ego, cmd))] while triggered, else []. No hand-off to stock:
it stays until it releases and the planner's min() means it can never make braking weaker. On release, [] at once.
A frame without a camera range while triggered holds the previous command (no step, no release); pedals,
longActive and lead lost are still checked first, so it stays bounded. A failure to READ sm returns [] (never a hold).

KNOWN LIMITS (DANGER_HOOK_HANDOFF.md section 11): only two stopped-traffic events in the data (bm1, 1fc); P6_GATE is
bm1's own minimum and KNEE_SLOPE is from bm1; no data at 27 m/s; a model update can change both the brake
prediction's scale and the far compression -- re-check P6_GATE and the stopped-lead f after any model change.

DangerController is the pure controller, a line-for-line port of analysis/lead_filter/early_mode.py EarlyMode (the
replays import it as `from openpilot.grt.danger_hook import DangerController as EarlyMode`). DangerHook is the adapter.
"""
import json
import os

from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalPlanSource
from openpilot.selfdrive.controls.lib.drive_helpers import should_stop

# ---- gate (FINDINGS 48a-b) ----
P6_GATE = 0.046              # brake-disengage prob at t = 6 s; bm1's sustained minimum in its first second after
                             # publication (48b) -- FITTED TO ONE EVENT; re-check after any model change
P6_T = 6.0                   # the prediction horizon read; its index is looked up in disengagePredictions.t every frame
GATE_HOLD = 10               # frames (0.5 s) the whole gate must hold (48b)
V_GATE = 21.0                # m/s; bm1 was 21.7 (48a) -- do not raise without new data
D_GATE = 100.0               # m, published dRel (48a-b)

# ---- braking (FINDINGS 52, 55, 57; operator D1, D2) ----
A1 = 1.0                     # m/s^2, stage-1 level and floor. D1: 1.0 for the practical evaluation period (2.0 validated)
CAP = 2.5                    # m/s^2, hardest command (D2); ACCEL_MIN -3.5 after the min() is harder
JERK = 1.5                   # m/s^3 on the falling edge only (hook 11's JERK_ARM); a rise is immediate
WAIT = 10                    # fresh stage-2 frames before the lead-speed estimate is used (49c, 49e)

# ---- 2 s stopped-or-moving test (FINDINGS 53, 55; compression 44c) ----
TEST_S = 2.0                 # s after trigger (1 s is too noisy, 53)
F_STOP = 0.5                 # f at or above -> stopped
KNEE_M = 90.0                # m; beyond this the camera range is compressed ...
KNEE_SLOPE = 0.43            # ... moving this much per true metre (bm1; 1fc fits it independently)
TEST_MIN_DROP_M = 3.0        # predicted drop below this -> no verdict yet (we barely moved); try again next frame

# ---- stages and releases (FINDINGS 49a-b, 51, 52) ----
ENTRY_M = 85.0               # stage 2 when the 0.5 s median camera range is below this (never one frame)
TRUST_M = 60.0               # the A1 floor holds while the median is at or above this
CALM_S = 1.0                 # stage 2: closing <= CALM_MPS this long with the median inside TRUST_M -> release
CALM_MPS = 0.5
D_NEG = -0.1                 # pulling away: camera shrink per metre driven below this ...
D_NEG_S = 1.0                # ... for this long (compression can slow the shrink, never reverse it)
D_MIN_TRAVEL_M = 3.0         # the shrink ratio needs this much travel inside its 20-frame window
STAGE1_MAX_S = 5.0           # stage 1 this long ...
STAGE1_FAR_M = 90.0          # ... with the median still beyond this -> release
LEAD_LOST_S = 0.5            # hook 11's value
REARM_S = 1.0                # after a release the gate must be False this long, continuously (hook 11's RE_ARM_HOLD_S)
ZMED_N = 10                  # frames in the 0.5 s trailing median
D_N = 20                     # frames in the shrink-ratio window
PEDAL_WINDOW = 5             # frames (0.25 s) a pedal may still claim a release made for another reason

# ---- logging (DANGER_HOOK_HANDOFF.md section 6) ----
LOG_MAX_BYTES = 4 * 1024 * 1024
HEARTBEAT_S = 30.0


def _median(v):
  s = sorted(v)
  n = len(s)
  return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


def _mean(v):
  return sum(v) / len(v)


class DangerController:
  """The triggered state only: stage 1, the 2 s test, stage 2, pulling-away / calm / timeout releases, jerk shaping.
  No sm, no I/O. Line-for-line port of analysis/lead_filter/early_mode.py EarlyMode (same keyword names); the
  literals there are the named constants above. `step(t, v, zc, x)` -> command (<= 0), or 0.0 once released.
  `why` keeps EarlyMode's text (replays match on it); `reason` is the log code."""

  def __init__(self, A1=A1, CAP=CAP, WAIT=WAIT, JERK=JERK, ENTRY=ENTRY_M, TRUST=TRUST_M, DT=DT_MDL, TEST=True,
               F_STOP=F_STOP, z0=None):
    self.A1, self.CAP, self.WAIT, self.JERK, self.ENTRY, self.TRUST, self.DT = A1, CAP, WAIT, JERK, ENTRY, TRUST, DT
    self.stage = 1; self.rel = False; self.cmd = 0.0; self.zh = []; self.dh = []; self.zb = []; self.vh = []
    self.calm = 0.0; self.neg = 0.0; self.t1 = 0.0; self.why = None; self.reason = None
    self.TEST, self.F_STOP, self.z0 = TEST, F_STOP, z0; self.t0 = None; self.x0 = None; self.verdict = None; self.f = None
    self.ds = None; self.pred = None; self.zmed = None          # for the verdict log record

  def _release(self, reason, why):
    self.rel = True; self.reason = reason; self.why = why
    return 0.0

  def step(self, t, v, zc, x):
    if self.rel:
      return 0.0
    DT = self.DT
    if self.t0 is None:
      self.t0, self.x0 = t, x
      if self.z0 is None:
        self.z0 = zc
    self.zh.append(zc); self.zh = self.zh[-ZMED_N:]; zmed = _median(self.zh); self.zmed = zmed
    self.dh.append((x, zc)); self.dh = self.dh[-D_N:]
    if len(self.dh) == D_N and self.dh[-1][0] - self.dh[0][0] > D_MIN_TRAVEL_M:
      xs = [p[0] for p in self.dh]; zs = [p[1] for p in self.dh]; mx, mz = _mean(xs), _mean(zs)
      D = -sum((a - mx) * (b - mz) for a, b in zip(xs, zs)) / sum((a - mx) ** 2 for a in xs)
      self.neg = self.neg + DT if D < D_NEG else 0.0
    if self.neg >= D_NEG_S:
      return self._release("pulling_away", "lead pulling away")
    if self.stage == 1 and self.TEST and self.verdict is None and t - self.t0 >= TEST_S:
      ds = x - self.x0; z0 = self.z0
      tr0 = z0 if z0 <= KNEE_M else KNEE_M + (z0 - KNEE_M) / KNEE_SLOPE; tr = tr0 - ds
      pred = tr if tr <= KNEE_M else KNEE_M + (tr - KNEE_M) * KNEE_SLOPE
      if z0 - pred > TEST_MIN_DROP_M:
        self.f = (z0 - zmed) / (z0 - pred); self.ds, self.pred = ds, pred
        self.verdict = "stopped" if self.f >= self.F_STOP else "moving"
        if self.verdict == "moving":
          return self._release("test_moving", f"2 s test: moving (f {self.f:.2f})")
    if self.stage == 1:
      tgt = -self.A1; self.t1 += DT
      if self.verdict == "stopped":
        tgt = -min(max(v * v / (2 * max(zmed - 6, 1.0)), self.A1), self.CAP)
      if zmed < self.ENTRY:
        self.stage = 2; self.zb = []
      elif self.t1 > STAGE1_MAX_S and zmed > STAGE1_FAR_M:
        return self._release("timeout_far", "5 s, lead not inside 90 m")
    if self.stage == 2:
      self.zb.append((t, zc + x)); self.zb = [p for p in self.zb if t - p[0] <= 1.0]
      if len(self.zb) >= self.WAIT:
        ts = [p[0] for p in self.zb]; gs = [p[1] for p in self.zb]; mt, mg = _mean(ts), _mean(gs)
        est = max(0.0, sum((a - mt) * (b - mg) for a, b in zip(ts, gs)) / sum((a - mt) ** 2 for a in ts))
        self.vh.append((t, est)); self.vh = [p for p in self.vh if t - p[0] <= 1.0]; vle = min(p[1] for p in self.vh)
        c = v - vle; need = c * c / (2 * max(zc - (1.75 * vle + 6), 1.0)) if c > 0 else 0.0
        floor = self.A1 if (zmed >= self.TRUST or c > CALM_MPS) else 0.0
        tgt = -min(max(need, floor), self.CAP)
        self.calm = self.calm + DT if (c <= CALM_MPS and zmed < self.TRUST) else 0.0
        if self.calm >= CALM_S:
          return self._release("closing_ended", "closing ended")
      else:
        tgt = -min(max(v * v / (2 * max(zc - 6, 1.0)), self.A1), self.CAP)
    self.cmd = tgt if tgt >= self.cmd else max(tgt, self.cmd - self.JERK * DT)
    return self.cmd


class _RotatingLog:
  """JSON lines, rolled at LOG_MAX_BYTES to ONE `.1` file with a `rotated` record -- a copy of hooks._lead_write,
  for its reason (a cap that stops writing silently once produced a wrong analysis). On a write failure it latches
  off for the drive and reports once through `log_exception`. path None -> logging off (tests, replays)."""

  def __init__(self, path, log_exception=None):
    self.path = path
    self.log_exception = log_exception
    self.dead = path is None

  def write(self, record: dict) -> None:
    if self.dead:
      return
    try:
      rolled = False
      try:
        if os.path.getsize(self.path) >= LOG_MAX_BYTES:
          os.replace(self.path, self.path + ".1")
          rolled = True
      except FileNotFoundError:
        pass
      os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
      with open(self.path, "a") as f:
        if rolled:
          f.write(json.dumps({"ev": "rotated", "why": f"reached {LOG_MAX_BYTES} bytes"}) + "\n")
        f.write(json.dumps(record) + "\n")
    except Exception:
      self.dead = True
      if self.log_exception is not None:
        try:
          self.log_exception("danger_hook log write; logging disabled for this drive")
        except Exception:
          pass


def _p6_from(dp):
  """disengagePredictions -> the brake-disengage probability at P6_T, or None (index looked up every frame)."""
  ts = list(dp.t)
  probs = list(dp.brakeDisengageProbs)
  for i, tt in enumerate(ts):
    if abs(float(tt) - P6_T) < 1e-3:
      return float(probs[i]) if i < len(probs) else None
  return None


class DangerHook:
  """The adapter: gate, trigger, eligibility, lead lost, re-trigger hold, pedal outcomes, logging. One instance,
  stepped every planner frame. `step()` takes plain values (tests, replays); `step_sm()` reads them from `sm`."""

  def __init__(self, log_path=None, log_exception=None):
    self.log = _RotatingLog(log_path, log_exception)
    self.log_exception = log_exception
    self.x = 0.0                    # odometry, previous speed x DT (as the validated replay integrates it)
    self.v_prev = None
    self.zbuf = []                  # last ZMED_N camera ranges, every frame, for z0 at trigger
    self.gate_run = 0
    self.hold_frames = 0            # consecutive gate-False frames since a release
    self.need_hold = False
    self.ctl = None
    self.ev = None                  # the open event's bookkeeping, for the release record
    self.lost_frames = 0
    self.last_cmd = 0.0
    self.pending = None             # [release record, frames left] -- a pedal may still claim it
    self.n_trig = 0
    self.n_rel = {}
    self.last_hb = None

  # -------------------------------------------------------------------------------- sm reads
  def step_sm(self, sm, v_ego: float, now: float) -> list:
    try:
      from openpilot.grt.far_lead import MODEL_RANGE_OFFSET
      personality = str(sm['selfdriveState'].personality)
      long_active = bool(sm['carControl'].longActive)
      cs = sm['carState']
      gas, brake = bool(cs.gasPressed), bool(cs.brakePressed)
      lead = sm['radarState'].leadOne
      present, dRel = bool(lead.present), float(lead.dRel)
      mv = sm['modelV2']
      p6 = _p6_from(mv.meta.disengagePredictions)
      z = None
      l3 = mv.leadsV3
      if len(l3) > 0 and len(l3[0].x) > 0:
        z = float(l3[0].x[0]) - MODEL_RANGE_OFFSET
    except Exception:
      # [] -- never hold braking here: the pedal, longActive and lead-lost releases all read this same sm, so a
      # persistent read failure while holding would leave no release path (DANGER_HOOK_HANDOFF.md section 0.5).
      if self.log_exception is not None:
        self.log_exception("danger_hook sm read")
      return []
    return self.step(now, p6, present, dRel, z, float(v_ego), long_active, gas, brake, personality)

  def _hold_output(self, v_ego):
    """A triggered frame with no camera range: keep the previous command. Safe because the pedal, longActive and
    lead-lost checks have already run on this frame (lead lost bounds it)."""
    if self.ctl is not None and self.last_cmd < 0.0:
      return [(self.last_cmd, LongitudinalPlanSource.lead0, should_stop(v_ego, self.last_cmd))]
    return []

  # -------------------------------------------------------------------------------- one frame
  def step(self, now, p6, present, dRel, z, v_ego, long_active, gas, brake, personality="relaxed") -> list:
    if self.v_prev is not None:
      self.x += self.v_prev * DT_MDL
    self.v_prev = v_ego
    if z is not None:
      self.zbuf.append(z); self.zbuf = self.zbuf[-ZMED_N:]
    relaxed = personality == "relaxed"
    pedal = "brake" if brake else ("gas" if gas else None)
    self._heartbeat(now)

    # a release made for another reason in the last PEDAL_WINDOW frames: a pedal now claims it
    if self.pending is not None:
      if pedal is not None:
        rec = self.pending[0]
        rec["reason"], rec["outcome"] = ("driver_brake", "fail") if pedal == "brake" else ("driver_gas", "false")
        rec["t_pedal"] = round(now, 2)       # t stays the release frame; this is the pedal frame, for the video
        self._write_release(rec)
        self.pending = None
      else:
        self.pending[1] -= 1
        if self.pending[1] <= 0:
          self._write_release(self.pending[0])
          self.pending = None

    if self.ctl is None:
      gate = (p6 is not None and p6 >= P6_GATE and v_ego > V_GATE and present and dRel >= D_GATE
              and long_active and pedal is None and relaxed and z is not None)
      if self.need_hold:
        self.hold_frames = 0 if gate else self.hold_frames + 1
        if self.hold_frames >= round(REARM_S / DT_MDL):
          self.need_hold = False; self.hold_frames = 0
        self.gate_run = 0
        return []
      self.gate_run = self.gate_run + 1 if gate else 0
      if self.gate_run < GATE_HOLD:
        return []
      self._trigger(now, v_ego, dRel, p6, personality)

    # ---- triggered. Pedals FIRST (D4/D5), then longActive, personality, lead lost.
    reason = None
    if pedal == "brake":
      reason = "driver_brake"
    elif pedal == "gas":
      reason = "driver_gas"
    elif not long_active:
      reason = "disengaged"
    elif not relaxed:
      reason = "personality"
    else:
      self.lost_frames = self.lost_frames + 1 if not present else 0
      if self.lost_frames > round(LEAD_LOST_S / DT_MDL):
        reason = "lead_lost"
    if reason is None:
      if z is None:
        return self._hold_output(v_ego)
      stage0, verdict0 = self.ctl.stage, self.ctl.verdict
      cmd = self.ctl.step(now, v_ego, z, self.x)
      if self.ctl.verdict is not None and verdict0 is None:
        c = self.ctl
        self.log.write({"ev": "verdict", "t": round(now, 2), "f": round(c.f, 3), "verdict": c.verdict,
                        "ds": round(c.ds, 1), "pred": round(c.pred, 1), "zmed": round(c.zmed, 1)})
        self.ev["verdict"], self.ev["f"] = c.verdict, round(c.f, 3)
      if self.ctl.stage == 2 and stage0 == 1:
        self.log.write({"ev": "stage2", "t": round(now, 2), "zmed": round(self.ctl.zmed, 1), "v_ego": round(v_ego, 2)})
      if present:
        self.ev["min_dRel"] = min(self.ev["min_dRel"], dRel) if self.ev["min_dRel"] is not None else dRel
      if self.ctl.rel:
        reason = self.ctl.reason
      else:
        self.ev["hardest"] = min(self.ev["hardest"], cmd)
        self.last_cmd = cmd
        return [(cmd, LongitudinalPlanSource.lead0, should_stop(v_ego, cmd))]
    self._release(now, reason, v_ego)
    return []

  # -------------------------------------------------------------------------------- events
  def _trigger(self, now, v_ego, dRel, p6, personality):
    z0 = _median(self.zbuf)
    self.ctl = DangerController(z0=z0)
    self.gate_run = 0
    self.lost_frames = 0
    self.last_cmd = 0.0
    self.n_trig += 1
    self.ev = {"t0": now, "v0": v_ego, "min_dRel": dRel, "hardest": 0.0, "verdict": None, "f": None}
    self.log.write({"ev": "trigger", "t": round(now, 2), "v_ego": round(v_ego, 2), "dRel": round(dRel, 1),
                    "z0": round(z0, 1), "zmed": round(z0, 1), "p6": round(p6, 4), "personality": personality})

  def _release(self, now, reason, v_ego):
    e, c = self.ev, self.ctl
    outcome = {"driver_brake": "fail", "driver_gas": "false"}.get(reason, "ok")
    rec = {"ev": "release", "t": round(now, 2), "reason": reason, "outcome": outcome,
           "dur_s": round(now - e["t0"], 2), "v_at_trigger": round(e["v0"], 2), "v_at_release": round(v_ego, 2),
           "min_dRel": round(e["min_dRel"], 1) if e["min_dRel"] is not None else None,
           "hardest_cmd": round(e["hardest"], 3), "stage": c.stage, "verdict": e["verdict"], "f": e["f"]}
    self.ctl = None
    self.ev = None
    self.last_cmd = 0.0
    self.need_hold = True
    self.hold_frames = 0
    self.gate_run = 0
    if outcome == "ok":
      self.pending = [rec, PEDAL_WINDOW]      # a pedal in the next PEDAL_WINDOW frames claims this release
    else:
      self._write_release(rec)

  def _write_release(self, rec):
    self.n_rel[rec["reason"]] = self.n_rel.get(rec["reason"], 0) + 1
    self.log.write(rec)

  def _heartbeat(self, now):
    if self.last_hb is None or now - self.last_hb >= HEARTBEAT_S:
      self.last_hb = now
      self.log.write({"ev": "hb", "t": round(now, 2), "triggers": self.n_trig, "releases": dict(self.n_rel),
                      "active": self.ctl is not None})
