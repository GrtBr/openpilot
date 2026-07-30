"""Thin shims called from upstream files. Keep the upstream side to ONE line per hook.

Injection design for THIS openpilot version
-------------------------------------------
`longitudinal_planner.update()` no longer feeds a speed target to the MPC (`long_mpc.update()`
lost its `v_cruise` parameter). Instead it builds a list of acceleration candidates and takes
the `min()`:

    candidates = [(a_mpc, mpc, ...), (a_cruise, cruise, ...)]  (+ e2e in experimental mode)
    output_a_target, source, _ = min(candidates, key=lambda c: c[0])

where `a_cruise = clip(v_cruise - v_ego, A_CRUISE_MIN, max_accel)` and `A_CRUISE_MIN = -1.2`.

So there are two natural injection points:

  hook 1  `limit_v_cruise()`          — lower the v_cruise ceiling before `get_cruise_accel`.
                                        A lower ceiling makes `a_cruise` negative and the
                                        `min()` selects it. This delivers curve, speed-limit
                                        and hazard *targets*.
  hook 2  `extra_accel_candidates()`  — append one more accel candidate before the `min()`.

IMPORTANT — how hook 2 differs from sunnypilot: sunnypilot threads `a_min_override` into the
MPC to *loosen its slack floor* (permissive: the MPC may brake harder). Here we append a
candidate (imperative: this accel competes in the `min()`). The net effect is equivalent in
the direction that matters, because `a_cruise` saturates at A_CRUISE_MIN = -1.2 while the
adaptive hazard decel ranges over [-1.5, -0.3]: the candidate therefore only *binds* when it
is harder than -1.2, i.e. it grants decel authority beyond the cruise floor, which is exactly
what loosening the slack floor achieved. When it is softer than -1.2 the `min()` ignores it,
so hook 2 can never make braking weaker than stock. The final value is still clipped to
ACCEL_MIN downstream.

Hook 2 is gated behind its own param (`SmartCruiseControlMapHazardAccel`, default off) so it
can be enabled separately during on-car testing, per the phased rollout.

  hook 3  `track_set_speed()`          — a DIFFERENT process (card, 100 Hz) and a different
                                        variable: it moves the driver-facing set speed
                                        (`VCruiseHelper.v_cruise_kph`) to follow the posted
                                        limit. See grt/set_speed.py. Hooks 1/2 shape how the
                                        car drives; hook 3 changes what the driver reads.
"""
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET

_scc = None
_scc_broken = False          # set if the controller cannot even be constructed
_hazard_accel_enabled: bool | None = None

_set_speed = None
_set_speed_broken = False

# Per-hook exception counters. A bug that throws every frame must not also flood swaglog: the
# `lead.status` incident logged 38,300 exceptions in one drive from a 20 Hz loop, and hook 3
# runs at 100 Hz in the process that has to hold the CAN deadline. Log the first, then rarely,
# carrying the running count so nothing is hidden.
_exc_counts: dict[str, int] = {}
_EXC_LOG_EVERY = 2000        # 20 s at 100 Hz, 100 s at 20 Hz


def _log_exception(tag: str) -> None:
  n = _exc_counts.get(tag, 0) + 1
  _exc_counts[tag] = n
  if n != 1 and n % _EXC_LOG_EVERY != 0:
    return
  try:
    from openpilot.common.swaglog import cloudlog
    cloudlog.exception(f"grt: {tag} failed (occurrence {n})")
  except Exception:
    pass


def _scc_singleton():
  """Return the controller, or None if it cannot be built.

  Construction is allowed to fail (e.g. Params raises UnknownKeyName when grt_params_keys.inc
  has not been compiled in yet). We latch that so we do not retry — and every hook then
  degrades to a no-op rather than taking plannerd down with it.
  """
  global _scc, _scc_broken
  if _scc_broken:
    return None
  if _scc is None:
    try:
      from openpilot.grt.scc_map import SmartCruiseControlMap
      _scc = SmartCruiseControlMap()
    except Exception:
      _scc_broken = True
      try:
        from openpilot.common.swaglog import cloudlog
        cloudlog.exception("grt: scc_map unavailable; mapd control disabled")
      except Exception:
        pass
      return None
  return _scc


def limit_v_cruise(sm, v_cruise: float, v_ego: float, long_enabled: bool,
                   long_override: bool, a_ego: float) -> float:
  """Hook 1. Runs the controller for this frame and returns a possibly-lowered v_cruise.

  This is the ONLY place the controller is updated per frame; hook 2 reuses the result, so
  hook 1 must be called first (it is — it sits earlier in `update()`).

  Never raises v_cruise: a `forceDecel` v_cruise of 0.0 still wins.
  """
  scc = _scc_singleton()
  if scc is None:
    return v_cruise
  try:
    scc.update(sm, long_enabled, long_override, v_ego, a_ego, v_cruise)
  except Exception:
    # Never let the fork's controller take down plannerd.
    _log_exception("scc_map update")
    return v_cruise

  target = scc.output_v_target
  if 0 < target < V_CRUISE_UNSET:
    return min(v_cruise, target)
  return v_cruise


def extra_accel_candidates(v_ego: float) -> list:
  """Hook 2. Extra acceleration candidates to fold into the planner's min().

  Returns [] unless the hazard branch is actively in charge and the feature param is on.
  """
  global _hazard_accel_enabled
  scc = _scc_singleton()
  if scc is None:
    return []

  if _hazard_accel_enabled is None or scc.frame % 60 == 0:
    from openpilot.grt.scc_map import get_bool_safe
    _hazard_accel_enabled = get_bool_safe(scc.params, "SmartCruiseControlMapHazardAccel")
  if not _hazard_accel_enabled:
    return []

  a = scc.output_hazard_accel
  if a is None:
    return []

  from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalPlanSource
  from openpilot.selfdrive.controls.lib.drive_helpers import should_stop
  return [(float(a), LongitudinalPlanSource.cruise, should_stop(v_ego, float(a)))]


def _set_speed_singleton():
  """Return the set-speed tracker, or None if it cannot be built (latched, as above)."""
  global _set_speed, _set_speed_broken
  if _set_speed_broken:
    return None
  if _set_speed is None:
    try:
      from openpilot.grt.set_speed import SetSpeedLimitTracker
      _set_speed = SetSpeedLimitTracker()
    except Exception:
      _set_speed_broken = True
      _log_exception("set_speed construction; set-speed tracking disabled")
      return None
  return _set_speed


def set_speed_state_msg(v_cruise_helper):
  """Build the fork's card -> selfdrived status message, or None if there is nothing to say.

  Published from card because that is where the set speed and the pending state live; consumed
  by selfdrived because that is the only process that can raise a driver-facing alert.
  """
  try:
    if v_cruise_helper.CP.pcmCruise:   # symmetric with track_set_speed: nothing to report
      return None
  except Exception:
    return None

  tracker = _set_speed_singleton()
  if tracker is None:
    return None
  try:
    import openpilot.cereal.messaging as messaging
    msg = messaging.new_message('grtSetSpeedState')
    msg.valid = True
    s = msg.grtSetSpeedState
    s.pending = tracker.pending_limit_kph is not None
    s.pendingLimit = float(tracker.pending_limit_kph or 0.0)
    s.secondsLeft = float(tracker.pending_seconds_left)
    s.setSpeed = float(v_cruise_helper.v_cruise_kph)
    s.tracking = bool(tracker.tracking)
    s.authorisedLimit = tracker.authorised_limit_kph
    s.active = bool(tracker.enabled)
    s.pendingIsIncrease = bool(tracker._pending_is_increase)
    return msg
  except Exception:
    _log_exception("set_speed state publish")
    return None


def set_speed_alerts(sm, is_metric: bool) -> list:
  """Hook 4. Alerts to fold into selfdrived's AlertManager. Returns [] unless a limit change is
  awaiting confirmation.

  Deliberately builds a plain `Alert` with a fork-owned `alert_type` string rather than adding
  an `EventName` enumerant: `AlertManager.add_many` keys on `alert.alert_type`, and
  `selfdriveState.alertText1` is free-form Text with `alertSound` reusing the existing
  `AudibleAlert` enum. So the driver gets text and a chime with NO schema addition — which
  matters on a prebuilt branch where nothing can be recompiled.

  `ET.WARNING` is correct here: `update_alerts` only clears WARNING when the state machine is
  not in a warning-capable state, i.e. when not engaged, and this feature only runs engaged.
  """
  try:
    if not sm.alive.get('grtSetSpeedState', False):
      return []
    s = sm['grtSetSpeedState']
    if not s.pending:
      return []

    from openpilot.selfdrive.selfdrived.events import Alert, AlertStatus, AlertSize, Priority
    from openpilot.selfdrive.selfdrived.events import AudibleAlert, VisualAlert
    from openpilot.common.constants import CV

    limit = s.pendingLimit if is_metric else s.pendingLimit * CV.KPH_TO_MPH
    unit = "km/h" if is_metric else "mph"
    # Direction-matched: push the switch the way the speed is going. The direction comes from the
    # message, not from comparing against the live set speed, so the instruction cannot flip
    # mid-prompt if the driver nudges their own speed.
    btn = "RES/+" if s.pendingIsIncrease else "SET/-"
    alert = Alert(
      f"Speed limit {round(limit)} {unit}",
      f"Press {btn} to accept",
      AlertStatus.normal, AlertSize.mid, Priority.LOW,
      VisualAlert.none, AudibleAlert.prompt, 1.0,
    )
    alert.alert_type = "grtSetSpeedPending"
    alert.event_type = "warning"
    return [alert]
  except Exception:
    _log_exception("set_speed alert")
    return []


def track_set_speed(sm, CS, v_cruise_helper, enabled: bool, engage_edge: bool = False) -> None:
  """Hook 3. Runs in `card`, after `update_v_cruise`/`initialize_v_cruise` and before the
  `CS.vCruise` assignment, so an adopted limit is what gets published for this frame.

  Writes BOTH `v_cruise_kph` and `v_cruise_cluster_kph`: upstream keeps them equal in the
  non-pcm path (cruise.py sets cluster = kph inside `update_v_cruise`), and we run after that,
  so setting only one would make the cluster and the planner disagree.

  No-op on any failure — this must never be able to break the car daemon. `card` publishes
  carState; if it dies, everything downstream dies with it.
  """
  try:
    # PCM cars read the set speed off CAN every frame, so an adopted value would be overwritten
    # immediately and flap. The Staria is pcmCruise=False (verified), but card.py is shared by
    # every car and this hook has to survive a rebase.
    if v_cruise_helper.CP.pcmCruise:
      return
  except Exception:
    return

  tracker = _set_speed_singleton()
  if tracker is None:
    return
  try:
    v_cruise = float(v_cruise_helper.v_cruise_kph)
    new_v_cruise = tracker.update(sm, CS, v_cruise, enabled, engage_edge)
    if new_v_cruise != v_cruise:
      v_cruise_helper.v_cruise_kph = new_v_cruise
      v_cruise_helper.v_cruise_cluster_kph = new_v_cruise
  except Exception:
    _log_exception("set_speed update")
