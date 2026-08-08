from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from openpilot.common.params import Params
from openpilot.system.manager import process_config
from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.navigation_database_restore_runtime import (
  NavigationDatabaseRestoreRuntime,
)
from openpilot.system.ubloxd.yuma_almanac_config import (
  PUBLIC_YUMA_ALMANAC_ENABLED_PARAM,
  public_yuma_almanac_enabled,
)
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaDatabaseRestoreState,
  YumaSupplementationAction,
  YumaSupplementationReason,
  plan_yuma_supplementation,
)
from openpilot.system.ubloxd.yuma_almanac_runtime import (
  YumaSupplementationRuntimeOutcome,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  YumaAssistanceStateUnavailableError,
)
from openpilot.system.ubloxd.tests.test_pigeond_dbd_restore_integration import (
  BOOT_ID,
  TEST_BOOTTIME_SECONDS,
  snapshot,
)
from openpilot.system.ubloxd.tests.test_yuma_almanac_runtime import runtime


PARAMS_KEYS_PATH = Path(__file__).resolve().parents[3] / "common" / "params_keys.h"
YUMA_PRNS = frozenset((*range(1, 13), *range(14, 33)))


class FakeParams:
  def __init__(self, enabled: bool) -> None:
    self.enabled = enabled

  def get_bool(self, key: str) -> bool:
    assert key == PUBLIC_YUMA_ALMANAC_ENABLED_PARAM
    return self.enabled


def initialization(completed_at: float):
  return SimpleNamespace(
    navigation_assistance_restore_result=None,
    completed_at=completed_at,
    time_assistance_utc=None,
    time_assistance_source=None,
    gnss_start_sent_at=50.0,
  )


def evaluate(feature, now: float):
  return feature.evaluate(
    lambda _message: True,
    now=now,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )


def test_public_yuma_default_is_enabled_in_params_keys():
  text = PARAMS_KEYS_PATH.read_text(encoding="utf-8")
  match = re.search(
    r'\{\s*"PublicYumaAlmanacEnabled"\s*,\s*\{[^}]*BOOL\s*,\s*"([01])"\s*\}\s*\}',
    text,
  )
  assert match is not None
  assert match.group(1) == "1"


def test_public_yuma_params_default_value_is_enabled():
  assert Params().get_default_value(PUBLIC_YUMA_ALMANAC_ENABLED_PARAM) is True


def test_default_enabled_allows_yuma_download_path(monkeypatch):
  monkeypatch.setattr(process_config, "ublox_available", lambda: True)
  assert process_config.yuma_almanac_refresh(
    False,
    FakeParams(True),
    SimpleNamespace(),
  )


def test_kill_switch_disables_yuma_download_and_gate(monkeypatch):
  monkeypatch.setattr(process_config, "ublox_available", lambda: True)
  assert not public_yuma_almanac_enabled(FakeParams(False))
  assert not process_config.yuma_almanac_refresh(
    False,
    FakeParams(False),
    SimpleNamespace(),
  )


def test_kill_switch_disables_yuma_transmission(monkeypatch):
  created: list[object] = []

  def create_runtime(*_args, **_kwargs):
    created.append(True)
    raise AssertionError("YUMA runtime must not be created when disabled")

  monkeypatch.setattr(
    pigeond,
    "create_yuma_supplementation_runtime",
    create_runtime,
  )
  feature = pigeond.YumaSupplementationFeature(
    FakeParams(False),
    initialization(100.0),
    0,
  )
  assert not feature.runtime_active
  assert evaluate(feature, 100.0) is None
  assert (
    feature.evaluate_provisional(
      lambda _message: None,
      now=100.0,
      reliable_fix_available=False,
      database_restore_pending=False,
    )
    is None
  )
  assert created == []


def test_dbd_terminal_required_before_yuma_plan_and_claim(tmp_path):
  waiting = plan_yuma_supplementation(
    database_state=YumaDatabaseRestoreState.PENDING,
    database_age_seconds=60.0,
    yuma_reference_age_seconds=60.0,
    nav_sat=None,
    yuma_satellite_ids=YUMA_PRNS,
    trusted_time_available=True,
    reliable_fix_available=False,
    trusted_time_wait_expired=False,
    cache_wait_expired=False,
    nav_sat_observation_expired=False,
  )
  assert waiting.action is YumaSupplementationAction.WAIT
  assert waiting.reason is YumaSupplementationReason.WAITING_FOR_DATABASE_RESTORE

  runtime = NavigationDatabaseRestoreRuntime(
    "receiver",
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  runtime.prepare()
  writes: list[bytes] = []
  with pytest.raises(YumaAssistanceStateUnavailableError):
    pigeond.send_yuma_with_durable_claim(
      runtime,
      writes.append,
      b"yuma-frame",
    )
  assert writes == []
  assert runtime.database_restore_pending

  assert runtime.close_restore_window_wait_timeout()
  pigeond.send_yuma_with_durable_claim(
    runtime,
    writes.append,
    b"yuma-frame",
  )
  assert writes == [b"yuma-frame"]
  assert not runtime.database_restore_pending


def test_yuma_recorded_after_gnss_start_not_before():
  feature = pigeond.YumaSupplementationFeature.__new__(pigeond.YumaSupplementationFeature)
  feature._receiver_cycle = 0
  feature._initialization = SimpleNamespace(gnss_start_sent_at=40.0)
  outcome = YumaSupplementationRuntimeOutcome(
    plan=plan_yuma_supplementation(
      database_state=YumaDatabaseRestoreState.SKIPPED,
      database_age_seconds=None,
      yuma_reference_age_seconds=60.0,
      nav_sat=None,
      yuma_satellite_ids=YUMA_PRNS,
      trusted_time_available=True,
      reliable_fix_available=False,
      trusted_time_wait_expired=False,
      cache_wait_expired=False,
      nav_sat_observation_expired=True,
    ),
    completion_monotonic=55.0,
    terminal=True,
  )
  contextualized = feature._contextualize_outcome(outcome)
  assert contextualized.gnss_start_sent_at_monotonic == 40.0
  assert contextualized.completed_before_gnss_start is False


def test_yuma_transmit_failure_is_fail_open_terminal(monkeypatch):
  state = runtime(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
  )

  def boom(*_args, **_kwargs):
    raise RuntimeError("injected YUMA transmission failure")

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    boom,
  )

  outcome = state.evaluate(
    lambda _message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert outcome.terminal
  assert outcome.error is not None
  assert "injected YUMA transmission failure" in outcome.error
  assert state.completed
  assert not state.retry_pending
