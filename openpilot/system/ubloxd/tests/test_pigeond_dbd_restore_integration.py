from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from typing import cast

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import (
  NavigationQuality,
  QUALITY_POLICY_VERSION,
  QUALITY_VERSION,
)
import openpilot.system.ubloxd.navigation_database_restore_runtime as restore_runtime
from openpilot.system.ubloxd.navigation_database_restore import (
  NavigationDatabaseRestoreDisposition,
)
from openpilot.system.ubloxd.navigation_database_restore_runtime import (
  NavigationDatabaseRestoreExecution,
  NavigationDatabaseRestoreFrameFailure,
  NavigationDatabaseRestoreFrameFailureKind,
  NavigationDatabaseRestoreInitializationError,
  NavigationDatabaseRestoreRuntime,
  NavigationDatabaseRestoreSnapshot,
  PositionAssistanceAckStatus,
  PositionAssistanceFailureKind,
  PositionAssistanceWriteStatus,
)
from openpilot.system.ubloxd.position_assistance_retry import (
  PositionAssistanceRetryState,
  PositionAssistanceRetryStateError,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
  TrustedTimeSource,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AuthorizedTime,
  TimeAuthorizationEvidence,
)


BOOT_ID = "12345678-1234-5678-9234-567812345678"
TEST_RECEIVER_FINGERPRINT = "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov"
NOW = datetime(2026, 7, 29, 13, 0, tzinfo=UTC)
TEST_BOOTTIME_SECONDS = 100.0


class FakePigeon:
  def __init__(self, events: list[str] | None = None) -> None:
    self.sent: list[bytes] = []
    self.events = events

  def send(self, message: bytes) -> None:
    self.sent.append(message)
    if self.events is not None:
      if message == pigeond.CONTROLLED_GNSS_STOP_MESSAGE:
        self.events.append("gnss_stop")
      elif message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        self.events.append("gnss_start")


class FakeDiagnostics:
  cycle_number = 0

  def start_cycle(self, _reason: str, _now: float) -> None:
    self.cycle_number += 1

  def time_assistance_context(self, _now: float) -> str:
    return "test"


class FakeProvenance:
  cycle_id = 0

  def start_cycle(
    self,
    cycle_id: int,
    _now: float,
    *,
    observations_enabled: bool,
  ) -> None:
    assert not observations_enabled
    self.cycle_id = cycle_id

  def enable_receiver_observations(self, _now: float) -> None:
    pass


def network_time() -> AuthorizedTime:
  return AuthorizedTime(
    utc=NOW,
    uncertainty_seconds=1.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    independent=True,
    evidence=TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED,
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


def snapshot(
  *,
  age: timedelta = timedelta(minutes=10),
) -> NavigationDatabaseRestoreSnapshot:
  return NavigationDatabaseRestoreSnapshot(
    saved_at_utc=NOW - age,
    database_frames=(b"database-frame",),
    latitude_e7=320_000_000,
    longitude_e7=-960_000_000,
    altitude_cm=20_000,
    position_accuracy_cm=10_000,
    quality=startup_ready_quality(),
    generation="primary",
    selection_reason="test",
  )


@pytest.mark.parametrize(
  ("failure_kind", "expected_phase"),
  (
    (
      PositionAssistanceFailureKind.BUILD,
      pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_BUILD,
    ),
    (
      PositionAssistanceFailureKind.WRITE,
      pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_WRITE,
    ),
    (
      PositionAssistanceFailureKind.ACK_REJECTED,
      pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_REJECTED,
    ),
    (
      PositionAssistanceFailureKind.ACK_TIMEOUT,
      pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_TIMEOUT,
    ),
    (
      PositionAssistanceFailureKind.ACK_OBSERVATION_FAILED,
      pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_OBSERVATION_FAILED,
    ),
  ),
)
def test_position_failure_kind_survives_runtime_result_mapping(
  failure_kind: PositionAssistanceFailureKind,
  expected_phase: pigeond.NavigationAssistanceRestoreFailurePhase,
) -> None:
  execution = NavigationDatabaseRestoreExecution(
    disposition=NavigationDatabaseRestoreDisposition.RESTORED,
    total_frame_count=70,
    accepted_frame_count=70,
    database_write_attempt_count=70,
    position_assistance_attempted=True,
    position_assistance_succeeded=False,
    position_assistance_message_id=0x40,
    position_assistance_message_type=0x01,
    position_assistance_write_status=(
      PositionAssistanceWriteStatus.SUCCEEDED
      if failure_kind
      in (
        PositionAssistanceFailureKind.ACK_REJECTED,
        PositionAssistanceFailureKind.ACK_TIMEOUT,
        PositionAssistanceFailureKind.ACK_OBSERVATION_FAILED,
      )
      else PositionAssistanceWriteStatus.FAILED
    ),
    position_assistance_ack_status=(
      PositionAssistanceAckStatus.REJECTED
      if failure_kind is PositionAssistanceFailureKind.ACK_REJECTED
      else (
        PositionAssistanceAckStatus.TIMED_OUT
        if failure_kind is PositionAssistanceFailureKind.ACK_TIMEOUT
        else (
          PositionAssistanceAckStatus.OBSERVATION_FAILED
          if failure_kind is PositionAssistanceFailureKind.ACK_OBSERVATION_FAILED
          else PositionAssistanceAckStatus.NOT_ATTEMPTED
        )
      )
    ),
    position_assistance_ack_info_code=(5 if failure_kind is PositionAssistanceFailureKind.ACK_REJECTED else None),
    position_assistance_failure_kind=failure_kind,
    position_assistance_error_type="InjectedPositionError",
    position_assistance_error="InjectedPositionError:receiver detail",
  )

  result = pigeond.navigation_assistance_result_from_database_execution(execution)
  summary = pigeond.format_navigation_assistance_restore_summary(
    result,
    attempted=True,
    time_assistance_source="system_synchronized",
  )

  assert result.failure_phase is expected_phase
  assert result.position_assistance_attempted
  assert not result.position_assistance_succeeded
  assert result.position_assistance_message_id == 0x40
  assert result.position_assistance_message_type == 0x01
  assert result.position_assistance_error_type == "InjectedPositionError"
  assert result.position_assistance_error == "InjectedPositionError:receiver detail"
  assert f"failure_phase={expected_phase.value}" in summary
  assert "position_assistance_attempted=true" in summary
  assert "position_assistance_succeeded=false" in summary
  assert "position_assistance_message_id=0x40" in summary
  assert "position_assistance_message_type=0x01" in summary
  assert "position_assistance_error_type=InjectedPositionError" in summary
  assert "position_assistance_error=InjectedPositionError:receiver detail" in summary


def test_database_transfer_and_candidate_telemetry_survive_mapping() -> None:
  saved_at = NOW - timedelta(minutes=30)
  expires_at = saved_at + timedelta(hours=1)
  failure = NavigationDatabaseRestoreFrameFailure(
    frame_index=2,
    attempt=2,
    kind=NavigationDatabaseRestoreFrameFailureKind.TRANSFER_DEADLINE,
    error="NavigationDatabaseRestoreTransferDeadlineError:budget expired",
  )
  execution = NavigationDatabaseRestoreExecution(
    disposition=(NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE),
    total_frame_count=69,
    accepted_frame_count=2,
    database_write_attempt_count=3,
    permanent_failures=(failure,),
    cache_saved_at_utc=saved_at,
    cache_generation="primary",
    cache_selection_reason="primary_gps_startup_ready",
    cache_database_digest="a" * 64,
    cache_age_seconds=1800.0,
    cache_maximum_age_seconds=3600.0,
    cache_expires_at_utc=expires_at,
    transfer_budget_seconds=15.0,
    transfer_started_at=10.0,
    transfer_completed_at=25.0,
    transfer_deadline=25.0,
  )

  result = pigeond.navigation_assistance_result_from_database_execution(execution)
  summary = pigeond.format_navigation_assistance_restore_summary(
    result,
    attempted=True,
    time_assistance_source="system_synchronized",
  )

  assert result.restored_cache_database_digest == "a" * 64
  assert result.restored_cache_maximum_age_seconds == 3600.0
  assert result.restored_cache_expires_at_utc == expires_at
  assert result.database_restore_transfer_elapsed_seconds == 15.0
  assert result.database_restore_first_failed_frame_index == 2
  assert result.database_restore_first_failed_attempt == 2
  assert result.database_restore_first_failure_kind == NavigationDatabaseRestoreFrameFailureKind.TRANSFER_DEADLINE.value
  assert "restored_cache_database_digest=" + "a" * 64 in summary
  assert "restored_cache_maximum_age_seconds=3600.0" in summary
  assert "database_restore_transfer_elapsed_seconds=15.0" in summary
  assert "database_restore_first_failed_frame_index=2" in summary
  assert "database_restore_first_failure_kind=transfer_deadline" in summary


def test_position_nack_ack_detail_survives_restore_log() -> None:
  execution = NavigationDatabaseRestoreExecution(
    disposition=NavigationDatabaseRestoreDisposition.RESTORED,
    total_frame_count=70,
    accepted_frame_count=70,
    database_write_attempt_count=70,
    position_assistance_attempted=True,
    position_assistance_succeeded=False,
    position_assistance_message_id=0x40,
    position_assistance_message_type=0x01,
    position_assistance_write_status=(PositionAssistanceWriteStatus.SUCCEEDED),
    position_assistance_ack_status=(PositionAssistanceAckStatus.REJECTED),
    position_assistance_ack_info_code=5,
    position_assistance_failure_kind=(PositionAssistanceFailureKind.ACK_REJECTED),
    position_assistance_error_type="MgaReceiverNackError",
    position_assistance_error=("MgaReceiverNackError:u-blox rejected MGA message: ack_infoCode=5"),
  )

  result = pigeond.navigation_assistance_result_from_database_execution(execution)
  summary = pigeond.format_navigation_assistance_restore_summary(
    result,
    attempted=True,
    time_assistance_source="system_synchronized",
  )

  assert result.failure_phase is pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_REJECTED
  assert result.position_assistance_ack_status is PositionAssistanceAckStatus.REJECTED
  assert result.position_assistance_ack_info_code == 5
  assert "position_assistance_ack_status=rejected" in summary
  assert "position_assistance_ack_info_code=5" in summary
  assert "ack_infoCode=5" in summary


def test_startup_timeline_formats_correlated_fields() -> None:
  restore = pigeond.NavigationAssistanceRestoreResult(
    status=pigeond.NavigationAssistanceRestoreStatus.PARTIAL,
    total_frame_count=3,
    accepted_frame_count=2,
    initially_timed_out_indexes=(2,),
    permanently_rejected_indexes=(1,),
    permanently_timed_out_indexes=(2,),
    restored_cache_generation="primary",
    restored_cache_selection_reason=("trusted_age_only_eligible:primary"),
    restored_cache_age_seconds=1800.0,
    database_restore_disposition=(NavigationDatabaseRestoreDisposition.RESTORED),
    database_frames_attempted_count=3,
  )

  time_attempt = pigeond.TimeAssistanceAttemptDiagnostic(
    attempted_at=44.6,
    written_at=44.7,
    ack_observed_at=44.8,
    write_status=pigeond.TimeAssistanceWriteStatus.SUCCEEDED,
    ack_status=pigeond.TimeAssistanceAckStatus.REJECTED,
    ack_info_code=5,
    accepted_at=None,
    message_id=0x40,
    message_type=0x10,
    source="system_synchronized",
    correction=False,
    diagnostic_context="cycle=2, reason=process_start",
  )

  message = pigeond.format_gps_startup_timeline(
    cycle=2,
    reason="process_start",
    cycle_started_at=10.0,
    trusted_time_wait_started_at=12.0,
    trusted_time_wait_completed_at=44.5,
    independent_network_time_seen_at=44.5,
    acquisition_start_claimed_at=45.0,
    gnss_start_sent_at=45.1,
    restore_result=restore,
    authorized_time=network_time(),
    time_assistance_attempts=(time_attempt,),
  )

  assert "GPS startup timeline" in message
  assert "cycle=2" in message
  assert "reason=process_start" in message
  assert "trusted_time_wait_started_cycle_seconds=2.000" in message
  assert "trusted_time_wait_completed_cycle_seconds=34.500" in message
  assert "trusted_time_wait_duration_seconds=32.500" in message
  assert "independent_network_time_first_seen_cycle_seconds=34.500" in message
  assert "trusted_time_available=true" in message
  assert "database_restore_disposition=restored" in message
  assert "restored_cache_generation=primary" in message
  assert "restored_cache_selection_reason=trusted_age_only_eligible:primary" in message
  assert "restored_cache_age_seconds=1800.0" in message
  assert "database_frames_attempted=3" in message
  assert "database_frames_accepted=2" in message
  assert "database_frames_rejected=1" in message
  assert "database_timeout_events=2" in message
  assert "time_assistance_attempted_cycle_seconds=34.600" in message
  assert "time_assistance_written_cycle_seconds=34.700" in message
  assert "time_assistance_ack_observed_cycle_seconds=34.800" in message
  assert "time_assistance_write_status=succeeded" in message
  assert "time_assistance_ack_status=rejected" in message
  assert "time_assistance_ack_info_code=5" in message
  assert "time_assistance_accepted_cycle_seconds=none" in message
  assert "time_assistance_accepted_before_gnss_start=false" in message
  assert "acquisition_start_claimed_cycle_seconds=35.000" in message
  assert "gnss_start_sent_cycle_seconds=35.100" in message
  assert "related_acquisition_milestones=first_nonempty_rawx|first_fix_ok|first_reliable_fix" in message


def test_startup_timeline_rejects_incomplete_time_stub() -> None:
  incomplete = SimpleNamespace(
    utc=NOW,
    evidence=SimpleNamespace(value="system_synchronized"),
    mga_accuracy_seconds=30,
    independent=True,
    provenance=SimpleNamespace(value="network_independent"),
    observed_boottime_seconds=100.0,
  )

  assert not pigeond._startup_timeline_has_current_network_time(incomplete)
  assert pigeond._startup_timeline_has_current_network_time(network_time())


def test_paused_acquisition_records_start_timestamp(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  pigeon = FakePigeon()
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond.time, "monotonic", lambda: 12.5)

  with pigeond.install_pre_acquisition_initialization(lambda: None) as initialization:
    with pigeond.paused_gnss_acquisition(
      pigeon  # type: ignore[arg-type, ty:invalid-argument-type]
    ):
      initialization.run()

  assert initialization.gnss_start_sent_at == 12.5
  assert pigeon.sent[-1] == pigeond.CONTROLLED_GNSS_START_MESSAGE


def test_paused_gnss_acquisition_starts_after_body_failure(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  pigeon = FakePigeon()
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)

  with pytest.raises(RuntimeError, match="simulated setup failure"):
    with pigeond.paused_gnss_acquisition(pigeon):  # type: ignore[arg-type, ty:invalid-argument-type]
      raise RuntimeError("simulated setup failure")

  assert pigeon.sent == [
    pigeond.CONTROLLED_GNSS_STOP_MESSAGE,
    pigeond.CONTROLLED_GNSS_START_MESSAGE,
  ]


def test_paused_gnss_acquisition_starts_after_stop_logging_failure(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  pigeon = FakePigeon()
  monkeypatch.setattr(
    pigeond.cloudlog,
    "info",
    lambda _message: (_ for _ in ()).throw(RuntimeError("logging failed")),
  )

  with pytest.raises(RuntimeError, match="logging failed"):
    with pigeond.paused_gnss_acquisition(pigeon):  # type: ignore[arg-type, ty:invalid-argument-type]
      pass

  assert pigeon.sent == [
    pigeond.CONTROLLED_GNSS_STOP_MESSAGE,
    pigeond.CONTROLLED_GNSS_START_MESSAGE,
  ]


def test_paused_gnss_acquisition_starts_after_transition_sleep_failure(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  pigeon = FakePigeon()
  sleeps = 0

  def fail_first_sleep(_delay: float) -> None:
    nonlocal sleeps
    sleeps += 1
    if sleeps == 1:
      raise RuntimeError("transition sleep failed")

  monkeypatch.setattr(pigeond.time, "sleep", fail_first_sleep)

  with pytest.raises(RuntimeError, match="transition sleep failed"):
    with pigeond.paused_gnss_acquisition(pigeon):  # type: ignore[arg-type, ty:invalid-argument-type]
      pass

  assert pigeon.sent == [
    pigeond.CONTROLLED_GNSS_STOP_MESSAGE,
    pigeond.CONTROLLED_GNSS_START_MESSAGE,
  ]


def test_configuration_traffic_closes_database_window_before_write(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  pigeon = FakePigeon(events)
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    monotonic=lambda: 0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  database_indexes: list[int] = []

  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=network_time()),
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda _pigeon, _info: (
      events.append("configuration_traffic"),
      runtime.note_acquisition_started(),
    ),
  )
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)
  monkeypatch.setattr(
    pigeond,
    "send_mga_with_strict_ack",
    lambda _pigeon, _message, **kwargs: database_indexes.append(kwargs["database_frame_index"]) if kwargs.get("database_frame_index") is not None else None,
  )
  monkeypatch.setattr(pigeond, "send_time_assistance", lambda *_args, **_kwargs: False)
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    pigeond,
    "finish_pigeon_initialization",
    lambda _pigeon: events.append("normal_configuration"),
  )
  monkeypatch.setattr(
    pigeond,
    "log_assistnow_autonomous_support",
    lambda _info: True,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_assistnow_autonomous",
    lambda _pigeon, _info: None,
  )

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    navigation_database_runtime=runtime,
  )

  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_ACQUISITION_ALREADY_STARTED
  assert database_indexes == []
  assert events.index("gnss_stop") < events.index("configuration_traffic")
  assert events.index("configuration_traffic") < events.index("gnss_start")
  assert events.index("normal_configuration") < events.index("gnss_start")
  assert result.navigation_assistance_restore_attempted


# COMMIT6_PIGEOND_SAFETY_TESTS


def test_legacy_prestart_trusted_time_wait_helpers_are_removed() -> None:
  assert not hasattr(pigeond, "wait_for_current_independent_network_time")
  assert not hasattr(pigeond, "should_wait_for_navigation_database_trusted_time")
  assert not hasattr(pigeond, "NAVIGATION_DATABASE_TRUSTED_TIME_WAIT_SECONDS")
  assert not hasattr(pigeond, "NAVIGATION_DATABASE_TRUSTED_TIME_POLL_SECONDS")


def test_yuma_claim_happens_before_receiver_write() -> None:
  events: list[str] = []
  runtime = SimpleNamespace(claim_yuma_transmission=lambda: events.append("claim") or True)
  pigeond.send_yuma_with_durable_claim(
    runtime,  # type: ignore[arg-type, ty:invalid-argument-type]
    lambda _message: events.append("write"),
    b"yuma",
  )
  assert events == ["claim", "write"]


def test_failed_yuma_claim_performs_zero_receiver_writes() -> None:
  writes: list[bytes] = []
  runtime = SimpleNamespace(claim_yuma_transmission=lambda: False)
  with pytest.raises(pigeond.YumaAssistanceStateUnavailableError):
    pigeond.send_yuma_with_durable_claim(
      runtime,  # type: ignore[arg-type, ty:invalid-argument-type]
      writes.append,
      b"yuma",
    )
  assert writes == []


def test_post_power_stop_precedes_boot_wait(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  pigeon = FakePigeon(events)
  monkeypatch.setattr(pigeond.signal, "signal", lambda *_args: None)
  monkeypatch.setattr(
    pigeond,
    "set_power",
    lambda enabled: events.append("power_on" if enabled else "power_off"),
  )
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: events.append("sleep"))
  monkeypatch.setattr(pigeond, "init_baudrate", lambda _pigeon: events.append("init_baudrate"))
  monkeypatch.setattr(
    pigeond,
    "poll_mon_ver",
    lambda _pigeon, _timeout: SimpleNamespace(),
  )
  pigeond.start_pigeon_transport(pigeon)  # type: ignore[arg-type, ty:invalid-argument-type]
  assert events.index("power_on") < events.index("gnss_stop")
  assert events.index("gnss_stop") < events.index("sleep", events.index("power_on"))
  assert events.index("gnss_stop") < events.index("init_baudrate")


def test_delayed_network_time_is_not_awaited_before_start(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  pigeon = FakePigeon(events)

  def drain(_self, operation: str) -> None:
    events.append(operation)

  monkeypatch.setattr(
    type(pigeon),
    "drain_before_transaction",
    drain,
    raising=False,
  )
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    monotonic=lambda: 0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  original_claim_acquisition_start = runtime.claim_acquisition_start

  def claim_acquisition_start() -> bool:
    events.append("acquisition_start_claim")
    return original_claim_acquisition_start()

  monkeypatch.setattr(runtime, "claim_acquisition_start", claim_acquisition_start)
  database_indexes: list[int] = []
  monkeypatch.setattr(pigeond.time, "monotonic", lambda: 0.0)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=None),
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "configure_navx5_ack_aiding", lambda *_args: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)

  def send_mga(_pigeon, _message, **kwargs):
    if before_send := kwargs.get("before_send"):
      before_send()
    index = kwargs.get("database_frame_index")
    if index is None:
      events.append("position_write")
    else:
      database_indexes.append(index)
      events.append("dbd_write")

  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", send_mga)
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    lambda *_args, **_kwargs: events.append("time_write") or True,
  )
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    pigeond,
    "finish_pigeon_initialization",
    lambda _pigeon: events.append("normal_configuration"),
  )
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda _info: True)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *_args: None)

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    navigation_database_runtime=runtime,
    cycle_started_at=0.0,
    network_available=None,
  )

  restore = result.navigation_assistance_restore_result
  assert restore is not None
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME
  assert restore.database_frames_attempted_count == 0
  assert not restore.database_network_available
  assert database_indexes == []
  assert runtime.execution.position_assistance_attempted
  assert not runtime.execution.position_assistance_succeeded
  assert "trusted_time_arrived" not in events
  assert "dbd_write" not in events
  assert "time_write" not in events
  assert "position_write" not in events
  assert events.index("gnss_stop") < events.index("normal_configuration")
  assert events.index("normal_configuration") < events.index("acquisition_start_claim")
  assert events.index("acquisition_start_claim") < events.index("gnss_start")
  assert not result.trusted_time_assistance_sent
  assert result.navigation_assistance_restore_attempted


def test_initial_offline_state_is_not_rechecked_before_start(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  pigeon = FakePigeon(events)
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  database_indexes: list[int] = []

  def send_mga(_pigeon, _message, **kwargs):
    if before_send := kwargs.get("before_send"):
      before_send()
    index = kwargs.get("database_frame_index")
    if index is not None:
      database_indexes.append(index)

  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=None),
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "configure_navx5_ack_aiding", lambda *_args: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)
  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", send_mga)
  monkeypatch.setattr(pigeond, "send_time_assistance", lambda *_args, **_kwargs: False)
  monkeypatch.setattr(pigeond, "log_navigation_assistance_restore_result", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(pigeond, "finish_pigeon_initialization", lambda _pigeon: events.append("normal_configuration"))
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda _info: True)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *_args: None)

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    navigation_database_runtime=runtime,
    network_available=False,
  )

  restore = result.navigation_assistance_restore_result
  assert restore is not None
  assert not restore.database_network_available
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME
  assert database_indexes == []
  assert "trusted_time_arrived" not in events
  assert events.index("normal_configuration") < events.index("gnss_start")


def test_initialize_never_invokes_removed_trusted_time_wait(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  pigeon = FakePigeon()
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=None),
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "configure_navx5_ack_aiding", lambda *_args: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)
  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(pigeond, "send_time_assistance", lambda *_args, **_kwargs: False)
  monkeypatch.setattr(pigeond, "log_navigation_assistance_restore_result", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(pigeond, "finish_pigeon_initialization", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda _info: True)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *_args: None)

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    navigation_database_runtime=runtime,
    network_available=False,
  )

  restore = result.navigation_assistance_restore_result
  assert restore is not None
  assert not restore.database_network_available
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME
  assert restore.database_trusted_time_wait_error_type is None
  assert restore.database_trusted_time_wait_error is None


def test_bootstrap_acquisition_frames_use_exact_early_outcome(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  retry = pigeond.PositionAssistancePostStartRetryController(None)
  guard = pigeond.ReceiverAcquisitionStateGuard()

  class BootstrapPigeon(FakePigeon):
    def dispatch_pending_frames(self) -> None:
      events.append("dispatch_pending")
      pigeond.handle_receiver_acquisition_state(runtime, retry, guard)

  pigeon = BootstrapPigeon(events)
  database_indexes: list[int] = []
  position_writes: list[bytes] = []

  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=network_time()),
  )
  monkeypatch.setattr(pigeond, "resolve_pre_acquisition_mon_ver", lambda *_args: None)
  monkeypatch.setattr(pigeond, "configure_navx5_ack_aiding", lambda *_args: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)

  def send_mga(_pigeon, message, **kwargs):
    index = kwargs.get("database_frame_index")
    if index is None:
      position_writes.append(message)
    else:
      database_indexes.append(index)

  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", send_mga)
  monkeypatch.setattr(pigeond, "send_time_assistance", lambda *_args, **_kwargs: False)
  monkeypatch.setattr(pigeond, "log_navigation_assistance_restore_result", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(pigeond, "finish_pigeon_initialization", lambda _pigeon: events.append("normal_configuration"))
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda _info: True)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *_args: None)

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    navigation_database_runtime=runtime,
    position_assistance_retry=retry,
    transport_mon_ver_info=cast(pigeond.MonVerInfo, object()),
  )

  restore = result.navigation_assistance_restore_result
  assert restore is not None
  assert runtime.acquisition_started
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EARLY_ACQUISITION
  assert database_indexes == []
  assert len(position_writes) == 1
  assert events.index("gnss_stop") < events.index("normal_configuration")
  assert events.index("normal_configuration") < events.index("dispatch_pending")
  assert events.index("dispatch_pending") < events.index("gnss_start")


def configure_deferred_assistance_startup(
  monkeypatch: pytest.MonkeyPatch,
  events: list[str],
  clock: list[float],
) -> None:
  def finish_configuration(_pigeon) -> None:
    events.append("mandatory_configuration")
    clock[0] = 44.9

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "finish_pigeon_initialization",
    finish_configuration,
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "log_navx5_ack_aiding_support",
    lambda _info: None,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda *_args, **_kwargs: pigeond.Navx5AckAidingConfigurationResult.DEADLINE_EXHAUSTED,
  )
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=network_time()),
  )
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    pigeond,
    "send_mga_with_strict_ack",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda _info: True)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *_args: None)
  monkeypatch.setattr(
    pigeond,
    "finish_post_start_receiver_configuration",
    lambda _pigeon: None,
  )
  monkeypatch.setattr(
    pigeond,
    "run_post_start_legacy_assistance",
    lambda _pigeon: None,
  )


def initialize_deferred_assistance_cycle(
  pigeon: FakePigeon,
  factory,
):
  return pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    cycle_started_at=0.0,
    assistance_state_factory=factory,
  )


@pytest.mark.parametrize("blocked_phase", ("factory", "prepare"))
def test_deferred_assistance_worker_never_completes_does_not_block_start(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
  blocked_phase: str,
) -> None:
  events: list[str] = []
  clock = [0.0]
  pigeon = FakePigeon(events)
  configure_deferred_assistance_startup(monkeypatch, events, clock)
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    monotonic=lambda: clock[0],
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  retry = pigeond.PositionAssistancePostStartRetryController(None)
  guard = pigeond.ReceiverAcquisitionStateGuard()
  worker_entered = Event()
  release_worker = Event()
  worker_finished = Event()
  initialization_returned = Event()
  factory_calls = []
  outcome: dict[str, object] = {}
  original_prepare = runtime.prepare

  def prepare_runtime():
    try:
      if blocked_phase == "prepare":
        worker_entered.set()
        release_worker.wait()
      return original_prepare()
    finally:
      worker_finished.set()

  monkeypatch.setattr(runtime, "prepare", prepare_runtime)

  def create_assistance_state():
    factory_calls.append("factory")
    events.append("assistance_state_factory")
    if blocked_phase == "factory":
      worker_entered.set()
      release_worker.wait()
    return runtime, retry, guard

  def initialize_on_caller_thread() -> None:
    try:
      outcome["result"] = initialize_deferred_assistance_cycle(
        pigeon,
        create_assistance_state,
      )
    except BaseException as exc:
      outcome["error"] = exc
    finally:
      initialization_returned.set()

  caller = Thread(target=initialize_on_caller_thread, daemon=True)
  caller.start()
  try:
    assert worker_entered.wait(timeout=1.0)
    assert initialization_returned.wait(timeout=1.0)
    assert "error" not in outcome
    result = cast(
      pigeond.ReceiverCycleInitialization,
      outcome["result"],
    )
    assert result.gnss_start_sent_at is not None
    assert result.gnss_start_sent_at == pytest.approx(44.9)
    assert result.gnss_start_sent_at <= 45.0
    assert events.index("mandatory_configuration") < events.index("gnss_start")
    assert events.index("gnss_start") < events.index("assistance_state_factory")
    poll_deferred = result.poll_deferred_assistance_state
    assert poll_deferred is not None
    assert poll_deferred() is None
    assert poll_deferred() is None
    assert factory_calls == ["factory"]
  finally:
    release_worker.set()
    caller.join(timeout=1.0)
    assert worker_finished.wait(timeout=1.0)


def test_slow_deferred_assistance_is_adopted_once_after_receiver_start(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  clock = [0.0]
  pigeon = FakePigeon(events)
  configure_deferred_assistance_startup(monkeypatch, events, clock)
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    monotonic=lambda: clock[0],
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  retry = pigeond.PositionAssistancePostStartRetryController(None)
  guard = pigeond.ReceiverAcquisitionStateGuard()
  prepare_entered = Event()
  release_prepare = Event()
  prepare_finished = Event()
  activations = []
  original_prepare = runtime.prepare

  def prepare_runtime():
    prepare_entered.set()
    release_prepare.wait()
    try:
      return original_prepare()
    finally:
      prepare_finished.set()

  monkeypatch.setattr(runtime, "prepare", prepare_runtime)

  def activate(runtime_value, retry_value, guard_value) -> None:
    activations.append((runtime_value, retry_value, guard_value))

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    cycle_started_at=0.0,
    assistance_state_factory=lambda: (runtime, retry, guard),
    assistance_state_ready_callback=activate,
  )

  assert prepare_entered.wait(timeout=1.0)
  assert result.gnss_start_sent_at == pytest.approx(44.9)
  poll_deferred = result.poll_deferred_assistance_state
  assert poll_deferred is not None
  assert poll_deferred() is None
  assert activations == []
  release_prepare.set()
  assert prepare_finished.wait(timeout=1.0)
  restore_result = poll_deferred()
  assert restore_result is not None
  assert poll_deferred() is None
  assert activations == [(runtime, retry, guard)]
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EARLY_ACQUISITION
  assert not runtime.database_restore_pending
  assert retry.gnss_start_sent_at == pytest.approx(44.9)


def test_deferred_assistance_worker_exception_is_fail_open_and_observable(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  clock = [0.0]
  pigeon = FakePigeon(events)
  configure_deferred_assistance_startup(monkeypatch, events, clock)
  factory_finished = Event()
  factory_calls = []
  activations = []

  def fail_factory():
    factory_calls.append("factory")
    factory_finished.set()
    raise OSError("simulated deferred factory failure")

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    cycle_started_at=0.0,
    assistance_state_factory=fail_factory,
    assistance_state_ready_callback=lambda *values: activations.append(values),
  )

  assert result.gnss_start_sent_at == pytest.approx(44.9)
  assert factory_finished.wait(timeout=1.0)
  poll_deferred = result.poll_deferred_assistance_state
  assert poll_deferred is not None
  restore_result = poll_deferred()
  assert restore_result is not None
  assert restore_result.status is pigeond.NavigationAssistanceRestoreStatus.FAILED
  assert restore_result.database_restore_state_error is not None
  assert "simulated deferred factory failure" in restore_result.database_restore_state_error
  assert poll_deferred() is None
  assert factory_calls == ["factory"]
  assert len(activations) == 1


def test_drive2_expired_cache_no_time_cannot_starve_configuration(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  clock = [3.0]
  pigeon = FakePigeon(events)

  def drain(_self, operation: str) -> None:
    events.append(operation)

  monkeypatch.setattr(
    type(pigeon),
    "drain_before_transaction",
    drain,
    raising=False,
  )
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(
      age=timedelta(hours=1, minutes=53),
    ),
    retry_delay_seconds=0.0,
    monotonic=lambda: clock[0],
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  worker_complete = Event()
  original_prepare = runtime.prepare

  def prepare_runtime():
    try:
      return original_prepare()
    finally:
      worker_complete.set()

  monkeypatch.setattr(runtime, "prepare", prepare_runtime)
  retry = pigeond.PositionAssistancePostStartRetryController(None)
  guard = pigeond.ReceiverAcquisitionStateGuard()

  def create_assistance_state():
    events.append("assistance_state_factory")
    return runtime, retry, guard

  def activate_assistance_state(_runtime, _retry, _guard) -> None:
    assert _runtime is runtime
    assert _retry is retry
    assert _guard is guard
    events.append("assistance_state_ready")

  database_indexes: list[int] = []
  timeline_calls: list[dict[str, object]] = []
  pre_start_ack_timeouts: list[float] = []
  configuration_items: list[tuple[str, bool]] = []

  def monotonic() -> float:
    return clock[0]

  def send_mga(_pigeon, _message, **kwargs):
    if before_send := kwargs.get("before_send"):
      before_send()
    index = kwargs.get("database_frame_index")
    if index is None:
      events.append("position_write")
      pre_start_ack_timeouts.append(kwargs["timeout"])
    else:
      database_indexes.append(index)
      events.append("dbd_write")
      pre_start_ack_timeouts.append(kwargs["timeout"])
      clock[0] = 44.9

  def send_time(*_args, **kwargs):
    events.append("time_write")
    pre_start_ack_timeouts.append(kwargs["ack_timeout"])
    clock[0] = 45.0
    return True

  def run_configuration_item(**kwargs):
    configuration_items.append(
      (
        kwargs["item_name"],
        kwargs["mandatory"],
      )
    )
    events.append(f"configuration:{kwargs['item_name']}")
    return pigeond.ReceiverConfigurationItemResult(
      item_name=kwargs["item_name"],
      mandatory=kwargs["mandatory"],
      attempted=False,
      write_attempt_count=0,
      ack_status=pigeond.ReceiverConfigurationAckStatus.NOT_REQUIRED,
      poll_attempt_count=1,
      readback_status=pigeond.ReceiverConfigurationReadbackStatus.VERIFIED,
      verified=True,
      expected_value=kwargs["expected_value"],
      observed_value="verified",
      failure_kind=None,
      failure_phase=None,
      error_type=None,
      error=None,
    )

  monkeypatch.setattr(pigeond.time, "monotonic", monotonic)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "log_gps_startup_timeline", lambda **kwargs: timeline_calls.append(kwargs))
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: events.append("trusted_time_check") or None,
  )
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=None),
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "configure_navx5_ack_aiding", lambda *_args: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)
  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", send_mga)
  monkeypatch.setattr(pigeond, "send_time_assistance", send_time)
  monkeypatch.setattr(
    pigeond,
    "run_receiver_configuration_item",
    run_configuration_item,
  )
  monkeypatch.setattr(
    pigeond,
    "persist_receiver_configuration_summary",
    lambda _summary: True,
  )
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda _info: True)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *_args: None)

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    cycle_started_at=0.0,
    network_available=False,
    assistance_state_factory=create_assistance_state,
    assistance_state_ready_callback=activate_assistance_state,
  )

  assert worker_complete.wait(timeout=1.0)
  poll_deferred = result.poll_deferred_assistance_state
  assert poll_deferred is not None
  restore = poll_deferred()
  assert restore is not None
  assert clock[0] == pytest.approx(3.0)
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME
  assert restore.database_frames_attempted_count == 0
  assert not restore.database_trusted_time_wait_allowed
  assert not restore.database_network_available
  assert restore.database_trusted_time_wait_started_at is None
  assert restore.database_trusted_time_wait_completed_at is None
  assert restore.database_trusted_time_wait_deadline is None
  assert restore.database_trusted_time_wait_elapsed_seconds is None
  assert restore.database_trusted_time_wait_error_type is None
  assert database_indexes == []
  assert "position_write" not in events
  assert runtime.execution.position_assistance_attempted
  assert not runtime.execution.position_assistance_succeeded
  mandatory_items = tuple(item_name for item_name, mandatory in configuration_items if mandatory)
  assert mandatory_items == tuple(item_name for item_name, mandatory in pigeond.RECEIVER_CONFIGURATION_ITEM_INVENTORY if mandatory)
  assert {
    "CFG-MSG-NAV-PVT",
    "CFG-MSG-RXM-RAWX",
    "CFG-MSG-RXM-SFRBX",
  }.issubset(mandatory_items)
  assert runtime.acquisition_started
  assert "network_state_arrived" not in events
  assert "trusted_time_arrived" not in events
  assert "dbd_write" not in events
  assert events.index("configuration:CFG-PRT-3") < events.index("trusted_time_check")
  assert events.index("configuration:CFG-MSG-RXM-SFRBX") < events.index("trusted_time_check")
  assert events.index("trusted_time_check") < events.index("gnss_start")
  assert events.index("gnss_start") < events.index("assistance_state_factory")
  assert events.index("assistance_state_factory") < events.index("assistance_state_ready")
  optional_events = [
    event
    for event in events
    if event.startswith("configuration:")
    and event.split(":", 1)[1] in {item_name for item_name, mandatory in pigeond.RECEIVER_CONFIGURATION_ITEM_INVENTORY if not mandatory}
  ]
  assert optional_events
  assert all(events.index(event) > events.index("gnss_start") for event in optional_events)
  assert len(timeline_calls) == 1
  timeline = timeline_calls[0]
  assert timeline["restore_result"] is None
  assert timeline["authorized_time"] is None
  assert timeline["independent_network_time_seen_at"] is None
  assert timeline["trusted_time_wait_started_at"] is None
  assert timeline["trusted_time_wait_completed_at"] is None
  assert timeline["acquisition_start_claimed_at"] is None
  assert timeline["gnss_start_sent_at"] == pytest.approx(3.0)


def test_deadline_boundary_skips_assistance_after_mandatory_configuration(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  clock = [0.0]
  pigeon = FakePigeon(events)
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    monotonic=lambda: clock[0],
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  original_prepare = runtime.prepare
  worker_complete = Event()

  def prepare_runtime():
    try:
      events.append("assistance_state_prepare")
      return original_prepare()
    finally:
      worker_complete.set()

  monkeypatch.setattr(runtime, "prepare", prepare_runtime)
  retry = pigeond.PositionAssistancePostStartRetryController(None)
  guard = pigeond.ReceiverAcquisitionStateGuard()

  def create_assistance_state():
    events.append("assistance_state_factory")
    return runtime, retry, guard

  def finish_configuration(_pigeon) -> None:
    events.append("mandatory_configuration")
    clock[0] = 45.0

  position_writes: list[bytes] = []

  def send_mga(_pigeon, message, **kwargs):
    if kwargs.get("database_frame_index") is not None:
      raise AssertionError("DBD wrote after pre-START deadline")
    position_writes.append(message)

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "finish_pigeon_initialization", finish_configuration)
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda *_args, **_kwargs: pigeond.Navx5AckAidingConfigurationResult.DEADLINE_EXHAUSTED,
  )
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: (_ for _ in ()).throw(AssertionError("trusted time checked after deadline")),
  )
  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", send_mga)
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda _info: True)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *_args: None)

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    cycle_started_at=0.0,
    assistance_state_factory=create_assistance_state,
  )

  assert result.gnss_start_sent_at == pytest.approx(45.0)
  assert worker_complete.wait(timeout=1.0)
  poll_deferred = result.poll_deferred_assistance_state
  assert poll_deferred is not None
  restore = poll_deferred()
  assert restore is not None
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME
  assert runtime.acquisition_started
  assert len(position_writes) == 0
  assert runtime.execution.position_assistance_attempted
  assert not runtime.execution.position_assistance_succeeded
  assert events.index("mandatory_configuration") < events.index("gnss_start")
  assert events.index("gnss_start") < events.index("assistance_state_factory")
  assert events.index("assistance_state_factory") < events.index("assistance_state_prepare")


def test_tiny_factory_budget_defers_real_factory_until_after_start(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  clock = [0.0]
  pigeon = FakePigeon(events)
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    monotonic=lambda: clock[0],
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  original_prepare = runtime.prepare
  worker_complete = Event()

  def prepare_runtime():
    try:
      events.append("assistance_state_prepare")
      return original_prepare()
    finally:
      worker_complete.set()

  monkeypatch.setattr(runtime, "prepare", prepare_runtime)
  retry = pigeond.PositionAssistancePostStartRetryController(None)
  guard = pigeond.ReceiverAcquisitionStateGuard()

  def create_assistance_state():
    events.append("assistance_state_factory")
    clock[0] += 0.3
    return runtime, retry, guard

  def finish_configuration(_pigeon) -> None:
    events.append("mandatory_configuration")
    clock[0] = 44.9

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "finish_pigeon_initialization", finish_configuration)
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda *_args, **_kwargs: pigeond.Navx5AckAidingConfigurationResult.DEADLINE_EXHAUSTED,
  )
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=network_time()),
  )
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    pigeond,
    "send_mga_with_strict_ack",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda _info: True)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *_args: None)
  monkeypatch.setattr(pigeond, "finish_post_start_receiver_configuration", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "run_post_start_legacy_assistance", lambda _pigeon: None)

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    cycle_started_at=0.0,
    assistance_state_factory=create_assistance_state,
  )

  gnss_start_sent_at = result.gnss_start_sent_at
  assert gnss_start_sent_at is not None
  assert gnss_start_sent_at == pytest.approx(44.9)
  assert gnss_start_sent_at <= 45.0
  assert worker_complete.wait(timeout=1.0)
  poll_deferred = result.poll_deferred_assistance_state
  assert poll_deferred is not None
  assert poll_deferred() is not None
  assert events.index("mandatory_configuration") < events.index("gnss_start")
  assert events.index("gnss_start") < events.index("assistance_state_factory")
  assert events.index("assistance_state_factory") < events.index("assistance_state_prepare")
  assert clock[0] == pytest.approx(45.2)
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EARLY_ACQUISITION
  assert runtime.execution.position_assistance_attempted


@pytest.mark.parametrize("failure_phase", ("factory", "prepare"))
def test_factory_or_prepare_exception_is_fail_open_before_deadline(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
  failure_phase: str,
) -> None:
  events: list[str] = []
  clock = [5.0]
  pigeon = FakePigeon(events)
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    monotonic=lambda: clock[0],
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  retry = pigeond.PositionAssistancePostStartRetryController(None)
  guard = pigeond.ReceiverAcquisitionStateGuard()
  worker_complete = Event()

  if failure_phase == "prepare":

    def fail_prepare():
      try:
        events.append("assistance_state_prepare")
        raise OSError("simulated cache preparation failure")
      finally:
        worker_complete.set()

    monkeypatch.setattr(runtime, "prepare", fail_prepare)

  def create_assistance_state():
    events.append("assistance_state_factory")
    if failure_phase == "factory":
      worker_complete.set()
      raise OSError("simulated assistance factory failure")
    return runtime, retry, guard

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "finish_pigeon_initialization",
    lambda _pigeon: events.append("mandatory_configuration"),
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda *_args, **_kwargs: pigeond.Navx5AckAidingConfigurationResult.ALREADY_ENABLED,
  )
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=network_time()),
  )
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    pigeond,
    "send_mga_with_strict_ack",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(pigeond, "finish_post_start_receiver_configuration", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "run_post_start_legacy_assistance", lambda _pigeon: None)

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    cycle_started_at=0.0,
    assistance_state_factory=create_assistance_state,
  )

  assert result.gnss_start_sent_at == pytest.approx(5.0)
  assert worker_complete.wait(timeout=1.0)
  poll_deferred = result.poll_deferred_assistance_state
  if poll_deferred is not None:
    assert poll_deferred() is not None
  assert events.index("mandatory_configuration") < events.index("assistance_state_factory")
  assert events.index("assistance_state_factory") < events.index("gnss_start")
  if failure_phase == "prepare":
    assert events.index("assistance_state_factory") < events.index("assistance_state_prepare")


@pytest.mark.parametrize(
  ("cache_age", "expected_disposition", "expected_database_writes"),
  (
    (
      timedelta(minutes=10),
      NavigationDatabaseRestoreDisposition.RESTORED,
      1,
    ),
    (
      timedelta(hours=1, minutes=1),
      NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED,
      0,
    ),
  ),
)
def test_trusted_time_available_evaluates_dbd_after_mandatory_configuration(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
  cache_age: timedelta,
  expected_disposition: NavigationDatabaseRestoreDisposition,
  expected_database_writes: int,
) -> None:
  events: list[str] = []
  clock = [4.0]
  pigeon = FakePigeon(events)
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(age=cache_age),
    retry_delay_seconds=0.0,
    monotonic=lambda: clock[0],
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  retry = pigeond.PositionAssistancePostStartRetryController(None)
  guard = pigeond.ReceiverAcquisitionStateGuard()

  def create_assistance_state():
    events.append("assistance_state_factory")
    return runtime, retry, guard

  def activate_assistance_state(_runtime, _retry, _guard) -> None:
    assert _runtime is runtime
    assert _retry is retry
    assert _guard is guard
    events.append("assistance_state_ready")

  def drain(_self, operation: str) -> None:
    events.append(operation)

  def send_mga(_pigeon, _message, **kwargs):
    if before_send := kwargs.get("before_send"):
      before_send()
    if kwargs.get("database_frame_index") is None:
      events.append("position_write")
    else:
      events.append("dbd_write")

  monkeypatch.setattr(type(pigeon), "drain_before_transaction", drain, raising=False)
  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock[0])
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "finish_pigeon_initialization",
    lambda _pigeon: events.append("mandatory_configuration"),
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda *_args, **_kwargs: events.append("navx5") or pigeond.Navx5AckAidingConfigurationResult.ALREADY_ENABLED,
  )
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: events.append("trusted_time_check") or None,
  )
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=network_time()),
  )
  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", send_mga)
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    lambda *_args, **_kwargs: events.append("time_write") or True,
  )
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda _info: True)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *_args: None)

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    cycle_started_at=0.0,
    network_available=True,
    assistance_state_factory=create_assistance_state,
    assistance_state_ready_callback=activate_assistance_state,
  )

  restore = result.navigation_assistance_restore_result
  assert restore is not None
  assert runtime.controller.disposition is expected_disposition
  assert restore.database_frames_attempted_count == expected_database_writes
  assert not restore.database_trusted_time_wait_allowed
  gnss_start_sent_at = result.gnss_start_sent_at
  assert gnss_start_sent_at is not None
  assert gnss_start_sent_at == pytest.approx(4.0)
  assert gnss_start_sent_at <= 45.0
  assert events.index("mandatory_configuration") < events.index("navx5")
  assert events.index("navx5") < events.index("trusted_time_check")
  assert events.index("trusted_time_check") < events.index("assistance_state_factory")
  assert events.index("assistance_state_factory") < events.index("assistance_state_ready")
  assert events.count("dbd_write") == expected_database_writes
  if expected_database_writes:
    assert "position_write" in events
    assert events.index("trusted_time_check") < events.index("position_write")
    assert events.index("trusted_time_check") < events.index("navigation_database_post_time_wait")
    assert events.index("navigation_database_post_time_wait") < events.index("dbd_write")
    assert events.index("dbd_write") < events.index("position_write")
    assert events.index("position_write") < events.index("time_write")
  else:
    assert "position_write" not in events
    assert runtime.execution.position_assistance_attempted
    assert not runtime.execution.position_assistance_succeeded
  assert events.index("time_write") < events.index("gnss_start")


def test_pre_restore_drain_failure_is_terminal_and_gnss_starts(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  pigeon = FakePigeon(events)
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  database_writes: list[int] = []

  def drain(_self, operation: str) -> None:
    events.append(operation)
    if operation == "navigation_database_post_time_wait":
      raise OSError("drain failed")

  monkeypatch.setattr(type(pigeon), "drain_before_transaction", drain, raising=False)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "start_pigeon_transport", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=network_time()),
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(pigeond, "configure_navx5_ack_aiding", lambda *_args: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda _info: None)

  def send_mga(_pigeon, _message, **kwargs):
    index = kwargs.get("database_frame_index")
    if index is not None:
      database_writes.append(index)

  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", send_mga)
  monkeypatch.setattr(pigeond, "send_time_assistance", lambda *_args, **_kwargs: False)
  monkeypatch.setattr(pigeond, "log_navigation_assistance_restore_result", lambda *_args, **_kwargs: None)
  monkeypatch.setattr(pigeond, "finish_pigeon_initialization", lambda _pigeon: events.append("normal_configuration"))
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda _info: True)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *_args: None)

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    navigation_database_runtime=runtime,
    network_available=True,
  )

  restore = result.navigation_assistance_restore_result
  assert restore is not None
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR
  assert restore.database_restore_runtime_phase == "pre_restore_drain"
  assert restore.database_restore_execution_error == "OSError:drain failed"
  assert database_writes == []
  assert events.index("navigation_database_post_time_wait") < events.index("gnss_start")
  assert events.index("normal_configuration") < events.index("gnss_start")


def test_pending_dbd_blocks_yuma_until_terminal_then_survives_restart(
  tmp_path: Path,
) -> None:
  first = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  first.prepare()
  yuma_writes: list[bytes] = []
  with pytest.raises(pigeond.YumaAssistanceStateUnavailableError):
    pigeond.send_yuma_with_durable_claim(
      first,
      yuma_writes.append,
      b"provisional-yuma",
    )
  assert yuma_writes == []

  assert first.close_restore_window_wait_timeout()
  pigeond.send_yuma_with_durable_claim(
    first,
    yuma_writes.append,
    b"provisional-yuma",
  )
  assert yuma_writes == [b"provisional-yuma"]

  second = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  database_writes: list[tuple[bytes, int]] = []
  result = second.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=(lambda frame, index, _mark: database_writes.append((frame, index))),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_WAIT_TIMEOUT
  assert database_writes == []


def test_trusted_time_wait_error_is_durable_and_not_a_timeout(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  first = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  assert first.close_restore_window_wait_error()

  second = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  result = second.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=lambda _frame, _index, _mark: None,
  )

  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_WAIT_ERROR


def test_new_receiver_cycle_reopens_navigation_assistance_state(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  first = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  assert not first.claim_yuma_transmission()
  assert first.close_restore_window_wait_timeout()
  assert first.claim_yuma_transmission()
  assert first.claim_acquisition_start()

  same_cycle = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  assert same_cycle.yuma_sent
  assert same_cycle.acquisition_started

  next_cycle = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    new_receiver_cycle=True,
  )
  assert not next_cycle.yuma_sent
  assert not next_cycle.acquisition_started
  assert next_cycle.controller.pending
  assert not next_cycle.claim_yuma_transmission()
  assert next_cycle.close_restore_window_wait_timeout()
  assert next_cycle.claim_yuma_transmission()
  assert next_cycle.claim_acquisition_start()


def test_new_receiver_cycle_navigation_read_error_does_not_overwrite(
  tmp_path: Path,
) -> None:
  stores: list[tuple[object, Path]] = []

  def fail_load(_path: Path) -> object:
    raise OSError("read unavailable")

  def record_store(state: object, path: Path) -> None:
    stores.append((state, path))

  with pytest.raises(
    NavigationDatabaseRestoreInitializationError,
    match="state_load_failed:OSError:read unavailable",
  ):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      retry_delay_seconds=0.0,
      state_path=tmp_path / "dbd_state.json",
      boot_id_reader=lambda: BOOT_ID,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
      state_loader=fail_load,  # type: ignore[arg-type, ty:invalid-argument-type]
      state_storer=record_store,
      new_receiver_cycle=True,
    )

  assert stores == []


def test_new_receiver_cycle_reopens_position_retry_state(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "position_retry_state.json"
  execution = NavigationDatabaseRestoreExecution(
    disposition=NavigationDatabaseRestoreDisposition.SKIPPED_NO_USABLE_CACHE,
    total_frame_count=0,
    accepted_frame_count=0,
    database_write_attempt_count=0,
    position_assistance_attempted=True,
    position_assistance_write_status=PositionAssistanceWriteStatus.SUCCEEDED,
    position_assistance_ack_status=PositionAssistanceAckStatus.REJECTED,
    position_assistance_ack_info_code=5,
  )
  first = pigeond.PositionAssistanceRetryRuntime(
    TEST_RECEIVER_FINGERPRINT,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  assert first.arm_from_initial(execution, b"position-message")
  first.cancel(
    pigeond.PositionAssistanceRetryResult.CANCELLED_RECEIVER_CYCLE_CHANGED,
    TEST_BOOTTIME_SECONDS,
  )
  assert first.state.retry_completed

  same_cycle = pigeond.PositionAssistanceRetryRuntime(
    TEST_RECEIVER_FINGERPRINT,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  assert same_cycle.state.retry_completed

  next_cycle = pigeond.PositionAssistanceRetryRuntime(
    TEST_RECEIVER_FINGERPRINT,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    new_receiver_cycle=True,
  )
  assert not next_cycle.state.initial_attempted
  assert not next_cycle.state.retry_armed
  assert not next_cycle.state.retry_claimed
  assert not next_cycle.state.retry_completed
  assert next_cycle.arm_from_initial(
    execution,
    b"position-message",
  )


def test_new_receiver_cycle_retry_read_error_does_not_overwrite(
  tmp_path: Path,
) -> None:
  stores: list[tuple[PositionAssistanceRetryState, Path]] = []

  def fail_load(
    _path: Path,
  ) -> PositionAssistanceRetryState | None:
    raise OSError("read unavailable")

  def record_store(
    state: PositionAssistanceRetryState,
    path: Path,
  ) -> None:
    stores.append((state, path))

  with pytest.raises(
    PositionAssistanceRetryStateError,
    match="OSError:read unavailable",
  ):
    pigeond.PositionAssistanceRetryRuntime(
      TEST_RECEIVER_FINGERPRINT,
      state_path=tmp_path / "position_retry_state.json",
      boot_id_reader=lambda: BOOT_ID,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
      state_loader=fail_load,
      state_storer=record_store,
      new_receiver_cycle=True,
    )

  assert stores == []


def test_receiver_cycle_navigation_factory_requests_fresh_state(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  navigation_calls: list[bool] = []

  class NavigationRuntime:
    def __init__(
      self,
      _receiver_fingerprint: str,
      *,
      new_receiver_cycle: bool,
    ) -> None:
      navigation_calls.append(new_receiver_cycle)

  monkeypatch.setattr(
    pigeond,
    "NavigationDatabaseRestoreRuntime",
    NavigationRuntime,
  )

  first = pigeond.create_receiver_cycle_navigation_state("receiver")
  second = pigeond.create_receiver_cycle_navigation_state("receiver")

  assert navigation_calls == [True, True]
  assert first is not second


def test_prepared_receiver_response_state_is_not_reset_twice(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  resets = 0

  class Pigeon:
    _stream_parser = object()

    def reset_response_state(self) -> None:
      nonlocal resets
      resets += 1

    def send(self, _message: bytes) -> None:
      pass

  monkeypatch.setattr(
    pigeond.signal,
    "signal",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    pigeond,
    "set_power",
    lambda _enabled: None,
  )
  monkeypatch.setattr(
    pigeond.time,
    "sleep",
    lambda _delay: None,
  )
  monkeypatch.setattr(
    pigeond,
    "init_baudrate",
    lambda _pigeon: None,
  )
  monkeypatch.setattr(
    pigeond,
    "poll_mon_ver",
    lambda _pigeon, _timeout: SimpleNamespace(),
  )

  pigeon = Pigeon()
  pigeond.prepare_receiver_cycle_response_state(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
  )
  pigeond.start_pigeon_transport(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
  )

  assert resets == 1
  assert not pigeon._receiver_cycle_response_state_prepared


# COMMIT7_DBD_LIVE_BOUNDARY_TESTS


def test_pr68_bootstrap_acquisition_skips_wait_drain_and_dbd(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  class DrainPigeon(FakePigeon):
    def drain_before_transaction(self, operation: str) -> None:
      events.append(operation)
      if operation == "navigation_database_post_time_wait":
        raise AssertionError("early acquisition must not execute the obsolete DBD drain")

  pigeon = DrainPigeon(events)
  database_indexes: list[int] = []
  assert runtime.note_acquisition_started()

  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(
    pigeond,
    "start_pigeon_transport",
    lambda _pigeon: None,
  )
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: None,
  )
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=None),
  )
  monkeypatch.setattr(
    pigeond,
    "poll_mon_ver",
    lambda _pigeon: None,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda *_args: None,
  )
  monkeypatch.setattr(
    pigeond,
    "log_navx5_ack_aiding_support",
    lambda _info: None,
  )
  monkeypatch.setattr(
    pigeond,
    "send_mga_with_strict_ack",
    lambda _pigeon, _message, **kwargs: database_indexes.append(kwargs["database_frame_index"]) if kwargs.get("database_frame_index") is not None else None,
  )
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    lambda *_args, **_kwargs: False,
  )
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    pigeond,
    "finish_pigeon_initialization",
    lambda _pigeon: None,
  )
  monkeypatch.setattr(
    pigeond,
    "log_assistnow_autonomous_support",
    lambda _info: True,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_assistnow_autonomous",
    lambda *_args: None,
  )

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # ty: ignore[invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # ty: ignore[invalid-argument-type]
    "test",
    time_authority=object(),  # ty: ignore[invalid-argument-type]
    time_provenance=FakeProvenance(),  # ty: ignore[invalid-argument-type]
    navigation_database_runtime=runtime,
    network_available=True,
  )

  assert "navigation_database_post_time_wait" not in events
  assert database_indexes == []
  assert runtime.controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_ACQUISITION_ALREADY_STARTED
  assert events.index("gnss_stop") < events.index("gnss_start")
  assert result.navigation_assistance_restore_attempted


def test_database_ack_timeout_is_clamped_to_remaining_transfer_budget(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    transfer_budget_seconds=0.5,
    monotonic=lambda: 0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  database_timeouts: list[float] = []

  def send_with_ack(
    _pigeon,
    _message: bytes,
    timeout: float = pigeond.GPS_ASSISTANCE_ACK_TIMEOUT,
    database_frame_index: int | None = None,
    **kwargs,
  ) -> None:
    if before_send := kwargs.get("before_send"):
      before_send()
    if database_frame_index is not None:
      database_timeouts.append(timeout)

  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", send_with_ack)
  result = pigeond.restore_navigation_assistance(
    object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    navigation_database_runtime=runtime,
    authorized_time=network_time(),
  )

  assert result.status is pigeond.NavigationAssistanceRestoreStatus.COMPLETE
  assert database_timeouts
  assert database_timeouts == [0.5] * len(database_timeouts)


def test_frame_zero_transaction_drain_guard_blocks_receiver_write(tmp_path: Path) -> None:
  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  receiver_writes: list[bytes] = []

  class DrainPigeon:
    def begin_response_transaction(self, message: bytes, _operation: str, before_send):
      assert runtime.note_acquisition_started()
      before_send()
      receiver_writes.append(message)
      raise AssertionError("DBD write guard returned after acquisition")

  pigeon = DrainPigeon()

  def send_database_frame(
    message: bytes,
    frame_index: int,
    mark_write_attempt,
  ) -> None:
    def before_send() -> None:
      runtime.validate_database_write_boundary(frame_index)
      mark_write_attempt()

    pigeond.send_mga_with_strict_ack(
      pigeon,  # ty: ignore[invalid-argument-type]
      message,
      database_frame_index=frame_index,
      before_send=before_send,
    )

  result = runtime.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=send_database_frame,
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.WRITE_FAILED
  assert receiver_writes == []
  assert result.permanent_failures


def test_assistance_state_initialization_failure_still_starts_gnss(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  pigeon = FakePigeon(events)
  assistance_writes: list[bytes] = []
  retry_controller = pigeond.PositionAssistancePostStartRetryController(cast(pigeond.PositionAssistanceRetryRuntime, SimpleNamespace()))

  def unavailable_runtime(
    _receiver_fingerprint: str,
  ) -> NavigationDatabaseRestoreRuntime:
    raise NavigationDatabaseRestoreInitializationError("boot_state:storage_unavailable")

  monkeypatch.setattr(
    pigeond,
    "NavigationDatabaseRestoreRuntime",
    unavailable_runtime,
  )
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(
    pigeond,
    "start_pigeon_transport",
    lambda _pigeon: None,
  )
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: None,
  )
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=network_time()),
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda *_args: None,
  )
  monkeypatch.setattr(
    pigeond,
    "log_navx5_ack_aiding_support",
    lambda _info: None,
  )
  monkeypatch.setattr(
    pigeond,
    "send_mga_with_strict_ack",
    lambda _pigeon, message, **_kwargs: assistance_writes.append(message),
  )
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    lambda *_args, **_kwargs: events.append("time_assistance") or True,
  )
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    pigeond,
    "finish_pigeon_initialization",
    lambda _pigeon: events.append("normal_configuration"),
  )
  monkeypatch.setattr(
    pigeond,
    "log_assistnow_autonomous_support",
    lambda _info: True,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_assistnow_autonomous",
    lambda *_args: None,
  )

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    position_assistance_retry=retry_controller,
    network_available=True,
  )

  assert assistance_writes == []
  assert retry_controller.runtime is None
  assert events.index("gnss_stop") < events.index("time_assistance")
  assert events.index("time_assistance") < events.index("gnss_start")
  assert events.index("normal_configuration") < events.index("gnss_start")
  restore = result.navigation_assistance_restore_result
  assert restore is not None
  assert restore.database_restore_state_error == ("boot_state:storage_unavailable")


def test_restore_state_persistence_failure_does_not_block_gnss_start(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  pigeon = FakePigeon(events)
  store_calls = 0
  assistance_writes: list[bytes] = []

  def fail_after_baseline(state, path: Path) -> None:
    nonlocal store_calls
    store_calls += 1
    if store_calls > 1:
      raise OSError("storage unavailable")
    restore_runtime.store_navigation_database_restore_boot_state(
      state,
      path,
    )

  runtime = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    state_storer=fail_after_baseline,
  )

  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(
    pigeond,
    "start_pigeon_transport",
    lambda _pigeon: None,
  )
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: None,
  )
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda _authority, _observation: SimpleNamespace(authorized_time=None),
  )
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda _pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda *_args: None,
  )
  monkeypatch.setattr(
    pigeond,
    "log_navx5_ack_aiding_support",
    lambda _info: None,
  )
  monkeypatch.setattr(
    pigeond,
    "send_mga_with_strict_ack",
    lambda _pigeon, message, **_kwargs: assistance_writes.append(message),
  )
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    lambda *_args, **_kwargs: False,
  )
  monkeypatch.setattr(
    pigeond,
    "log_navigation_assistance_restore_result",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    pigeond,
    "finish_pigeon_initialization",
    lambda _pigeon: events.append("normal_configuration"),
  )
  monkeypatch.setattr(
    pigeond,
    "log_assistnow_autonomous_support",
    lambda _info: True,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_assistnow_autonomous",
    lambda *_args: None,
  )

  result = pigeond.initialize_receiver_cycle(
    pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
    TEST_RECEIVER_FINGERPRINT,
    FakeDiagnostics(),  # type: ignore[arg-type, ty:invalid-argument-type]
    "test",
    time_authority=object(),  # type: ignore[arg-type, ty:invalid-argument-type]
    time_provenance=FakeProvenance(),  # type: ignore[arg-type, ty:invalid-argument-type]
    navigation_database_runtime=runtime,
  )

  assert not runtime.state_available
  assert assistance_writes == []
  assert events.index("gnss_stop") < events.index("gnss_start")
  assert events.index("normal_configuration") < events.index("gnss_start")
  restore = result.navigation_assistance_restore_result
  assert restore is not None
  assert restore.database_restore_state_error is not None


def test_acquisition_state_failure_is_handled_once(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  persistence_attempts = 0
  errors: list[str] = []

  def fail_once() -> bool:
    nonlocal persistence_attempts
    persistence_attempts += 1
    return False

  runtime = SimpleNamespace(
    acquisition_started=False,
    database_restore_pending=False,
    note_early_acquisition_started=fail_once,
    note_acquisition_started=fail_once,
  )
  retry = SimpleNamespace(runtime=object())
  guard = pigeond.ReceiverAcquisitionStateGuard()
  monkeypatch.setattr(pigeond.cloudlog, "error", errors.append)

  for _ in range(25):
    pigeond.handle_receiver_acquisition_state(
      runtime,  # type: ignore[arg-type, ty:invalid-argument-type]
      retry,  # type: ignore[arg-type, ty:invalid-argument-type]
      guard,
    )

  assert persistence_attempts == 1
  assert len(errors) == 1
  assert retry.runtime is None


def test_yuma_assistance_state_suppression_uses_explicit_marker() -> None:
  normal_outcome = SimpleNamespace(
    transmit_result=SimpleNamespace(
      assistance_state_unavailable=True,
      status="partial",
      attempted_satellite_ids=(1,),
      accepted_satellite_ids=(1,),
      unavailable_satellite_ids=(2, 3),
    ),
    terminal=True,
    retry_pending=False,
  )
  provisional_outcome = SimpleNamespace(
    transmit_result=SimpleNamespace(
      assistance_state_unavailable=True,
      status="unavailable",
      attempted_satellite_ids=(),
      accepted_satellite_ids=(),
      unavailable_satellite_ids=(1, 2, 3),
    ),
    receiver_write_attempted=False,
  )
  structural_shape_without_marker = SimpleNamespace(
    transmit_result=SimpleNamespace(
      status="unavailable",
      requested_satellite_ids=(1, 2, 3),
      attempted_satellite_ids=(),
      accepted_satellite_ids=(),
      failed_satellite_ids=(),
      unavailable_satellite_ids=(1, 2, 3),
    )
  )

  assert pigeond.yuma_assistance_state_unavailable_outcome(normal_outcome)
  assert pigeond.yuma_assistance_state_unavailable_outcome(provisional_outcome)
  assert not pigeond.yuma_assistance_state_unavailable_outcome(structural_shape_without_marker)


# PR70_STALE_WORKER_PERSISTENCE_GUARD_TESTS


def _begin_next_generation_without_blocking(
  ownership: pigeond.ReceiverCyclePersistenceOwnership,
) -> int:
  result: list[int] = []
  completed = Event()

  def begin() -> None:
    result.append(ownership.begin_cycle())
    completed.set()

  thread = Thread(target=begin, daemon=True)
  thread.start()
  assert completed.wait(timeout=1.0), "receiver recovery blocked behind stale persistence I/O"
  thread.join(timeout=1.0)
  assert not thread.is_alive()
  assert len(result) == 1
  return result[0]


def test_superseded_constructor_worker_cannot_overwrite_new_cycle_dbd_state(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  ownership = pigeond.ReceiverCyclePersistenceOwnership()
  generation_a = ownership.begin_cycle()
  constructor_store_entered = Event()
  release_constructor_store = Event()
  constructor_errors: list[Exception] = []

  def slow_cycle_a_store(state, path: Path) -> None:
    constructor_store_entered.set()
    assert release_constructor_store.wait(timeout=2.0)
    restore_runtime.store_navigation_database_restore_boot_state(state, path)

  def construct_cycle_a() -> None:
    try:
      NavigationDatabaseRestoreRuntime(
        TEST_RECEIVER_FINGERPRINT,
        state_path=state_path,
        boot_id_reader=lambda: BOOT_ID,
        boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
        state_storer=ownership.guarded_state_storer(
          generation_a,
          slow_cycle_a_store,
        ),
        new_receiver_cycle=True,
      )
    except Exception as exc:
      constructor_errors.append(exc)

  worker_a = Thread(target=construct_cycle_a, daemon=True)
  worker_a.start()
  assert constructor_store_entered.wait(timeout=2.0)

  # Cycle B ownership must be established while A is still blocked in private
  # staging I/O. This fails if the slow write is performed under the owner lock.
  generation_b = _begin_next_generation_without_blocking(ownership)
  runtime_b = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    state_storer=ownership.guarded_state_storer(
      generation_b,
      restore_runtime.store_navigation_database_restore_boot_state,
    ),
    new_receiver_cycle=True,
  )
  assert runtime_b.note_acquisition_started()

  release_constructor_store.set()
  worker_a.join(timeout=2.0)
  assert not worker_a.is_alive()
  assert constructor_errors
  assert "superseded" in str(constructor_errors[0])

  persisted = restore_runtime.load_navigation_database_restore_boot_state(state_path)
  assert persisted is not None
  assert persisted.acquisition_started


def test_superseded_prepare_worker_cannot_overwrite_new_cycle_dbd_state(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  ownership = pigeond.ReceiverCyclePersistenceOwnership()
  generation_a = ownership.begin_cycle()
  prepare_store_entered = Event()
  release_prepare_store = Event()
  store_calls = 0

  def slow_second_cycle_a_store(state, path: Path) -> None:
    nonlocal store_calls
    store_calls += 1
    if store_calls == 2:
      prepare_store_entered.set()
      assert release_prepare_store.wait(timeout=2.0)
    restore_runtime.store_navigation_database_restore_boot_state(state, path)

  runtime_a = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    state_storer=ownership.guarded_state_storer(
      generation_a,
      slow_second_cycle_a_store,
    ),
    new_receiver_cycle=True,
  )

  worker_a = Thread(target=runtime_a.prepare, daemon=True)
  worker_a.start()
  assert prepare_store_entered.wait(timeout=2.0)

  generation_b = _begin_next_generation_without_blocking(ownership)
  runtime_b = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    state_storer=ownership.guarded_state_storer(
      generation_b,
      restore_runtime.store_navigation_database_restore_boot_state,
    ),
    new_receiver_cycle=True,
  )
  assert runtime_b.note_acquisition_started()

  release_prepare_store.set()
  worker_a.join(timeout=2.0)
  assert not worker_a.is_alive()
  assert not runtime_a.state_available
  assert runtime_a.assistance_state_disabled_reason is not None
  assert "superseded" in runtime_a.assistance_state_disabled_reason

  persisted = restore_runtime.load_navigation_database_restore_boot_state(state_path)
  assert persisted is not None
  assert persisted.acquisition_started


def test_receiver_cycle_persistence_ownership_blocks_stale_quarantine(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  state_path.write_text("cycle-b", encoding="utf-8")
  ownership = pigeond.ReceiverCyclePersistenceOwnership()
  generation_a = ownership.begin_cycle()
  stale_quarantiner = ownership.guarded_state_quarantiner(
    generation_a,
    restore_runtime.quarantine_navigation_database_restore_boot_state,
  )
  ownership.begin_cycle()

  with pytest.raises(
    pigeond.ReceiverCyclePersistenceSupersededError,
    match="superseded",
  ):
    stale_quarantiner(state_path, BOOT_ID)

  assert state_path.read_text(encoding="utf-8") == "cycle-b"
  assert list(tmp_path.glob("*.invalid-*")) == []


def test_receiver_cycle_persistence_ownership_blocks_stale_retry_writer(
  tmp_path: Path,
) -> None:
  ownership = pigeond.ReceiverCyclePersistenceOwnership()
  state_path = tmp_path / "retry.json"

  def write_marker(state: object, path: Path) -> None:
    path.write_text(str(state), encoding="utf-8")

  generation_a = ownership.begin_cycle()
  stale_storer = ownership.guarded_state_storer(
    generation_a,
    write_marker,
  )
  generation_b = ownership.begin_cycle()
  current_storer = ownership.guarded_state_storer(
    generation_b,
    write_marker,
  )

  with pytest.raises(
    pigeond.ReceiverCyclePersistenceSupersededError,
    match="superseded",
  ):
    stale_storer("stale", state_path)
  assert not state_path.exists()

  current_storer("current", state_path)
  assert state_path.read_text(encoding="utf-8") == "current"
