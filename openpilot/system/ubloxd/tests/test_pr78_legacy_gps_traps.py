from __future__ import annotations

import inspect
from pathlib import Path
from typing import cast

import pytest

from openpilot.common.params import Params, UnknownKeyName
from openpilot.system.ubloxd import pigeond


PIGEOND_SOURCE = Path(pigeond.__file__).read_text(encoding="utf-8")
PARAMS_KEYS_SOURCE = Path(Path(__file__).resolve().parents[3] / "common" / "params_keys.h").read_text(encoding="utf-8")


def test_production_api_cannot_reenable_prestart_trusted_time_wait() -> None:
  signature = inspect.signature(pigeond.initialize_receiver_cycle)
  assert "allow_database_trusted_time_wait" not in signature.parameters
  assert "network_available_reader" not in signature.parameters
  assert not hasattr(pigeond, "wait_for_current_independent_network_time")
  assert not hasattr(pigeond, "should_wait_for_navigation_database_trusted_time")
  assert "allow_database_trusted_time_wait" not in PIGEOND_SOURCE
  assert "network_available_reader" not in PIGEOND_SOURCE
  assert "NAVIGATION_DATABASE_TRUSTED_TIME_WAIT_SECONDS" not in PIGEOND_SOURCE


def test_gnss_start_path_has_no_prestart_network_or_trusted_time_wait() -> None:
  assert "wait_for_current_independent_network_time" not in PIGEOND_SOURCE
  assert "get_assistnow_messages" not in PIGEOND_SOURCE
  assert "online-live2.services.u-blox.com" not in PIGEOND_SOURCE
  assert "GetOnlineData.ashx" not in PIGEOND_SOURCE
  assert "def send_time_assistance(" in PIGEOND_SOURCE
  assert pigeond.NAVIGATION_DATABASE_PROCESS_START_TIME_DEADLINE_SECONDS == 45.0


def test_legacy_assistnow_online_is_retired_from_production() -> None:
  assert not hasattr(pigeond, "get_assistnow_messages")
  assert "AssistNowToken" not in PIGEOND_SOURCE
  assert "AssistNowToken" not in PARAMS_KEYS_SOURCE
  assert "def run_post_start_legacy_assistance(" in PIGEOND_SOURCE
  assert "def configure_assistnow_autonomous(" in PIGEOND_SOURCE


def test_assistnow_token_is_no_longer_a_registered_param() -> None:
  params = Params()
  assert b"AssistNowToken" not in params.all_keys()
  with pytest.raises(UnknownKeyName):
    params.check_key("AssistNowToken")
  with pytest.raises(UnknownKeyName):
    params.get("AssistNowToken")


def test_post_start_legacy_assistance_does_not_read_token_or_network(
  monkeypatch,
) -> None:
  events: list[str] = []

  class Pigeon:
    def poll_backup_restore_status(self) -> int:
      events.append("backup")
      return 3

    def send_with_ack(self, *_args, **_kwargs) -> None:
      raise AssertionError("AssistNow Online injection must not run")

  monkeypatch.setattr(
    pigeond,
    "Params",
    lambda: (_ for _ in ()).throw(AssertionError("AssistNowToken must not be read")),
  )
  pigeond.run_post_start_legacy_assistance(cast(pigeond.TTYPigeon, Pigeon()))
  assert events == ["backup"]


def test_init_orders_legacy_assistance_after_gnss_start() -> None:
  init_start = PIGEOND_SOURCE.index("def init(pigeon:")
  init_end = PIGEOND_SOURCE.index("\nclass TimeAssistanceWriteStatus", init_start)
  init_segment = PIGEOND_SOURCE[init_start:init_end]
  assert init_segment.index("finish_pigeon_initialization(") < init_segment.index("run_post_start_legacy_assistance(")
  assert "get_assistnow_messages" not in init_segment
  assert "AssistNowToken" not in init_segment
