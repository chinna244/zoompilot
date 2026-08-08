"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.
"""

# Which torque controller an unset TorqueControlTune selects. This is easy to get wrong by
# dropping `return_default=True` from the params read: params_keys.h declares "0.0" (v0), but
# a bare params.get() returns None for an unset param, and `None == 0.0` is False — which
# silently selects the newest tune instead. Nothing errors; the car just steers on v1.
#
# The v0 constructor is patched out: these tests pin the branch that gets taken, not the
# controller's behavior, and building the real one pulls in NNLC model loading.

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opendbc.car.structs import car
from openpilot.cereal import custom
from openpilot.common.params import Params
from openpilot.common.prefix import OpenpilotPrefix
from openpilot.sunnypilot.selfdrive.controls import controlsd_ext
from openpilot.sunnypilot.selfdrive.controls.controlsd_ext import ControlsExt

V0 = "v0"
V1 = "v1"  # stands in for the `lac` upstream controller controlsd passes in


@pytest.fixture
def ctx(monkeypatch):
  monkeypatch.setattr(controlsd_ext, "LatControlTorqueV0", lambda *a, **k: V0)
  with OpenpilotPrefix():
    params = Params()
    CP = car.CarParams.new_message(steerControlType="torque")
    CP.lateralTuning.init('torque')
    controls = SimpleNamespace(params=params, CP=CP.as_reader(),
                               CP_SP=custom.CarParamsSP.new_message().as_reader())
    yield params, controls


def select(controls):
  return ControlsExt.initialize_lateral_control(controls, V1, MagicMock(), 0.01)


class TestTorqueTuneSelection:
  def test_unset_selects_v0(self, ctx):
    """The declared default in params_keys.h is 0.0 — an unset param must honor it."""
    params, controls = ctx
    params.put_bool("EnforceTorqueControl", True, block=True)
    params.remove("TorqueControlTune")
    assert select(controls) == V0

  @pytest.mark.parametrize(("version", "expected"), [(0.0, V0), (1.0, V1)])
  def test_explicit_version_is_honored(self, ctx, version, expected):
    params, controls = ctx
    params.put_bool("EnforceTorqueControl", True, block=True)
    params.put("TorqueControlTune", version, block=True)
    assert select(controls) == expected

  def test_torque_control_not_enforced_still_uses_v0_for_torque_cars(self, ctx):
    """Pre-existing behavior worth pinning: torque-tuned cars get v0 even with the toggle off."""
    params, controls = ctx
    params.put_bool("EnforceTorqueControl", False, block=True)
    params.put("TorqueControlTune", 1.0, block=True)
    assert select(controls) == V0

  def test_ui_default_option_matches_what_controls_runs(self, ctx):
    """The MICI selector shows the first (oldest) version for an unset param — it must be the
    same tune initialize_lateral_control picks, or the UI claims a tune the car isn't running."""
    from openpilot.selfdrive.ui.sunnypilot.mici.layouts.steering import SteeringLayoutMici

    params, controls = ctx
    versions = SteeringLayoutMici._load_torque_versions()
    shown_version = next(iter(versions.values()))  # oldest-first ordering

    params.put_bool("EnforceTorqueControl", True, block=True)
    params.remove("TorqueControlTune")
    assert (select(controls) == V0) is (shown_version == 0.0)
