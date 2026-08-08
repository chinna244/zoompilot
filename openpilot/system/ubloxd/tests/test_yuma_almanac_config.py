import re
from pathlib import Path

from openpilot.common.params import Params
from openpilot.system.ubloxd.yuma_almanac_config import (
  PUBLIC_YUMA_ALMANAC_ENABLED_PARAM,
  public_yuma_almanac_enabled,
)


PARAMS_KEYS_PATH = Path(__file__).resolve().parents[3] / "common" / "params_keys.h"


class FakeParams:
  def __init__(self, enabled: bool) -> None:
    self.enabled = enabled
    self.requested_keys: list[str] = []

  def get_bool(self, key: str) -> bool:
    self.requested_keys.append(key)
    return self.enabled


def test_public_yuma_default_enabled_in_params_keys():
  text = PARAMS_KEYS_PATH.read_text(encoding="utf-8")
  match = re.search(
    r'\{\s*"PublicYumaAlmanacEnabled"\s*,\s*\{[^}]*BOOL\s*,\s*"([01])"\s*\}\s*\}',
    text,
  )
  assert match is not None
  assert match.group(1) == "1"


def test_public_yuma_params_default_value_enabled():
  assert Params().get_default_value(PUBLIC_YUMA_ALMANAC_ENABLED_PARAM) is True


def test_public_yuma_gate_reads_configured_param():
  params = FakeParams(True)

  assert public_yuma_almanac_enabled(params)
  assert params.requested_keys == [PUBLIC_YUMA_ALMANAC_ENABLED_PARAM]


def test_public_yuma_kill_switch_disables_gate():
  assert not public_yuma_almanac_enabled(FakeParams(False))


def test_public_yuma_gate_fails_closed_without_params_api():
  assert not public_yuma_almanac_enabled(object())


def test_public_yuma_gate_fails_closed_when_read_raises(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_config.cloudlog.exception",
    logs.append,
  )

  class RaisingParams:
    def __init__(self, error: Exception) -> None:
      self.error = error

    def get_bool(self, key: str) -> bool:
      raise self.error

  for error in (
    OSError("injected Params I/O failure"),
    RuntimeError("injected Params failure"),
  ):
    assert not public_yuma_almanac_enabled(RaisingParams(error))

  assert logs == [
    "Failed to read public YUMA feature gate",
    "Failed to read public YUMA feature gate",
  ]
