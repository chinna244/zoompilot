"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""
import numpy as np

from openpilot.cereal import custom
from opendbc.car.structs import car
from opendbc.car import structs
from openpilot.common.constants import CV
from openpilot.common.params import Params
from openpilot.common.realtime import DT_CTRL
from openpilot.sunnypilot.selfdrive.car.cruise_arbiter import CruiseArbiter, V_CRUISE_MAX, V_CRUISE_UNSET, \
  ACTIVE_STATES as SESSION_ACTIVE_STATES
from openpilot.sunnypilot.selfdrive.car.intelligent_cruise_button_management.helpers import get_minimum_set_speed

ButtonType = car.CarState.ButtonEvent.Type
LongitudinalPlanSource = custom.LongitudinalPlanSP.LongitudinalPlanSource
IcbmState = custom.IntelligentCruiseButtonManagement.IntelligentCruiseButtonManagementState

CRUISE_BUTTON_TIMER = {ButtonType.decelCruise: 0, ButtonType.accelCruise: 0,
                       ButtonType.setCruise: 0, ButtonType.resumeCruise: 0,
                       ButtonType.cancel: 0, ButtonType.mainCruise: 0}

V_CRUISE_MIN = 8  # V_CRUISE_MAX / V_CRUISE_UNSET come from cruise_arbiter

# Setpoint reconciliation for non-pcmCruiseSpeed (ICBM) cars. The stock ECU keeps the real
# set speed and steps it on wheel presses while openpilot integrates the same presses, so
# the two drift (grid-snapped long presses, gas-override presses, trailing increments).
# Around a driver press the dash is the ECU's truth of the setpoint, but only when nothing
# is deliberately holding it away from v_cruise: adopt it if the plan source is cruise and
# ICBM is not mid-move. The settle time absorbs the ECU's trailing long-press increment
# (lands well inside 1 s on a CX-5 2022).
RECONCILE_SETTLE_TIME = 1.0  # s after the last press
RECONCILE_SETTLE_FRAMES = int(RECONCILE_SETTLE_TIME / DT_CTRL)
RECONCILE_BUTTONS = (ButtonType.accelCruise, ButtonType.decelCruise)
# The dash must also have been at rest when the press started: at the setpoint (normal
# cruising, small drift) or at an active SLA session's target (settled re-anchor). A dash
# in transit matches neither; adopting it would destroy the baseline the servo is about to
# restore, since a press that aborts an SLA move knocks both regime gates idle on the spot.
RECONCILE_AGREE_KPH = 2 * CV.MPH_TO_KPH


def update_manual_button_timers(CS: car.CarState, button_timers: dict[car.CarState.ButtonEvent.Type, int]) -> None:
  # increment timer for buttons still pressed
  for k in button_timers:
    if button_timers[k] > 0:
      button_timers[k] += 1

  for b in CS.buttonEvents:
    if b.type.raw in button_timers:
      # Start/end timer and store current state on change of button pressed
      button_timers[b.type.raw] = 1 if b.pressed else 0


class VCruiseHelperSP:
  def __init__(self, CP: structs.CarParams, CP_SP: structs.CarParamsSP) -> None:
    self.CP = CP
    self.CP_SP = CP_SP
    self.v_cruise_kph = V_CRUISE_UNSET
    self.v_cruise_cluster_kph = V_CRUISE_UNSET
    self.params = Params()
    self.v_cruise_min = 0
    self.enabled_prev = False

    self.custom_acc_enabled = self.params.get_bool("CustomAccIncrementsEnabled")
    self.short_increment = self.params.get("CustomAccShortPressIncrement", return_default=True)
    self.long_increment = self.params.get("CustomAccLongPressIncrement", return_default=True)

    self.enable_button_timers = dict(CRUISE_BUTTON_TIMER)

    # Setpoint reconciliation (non-pcmCruiseSpeed cars)
    self.reconcile_frames = 0
    self.reconcile_allowed = False

    # Plan/actuation regime, updated from longitudinalPlanSP + carControlSP each frame
    self.lp_source = LongitudinalPlanSource.cruise
    self.icbm_state = IcbmState.inactive

    # Cruise arbiter: classifies every +/- press once and owns the SLA session on
    # non-pcm (stock-ACC button) cars; no-op elsewhere
    self.cruise_arbiter = CruiseArbiter(CP, CP_SP)
    self.cruise_arbiter.read_params(self.params)

  def read_custom_set_speed_params(self) -> None:
    self.custom_acc_enabled = self.params.get_bool("CustomAccIncrementsEnabled")
    self.short_increment = self.params.get("CustomAccShortPressIncrement", return_default=True)
    self.long_increment = self.params.get("CustomAccLongPressIncrement", return_default=True)
    # rides card's params thread, keeping param reads off the 100 Hz path
    self.cruise_arbiter.read_params(self.params)

  def update_v_cruise_delta(self, long_press: bool, v_cruise_delta: float) -> tuple[bool, float]:
    if not self.custom_acc_enabled:
      v_cruise_delta = v_cruise_delta * (5 if long_press else 1)
      return long_press, v_cruise_delta

    # Apply user-specified multipliers to the base increment
    short_increment = np.clip(self.short_increment, 1, 10)
    long_increment = np.clip(self.long_increment, 1, 10)

    actual_increment = long_increment if long_press else short_increment
    round_to_nearest = actual_increment in (5, 10)
    v_cruise_delta = v_cruise_delta * actual_increment

    return round_to_nearest, v_cruise_delta

  def get_minimum_set_speed(self, is_metric: bool) -> None:
    if self.CP_SP.pcmCruiseSpeed:
      self.v_cruise_min = V_CRUISE_MIN
      return

    self.v_cruise_min = get_minimum_set_speed(is_metric)

  def update_enabled_state(self, CS: car.CarState, enabled: bool) -> bool:
    # special enabled state for non pcmCruiseSpeed, unchanged for non pcmCruise
    if not self.CP_SP.pcmCruiseSpeed:
      update_manual_button_timers(CS, self.enable_button_timers)
      button_pressed = any(self.enable_button_timers[k] > 0 for k in self.enable_button_timers)

      if enabled and not self.enabled_prev:
        self.enabled_prev = not button_pressed
        enabled = False
      elif not enabled:
        self.enabled_prev = enabled

      return enabled and self.enabled_prev

    return enabled

  def reconcile_setpoint_with_dash(self, CS: car.CarState) -> None:
    if self.CP_SP.pcmCruiseSpeed or not self.CP.pcmCruise:
      return

    if not CS.cruiseState.available or self.v_cruise_kph in (V_CRUISE_UNSET, -1):
      self.reconcile_frames = 0
      return

    pressed = any(self.enable_button_timers[b] > 0 for b in RECONCILE_BUTTONS)
    if not pressed and self.reconcile_frames <= 0:
      return

    dash_kph = CS.cruiseState.speed * CV.MS_TO_KPH
    if pressed:
      if self.reconcile_frames <= 0:
        # evaluated once at press start, before the press's own ECU effect lands; the
        # arbiter's pre-frame snapshot is the session state the press was aimed at
        agree_setpoint = abs(dash_kph - self.v_cruise_kph) <= RECONCILE_AGREE_KPH
        sla_session = self.cruise_arbiter.state_prev_frame in SESSION_ACTIVE_STATES
        agree_sla = sla_session and abs(dash_kph - self.cruise_arbiter.slf_kph) <= RECONCILE_AGREE_KPH
        self.reconcile_allowed = agree_setpoint or agree_sla
      self.reconcile_frames = RECONCILE_SETTLE_FRAMES
    else:
      self.reconcile_frames -= 1

    if not self.reconcile_allowed:
      return

    # even a legitimate window must not adopt while a limiter drives the plan, ICBM is
    # stepping the dash, or a confirm prompt is pending (the frozen dash is not the
    # driver's answer)
    if self.lp_source != LongitudinalPlanSource.cruise:
      return
    if self.icbm_state in (IcbmState.increasing, IcbmState.decreasing):
      return
    if self.cruise_arbiter.prompting:
      return

    if dash_kph > 1:
      self.v_cruise_kph = float(np.clip(round(dash_kph, 1), self.v_cruise_min, V_CRUISE_MAX))
      self.v_cruise_cluster_kph = self.v_cruise_kph

  def update_speed_limit_assist(self, is_metric, LP_SP: custom.LongitudinalPlanSP,
                                CC_SP: custom.CarControlSP, lp_updated: bool = True) -> None:
    self.lp_source = LP_SP.longitudinalPlanSource
    self.icbm_state = CC_SP.intelligentCruiseButtonManagement.state
    self.cruise_arbiter.is_metric = is_metric
    if lp_updated:  # resolver values only change when the plan message does
      self.cruise_arbiter.update_limit(LP_SP)

  def press_owned(self, button_type) -> bool:
    # a press the arbiter classified as confirm or dismiss never increments v_cruise
    return self.cruise_arbiter.press_owned(button_type)

  def update_cruise_arbiter(self, CS: car.CarState, enabled: bool) -> None:
    if not self.cruise_arbiter.applicable:
      return

    v_cruise_kph = self.cruise_arbiter.step(CS, enabled, self.v_cruise_kph, self.v_cruise_cluster_kph)
    if self.cruise_arbiter.adopted_this_frame:
      # an upward confirm adopted the limit as the setpoint; the ECU's own +1 step from
      # the confirm press must not be re-adopted over it
      self.v_cruise_kph = v_cruise_kph
      self.v_cruise_cluster_kph = v_cruise_kph
      self.reconcile_frames = 0
      self.reconcile_allowed = False
