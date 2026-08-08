"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

import json
import os

from opendbc.car.structs import car
from openpilot.common.basedir import BASEDIR
from openpilot.selfdrive.ui.mici.widgets.button import BigParamControl
from openpilot.selfdrive.ui.sunnypilot.mici.widgets.button import (
  BigButtonSP,
  BigMultiParamToggleSP,
  BigParamControlSP,
  BigParamOption,
  speed_unit,
)
from openpilot.system.ui.lib.multilang import tr
from openpilot.system.ui.widgets.scroller import NavScroller
from openpilot.selfdrive.ui.ui_state import ui_state
from openpilot.sunnypilot.mads.helpers import MadsSteeringModeOnBrake, get_mads_limited_brands
from openpilot.sunnypilot.selfdrive.controls.lib.auto_lane_change import AUTO_LANE_CHANGE_TIMER, AutoLaneChangeMode
from openpilot.system.ui.lib.application import gui_app

TORQUE_VERSIONS_PATH = os.path.join(BASEDIR, "openpilot", "sunnypilot", "selfdrive", "controls", "lib",
                                    "latcontrol_torque_versions.json")

MADS_STEERING_MODE_LABELS = [tr("remain"), tr("pause"), tr("disengage")]

# AutoLaneChangeTimer stores the mode itself (OFF is -1), so the keys are the stored values, not
# indices. Timed labels come from AUTO_LANE_CHANGE_TIMER so they can't drift from the controller.
ALC_LABELS = {
  AutoLaneChangeMode.OFF: tr("off"),
  AutoLaneChangeMode.NUDGE: tr("nudge"),
  AutoLaneChangeMode.NUDGELESS: tr("nudgeless"),
} | {mode: f"{AUTO_LANE_CHANGE_TIMER[mode]:g} {tr('s')}" for mode in
     (AutoLaneChangeMode.HALF_SECOND, AutoLaneChangeMode.ONE_SECOND,
      AutoLaneChangeMode.TWO_SECONDS, AutoLaneChangeMode.THREE_SECONDS)}


def _on_off(val: bool) -> str:
  return "on" if val else "off"


def _alc_label(v: int) -> str:
  return ALC_LABELS.get(v, ALC_LABELS[AutoLaneChangeMode.NUDGE])


class SteeringLayoutMici(NavScroller):
  """Steering settings: MADS, lane change, blinker pause, torque control, NNLC.

  Sub-panels are pre-built NavScrollers pushed onto the nav stack via link_sub_panel.
  See CruiseLayoutMici for the transition tracking pattern explanation.
  """

  def __init__(self):
    super().__init__()

    # Transition tracking — None means first frame (triggers cleanup like False→False would)
    self._prev_torque_allowed: bool | None = None
    self._prev_mads_limited: bool | None = None

    # Per-frame state the enable/depends_on callbacks read, so they cost no extra param reads
    self._mads_limited = False
    self._alc_val = AutoLaneChangeMode.NUDGE
    self._torque_allowed = False
    self._enforce_torque = False

    # --- Main view items ---
    self._mads_settings_btn = BigButtonSP(tr("mads"))
    self._lane_change_btn = BigButtonSP(tr("lane change"))
    self._blinker_settings_btn = BigButtonSP(tr("blinker pause"))
    self._torque_settings_btn = BigButtonSP(tr("torque control"))
    self._nnlc_toggle = BigParamControl(tr("nnlc"), "NeuralNetworkLateralControl")

    for btn in [self._mads_settings_btn, self._lane_change_btn, self._blinker_settings_btn, self._torque_settings_btn]:
      btn.set_subtitle_font_size(24)

    self._scroller.add_widgets([
      self._mads_settings_btn, self._lane_change_btn,
      self._blinker_settings_btn, self._torque_settings_btn,
      self._nnlc_toggle,
    ])

    # --- MADS sub-panel ---
    self._mads_toggle = BigParamControl(tr("enable mads"), "Mads")
    self._mads_toggle.set_enabled(ui_state.is_offroad)
    # depends_on reads the live toggle, not the param, so a tap lands the same frame
    self._mads_main_cruise = BigParamControlSP(tr("main cruise toggle"), "MadsMainCruiseAllowed",
                                               depends_on=lambda: self._mads_toggle._checked and not self._mads_limited)
    self._mads_unified = BigParamControlSP(tr("unified engagement"), "MadsUnifiedEngagementMode",
                                           depends_on=lambda: self._mads_toggle._checked and not self._mads_limited)
    self._mads_steering = BigMultiParamToggleSP(tr("steering on brake"), "MadsSteeringMode", MADS_STEERING_MODE_LABELS)
    self._mads_view = self._mads_settings_btn.link_sub_panel([self._mads_toggle, self._mads_main_cruise, self._mads_unified, self._mads_steering])

    # --- Lane change sub-panel ---
    # AutoLaneChangeTimer is a 7-value mode (-1..5), not a boolean — matches TICI lane_change_settings
    self._lc_timer = BigMultiParamToggleSP(tr("auto lane change"), "AutoLaneChangeTimer",
                                           list(ALC_LABELS.values()), values=list(ALC_LABELS))
    self._lc_bsm = BigParamControlSP(tr("bsm delay"), "AutoLaneChangeBsmDelay",
                                     depends_on=lambda: self._bsm_applies(self._alc_val) and self._car_has_bsm())
    self._lc_view = self._lane_change_btn.link_sub_panel([self._lc_timer, self._lc_bsm])

    # --- Blinker sub-panel ---
    self._blinker_toggle = BigParamControl(tr("enable blinker pause"), "BlinkerPauseLateralControl")
    self._blinker_speed = BigParamOption(tr("blinker speed"), "BlinkerMinLateralControlSpeed",
                                         min_value=0, max_value=255, value_change_step=5,
                                         label_callback=lambda v: f"{v} {speed_unit()}", picker_unit=speed_unit)
    self._blinker_delay = BigParamOption(tr("blinker delay"), "BlinkerLateralReengageDelay",
                                         min_value=0, max_value=10,
                                         label_callback=lambda v: f"{v} " + tr("seconds"), picker_unit=tr("seconds"))
    for opt in (self._blinker_speed, self._blinker_delay):
      opt.set_enabled(lambda: self._blinker_toggle._checked)
    self._blinker_view = self._blinker_settings_btn.link_sub_panel([self._blinker_toggle, self._blinker_speed, self._blinker_delay])

    # --- Torque sub-panel ---
    self._torque_toggle = BigParamControl(tr("enable torque control"), "EnforceTorqueControl")
    self._torque_toggle.set_enabled(lambda: self._torque_allowed and ui_state.is_offroad() and
                                    not ui_state.params.get_bool("NeuralNetworkLateralControl"))

    # Torque tune version selector — inline pill selector over the TICI TorqueControlTune options,
    # oldest first. No "default" option: the param's own default (0.0, v0) is what unset resolves to.
    # The fallback keeps the widget constructible if the versions file is ever unreadable.
    tq_versions = self._load_torque_versions() or {tr("default"): 0.0}
    self._tq_version = BigMultiParamToggleSP(tr("tune version"), "TorqueControlTune",
                                             list(tq_versions), values=list(tq_versions.values()))

    self._tq_self_tune_btn = BigButtonSP(tr("self tune"))
    self._tq_self_tune_btn.set_subtitle_font_size(24)
    # Nested sub-panels sit 3 deep, and gui_app only renders the top 2 widgets — this layout's
    # _update_state does NOT run while they're open, so their gating must be declared here
    self._tq_self_tune = BigParamControl(tr("enable self-tune"), "LiveTorqueParamsToggle")
    self._tq_self_tune.set_enabled(ui_state.is_offroad)
    self._tq_relaxed = BigParamControlSP(tr("less restrict"), "LiveTorqueParamsRelaxedToggle",
                                         depends_on=lambda: self._tq_self_tune._checked)
    self._tq_speed_dep = BigParamControlSP(tr("speed dependent"), "SpeedDependentTorqueToggle",
                                           depends_on=lambda: self._tq_self_tune._checked)
    self._tq_self_tune_view = self._tq_self_tune_btn.link_sub_panel([self._tq_self_tune, self._tq_relaxed, self._tq_speed_dep])

    self._tq_custom_btn = BigButtonSP(tr("custom tune"))
    self._tq_custom_btn.set_subtitle_font_size(24)
    self._tq_custom = BigParamControl(tr("enable custom tuning"), "CustomTorqueParams")
    self._tq_custom.set_enabled(ui_state.is_offroad)
    self._tq_manual_rt = BigParamControlSP(tr("manual realtime"), "TorqueParamsOverrideEnabled",
                                           depends_on=lambda: self._tq_custom._checked)
    self._tq_lat_accel = BigParamOption(tr("lat accel"), "TorqueParamsOverrideLatAccelFactor",
                                        min_value=1, max_value=500, label_callback=lambda x: f"{x / 100} m/s\u00b2",
                                        picker_label_callback=lambda x: f"{x / 100}", float_param=True, picker_unit="m/s\u00b2")
    self._tq_friction = BigParamOption(tr("friction"), "TorqueParamsOverrideFriction",
                                       min_value=1, max_value=100, label_callback=lambda x: f"{x / 100}",
                                       picker_label_callback=lambda x: f"{x / 100}", float_param=True)
    for opt in (self._tq_lat_accel, self._tq_friction):
      opt.set_enabled(lambda: self._tq_custom._checked)
    self._tq_custom_view = self._tq_custom_btn.link_sub_panel([self._tq_custom, self._tq_manual_rt, self._tq_lat_accel, self._tq_friction])

    self._tq_items_rest = [self._tq_self_tune_btn, self._tq_custom_btn]
    for item in [self._tq_version] + self._tq_items_rest:
      item.set_enabled(lambda: self._enforce_torque)
    self._tq_view = self._torque_settings_btn.link_sub_panel([self._torque_toggle, self._tq_version] + self._tq_items_rest)

  # --- Torque tune version selector ---
  @staticmethod
  def _load_torque_versions() -> dict[str, float]:
    """Load {label: version} from latcontrol_torque_versions.json, sorted oldest-first so the
    selector reads v0 → v1 and a future version appends at the newest end."""
    try:
      with open(TORQUE_VERSIONS_PATH) as f:
        data = json.load(f)
    except (OSError, ValueError):
      return {}
    versions: dict[str, float] = {}
    for label, info in data.items():
      try:
        versions[label] = float(info["version"])
      except (KeyError, ValueError, TypeError):
        pass
    return dict(sorted(versions.items(), key=lambda kv: kv[1]))

  # --- Main view state ---
  def _update_state(self):
    super()._update_state()

    self._nnlc_toggle.refresh()

    torque_allowed = self._torque_allowed = (ui_state.CP is not None and
                                             ui_state.CP.steerControlType != car.CarParams.SteerControlType.angle)
    # wipe only on a known angle-steering car; CP None just means the car is not
    # fingerprinted yet (fresh install), where a wipe would race card's seeded defaults
    if ui_state.CP is not None and not torque_allowed and self._prev_torque_allowed is not False:
      ui_state.params.remove("EnforceTorqueControl")
      ui_state.params.remove("NeuralNetworkLateralControl")
    self._prev_torque_allowed = torque_allowed

    mads_on = ui_state.params.get_bool("Mads")
    offroad = ui_state.is_offroad()
    self._mads_settings_btn.set_enabled(offroad)
    if not mads_on:
      self._mads_settings_btn.set_disabled()
    else:
      cruise = _on_off(ui_state.params.get_bool("MadsMainCruiseAllowed"))
      unified = _on_off(ui_state.params.get_bool("MadsUnifiedEngagementMode"))
      steer_idx = ui_state.params.get("MadsSteeringMode", return_default=True) or 0
      steer_mode = MADS_STEERING_MODE_LABELS[min(steer_idx, len(MADS_STEERING_MODE_LABELS) - 1)]
      self._mads_settings_btn.set_badges([(tr("enabled"), "on"), (tr("main-cruise"), cruise), (tr("unified"), unified), (steer_mode, "on")])

    blinker_on = ui_state.params.get_bool("BlinkerPauseLateralControl")
    if not blinker_on:
      self._blinker_settings_btn.set_disabled()
    else:
      speed_val = ui_state.params.get("BlinkerMinLateralControlSpeed", return_default=True) or 0
      delay_val = ui_state.params.get("BlinkerLateralReengageDelay", return_default=True) or 0
      self._blinker_settings_btn.set_badges([(tr("enabled"), "on"), (tr("pause"), f"{speed_val}{speed_unit()}"), (tr("delay"), f"{delay_val}s")])

    alc_val = self._alc_val = int(ui_state.params.get("AutoLaneChangeTimer", return_default=True) or AutoLaneChangeMode.NUDGE)
    # Show BSM delay off where it does nothing, but leave the param alone — auto_lane_change
    # already ignores it below Nudgeless, and the user's choice comes back when they re-enable
    lc_bsm = _on_off(ui_state.params.get_bool("AutoLaneChangeBsmDelay") and self._bsm_applies(alc_val))
    if alc_val <= AutoLaneChangeMode.OFF and lc_bsm == "off":
      self._lane_change_btn.set_disabled()
    else:
      auto_badge = _alc_label(alc_val) if alc_val > AutoLaneChangeMode.OFF else "off"
      self._lane_change_btn.set_badges([(tr("auto"), auto_badge), (tr("bsm-delay"), lc_bsm)])

    enforce_torque = self._enforce_torque = ui_state.params.get_bool("EnforceTorqueControl")
    self_tune_on = ui_state.params.get_bool("LiveTorqueParamsToggle")
    custom_on = ui_state.params.get_bool("CustomTorqueParams")

    self._torque_settings_btn.set_enabled(torque_allowed)
    if not enforce_torque:
      self._torque_settings_btn.set_disabled()
    else:
      self._torque_settings_btn.set_badges([(tr("enabled"), "on"), (tr("self-tune"), _on_off(self_tune_on)), (tr("custom-tuning"), _on_off(custom_on))])
    self._nnlc_toggle.set_enabled(torque_allowed and offroad and not enforce_torque)

    # --- Sub-panel state (sub-panels refresh themselves; this is transition cleanup + badges) ---
    self._update_mads_state()
    self._update_torque_state(self_tune_on, custom_on)

  # --- MADS sub-panel ---
  def _update_mads_state(self):
    # Transition tracking — force safe defaults for MADS-limited brands (rivian, tesla w/o vehicle bus)
    is_mads_limited = self._mads_limited = bool(ui_state.CP is not None and ui_state.CP_SP is not None and
                                                get_mads_limited_brands(ui_state.CP, ui_state.CP_SP))
    if is_mads_limited and self._prev_mads_limited is not True:
      ui_state.params.remove("MadsMainCruiseAllowed")
      ui_state.params.put_bool("MadsUnifiedEngagementMode", True)
      ui_state.params.put("MadsSteeringMode", MadsSteeringModeOnBrake.DISENGAGE)
    self._prev_mads_limited = is_mads_limited

    # Sub-panels refresh their own widgets (SubPanelSP); only the mads-limited lockout, which
    # isn't expressible as a depends_on, has to be pushed in from here
    self._mads_steering.set_enabled(not is_mads_limited and ui_state.params.get_bool("Mads"))

  # --- Lane change sub-panel ---
  @staticmethod
  def _bsm_applies(alc_val: int) -> bool:
    """BSM delay only applies once auto lane change is past Nudge (Nudgeless or timed) — matches TICI."""
    return alc_val > AutoLaneChangeMode.NUDGE

  @staticmethod
  def _car_has_bsm() -> bool:
    return ui_state.CP is not None and ui_state.CP.enableBsm

  # --- Torque sub-panel ---
  def _update_torque_state(self, self_tune_on: bool, custom_on: bool):
    # Badges only — the widgets themselves refresh and gate on their own
    if not gui_app.widget_in_stack(self._tq_view):
      return

    if not self_tune_on:
      self._tq_self_tune_btn.set_disabled()
    else:
      self._tq_self_tune_btn.set_badges([(tr("enabled"), "on"), (tr("less-restrict"), _on_off(ui_state.params.get_bool("LiveTorqueParamsRelaxedToggle"))),
                                          (tr("speed-dependent"), _on_off(ui_state.params.get_bool("SpeedDependentTorqueToggle")))])

    if not custom_on:
      self._tq_custom_btn.set_disabled()
    else:
      manual_rt = _on_off(ui_state.params.get_bool("TorqueParamsOverrideEnabled"))
      lat_val = int(ui_state.params.get("TorqueParamsOverrideLatAccelFactor", return_default=True) or 1) / 100
      fric_val = int(ui_state.params.get("TorqueParamsOverrideFriction", return_default=True) or 1) / 100
      self._tq_custom_btn.set_badges([(tr("enabled"), "on"), (tr("realtime"), manual_rt), (f"{lat_val}m/s\u00b2", "on"), (str(fric_val), "on")])

    # The nested self-tune/custom sub-panels refresh themselves (link_sub_panel) and gate
    # themselves (depends_on / constructor set_enabled) — nothing to drive from here
