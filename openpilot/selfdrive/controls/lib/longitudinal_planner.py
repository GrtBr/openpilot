#!/usr/bin/env python3
import math
import numpy as np

import openpilot.cereal.messaging as messaging
from opendbc.car.interfaces import ACCEL_MIN, ACCEL_MAX
from openpilot.common.constants import CV
from openpilot.common.filter_simple import FirstOrderFilter
from openpilot.common.realtime import DT_MDL
from openpilot.selfdrive.modeld.constants import ModelConstants
from openpilot.selfdrive.controls.lib.longcontrol import LongCtrlState
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import LongitudinalMpc, LongitudinalPlanSource
from openpilot.selfdrive.controls.lib.longitudinal_mpc_lib.long_mpc import T_IDXS as T_IDXS_MPC
from openpilot.selfdrive.controls.lib.drive_helpers import CONTROL_N, get_accel_from_plan, should_stop
from openpilot.selfdrive.car.cruise import V_CRUISE_MAX, V_CRUISE_UNSET
from openpilot.common.swaglog import cloudlog
from openpilot.grt import hooks as grt_hooks  # GRT-MOD

A_CRUISE_MAX_VALS = [1.6, 1.2, 0.8, 0.6]
A_CRUISE_MAX_BP = [0., 10.0, 25., 40.]
J_CRUISE_VALS = [1.6, 1.2, 0.8, 0.6]
A_CRUISE_MIN = -1.2
CONTROL_N_T_IDX = ModelConstants.T_IDXS[:CONTROL_N]
ALLOW_THROTTLE_THRESHOLD = 0.4
MIN_ALLOW_THROTTLE_SPEED = 2.5

# Lookup table for turns
_A_TOTAL_MAX_V = [1.7, 3.2]
_A_TOTAL_MAX_BP = [20., 40.]

def get_max_accel(v_ego):
  return np.interp(v_ego, A_CRUISE_MAX_BP, A_CRUISE_MAX_VALS)

def get_coast_accel(pitch):
  return np.sin(pitch) * -5.65 - 0.3  # fitted from data using xx/projects/allow_throttle/compute_coast_accel.py

def get_cruise_accel(e2e, v_cruise, v_ego, a_cruise_prev, angle_steers, CP, dt, accel_coast, allow_throttle):
  max_accel = ACCEL_MAX if e2e else get_max_accel(v_ego)

  if not e2e:
    a_total_max = np.interp(v_ego, _A_TOTAL_MAX_BP, _A_TOTAL_MAX_V)
    a_y = v_ego ** 2 * angle_steers * CV.DEG_TO_RAD / (CP.steerRatio * CP.wheelbase)
    a_x_allowed = math.sqrt(max(a_total_max ** 2 - a_y ** 2, 0.))
    max_accel = min(max_accel, a_x_allowed)
    if not allow_throttle:
      clipped_accel_coast = max(accel_coast, ACCEL_MIN)
      coast_limit = np.interp(v_ego, [MIN_ALLOW_THROTTLE_SPEED, MIN_ALLOW_THROTTLE_SPEED*2], [max_accel, clipped_accel_coast])
      max_accel = min(max_accel, coast_limit)

  target_accel = np.clip(v_cruise - v_ego, A_CRUISE_MIN, max_accel)
  if not e2e:
    j_cruise = np.interp(v_ego, A_CRUISE_MAX_BP, J_CRUISE_VALS)
    target_accel = float(np.clip(target_accel, a_cruise_prev - j_cruise * dt, a_cruise_prev + j_cruise * dt))

  return target_accel


class LongitudinalPlanner:
  def __init__(self, CP, init_v=0.0, init_a=0.0, dt=DT_MDL):
    self.CP = CP
    self.mpc = LongitudinalMpc(dt=dt)
    self.fcw = False
    self.dt = dt
    self.allow_throttle = True

    self.a_desired = init_a
    self.v_desired_filter = FirstOrderFilter(init_v, 2.0, self.dt)
    self.a_cruise = 0.0
    self.output_a_target = 0.0
    self.output_should_stop = False

    self.v_desired_trajectory = np.zeros(CONTROL_N)
    self.a_desired_trajectory = np.zeros(CONTROL_N)
    self.j_desired_trajectory = np.zeros(CONTROL_N)

  def update(self, sm):
    if len(sm['carControl'].orientationNED) == 3:
      accel_coast = get_coast_accel(sm['carControl'].orientationNED[1])
    else:
      accel_coast = ACCEL_MAX

    v_ego = sm['carState'].vEgo
    v_cruise_kph = min(sm['carState'].vCruise, V_CRUISE_MAX)
    v_cruise = v_cruise_kph * CV.KPH_TO_MS
    if sm['controlsState'].forceDecel:
      v_cruise = 0.0

    long_control_off = sm['controlsState'].longControlState == LongCtrlState.off

    # Reset current state when not engaged, or user is controlling the speed
    reset_state = long_control_off if self.CP.openpilotLongitudinalControl else not sm['selfdriveState'].enabled
    # PCM cruise speed may be updated a few cycles later, check if initialized
    v_cruise_initialized = sm['carState'].vCruise != V_CRUISE_UNSET
    reset_state = reset_state or not v_cruise_initialized

    throttle_probs = sm['modelV2'].meta.disengagePredictions.gasPressProbs
    throttle_prob = throttle_probs[1] if len(throttle_probs) > 1 else 1.0
    self.allow_throttle = throttle_prob > ALLOW_THROTTLE_THRESHOLD or v_ego <= MIN_ALLOW_THROTTLE_SPEED

    steer_angle_without_offset = sm['carState'].steeringAngleDeg - sm['liveParameters'].angleOffsetDeg

    if reset_state:
      self.v_desired_filter.x = v_ego
      self.a_desired = np.clip(sm['carState'].aEgo, ACCEL_MIN, ACCEL_MAX)

    # Prevent divergence, smooth in current v_ego
    self.v_desired_filter.x = max(0.0, self.v_desired_filter.update(v_ego))

    # No change cost when user is controlling the speed, or when standstill
    prev_accel_constraint = not (reset_state or sm['carState'].standstill)

    self.mpc.set_weights(prev_accel_constraint, personality=sm['selfdriveState'].personality)
    self.mpc.set_cur_state(self.v_desired_filter.x, self.a_desired)
    self.mpc.update(sm['radarState'], personality=sm['selfdriveState'].personality)

    self.v_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.v_solution)
    self.a_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC, self.mpc.a_solution)
    self.j_desired_trajectory = np.interp(CONTROL_N_T_IDX, T_IDXS_MPC[:-1], self.mpc.j_solution)

    # TODO counter is only needed because radar is glitchy, remove once radar is gone
    self.fcw = self.mpc.crash_cnt > 2 and not sm['carState'].standstill
    if self.fcw:
      cloudlog.info("FCW triggered")

    # Save starting point for next iteration
    a_prev = self.a_desired

    action_t =  self.CP.longitudinalActuatorDelay + DT_MDL
    output_a_target_mpc = get_accel_from_plan(self.v_desired_trajectory, self.a_desired_trajectory, CONTROL_N_T_IDX,
                                              action_t=action_t)
    output_should_stop_mpc = should_stop(v_ego, output_a_target_mpc)
    output_a_target_e2e = sm['modelV2'].action.desiredAcceleration
    output_should_stop_e2e = sm['modelV2'].action.shouldStop

    # GRT-MOD-START — mapd speed ceiling (curve / speed limit / hazard targets).
    # Must stay BEFORE get_cruise_accel: a lower v_cruise makes a_cruise negative and the
    # min() below selects it. limit_v_cruise only ever lowers v_cruise, so the forceDecel
    # v_cruise = 0.0 set above still wins. This call also runs the controller for this frame;
    # extra_accel_candidates() below reuses its result and must come after.
    v_cruise = grt_hooks.limit_v_cruise(sm, v_cruise, v_ego, sm['carControl'].enabled,
                                        sm['carControl'].cruiseControl.override,
                                        sm['carState'].aEgo)
    # GRT-MOD-END

    self.a_cruise = get_cruise_accel(sm['selfdriveState'].experimentalMode, v_cruise, v_ego,
                                     self.a_cruise, steer_angle_without_offset, self.CP, self.dt,
                                     accel_coast, self.allow_throttle)
    # GRT-MOD-START — gentle coast-down to the set speed. Stock clips a_cruise at A_CRUISE_MIN
    # (-1.2), so letting off the throttle at 110 with cruise set to 100 brakes at the full -1.2.
    # This raises that floor to COAST_DECEL for PLAIN overspeed only: it is skipped when hook 1
    # lowered v_cruise (map/curve/hazard keep full authority) and when v_cruise ~ 0 (forceDecel).
    # Because a_cruise is only one candidate in the min() below, it can never make braking
    # weaker than the MPC (lead) or hook 2 (hazard) branches.
    self.a_cruise = grt_hooks.soften_cruise_decel(self.a_cruise, v_cruise, v_ego)
    # GRT-MOD-END

    # GRT-MOD-START — hook 10 layer B: at or below the set speed, stop the cruise candidate
    # vetoing hooks 6/8. At the set speed a_cruise is 0, so min(raised_e2e, 0) = 0 and every
    # thing hooks 6 and 8 add is discarded — measured 2026-08-22 17:34-17:40, cruise won 77%
    # of frames while the hooks reached the wheels on 7%. MUST stay after hook 5 (which may
    # soften the same value) and before the min(). Above the set speed this is a no-op, so
    # overspeed authority is unchanged, and hook 1 still lowers v_cruise for map curves and
    # limits — which puts us in the v_ego > v_cruise branch where cruise goes negative as
    # designed. See grt/throttle_hold.py: this removes the cruise APPROACH TAPER, a direction
    # no replay gate covers.
    self.a_cruise = grt_hooks.deadband_cruise_accel(self.a_cruise, v_ego, v_cruise)
    # GRT-MOD-END
    cruise_should_stop = should_stop(v_ego, self.a_cruise)

    # GRT-MOD-START — offer back unused headroom below the set speed (default OFF, param
    # GrtE2EAccelFloor, aggressive personality only). UNLIKE hooks 1/2/5 this one CAN make the
    # car less cautious: it RAISES the e2e candidate, which is how that candidate stops winning
    # the min() below, and in experimental mode e2e is the only vision-based caution in the
    # chain (get_cruise_accel skips the lateral-accel and coast limits, and the MPC has no
    # curvature input). Mapped curves/limits/hazards are still covered by hook 1, radar leads by
    # the MPC branches, the set speed by the cruise candidate. Safety rests on the arm condition
    # — the model must have just accelerated for real and tapered off — plus instant latched
    # release. MUST stay after limit_v_cruise(): a lowered v_cruise shrinks the headroom this
    # hook sees and stops it arming. See openpilot/grt/e2e_floor.py before touching this.
    output_a_target_e2e = grt_hooks.floor_e2e_accel(output_a_target_e2e, sm, v_ego, v_cruise)
    # GRT-MOD-END

    candidates = [(output_a_target_mpc, self.mpc.source, output_should_stop_mpc),
                  (self.a_cruise, LongitudinalPlanSource.cruise, cruise_should_stop)]
    if sm['selfdriveState'].experimentalMode:
      candidates.append((output_a_target_e2e, LongitudinalPlanSource.e2e, output_should_stop_e2e))

    # GRT-MOD-START — firm hazard pre-braking (default OFF, param SmartCruiseControlMapHazardAccel).
    # Returns [] unless the hazard branch is in charge. Because a_cruise saturates at
    # A_CRUISE_MIN (-1.2) and this candidate spans [-1.5, -0.3], it only wins the min() when
    # harder than the cruise floor, so it can never brake more weakly than stock. Must come
    # after the limit_v_cruise() hook above, which runs the controller for this frame.
    candidates += grt_hooks.extra_accel_candidates(v_ego)
    # GRT-MOD-END

    # GRT-MOD-START — hook 11: far-lead pre-brake (relaxed personality only). Fills the
    # 115 m -> ~75 m hole measured 2026-08-25 10:49, where the candidates above trusted
    # radarState.leadOne.vRel and stayed near 0 m/s^2 despite a genuine high closing rate (see
    # grt/far_lead.py). Needs the best of the candidates already built this frame so it can
    # hand off once one of them has genuinely caught up, rather than on a bare distance cutoff.
    # Bounded to [-1.2, -0.40] and returns [] when inert, so it can only compete in the min()
    # below, never make braking weaker than stock.
    candidates += grt_hooks.far_lead_candidates(sm, v_ego, min(c[0] for c in candidates))
    # GRT-MOD-END

    output_a_target, self.mpc.source, _ = min(candidates, key=lambda c: c[0])
    self.output_should_stop = any(should_stop for _, _, should_stop in candidates)

    # GRT-MOD-START — gentle the RISE of the command in relaxed personality (jerk cap, not a
    # time constant: the plan's commands are short transients, so a time constant cuts their
    # amplitude rather than their slope — measured, see grt/accel_ramp.py). Applied AFTER the
    # min() so it shapes delivery of whichever candidate won rather than biasing the choice.
    # Cannot make braking weaker: on a rise the output is min(plan, ...) and on a fall it is
    # exactly plan, so the command is never greater than the planner asked for. A rise that is
    # merely the release of braking is not delayed.
    output_a_target = grt_hooks.ramp_relaxed_accel(output_a_target, sm, sm['carControl'].longActive)
    # GRT-MOD-END

    # GRT-MOD-START — hook 10 layers A + C: SCC sign debounce, and no mild coast while set-
    # speed headroom is unused. AFTER the min() and AFTER hook 7, so it shapes the command
    # actually being sent rather than biasing which candidate wins. ALL PERSONALITIES: the
    # deadband chatter it fixes was measured in relaxed too (15:28 on 2026-08-22). On this car
    # aReq ~ 0 is the SCC throttle deadband, so crossing zero is throttle off-then-on; this
    # holds the PRE-GLITCH command through a dip shorter than 0.30 s rather than filtering it.
    # A request at or beyond -0.20 is passed through unfiltered on this same tick.
    # See openpilot/grt/throttle_hold.py.
    output_a_target = grt_hooks.hold_throttle(output_a_target, sm, v_ego, v_cruise,
                                              sm['carControl'].longActive)
    # GRT-MOD-END

    self.output_a_target = np.clip(output_a_target, ACCEL_MIN, ACCEL_MAX)

    self.a_desired = float(self.output_a_target)
    self.v_desired_filter.x = self.v_desired_filter.x + self.dt * (self.output_a_target + a_prev) / 2.0

  def publish(self, sm, pm):
    plan_send = messaging.new_message('longitudinalPlan')

    plan_send.valid = sm.all_checks()

    longitudinalPlan = plan_send.longitudinalPlan
    longitudinalPlan.modelMonoTime = sm.logMonoTime['modelV2']
    longitudinalPlan.processingDelay = (plan_send.logMonoTime / 1e9) - sm.logMonoTime['modelV2']
    longitudinalPlan.solverExecutionTime = self.mpc.solve_time

    longitudinalPlan.speeds = self.v_desired_trajectory.tolist()
    longitudinalPlan.accels = self.a_desired_trajectory.tolist()
    longitudinalPlan.jerks = self.j_desired_trajectory.tolist()

    longitudinalPlan.hasLead = sm['radarState'].leadOne.present
    longitudinalPlan.longitudinalPlanSource = self.mpc.source
    longitudinalPlan.fcw = self.fcw

    longitudinalPlan.aTarget = float(self.output_a_target)
    longitudinalPlan.shouldStop = bool(self.output_should_stop)
    longitudinalPlan.allowBrake = True
    longitudinalPlan.allowThrottle = bool(self.allow_throttle)

    pm.send('longitudinalPlan', plan_send)
