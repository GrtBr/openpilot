"""Flicker filter for this car's VISION-ONLY lead distance. Fork-owned; see captains_log.md
2026-09-02/03 for the full measurement record.

WHY THIS EXISTS
---------------
This Staria has NO RADAR. `HYUNDAI_STARIA_4TH_GEN` carries no radar DBC entry, so
`radarUnavailable` is True and `liveTracks` is a perfect 20 Hz stream of ZERO points -- verified
over 5.63 h of logs. Every lead therefore comes from `get_RadarState_from_vision()` in stock
radard.py, which publishes the model's raw `leadsV3[0].x[0]` with NO filtering at all, and the
mici HUD draws that raw value directly (`model_renderer.py`, `_update_leads`).

Measured on 5.63 h / 8 drives: 19.8% of far-lead frames move more than 3 m in one 20 Hz frame.
At 20 Hz a real lead cannot move more than |vRel| * 0.05 s -- 1.5 m even closing at 30 m/s -- and
the fastest genuine single-object motion observed anywhere in the corpus is 1.77 m/frame. So
every one of those excursions is measurement error, established by physics rather than by tuning.

WHY HAMPEL AND NOT A MEDIAN (this was measured, and it reversed an earlier recommendation)
-----------------------------------------------------------------------------------------
A median delays EVERY sample, including during a genuine approach. 18.1% of far-lead seconds
close faster than 5 m/s, so that lag is not a corner case. On the 09:57 FCW emergency -- the
fastest real closure in the corpus, 105 -> 3 m from 89 km/h -- the median chains read the lead
FARTHER away than it truly was at the worst moment: median-9 by 12.29 m, median-5 by 9.72 m,
against stock's 6.75 m. Reading a closing lead as farther than it is, is the unsafe direction.

Hampel substitutes ONLY samples it flags as outliers and passes clean samples through untouched,
so it does not lag a real ramp: 6.30 m on the same event, i.e. safer than stock. That is why the
primary stage is an outlier IDENTIFIER, not an order-statistic smoother.

REJECTED, with reasons, so they are not retried:
  * innovation CLAMP (limit the residual): applied before the gain it caps tracking at
    alpha * clamp = 6 m/s, and 18.1% of far-lead seconds exceed 5 m/s. It delayed the 14:56 arm
    from 1.40 s to 2.04 s.
  * innovation REJECTION (coast through outliers): diverges -- 929 ms lag, 5.35 m RMS.
  * xStd-weighted Kalman (commaai/openpilot#36965, sshane, closed/unmerged): worst filtered
    option here -- 2.27 m RMS, 518 ms lag. This model's xStd is uninformative at range, and ~27%
    of the worst excursions arrive with LOW reported std ("confidently wrong").

MEASURED RESULT of the tuning below, vs stock's `_RangeRateFilter(0.10, 0.003)`:
  RMS error vs a non-causal reference   0.89 m  vs  1.36 m
  FCW emergency, worst reads-too-far    6.30 m  vs  6.75 m
  >110 m band specifically, RMS         1.44 m  vs  1.93 m
  impossible jumps let through          41      vs  3      (raw signal: 2112)

NO openpilot imports at module level -- ON PURPOSE. The UI process does not import
`openpilot.grt` today, and `grt/hooks.py` pulls `selfdrive.car.cruise` at import time. Dragging
plannerd's dependency graph into the UI risks blanking the HUD on a bad boot. Keep this module
free-standing so both processes can import it cheaply.
"""
from collections import deque

# Must match openpilot.common.realtime.DT_MDL. Hardcoded rather than imported to keep this module
# free-standing (see module docstring); DT_MDL has been 0.05 for the life of this fork.
DT = 0.05

# ---- Hampel identifier (stage 1: remove impulses) -------------------------------------------
# WINDOW 7 (0.35 s): 5 and 9 were both measured; 7 was the best accuracy/latency trade.
# K 3.0: threshold is k * 1.4826 * MAD, the standard Hampel form.
# FLOOR 1.5 m: without it, a very quiet stretch drives MAD toward zero and the identifier starts
# rejecting good samples. 1.5 m is just above the 1.77 m/frame fastest genuine motion, so a real
# lead is never flagged as an outlier by the floor alone.
HAMPEL_N = 7
HAMPEL_K = 3.0
HAMPEL_FLOOR = 1.5

# ---- Range-rate stage (stage 2: smooth what survives) ---------------------------------------
# Faster than stock's (0.10, 0.003) BECAUSE stage 1 removed the impulses that forced stock to be
# slow. Swept over 19 dirty episodes; (0.20, 0.008) was the knee.
ALPHA = 0.20
BETA = 0.008


def _median(vals):
  s = sorted(vals)
  n = len(s)
  return s[n // 2] if n % 2 else 0.5 * (s[n // 2 - 1] + s[n // 2])


class Hampel:
  """Replace a sample by the local median only when it deviates by more than k * 1.4826 * MAD.
  Clean samples pass through UNCHANGED -- that is the whole point, and the reason this is used
  instead of a plain median (see module docstring)."""

  def __init__(self, n: int = HAMPEL_N, k: float = HAMPEL_K, floor: float = HAMPEL_FLOOR):
    self.n, self.k, self.floor = n, k, floor
    self.buf: deque = deque(maxlen=n)

  def reset(self) -> None:
    self.buf.clear()

  def update(self, z: float) -> float:
    self.buf.append(z)
    if len(self.buf) < 3:
      return z                                  # not enough history to judge; never delay a lock
    m = _median(self.buf)
    mad = 1.4826 * _median([abs(b - m) for b in self.buf])
    return z if abs(z - m) <= max(self.k * mad, self.floor) else m


class RangeRate:
  """Same [x, v] form as far_lead.py's `_RangeRateFilter`: alpha on position, beta on rate, fed a
  POSITION measurement. Kept separate from that class deliberately -- hook 11's arming filter is
  a validated, deployed component and is NOT modified by this work."""

  def __init__(self, alpha: float = ALPHA, beta: float = BETA):
    self.alpha, self.beta = alpha, beta
    self.x = None
    self.v = 0.0

  def reset(self) -> None:
    self.x = None
    self.v = 0.0

  def update(self, z: float):
    if self.x is None:
      self.x, self.v = z, 0.0
      return self.x, self.v
    x_pred = self.x + self.v * DT
    r = z - x_pred
    self.x = x_pred + self.alpha * r
    self.v = self.v + (self.beta / DT) * r
    return self.x, self.v


class LeadFilter:
  """Hampel -> RangeRate. One instance per consumer; deterministic, so two instances fed the same
  `radarState.leadOne` produce byte-identical output. That is what lets the HUD show exactly what
  the shadow log in plannerd recorded."""

  def __init__(self, alpha: float = ALPHA, beta: float = BETA):
    self.h = Hampel()
    self.rr = RangeRate(alpha, beta)

  def reset(self) -> None:
    self.h.reset()
    self.rr.reset()

  def update(self, present: bool, dRel: float):
    """Returns (x, v): filtered distance in m, and closing rate in m/s (negative = closing).
    Returns (None, 0.0) when no lead is present, and resets, so a stale estimate can never be
    drawn or consumed after the lead is lost."""
    if not present:
      self.reset()
      return None, 0.0
    return self.rr.update(self.h.update(float(dRel)))


# ---- Display helper -------------------------------------------------------------------------
# The UI holds its instances via this singleton so `model_renderer.py` needs one line per lead and
# no state of its own. Never raises: on any failure the caller keeps the raw value, which is
# precisely today's behaviour, so a bug here degrades to the status quo rather than to a blank HUD.
#
# PER-INDEX, and it MUST be called on absent frames too. Two mistakes were caught while wiring
# this up and are worth stating: leadOne and leadTwo sharing one instance would interleave two
# different objects into one filter, and calling only when `present` is True would leave stale
# state to be smoothed across a dropout -- so a lead reacquired at a different distance would be
# dragged toward the old one. Both are avoided by calling this for every lead, every frame.
_display: dict = {}
_last: dict = {}


def filtered_dRel(index: int, present: bool, dRel: float) -> float:
  """ADVANCE lead `index`'s filter by one frame and return the filtered distance.

  Call this EXACTLY ONCE PER FRAME per lead. It is a recursive filter, so calling it twice in a
  frame advances the state twice and changes the dynamics. Consumers that need the same frame's
  value again must use `last_dRel()` instead. Returns the raw value on any error, and passes
  `present=False` through so the filter resets on a dropout.
  """
  try:
    f = _display.get(index)
    if f is None:
      f = _display[index] = LeadFilter()
    x, _ = f.update(present, dRel)
    out = float(dRel) if x is None else float(x)
    _last[index] = out
    return out
  except Exception:
    return float(dRel)


def last_dRel(index: int, fallback: float) -> float:
  """Read the value `filtered_dRel` produced for this lead THIS frame, WITHOUT advancing the
  filter. Exists because the mici renderer consumes leadOne's distance twice per frame -- once for
  the chevron and once to clamp how far the driving path is drawn -- and the second consumer must
  not re-run the filter. Falls back to the caller's raw value if nothing has been cached yet.
  """
  try:
    v = _last.get(index)
    return float(fallback) if v is None else float(v)
  except Exception:
    return float(fallback)
