from types import SimpleNamespace

from openpilot.system.manager import process_config


class DummyParams:
  def __init__(self, disable_logging: bool = False):
    self.disable_logging = disable_logging

  def get_bool(self, key: str) -> bool:
    assert key == "DisableLogging"
    return self.disable_logging


def test_logging_starts_immediately_onroad_for_car():
  params = DummyParams(disable_logging=True)
  car_params = SimpleNamespace(notCar=False)

  assert not process_config.logging(False, params, car_params)
  assert process_config.logging(True, params, car_params)


def test_logging_starts_onroad_for_notcar_when_enabled():
  params = DummyParams(disable_logging=False)
  car_params = SimpleNamespace(notCar=True)

  assert not process_config.logging(False, params, car_params)
  assert process_config.logging(True, params, car_params)


def test_logging_stays_disabled_for_notcar_when_requested():
  params = DummyParams(disable_logging=True)
  car_params = SimpleNamespace(notCar=True)

  assert not process_config.logging(False, params, car_params)
  assert not process_config.logging(True, params, car_params)
