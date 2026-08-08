from types import SimpleNamespace

from openpilot.system.manager import process_config
from openpilot.system.ubloxd.yuma_almanac_config import (
  PUBLIC_YUMA_ALMANAC_ENABLED_PARAM,
)


class FakeParams:
  def __init__(self, enabled: bool) -> None:
    self.enabled = enabled

  def get_bool(self, key: str) -> bool:
    assert key == PUBLIC_YUMA_ALMANAC_ENABLED_PARAM
    return self.enabled


def test_yuma_downloader_kill_switch_disables_refresh(monkeypatch):
  monkeypatch.setattr(
    process_config,
    "ublox_available",
    lambda: True,
  )

  assert not process_config.yuma_almanac_refresh(
    False,
    FakeParams(False),
    SimpleNamespace(),
  )


def test_yuma_downloader_enabled_by_default_gate(monkeypatch):
  monkeypatch.setattr(
    process_config,
    "ublox_available",
    lambda: True,
  )

  assert process_config.yuma_almanac_refresh(
    False,
    FakeParams(True),
    SimpleNamespace(),
  )


def test_yuma_downloader_requires_offroad_ublox_and_enable(
  monkeypatch,
):
  params = FakeParams(True)
  car_params = SimpleNamespace()

  monkeypatch.setattr(
    process_config,
    "ublox_available",
    lambda: True,
  )
  assert process_config.yuma_almanac_refresh(
    False,
    params,
    car_params,
  )
  assert not process_config.yuma_almanac_refresh(
    True,
    params,
    car_params,
  )

  monkeypatch.setattr(
    process_config,
    "ublox_available",
    lambda: False,
  )
  assert not process_config.yuma_almanac_refresh(
    False,
    params,
    car_params,
  )
