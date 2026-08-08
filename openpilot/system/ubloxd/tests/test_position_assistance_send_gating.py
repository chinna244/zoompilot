from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import (
  QUALITY_POLICY_VERSION,
  QUALITY_VERSION,
  NavigationQuality,
)
from openpilot.system.ubloxd.navigation_database_restore import (
  NavigationDatabaseRestoreDisposition,
)
from openpilot.system.ubloxd.navigation_database_restore_runtime import (
  NavigationDatabaseRestoreFrozenCaches,
  NavigationDatabaseRestoreRuntime,
  NavigationDatabaseRestoreSnapshot,
  PositionAssistanceAckStatus,
  PositionAssistanceFailureKind,
  PositionAssistanceWriteStatus,
)
from openpilot.system.ubloxd.position_assistance_retry import (
  PositionAssistanceRetryResult,
  PositionAssistanceRetryRuntime,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
  TrustedTimeSource,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AuthorizedTime,
  TimeAuthorizationEvidence,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import MgaReceiverNackError


BOOT_ID = "12345678-1234-5678-9234-567812345678"
TEST_RECEIVER_FINGERPRINT = "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov"
NOW = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)
TEST_BOOTTIME_SECONDS = 100.0


def same_boot_time() -> AuthorizedTime:
  return AuthorizedTime(
    utc=NOW,
    uncertainty_seconds=2.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    independent=False,
    evidence=TimeAuthorizationEvidence.SAME_BOOT_BOOTTIME,
    observed_boottime_seconds=TEST_BOOTTIME_SECONDS,
  )


def startup_ready_quality() -> NavigationQuality:
  return NavigationQuality(
    quality_version=QUALITY_VERSION,
    policy_version=QUALITY_POLICY_VERSION,
    capture_context="onroad",
    continuous_reliable_fix_seconds=20.0,
    continuous_orbit_quality_seconds=10.0,
    gps_satellites_known=8,
    glonass_satellites_known=8,
    gps_ephemeris_available=4,
    glonass_ephemeris_available=6,
    satellites_used=8,
    gps_almanac_available=5,
    glonass_almanac_available=5,
    assistnow_offline_available=0,
    orbit_source_counts={"ephemeris": 10, "almanac": 6},
  )


def position_snapshot(
  *,
  age: timedelta = timedelta(minutes=5),
  with_database: bool = True,
) -> NavigationDatabaseRestoreSnapshot:
  return NavigationDatabaseRestoreSnapshot(
    saved_at_utc=NOW - age,
    database_frames=(b"database-frame",) if with_database else (),
    latitude_e7=320_000_000,
    longitude_e7=-960_000_000,
    altitude_cm=20_000,
    position_accuracy_cm=10_000,
    quality=startup_ready_quality(),
    generation="primary",
    selection_reason="test",
  )


def make_runtime(
  tmp_path: Path,
  *,
  loaded: NavigationDatabaseRestoreFrozenCaches | NavigationDatabaseRestoreSnapshot | None,
  state_name: str = "dbd_state.json",
) -> NavigationDatabaseRestoreRuntime:
  return NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: loaded,
    retry_delay_seconds=0.0,
    state_path=tmp_path / state_name,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )


def install_send_recorder(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
  writes: list[bytes] = []

  def send_mga(_pigeon, message: bytes, **kwargs) -> None:
    if kwargs.get("database_frame_index") is not None:
      raise AssertionError("unexpected DBD write during position gating test")
    writes.append(message)

  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", send_mga)
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  return writes


def test_field_case_skipped_no_trusted_time_after_acquisition_skips_unverified_position(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  runtime = make_runtime(tmp_path, loaded=position_snapshot())
  runtime.prepare()
  runtime.close_restore_window_no_trusted_time()
  assert runtime.note_acquisition_started()
  assert runtime.acquisition_started
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME
  assert not runtime.execution.position_assistance_attempted

  writes = install_send_recorder(monkeypatch)
  result = pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
    authorized_time=None,
  )

  assert len(writes) == 0
  assert runtime.execution.position_assistance_attempted
  assert not runtime.execution.position_assistance_succeeded
  assert result.position_assistance_attempted
  assert not result.position_assistance_succeeded


@pytest.mark.parametrize(
  "close_method,expected_disposition",
  (
    (
      "close_restore_window_no_trusted_time",
      NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME,
    ),
    (
      "close_restore_window_unverified",
      NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED,
    ),
    (
      "close_restore_window_wait_timeout",
      NavigationDatabaseRestoreDisposition.SKIPPED_WAIT_TIMEOUT,
    ),
  ),
)
def test_non_success_dbd_outcomes_still_send_fresh_same_boot_position(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
  close_method: str,
  expected_disposition: NavigationDatabaseRestoreDisposition,
) -> None:
  runtime = make_runtime(tmp_path, loaded=position_snapshot(age=timedelta(minutes=5)))
  runtime.prepare()
  getattr(runtime, close_method)()
  runtime.note_acquisition_started()
  assert runtime.controller.disposition is expected_disposition

  writes = install_send_recorder(monkeypatch)
  pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
    authorized_time=same_boot_time(),
  )

  assert len(writes) == 1
  assert runtime.execution.position_assistance_attempted
  assert runtime.execution.position_assistance_succeeded


def test_non_success_dbd_without_age_evidence_skips_position(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  runtime = make_runtime(tmp_path, loaded=position_snapshot())
  runtime.prepare()
  runtime.close_restore_window_no_trusted_time()
  runtime.note_acquisition_started()

  writes = install_send_recorder(monkeypatch)
  pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
    authorized_time=None,
  )

  assert len(writes) == 0
  assert runtime.execution.position_assistance_attempted
  assert not runtime.execution.position_assistance_succeeded


def test_no_position_snapshot_does_not_send_or_arm_retry(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  frozen = NavigationDatabaseRestoreFrozenCaches(
    position_snapshot=None,
    primary_snapshot=None,
    previous_snapshot=None,
  )
  runtime = make_runtime(tmp_path, loaded=frozen)
  runtime.prepare()
  runtime.close_restore_window_no_trusted_time()
  runtime.note_acquisition_started()

  writes = install_send_recorder(monkeypatch)
  retry = PositionAssistanceRetryRuntime(
    TEST_RECEIVER_FINGERPRINT,
    state_path=tmp_path / "retry_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    new_receiver_cycle=True,
  )
  controller = pigeond.PositionAssistancePostStartRetryController(retry)

  pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
  )
  controller.runtime = retry
  retry.arm_from_initial(
    runtime.execution,
    runtime.position_assistance_message,
  )

  assert writes == []
  assert not runtime.execution.position_assistance_attempted
  assert not retry.state.initial_attempted
  assert not retry.state.retry_armed


def test_invalid_snapshot_load_does_not_send_position(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  def fail_loader(_fingerprint: str):
    raise OSError("cache read failed")

  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=fail_loader,
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  runtime.prepare()
  runtime.close_restore_window_no_trusted_time()
  runtime.note_acquisition_started()

  writes = install_send_recorder(monkeypatch)
  pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
  )

  assert writes == []
  assert not runtime.execution.position_assistance_attempted


def test_one_shot_claim_prevents_duplicate_position_send(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  runtime = make_runtime(tmp_path, loaded=position_snapshot())
  runtime.prepare()
  runtime.close_restore_window_no_trusted_time()
  runtime.note_acquisition_started()
  writes = install_send_recorder(monkeypatch)

  pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
    authorized_time=same_boot_time(),
  )
  pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
    authorized_time=same_boot_time(),
  )

  assert len(writes) == 1
  assert runtime.execution.position_assistance_attempted


def test_info_code_5_rejection_arms_bounded_retry_once(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  runtime = make_runtime(tmp_path, loaded=position_snapshot())
  runtime.prepare()
  runtime.close_restore_window_no_trusted_time()
  runtime.note_acquisition_started()

  def reject(_pigeon, message: bytes, **_kwargs) -> None:
    raise MgaReceiverNackError(
      "receiver not ready",
      message_id=message[3],
      message_type=message[6],
      ack_type=0,
      ack_version=0,
      info_code=5,
      rejected_message_id=message[3],
    )

  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", reject)
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )

  pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
    authorized_time=same_boot_time(),
  )

  retry = PositionAssistanceRetryRuntime(
    TEST_RECEIVER_FINGERPRINT,
    state_path=tmp_path / "retry_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    new_receiver_cycle=True,
  )
  assert retry.arm_from_initial(
    runtime.execution,
    runtime.position_assistance_message,
  )
  assert runtime.execution.position_assistance_attempted
  assert not runtime.execution.position_assistance_succeeded
  assert runtime.execution.position_assistance_write_status is PositionAssistanceWriteStatus.SUCCEEDED
  assert runtime.execution.position_assistance_ack_status is PositionAssistanceAckStatus.REJECTED
  assert runtime.execution.position_assistance_ack_info_code == 5
  assert retry.state.initial_attempted
  assert retry.state.retry_armed
  assert retry.state.retry_result is PositionAssistanceRetryResult.ARMED
  assert retry.state.pending
  # Second arm is a no-op that preserves the already-armed pending retry.
  assert retry.arm_from_initial(
    runtime.execution,
    runtime.position_assistance_message,
  )
  assert retry.state.retry_armed
  assert retry.state.pending
  assert retry.state.retry_result is PositionAssistanceRetryResult.ARMED


def test_successful_initial_send_does_not_arm_retry(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  runtime = make_runtime(tmp_path, loaded=position_snapshot())
  runtime.prepare()
  runtime.close_restore_window_no_trusted_time()
  runtime.note_acquisition_started()
  install_send_recorder(monkeypatch)

  pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
    authorized_time=same_boot_time(),
  )

  retry = PositionAssistanceRetryRuntime(
    TEST_RECEIVER_FINGERPRINT,
    state_path=tmp_path / "retry_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    new_receiver_cycle=True,
  )
  assert not retry.arm_from_initial(
    runtime.execution,
    runtime.position_assistance_message,
  )
  assert retry.state.initial_attempted
  assert not retry.state.retry_armed
  assert retry.state.retry_result is PositionAssistanceRetryResult.NOT_ARMED


def test_receiver_cycle_isolation_clears_claimed_and_retry_state(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  first = make_runtime(tmp_path, loaded=position_snapshot(), state_name="cycle1.json")
  first.prepare()
  first.close_restore_window_no_trusted_time()
  first.note_acquisition_started()
  writes = install_send_recorder(monkeypatch)
  pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=first,
    authorized_time=same_boot_time(),
  )
  assert len(writes) == 1

  retry = PositionAssistanceRetryRuntime(
    TEST_RECEIVER_FINGERPRINT,
    state_path=tmp_path / "retry_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    new_receiver_cycle=True,
  )
  retry.arm_from_initial(first.execution, first.position_assistance_message)

  next_cycle = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: position_snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "cycle2.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    new_receiver_cycle=True,
  )
  next_retry = PositionAssistanceRetryRuntime(
    TEST_RECEIVER_FINGERPRINT,
    state_path=tmp_path / "retry_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    new_receiver_cycle=True,
  )
  next_cycle.prepare()
  next_cycle.close_restore_window_no_trusted_time()
  next_cycle.note_acquisition_started()

  assert not next_cycle.execution.position_assistance_attempted
  assert not next_retry.state.initial_attempted
  assert not next_retry.state.retry_armed

  pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=next_cycle,
    authorized_time=same_boot_time(),
  )
  assert len(writes) == 2
  assert next_cycle.execution.position_assistance_attempted


def test_restore_position_path_does_not_reintroduce_trusted_time_wait(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  runtime = make_runtime(tmp_path, loaded=position_snapshot())
  runtime.prepare()
  runtime.close_restore_window_no_trusted_time()
  runtime.note_acquisition_started()
  install_send_recorder(monkeypatch)

  assert not hasattr(pigeond, "wait_for_current_independent_network_time")

  result = pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
    authorized_time=None,
  )
  assert result.database_trusted_time_wait_started_at is None
  assert result.database_trusted_time_wait_elapsed_seconds is None
  assert runtime.execution.position_assistance_attempted
  assert not runtime.execution.position_assistance_succeeded
  assert runtime.execution.position_assistance_failure_kind is PositionAssistanceFailureKind.AGE_UNVERIFIED


def test_restore_source_no_longer_gates_position_on_acquisition_or_early_skip() -> None:
  source = Path(pigeond.__file__).read_text()
  restore_start = source.index("def restore_navigation_assistance(")
  restore_end = source.index("\ndef ", restore_start + 1)
  restore_segment = source[restore_start:restore_end]
  assert "send_position_once(" in restore_segment
  assert "SKIPPED_EARLY_ACQUISITION" not in restore_segment
  assert "not navigation_database_runtime.acquisition_started" not in restore_segment
