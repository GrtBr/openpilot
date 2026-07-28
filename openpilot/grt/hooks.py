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
"""
from openpilot.selfdrive.car.cruise import V_CRUISE_UNSET

_scc = None
_hazard_accel_enabled: bool | None = None


def _scc_singleton():
  global _scc
  if _scc is None:
    from openpilot.grt.scc_map import SmartCruiseControlMap
    _scc = SmartCruiseControlMap()
  return _scc


def limit_v_cruise(sm, v_cruise: float, v_ego: float, long_enabled: bool,
                   long_override: bool, a_ego: float) -> float:
  """Hook 1. Runs the controller for this frame and returns a possibly-lowered v_cruise.

  This is the ONLY place the controller is updated per frame; hook 2 reuses the result, so
  hook 1 must be called first (it is — it sits earlier in `update()`).

  Never raises v_cruise: a `forceDecel` v_cruise of 0.0 still wins.
  """
  scc = _scc_singleton()
  try:
    scc.update(sm, long_enabled, long_override, v_ego, a_ego, v_cruise)
  except Exception:
    # Never let the fork's controller take down plannerd.
    from openpilot.common.swaglog import cloudlog
    cloudlog.exception("grt: scc_map update failed")
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

  if _hazard_accel_enabled is None or scc.frame % 60 == 0:
    try:
      _hazard_accel_enabled = scc.params.get_bool("SmartCruiseControlMapHazardAccel")
    except Exception:
      _hazard_accel_enabled = False
  if not _hazard_accel_enabled:
    return []

  a = scc.output_hazard_accel
  if a is None:
    return []

  from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalPlanSource
  from openpilot.selfdrive.controls.lib.drive_helpers import should_stop
  return [(float(a), LongitudinalPlanSource.cruise, should_stop(v_ego, float(a)))]
