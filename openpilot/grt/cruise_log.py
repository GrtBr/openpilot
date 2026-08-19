"""TEMPORARY diagnostic: record the INTERNAL cruise inputs so the set-speed oscillation can
be fitted offline.

WHY THIS EXISTS
---------------
On 2026-08-19, 14:23-14:24, the car sat at 110 km/h against a 110 set speed and the command
reversed 70 times a minute. The plan source was `cruise` 89% of the window and `a_cmd`
correlated +0.979 with the planner's own output while correlating -0.292 with the model -- so
the cruise branch was producing it.

But it could not be reconstructed offline. `a_cruise = clip(v_cruise - v_ego, -1.2, 2.0)`
needs the INTERNAL v_cruise, i.e. AFTER `limit_v_cruise` has applied the mapd ceiling, and
that value is not in any logged message. `carState.vCruise` is the driver-facing set speed and
is NOT the same thing -- on 08-18 the dash read 110 while the internal target was ~53.

An attempt to reconstruct it from `carState.vCruise` produced a smooth signal (0 rev/min) that
did not reproduce the symptom at all, which briefly looked like the mechanism being wrong. It
was a missing input, not a wrong theory. This file closes that blind spot.

WHAT IT WRITES
--------------
One CSV row per planner tick (20 Hz) to <GRT_CONFIG_DIR>/cruise_log.csv:

    wall_time, v_cruise_internal_ms, v_ego_ms, a_cruise_raw

Buffered and flushed every _FLUSH_N rows so the 20 Hz planner never waits on the filesystem,
and capped at _MAX_BYTES so a forgotten enable cannot fill the device.

THIS IS TEMPORARY. Remove it once the cruise filter is fitted and shipped. It changes no
behaviour: the hook it lives in returns its input untouched.
"""
import os
import time

_FLUSH_N = 100            # rows, == 5 s at 20 Hz
_MAX_BYTES = 50 * 1024 * 1024


class CruiseLog:
  """One instance, owned by grt.hooks. Never raises into plannerd."""

  def __init__(self):
    self.buf = []
    self.path = None
    self.bytes = 0
    self.dead = False
    try:
      from openpilot.grt.registry import GRT_CONFIG_DIR
      os.makedirs(GRT_CONFIG_DIR, exist_ok=True)
      self.path = os.path.join(GRT_CONFIG_DIR, "cruise_log.csv")
      self.bytes = os.path.getsize(self.path) if os.path.exists(self.path) else 0
      if self.bytes == 0:
        with open(self.path, "a") as f:
          f.write("wall_time,v_cruise_internal_ms,v_ego_ms,a_cruise_raw\n")
    except Exception:
      self.dead = True

  def record(self, v_cruise: float, v_ego: float, a_cruise: float) -> None:
    if self.dead or self.path is None:
      return
    try:
      self.buf.append(f"{time.time():.3f},{v_cruise:.4f},{v_ego:.4f},{a_cruise:.4f}\n")
      if len(self.buf) < _FLUSH_N:
        return
      chunk = "".join(self.buf)
      self.buf.clear()
      if self.bytes + len(chunk) > _MAX_BYTES:
        self.dead = True                 # cap reached; stop rather than fill the device
        return
      with open(self.path, "a") as f:
        f.write(chunk)
      self.bytes += len(chunk)
    except Exception:
      # A diagnostic must never be able to take down plannerd. Latch off on any failure.
      self.dead = True
      self.buf.clear()
