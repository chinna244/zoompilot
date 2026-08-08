from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from openpilot.system.ubloxd.navigation_database_restore import (
  NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS,
  NavigationDatabaseRestoreDisposition,
)
import openpilot.system.ubloxd.navigation_database_restore_runtime as restore_runtime
from openpilot.system.ubloxd.gps_assistance import (
  CacheFileInspection,
  CacheFileState,
  CacheInventory,
  NavigationQuality,
  QUALITY_POLICY_VERSION,
  QUALITY_VERSION,
)
from openpilot.system.ubloxd.navigation_database_restore_runtime import (
  NavigationDatabaseRestoreBootState,
  NavigationDatabaseRestoreCandidateIdentity,
  NavigationDatabaseRestoreCandidatePolicy,
  NavigationDatabaseRestoreFrameFailure,
  NavigationDatabaseRestoreFrameFailureKind,
  NavigationDatabaseRestorePersistedExecution,
  LEGACY_NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
  POLICY_NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
  NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
  NAVIGATION_DATABASE_RESTORE_TRANSFER_BUDGET_SECONDS,
  NavigationDatabaseRestoreFrozenCaches,
  NavigationDatabaseRestoreInitializationError,
  NavigationDatabaseRestoreRuntime,
  NavigationDatabaseRestoreSnapshot,
  NavigationDatabaseRestoreUnavailableRuntime,
  PositionAssistanceAckStatus,
  PositionAssistanceFailureKind,
  PositionAssistanceWriteStatus,
  load_navigation_database_restore_boot_state,
  store_navigation_database_restore_boot_state,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
  TrustedTimeSource,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AuthorizedTime,
  TimeAuthorizationEvidence,
)

from openpilot.system.ubloxd.yuma_almanac_transmit import (
  MgaReceiverNackError,
  MgaTransactionError,
  MgaWriteError,
)


NOW = datetime(2026, 7, 28, 20, 0, tzinfo=UTC)
BOOT_ID = "12345678-1234-5678-9234-567812345678"
TEST_RECEIVER_FINGERPRINT = "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov"
OTHER_BOOT_ID = "87654321-4321-6789-9234-567812345678"
TEST_BOOTTIME_SECONDS = 100.0
FRAMES = (b"frame-0", b"frame-1")


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


DEFAULT_STARTUP_READY_QUALITY = startup_ready_quality()


def snapshot(
  age_seconds: float = 1800.0,
  *,
  generation: str = "primary",
  quality: NavigationQuality | None = DEFAULT_STARTUP_READY_QUALITY,
  database_frames: tuple[bytes, ...] = FRAMES,
) -> NavigationDatabaseRestoreSnapshot:
  return NavigationDatabaseRestoreSnapshot(
    saved_at_utc=NOW - timedelta(seconds=age_seconds),
    database_frames=database_frames,
    latitude_e7=320_000_000,
    longitude_e7=-960_000_000,
    altitude_cm=20_000,
    position_accuracy_cm=10_000,
    quality=quality,
    generation=generation,
    selection_reason="test",
  )


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


def receiver_time() -> AuthorizedTime:
  return AuthorizedTime(
    utc=NOW,
    uncertainty_seconds=1.0,
    source=TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS,
    provenance=TimeProvenance.GNSS_INDEPENDENT,
    independent=True,
    evidence=TimeAuthorizationEvidence.RECEIVER_UTC_UNASSISTED_GNSS,
  )


def same_boot_time() -> AuthorizedTime:
  return AuthorizedTime(
    utc=NOW,
    uncertainty_seconds=2.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    independent=False,
    evidence=TimeAuthorizationEvidence.SAME_BOOT_BOOTTIME,
  )


def fresh_position_snapshot(**kwargs) -> NavigationDatabaseRestoreSnapshot:
  return snapshot(age_seconds=300.0, **kwargs)


def authorize_position(
  value: NavigationDatabaseRestoreRuntime,
  authorized: AuthorizedTime | None = None,
) -> None:
  value.prepare()
  value._last_authorized_time = same_boot_time() if authorized is None else authorized


def runtime(
  tmp_path: Path,
  *,
  selected: NavigationDatabaseRestoreSnapshot | None = None,
  boot_id: str = BOOT_ID,
  state_storer=store_navigation_database_restore_boot_state,
  transfer_budget_seconds: float = (
    NAVIGATION_DATABASE_RESTORE_TRANSFER_BUDGET_SECONDS
  ),
  monotonic=lambda: 0.0,
  sleeper=lambda _seconds: None,
) -> NavigationDatabaseRestoreRuntime:
  selected = snapshot() if selected is None else selected
  return NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: selected,
    retry_delay_seconds=0.0,
    transfer_budget_seconds=transfer_budget_seconds,
    monotonic=monotonic,
    sleeper=sleeper,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: boot_id,
    state_storer=state_storer,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )


def no_cache_runtime(tmp_path: Path) -> NavigationDatabaseRestoreRuntime:
  return NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: None,
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )


def evaluate(
  value: NavigationDatabaseRestoreRuntime,
  *,
  authorized_time: AuthorizedTime | None = None,
  reliable: bool = False,
  yuma: bool = False,
  send=None,
):
  send = send or (lambda _frame, _index: None)

  def send_at_receiver_boundary(
    frame: bytes,
    index: int,
    mark_write_attempt,
  ):
    mark_write_attempt()
    return send(frame, index)

  return value.evaluate(
    authorized_time=authorized_time,
    reliable_fix_available=reliable,
    yuma_already_sent=yuma,
    send_database_message=send_at_receiver_boundary,
  )


def test_state_round_trip(tmp_path: Path) -> None:
  path = tmp_path / "state.json"
  state = NavigationDatabaseRestoreBootState(
    version=NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
    boot_id=BOOT_ID,
    receiver_fingerprint=TEST_RECEIVER_FINGERPRINT,
    disposition=NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED,
    restore_attempted=False,
    position_assistance_claimed=True,
    acquisition_started=True,
    yuma_sent=True,
    cache_generation="primary",
    cache_saved_at_utc=NOW,
    cache_database_digest="a" * 64,
    cache_maximum_age_seconds=NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS,
    cache_expires_at_utc=(
      NOW + timedelta(seconds=NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS)
    ),
  )
  store_navigation_database_restore_boot_state(state, path)
  assert load_navigation_database_restore_boot_state(path) == state


def test_terminal_restore_result_round_trip(tmp_path: Path) -> None:
  path = tmp_path / "state.json"
  initial_failure = NavigationDatabaseRestoreFrameFailure(
    frame_index=1,
    attempt=1,
    kind=NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT,
    error="TimeoutError:initial timeout",
  )
  permanent_failure = NavigationDatabaseRestoreFrameFailure(
    frame_index=1,
    attempt=2,
    kind=NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT,
    error="TimeoutError:retry timeout",
  )
  restore_result = NavigationDatabaseRestorePersistedExecution(
    disposition=NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
    total_frame_count=2,
    accepted_frame_count=1,
    database_write_attempt_count=3,
    initial_failures=(initial_failure,),
    permanent_failures=(permanent_failure,),
    execution_error=None,
    failure_phase="retry_pass",
    cache_selection_reason="trusted_age_only_eligible:primary",
    cache_age_seconds=1800.0,
    transfer_budget_seconds=15.0,
    transfer_started_at=100.0,
    transfer_completed_at=101.0,
    transfer_deadline=115.0,
  )
  state = NavigationDatabaseRestoreBootState(
    version=NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
    boot_id=BOOT_ID,
    receiver_fingerprint=TEST_RECEIVER_FINGERPRINT,
    disposition=NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
    restore_attempted=True,
    position_assistance_claimed=False,
    acquisition_started=False,
    yuma_sent=False,
    cache_generation="primary",
    cache_saved_at_utc=NOW,
    cache_database_digest="a" * 64,
    cache_maximum_age_seconds=NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS,
    cache_expires_at_utc=(
      NOW + timedelta(seconds=NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS)
    ),
    restore_result=restore_result,
  )

  store_navigation_database_restore_boot_state(state, path)

  assert load_navigation_database_restore_boot_state(path) == state


@pytest.mark.parametrize(
  "overrides",
  (
    {"database_write_attempt_count": 0},
    {"retry_accepted_indexes": (0,)},
    {
      "permanent_failures": (
        NavigationDatabaseRestoreFrameFailure(
          frame_index=1,
          attempt=2,
          kind=NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT,
          error="TimeoutError:first",
        ),
        NavigationDatabaseRestoreFrameFailure(
          frame_index=1,
          attempt=2,
          kind=NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT,
          error="TimeoutError:duplicate",
        ),
      ),
    },
    {
      "disposition": (
        NavigationDatabaseRestoreDisposition.WRITE_FAILED
      ),
    },
  ),
)
def test_persisted_execution_rejects_cross_field_inconsistency(
  overrides: dict[str, object],
) -> None:
  values: dict[str, object] = {
    "disposition": NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
    "total_frame_count": 2,
    "accepted_frame_count": 1,
    "database_write_attempt_count": 3,
    "initial_failures": (
      NavigationDatabaseRestoreFrameFailure(
        frame_index=1,
        attempt=1,
        kind=NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT,
        error="TimeoutError:initial",
      ),
    ),
    "permanent_failures": (
      NavigationDatabaseRestoreFrameFailure(
        frame_index=1,
        attempt=2,
        kind=NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT,
        error="TimeoutError:retry",
      ),
    ),
  }
  values.update(overrides)

  with pytest.raises(restore_runtime.NavigationDatabaseRestoreStateError):
    NavigationDatabaseRestorePersistedExecution(**values)  # ty: ignore[invalid-argument-type]


def test_pr69_policy_state_schema_migrates_without_inventing_result(
  tmp_path: Path,
) -> None:
  path = tmp_path / "state.json"
  policy_state = {
    "version": POLICY_NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
    "boot_id": BOOT_ID,
    "receiver_fingerprint": TEST_RECEIVER_FINGERPRINT,
    "disposition": NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL.value,
    "restore_attempted": True,
    "position_assistance_claimed": False,
    "acquisition_started": False,
    "yuma_sent": False,
    "candidate_identities": [],
    "cache_generation": "primary",
    "cache_saved_at_utc": NOW.isoformat(),
    "cache_database_digest": "a" * 64,
    "cache_maximum_age_seconds": NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS,
    "cache_expires_at_utc": (
      NOW + timedelta(seconds=NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS)
    ).isoformat(),
  }
  path.write_text(json.dumps(policy_state), encoding="utf-8")

  migrated = load_navigation_database_restore_boot_state(path)

  assert migrated is not None
  assert migrated.version == NAVIGATION_DATABASE_RESTORE_STATE_VERSION
  assert migrated.disposition is NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL
  assert migrated.restore_result is None
  assert not list(tmp_path.glob(f"{path.name}.invalid-*"))


def test_pr68_state_schema_migrates_without_quarantine(
  tmp_path: Path,
) -> None:
  path = tmp_path / "state.json"
  legacy = {
    "version": LEGACY_NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
    "boot_id": BOOT_ID,
    "receiver_fingerprint": TEST_RECEIVER_FINGERPRINT,
    "disposition": NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED.value,
    "restore_attempted": False,
    "position_assistance_claimed": True,
    "acquisition_started": True,
    "yuma_sent": True,
    "candidate_identities": [],
    "cache_generation": None,
    "cache_saved_at_utc": None,
  }
  path.write_text(json.dumps(legacy), encoding="utf-8")

  migrated = load_navigation_database_restore_boot_state(path)

  assert migrated is not None
  assert migrated.version == NAVIGATION_DATABASE_RESTORE_STATE_VERSION
  assert migrated.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  assert migrated.cache_generation is None
  assert migrated.cache_database_digest is None
  assert not list(tmp_path.glob(f"{path.name}.invalid-*"))


def test_corrupt_state_is_quarantined_and_fails_closed(
  tmp_path: Path,
) -> None:
  path = tmp_path / "dbd_state.json"
  path.write_text("not-json", encoding="utf-8")

  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    state_path=path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  assert (
    value.controller.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )

  persisted = load_navigation_database_restore_boot_state(path)
  assert persisted is not None
  assert (
    persisted.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )

  quarantined = list(tmp_path.glob(f"{path.name}.invalid-*"))
  assert len(quarantined) == 1
  assert quarantined[0].read_text(encoding="utf-8") == "not-json"


def test_new_linux_boot_discards_old_state(tmp_path: Path) -> None:
  path = tmp_path / "dbd_state.json"
  old = NavigationDatabaseRestoreBootState(
    version=NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
    boot_id=OTHER_BOOT_ID,
    receiver_fingerprint=TEST_RECEIVER_FINGERPRINT,
    disposition=NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED,
    restore_attempted=False,
    position_assistance_claimed=True,
    acquisition_started=True,
    yuma_sent=True,
  )
  store_navigation_database_restore_boot_state(old, path)
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  assert value.controller.pending
  assert not value.acquisition_started
  assert not value.yuma_sent


def test_snapshot_is_loaded_only_once(tmp_path: Path) -> None:
  calls = 0

  def loader(_fingerprint: str):
    nonlocal calls
    calls += 1
    return snapshot()

  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=loader,
    retry_delay_seconds=0.0,
    state_path=tmp_path / "state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  value.prepare()
  value.prepare()
  evaluate(value)
  assert calls == 1


def test_no_cache_is_terminal_without_writes(tmp_path: Path) -> None:
  value = no_cache_runtime(tmp_path)
  writes = []
  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_NO_CACHE
  assert writes == []


def test_snapshot_loader_failure_is_cache_unqualified(
  tmp_path: Path,
) -> None:
  def fail_loader(_fingerprint: str):
    raise OSError("cache read failed")

  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=fail_loader,
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  result = evaluate(value, authorized_time=network_time())

  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_CACHE_UNQUALIFIED
  )
  assert result.position_assistance_error is not None
  assert "snapshot_load:OSError:cache read failed" in result.position_assistance_error


def test_present_invalid_cache_is_not_reported_as_missing(
  tmp_path: Path,
) -> None:
  frozen = NavigationDatabaseRestoreFrozenCaches(
    position_snapshot=None,
    primary_snapshot=None,
    previous_snapshot=None,
    inventory=CacheInventory(
      primary=CacheFileInspection(
        "primary",
        tmp_path / "gps_cache.json",
        CacheFileState.INVALID,
        error="invalid json",
      ),
      previous=CacheFileInspection(
        "previous",
        tmp_path / "gps_cache_previous.json",
        CacheFileState.ABSENT,
      ),
    ),
  )
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: frozen,
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  result = evaluate(value, authorized_time=network_time())

  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_CACHE_UNQUALIFIED
  )


def test_unqualified_cache_is_terminal_without_writes(tmp_path: Path) -> None:
  weak = snapshot(quality=NavigationQuality(
    quality_version=QUALITY_VERSION,
    policy_version=QUALITY_POLICY_VERSION,
    capture_context="onroad",
    continuous_reliable_fix_seconds=20.0,
    continuous_orbit_quality_seconds=0.0,
    gps_satellites_known=8,
    glonass_satellites_known=8,
    gps_ephemeris_available=1,
    glonass_ephemeris_available=1,
    satellites_used=3,
    gps_almanac_available=5,
    glonass_almanac_available=5,
    assistnow_offline_available=0,
    orbit_source_counts={"ephemeris": 2, "almanac": 14},
  ))
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: weak,
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  result = evaluate(value, authorized_time=network_time())
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_CACHE_UNQUALIFIED


def test_only_quality_qualified_cache_can_delay_startup(tmp_path: Path) -> None:
  legacy = runtime(tmp_path, selected=snapshot(quality=None))
  assert not legacy.has_prequalified_database_candidate

  legacy_result = evaluate(legacy, authorized_time=network_time())
  assert (
    legacy_result.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_CACHE_UNQUALIFIED
  )

  qualified = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(
      quality=startup_ready_quality()
    ),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "qualified.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  assert qualified.has_prequalified_database_candidate


def test_position_assistance_claim_survives_process_restart(tmp_path: Path) -> None:
  first = runtime(tmp_path, selected=fresh_position_snapshot())
  authorize_position(first)
  messages = []
  first.send_position_once(messages.append)
  second = runtime(tmp_path, selected=fresh_position_snapshot())
  authorize_position(second)
  second.send_position_once(messages.append)
  assert len(messages) == 1


def test_position_assistance_success_is_structured(tmp_path: Path) -> None:
  value = runtime(tmp_path, selected=fresh_position_snapshot())
  authorize_position(value)
  result = value.send_position_once(lambda _message: None)

  assert result.position_assistance_attempted
  assert result.position_assistance_succeeded
  assert result.position_assistance_message_id == 0x40
  assert result.position_assistance_message_type == 0x01
  assert (
    result.position_assistance_write_status
    is PositionAssistanceWriteStatus.SUCCEEDED
  )
  assert (
    result.position_assistance_ack_status
    is PositionAssistanceAckStatus.ACCEPTED
  )
  assert result.position_assistance_ack_info_code == 0
  assert result.position_assistance_failure_kind is None
  assert result.position_assistance_error_type is None
  assert result.position_assistance_error is None


def test_position_assistance_skips_without_verified_age(tmp_path: Path) -> None:
  value = runtime(tmp_path, selected=fresh_position_snapshot())
  value.prepare()
  result = value.send_position_once(lambda _message: None)
  assert result.position_assistance_attempted
  assert not result.position_assistance_succeeded
  assert (
    result.position_assistance_failure_kind
    is PositionAssistanceFailureKind.AGE_UNVERIFIED
  )


def test_position_assistance_skips_receiver_derived_time(tmp_path: Path) -> None:
  value = runtime(tmp_path, selected=fresh_position_snapshot())
  authorize_position(value, receiver_time())
  result = value.send_position_once(lambda _message: None)
  assert result.position_assistance_attempted
  assert not result.position_assistance_succeeded
  assert (
    result.position_assistance_failure_kind
    is PositionAssistanceFailureKind.AGE_UNVERIFIED
  )


def test_position_assistance_skips_stale_verified_age(tmp_path: Path) -> None:
  value = runtime(tmp_path, selected=snapshot(age_seconds=1800.0))
  authorize_position(value, network_time())
  result = value.send_position_once(lambda _message: None)
  assert result.position_assistance_attempted
  assert not result.position_assistance_succeeded
  assert (
    result.position_assistance_failure_kind
    is PositionAssistanceFailureKind.UNCERTAINTY_UNREPRESENTABLE
  )

@pytest.mark.parametrize(
  (
    "exception",
    "write_status",
    "ack_status",
    "failure_kind",
    "info_code",
  ),
  (
    (
      MgaReceiverNackError(
        "receiver not ready",
        message_id=0x40,
        message_type=0x01,
        ack_type=0,
        ack_version=0,
        info_code=5,
        rejected_message_id=0x40,
      ),
      PositionAssistanceWriteStatus.SUCCEEDED,
      PositionAssistanceAckStatus.REJECTED,
      PositionAssistanceFailureKind.ACK_REJECTED,
      5,
    ),
    (
      TimeoutError("position ACK timeout"),
      PositionAssistanceWriteStatus.SUCCEEDED,
      PositionAssistanceAckStatus.TIMED_OUT,
      PositionAssistanceFailureKind.ACK_TIMEOUT,
      None,
    ),
    (
      MgaWriteError(
        "position write failed",
        message_id=0x40,
        message_type=0x01,
      ),
      PositionAssistanceWriteStatus.FAILED,
      PositionAssistanceAckStatus.NOT_ATTEMPTED,
      PositionAssistanceFailureKind.WRITE,
      None,
    ),
    (
      MgaTransactionError(
        "position ACK observation failed",
        message_id=0x40,
        message_type=0x01,
        write_succeeded=True,
      ),
      PositionAssistanceWriteStatus.SUCCEEDED,
      PositionAssistanceAckStatus.OBSERVATION_FAILED,
      PositionAssistanceFailureKind.ACK_OBSERVATION_FAILED,
      None,
    ),
    (
      MgaTransactionError(
        "position transaction failed before confirmed write",
        message_id=0x40,
        message_type=0x01,
        write_succeeded=False,
      ),
      PositionAssistanceWriteStatus.FAILED,
      PositionAssistanceAckStatus.NOT_ATTEMPTED,
      PositionAssistanceFailureKind.WRITE,
      None,
    ),
    (
      MgaTransactionError(
        "position transaction write state unknown",
        message_id=0x40,
        message_type=0x01,
        write_succeeded=None,
      ),
      PositionAssistanceWriteStatus.FAILED,
      PositionAssistanceAckStatus.NOT_ATTEMPTED,
      PositionAssistanceFailureKind.WRITE,
      None,
    ),
  ),
)
def test_position_assistance_failures_remain_structured(
  tmp_path: Path,
  exception: Exception,
  write_status: PositionAssistanceWriteStatus,
  ack_status: PositionAssistanceAckStatus,
  failure_kind: PositionAssistanceFailureKind,
  info_code: int | None,
) -> None:
  def fail(_message: bytes) -> None:
    raise exception

  value = runtime(tmp_path, selected=fresh_position_snapshot())
  authorize_position(value)
  result = value.send_position_once(fail)

  assert result.position_assistance_attempted
  assert not result.position_assistance_succeeded
  assert result.position_assistance_message_id == 0x40
  assert result.position_assistance_message_type == 0x01
  assert result.position_assistance_write_status is write_status
  assert result.position_assistance_ack_status is ack_status
  assert result.position_assistance_ack_info_code == info_code
  assert result.position_assistance_failure_kind is failure_kind
  assert result.position_assistance_error_type == type(exception).__name__
  assert str(exception) in (result.position_assistance_error or "")


def test_position_assistance_build_failure_is_structured(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  monkeypatch.setattr(
    restore_runtime,
    "build_position_assistance_message",
    lambda **_kwargs: (_ for _ in ()).throw(
      ValueError("position build failed")
    ),
  )

  value = runtime(tmp_path, selected=fresh_position_snapshot())
  authorize_position(value)
  result = value.send_position_once(
    lambda _message: pytest.fail("position message must not be written")
  )

  assert result.position_assistance_attempted
  assert not result.position_assistance_succeeded
  assert result.position_assistance_message_id is None
  assert result.position_assistance_message_type is None
  assert (
    result.position_assistance_write_status
    is PositionAssistanceWriteStatus.NOT_ATTEMPTED
  )
  assert (
    result.position_assistance_ack_status
    is PositionAssistanceAckStatus.NOT_ATTEMPTED
  )
  assert (
    result.position_assistance_failure_kind
    is PositionAssistanceFailureKind.BUILD
  )
  assert result.position_assistance_error_type == "ValueError"
  assert "position build failed" in (
    result.position_assistance_error or ""
  )


def test_unverified_startup_performs_zero_database_writes(tmp_path: Path) -> None:
  value = runtime(tmp_path)
  writes = []
  result = evaluate(
    value,
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.PENDING
  assert writes == []


def test_expired_cache_performs_zero_database_writes(tmp_path: Path) -> None:
  value = runtime(tmp_path, selected=snapshot(25 * 60 * 60))
  writes = []
  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED
  assert writes == []


def test_one_hour_boundary_restores_exactly_once(tmp_path: Path) -> None:
  value = runtime(
    tmp_path,
    selected=snapshot(NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS - 1.0),
  )
  writes = []
  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.RESTORED
  assert writes == [(FRAMES[0], 0), (FRAMES[1], 1)]
  evaluate(value, authorized_time=network_time(), send=lambda *_: writes.append("again"))
  assert len(writes) == 2


def test_startup_ready_cache_uses_default_one_hour_policy(
  tmp_path: Path,
) -> None:
  value = runtime(
    tmp_path,
    selected=snapshot(30 * 60, quality=startup_ready_quality()),
  )
  writes: list[tuple[bytes, int]] = []

  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )

  assert result.disposition is NavigationDatabaseRestoreDisposition.RESTORED
  assert result.cache_maximum_age_seconds == (
    NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS
  )
  assert writes == [(FRAMES[0], 0), (FRAMES[1], 1)]


def test_startup_ready_two_hour_cache_expires_under_default_policy(
  tmp_path: Path,
) -> None:
  value = runtime(
    tmp_path,
    selected=snapshot(2 * 60 * 60, quality=startup_ready_quality()),
  )
  writes: list[tuple[bytes, int]] = []

  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )

  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED
  assert writes == []


def test_selected_policy_is_used_at_every_frame_boundary(
  tmp_path: Path,
) -> None:
  value = runtime(tmp_path)
  observed_policies: list[NavigationDatabaseRestoreCandidatePolicy | None] = []

  def send(_frame: bytes, index: int) -> None:
    value.validate_database_write_boundary(index)
    observed_policies.append(value._database_policy)

  result = evaluate(value, authorized_time=network_time(), send=send)
  assert result.disposition is NavigationDatabaseRestoreDisposition.RESTORED
  assert len(observed_policies) == len(FRAMES)
  assert observed_policies[0] is not None
  assert all(policy is observed_policies[0] for policy in observed_policies)


def test_restored_terminal_state_survives_process_restart(tmp_path: Path) -> None:
  first = runtime(tmp_path)
  writes = []
  assert evaluate(
    first,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  ).database_available
  second = runtime(tmp_path)
  evaluate(
    second,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert second.controller.disposition is NavigationDatabaseRestoreDisposition.RESTORED
  assert len(writes) == len(FRAMES)


def test_interrupted_attempt_recovers_as_write_failed(tmp_path: Path) -> None:
  path = tmp_path / "dbd_state.json"
  interrupted = NavigationDatabaseRestoreBootState(
    version=NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
    boot_id=BOOT_ID,
    receiver_fingerprint=TEST_RECEIVER_FINGERPRINT,
    disposition=NavigationDatabaseRestoreDisposition.PENDING,
    restore_attempted=True,
    position_assistance_claimed=True,
    acquisition_started=False,
    yuma_sent=False,
    cache_generation="primary",
    cache_saved_at_utc=snapshot().saved_at_utc,
    cache_database_digest=snapshot().database_digest,
    cache_maximum_age_seconds=NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS,
    cache_expires_at_utc=(
      snapshot().saved_at_utc
      + timedelta(seconds=NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS)
    ),
  )
  store_navigation_database_restore_boot_state(interrupted, path)
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  writes = []
  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.WRITE_FAILED
  assert result.recovered_interrupted_attempt
  assert writes == []


def test_receiver_time_performs_zero_database_writes(tmp_path: Path) -> None:
  value = runtime(tmp_path)
  writes = []
  result = evaluate(
    value,
    authorized_time=receiver_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_LATE_RECEIVER_TIME
  assert writes == []


def test_same_boot_continuity_keeps_restore_pending(tmp_path: Path) -> None:
  value = runtime(tmp_path)
  writes = []
  result = evaluate(
    value,
    authorized_time=same_boot_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.PENDING
  assert writes == []

  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.RESTORED
  assert writes == [(FRAMES[0], 0), (FRAMES[1], 1)]


def test_acquisition_latch_survives_process_restart(tmp_path: Path) -> None:
  first = runtime(tmp_path)
  first.note_acquisition_started()
  second = runtime(tmp_path)
  writes = []
  result = evaluate(
    second,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_ACQUISITION_ALREADY_STARTED
  assert writes == []


def test_yuma_latch_survives_process_restart(tmp_path: Path) -> None:
  first = runtime(tmp_path)
  assert first.close_restore_window_no_trusted_time()
  assert first.note_yuma_sent()
  second = runtime(tmp_path)
  result = evaluate(second, authorized_time=network_time())
  assert second.yuma_sent
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME


def test_reliable_fix_is_terminal_before_restore(tmp_path: Path) -> None:
  value = runtime(tmp_path)
  result = evaluate(value, authorized_time=network_time(), reliable=True)
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_RELIABLE_FIX


def test_failed_frame_is_retried_once_and_can_recover(tmp_path: Path) -> None:
  value = runtime(tmp_path)
  attempts = {0: 0, 1: 0}

  def send(_frame: bytes, index: int) -> None:
    attempts[index] += 1
    if index == 0 and attempts[index] == 1:
      raise TimeoutError("first attempt")

  result = evaluate(value, authorized_time=network_time(), send=send)
  assert result.disposition is NavigationDatabaseRestoreDisposition.RESTORED
  assert attempts == {0: 2, 1: 1}
  assert result.retry_accepted_indexes == (0,)


def test_permanent_partial_failure_marks_database_unavailable(tmp_path: Path) -> None:
  value = runtime(tmp_path)

  def send(_frame: bytes, index: int) -> None:
    if index == 1:
      raise TimeoutError("always")

  result = evaluate(value, authorized_time=network_time(), send=send)
  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL
  )
  assert not result.database_available
  assert result.permanently_failed_indexes == (1,)


def test_write_failure_is_not_retried_after_process_restart(tmp_path: Path) -> None:
  first = runtime(tmp_path)
  attempts = []

  def fail(frame: bytes, index: int) -> None:
    attempts.append((frame, index))
    raise TimeoutError("failure")

  assert (
    evaluate(
      first,
      authorized_time=network_time(),
      send=fail,
    ).disposition
    is NavigationDatabaseRestoreDisposition.RESTORE_RESPONSE_TIMEOUT
  )
  second = runtime(tmp_path)
  evaluate(second, authorized_time=network_time(), send=fail)
  assert len(attempts) == 2 * len(FRAMES)


def test_first_frame_rejection_has_exact_terminal_outcome(
  tmp_path: Path,
) -> None:
  value = runtime(tmp_path)

  def reject(_frame: bytes, index: int) -> None:
    if index == 0:
      raise MgaReceiverNackError("rejected")

  result = evaluate(value, authorized_time=network_time(), send=reject)
  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL
  )
  assert result.accepted_frame_count == 1


def test_transaction_failure_has_exact_terminal_outcome(
  tmp_path: Path,
) -> None:
  value = runtime(tmp_path)

  def fail(_frame: bytes, _index: int) -> None:
    raise MgaTransactionError("transport failed")

  result = evaluate(value, authorized_time=network_time(), send=fail)
  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR
  )


def test_ack_completion_after_total_transfer_deadline_is_not_accepted(
  tmp_path: Path,
) -> None:
  now = [0.0]
  value = runtime(
    tmp_path,
    transfer_budget_seconds=1.0,
    monotonic=lambda: now[0],
  )
  writes: list[int] = []

  def send(_frame: bytes, index: int) -> None:
    writes.append(index)
    now[0] = 2.0

  result = evaluate(value, authorized_time=network_time(), send=send)
  assert writes == [0]
  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE
  )
  assert result.accepted_frame_count == 0
  assert result.permanent_failures[0].frame_index == 0
  assert (
    result.permanent_failures[0].kind
    is NavigationDatabaseRestoreFrameFailureKind.TRANSFER_DEADLINE
  )
  assert result.failure_phase == "initial_pass"
  assert result.transfer_budget_seconds == 1.0
  assert result.transfer_started_at == 0.0
  assert result.transfer_deadline == 1.0


def test_retry_delay_cannot_exceed_total_transfer_deadline(
  tmp_path: Path,
) -> None:
  now = [0.0]
  sleeps: list[float] = []
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.25,
    transfer_budget_seconds=0.2,
    monotonic=lambda: now[0],
    sleeper=lambda seconds: sleeps.append(seconds),
    state_path=tmp_path / "state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda _frame, _index: (_ for _ in ()).throw(
      TimeoutError("timeout")
    ),
  )
  assert sleeps == []
  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE
  )
  assert result.failure_phase == "retry_delay"


def test_snapshot_identity_change_within_boot_fails_closed(tmp_path: Path) -> None:
  first = runtime(tmp_path, selected=snapshot(generation="primary"))
  first.prepare()
  second = runtime(tmp_path, selected=snapshot(generation="previous"))
  writes = []
  result = evaluate(
    second,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  assert writes == []


def test_persistence_failure_prevents_database_write(tmp_path: Path) -> None:
  writes = []
  store_calls = 0

  def fail_after_initial_state(state, path):
    nonlocal store_calls
    store_calls += 1
    if store_calls >= 3:
      raise OSError("disk failure")
    store_navigation_database_restore_boot_state(state, path)

  value = runtime(tmp_path, state_storer=fail_after_initial_state)
  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.WRITE_FAILED
  assert result.state_persistence_error is not None
  assert writes == []


def test_boot_id_unavailable_aborts_initialization(
  tmp_path: Path,
) -> None:
  with pytest.raises(
    NavigationDatabaseRestoreInitializationError,
    match="boot_id_unavailable",
  ):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      state_path=tmp_path / "state.json",
      boot_id_reader=lambda: None,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    )


@pytest.mark.parametrize(
  "retry_delay_seconds",
  (-1.0, float("nan"), float("inf"), True, "0.25"),
)
def test_runtime_rejects_invalid_retry_delay(
  tmp_path: Path,
  retry_delay_seconds: object,
) -> None:
  with pytest.raises(ValueError):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      retry_delay_seconds=retry_delay_seconds,  # type: ignore[arg-type, ty:invalid-argument-type]
      state_path=tmp_path / "state.json",
      boot_id_reader=lambda: BOOT_ID,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    )


def frozen_caches(
  *,
  primary: NavigationDatabaseRestoreSnapshot | None,
  previous: NavigationDatabaseRestoreSnapshot | None,
  position: NavigationDatabaseRestoreSnapshot | None = None,
) -> NavigationDatabaseRestoreFrozenCaches:
  return NavigationDatabaseRestoreFrozenCaches(
    position_snapshot=position or primary or previous,
    primary_snapshot=primary,
    previous_snapshot=previous,
  )


def multi_runtime(
  tmp_path: Path,
  *,
  primary: NavigationDatabaseRestoreSnapshot | None,
  previous: NavigationDatabaseRestoreSnapshot | None,
) -> NavigationDatabaseRestoreRuntime:
  value = frozen_caches(primary=primary, previous=previous)
  return NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: value,
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )


def test_newer_eligible_primary_wins_over_expired_higher_quality_previous(
  tmp_path: Path,
) -> None:
  primary = snapshot(30 * 60, generation="primary")
  previous = snapshot(2 * 60 * 60, generation="previous")
  value = multi_runtime(tmp_path, primary=primary, previous=previous)

  result = evaluate(value, authorized_time=network_time())

  assert result.disposition is NavigationDatabaseRestoreDisposition.RESTORED
  assert result.cache_generation == "primary"
  assert result.cache_selection_reason == "trusted_age_only_eligible:primary"


def test_expired_primary_does_not_fallback_to_expired_startup_ready_previous(
  tmp_path: Path,
) -> None:
  primary = snapshot(2 * 60 * 60, generation="primary")
  previous = snapshot(
    2 * 60 * 60,
    generation="previous",
    quality=startup_ready_quality(),
  )
  value = multi_runtime(tmp_path, primary=primary, previous=previous)

  result = evaluate(value, authorized_time=network_time())

  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED
  assert result.cache_generation is None


def test_future_primary_falls_back_to_valid_previous(tmp_path: Path) -> None:
  primary = snapshot(-30.0, generation="primary")
  previous = snapshot(30 * 60, generation="previous")
  value = multi_runtime(tmp_path, primary=primary, previous=previous)

  result = evaluate(value, authorized_time=network_time())

  assert result.disposition is NavigationDatabaseRestoreDisposition.RESTORED
  assert result.cache_generation == "previous"


def test_both_generations_expired_skip_without_writes(tmp_path: Path) -> None:
  value = multi_runtime(
    tmp_path,
    primary=snapshot(2 * 60 * 60, generation="primary"),
    previous=snapshot(3 * 60 * 60, generation="previous"),
  )
  writes = []

  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )

  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED
  assert writes == []


def test_exactly_one_generation_on_one_hour_boundary_is_selected(
  tmp_path: Path,
) -> None:
  value = multi_runtime(
    tmp_path,
    primary=snapshot(
      NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS - 1.0,
      generation="primary",
    ),
    previous=snapshot(
      NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS,
      generation="previous",
    ),
  )

  result = evaluate(value, authorized_time=network_time())

  assert result.disposition is NavigationDatabaseRestoreDisposition.RESTORED
  assert result.cache_generation == "primary"


def test_recovered_same_boot_pending_state_fails_closed(
  tmp_path: Path,
) -> None:
  first = multi_runtime(
    tmp_path,
    primary=snapshot(generation="primary"),
    previous=snapshot(45 * 60, generation="previous"),
  )
  first.prepare()

  second = multi_runtime(
    tmp_path,
    primary=snapshot(generation="primary"),
    previous=snapshot(45 * 60, generation="previous"),
  )
  writes: list[tuple[bytes, int]] = []
  result = evaluate(
    second,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )

  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )
  assert writes == []

  persisted = load_navigation_database_restore_boot_state(
    tmp_path / "dbd_state.json"
  )
  assert persisted is not None
  assert (
    persisted.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )


def test_terminal_restore_details_survive_runtime_reconstruction(
  tmp_path: Path,
) -> None:
  first = runtime(tmp_path)
  frame_one_attempts = 0

  def send(_frame: bytes, index: int) -> None:
    nonlocal frame_one_attempts
    if index == 1:
      frame_one_attempts += 1
      raise TimeoutError(f"frame one timeout {frame_one_attempts}")

  original = evaluate(first, authorized_time=network_time(), send=send)
  reconstructed = runtime(tmp_path).execution

  assert original.disposition is NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL
  assert reconstructed.disposition is original.disposition
  assert reconstructed.total_frame_count == original.total_frame_count == 2
  assert reconstructed.accepted_frame_count == original.accepted_frame_count == 1
  assert reconstructed.database_write_attempt_count == original.database_write_attempt_count == 3
  assert reconstructed.initial_failures == original.initial_failures
  assert reconstructed.retry_accepted_indexes == original.retry_accepted_indexes
  assert reconstructed.permanent_failures == original.permanent_failures
  assert reconstructed.execution_error == original.execution_error
  assert reconstructed.failure_phase == original.failure_phase == "retry_pass"
  assert reconstructed.cache_generation == original.cache_generation == "primary"
  assert reconstructed.cache_selection_reason == original.cache_selection_reason
  assert reconstructed.cache_age_seconds == original.cache_age_seconds == 1801.0
  assert reconstructed.transfer_budget_seconds == original.transfer_budget_seconds
  assert reconstructed.transfer_started_at == original.transfer_started_at
  assert reconstructed.transfer_completed_at == original.transfer_completed_at
  assert reconstructed.transfer_deadline == original.transfer_deadline
  assert reconstructed.first_failure == original.first_failure
  assert reconstructed.first_failure is not None
  assert reconstructed.first_failure.frame_index == 1
  assert reconstructed.first_failure.attempt == 1
  assert reconstructed.first_failure.kind is NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT

  persisted = load_navigation_database_restore_boot_state(
    tmp_path / "dbd_state.json"
  )
  assert persisted is not None
  assert persisted.restore_result is not None
  assert persisted.restore_result.first_failure == original.first_failure


def test_candidate_identity_round_trip() -> None:
  identity = NavigationDatabaseRestoreCandidateIdentity.from_snapshot(snapshot())
  assert NavigationDatabaseRestoreCandidateIdentity.from_json_dict(identity.to_json_dict()) == identity


@pytest.mark.parametrize(
  ("exception", "kind", "retried", "expected_disposition"),
  (
    (
      MgaReceiverNackError("nack"),
      NavigationDatabaseRestoreFrameFailureKind.REJECTED,
      False,
      NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
    ),
    (
      TimeoutError("timeout"),
      NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT,
      True,
      NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
    ),
    (
      MgaWriteError("write"),
      NavigationDatabaseRestoreFrameFailureKind.WRITE_ERROR,
      True,
      NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
    ),
    (
      MgaTransactionError("transaction"),
      NavigationDatabaseRestoreFrameFailureKind.TRANSACTION_ERROR,
      True,
      NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
    ),
    (
      ValueError("unexpected"),
      NavigationDatabaseRestoreFrameFailureKind.UNEXPECTED_ERROR,
      False,
      NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
    ),
  ),
)
def test_typed_frame_failure_and_retry_policy(
  tmp_path: Path,
  exception: Exception,
  kind: NavigationDatabaseRestoreFrameFailureKind,
  retried: bool,
  expected_disposition: NavigationDatabaseRestoreDisposition,
) -> None:
  value = runtime(tmp_path)
  attempts = 0

  def send(_frame: bytes, index: int) -> None:
    nonlocal attempts
    if index != 0:
      return
    attempts += 1
    raise exception

  result = evaluate(value, authorized_time=network_time(), send=send)

  assert result.disposition is expected_disposition
  assert result.initial_failures[0].kind is kind
  assert attempts == (2 if retried else 1)


def test_retry_sleeper_failure_records_phase_and_error(tmp_path: Path) -> None:
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.25,
    sleeper=lambda _delay: (_ for _ in ()).throw(RuntimeError("sleep failed")),
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda _frame, _index: (_ for _ in ()).throw(TimeoutError("timeout")),
  )

  assert result.disposition is NavigationDatabaseRestoreDisposition.WRITE_FAILED
  assert result.failure_phase == "retry_delay"
  assert result.execution_error == "RuntimeError:sleep failed"


def test_retry_sleeper_failure_after_accepted_frame_is_partial(
  tmp_path: Path,
) -> None:
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.25,
    sleeper=lambda _delay: (_ for _ in ()).throw(
      RuntimeError("sleep failed after partial transfer")
    ),
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  def send(_frame: bytes, index: int) -> None:
    if index == 1:
      raise TimeoutError("retry frame one")

  result = evaluate(value, authorized_time=network_time(), send=send)

  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL
  )
  assert result.accepted_frame_count == 1
  assert result.failure_phase == "retry_delay"
  assert result.execution_error == (
    "RuntimeError:sleep failed after partial transfer"
  )


def test_transfer_start_clock_exception_is_terminal_and_durable(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    monotonic=lambda: (_ for _ in ()).throw(
      RuntimeError("transfer clock unavailable")
    ),
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  result = evaluate(value, authorized_time=network_time())

  assert result.disposition is NavigationDatabaseRestoreDisposition.WRITE_FAILED
  assert result.database_write_attempt_count == 0
  assert result.accepted_frame_count == 0
  assert result.failure_phase == "transfer_start_clock"
  assert result.execution_error == "RuntimeError:transfer clock unavailable"

  restored = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  ).execution
  assert restored.disposition is result.disposition
  assert restored.failure_phase == result.failure_phase
  assert restored.execution_error == result.execution_error


def test_transfer_completion_clock_exception_after_accept_is_partial(
  tmp_path: Path,
) -> None:
  clock_calls = 0

  def monotonic() -> float:
    nonlocal clock_calls
    clock_calls += 1
    if clock_calls == 4:
      raise RuntimeError("completion clock unavailable")
    return 0.0

  value = runtime(
    tmp_path,
    selected=snapshot(database_frames=FRAMES[:1]),
    monotonic=monotonic,
  )
  result = evaluate(value, authorized_time=network_time())

  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL
  )
  assert result.database_write_attempt_count == 1
  assert result.accepted_frame_count == 1
  assert result.failure_phase == "transfer_completion_clock"
  assert result.execution_error == (
    "RuntimeError:completion clock unavailable"
  )
  assert result.transfer_completed_at is None


def test_acquisition_during_pre_database_configuration_closes_window(
  tmp_path: Path,
) -> None:
  value = runtime(tmp_path)
  writes: list[tuple[bytes, int]] = []

  # Models RAWX/NAV-SAT dispatched by a synchronous MON-VER or NAVX5
  # transaction before initialize_receiver_cycle reaches its DBD decision.
  value.note_acquisition_started()
  result = evaluate(
    value,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )

  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_ACQUISITION_ALREADY_STARTED
  assert writes == []
  assert value.controller.terminal


# COMMIT6_DBD_SAFETY_TESTS

def test_conservative_age_includes_uncertainty_and_elapsed_time(
  tmp_path: Path,
) -> None:
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(
      NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS - 15.0
    ),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: 105.0,
  )
  authorized = AuthorizedTime(
    utc=NOW,
    uncertainty_seconds=10.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    independent=True,
    evidence=TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED,
    observed_boottime_seconds=100.0,
  )
  result = evaluate(value, authorized_time=authorized)
  assert result.disposition is NavigationDatabaseRestoreDisposition.RESTORED
  assert result.cache_age_seconds == NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS


def test_conservative_age_one_second_over_boundary_skips(
  tmp_path: Path,
) -> None:
  writes: list[tuple[bytes, int]] = []
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(
      NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS - 15.0
    ),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: 106.0,
  )
  authorized = AuthorizedTime(
    utc=NOW,
    uncertainty_seconds=10.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    independent=True,
    evidence=TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED,
    observed_boottime_seconds=100.0,
  )
  result = evaluate(
    value,
    authorized_time=authorized,
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED
  assert result.cache_age_seconds == NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS + 1.0
  assert writes == []


def test_acquisition_claim_failure_is_reported_before_receiver_start(
  tmp_path: Path,
) -> None:
  calls = 0
  def fail_claim(state, path):
    nonlocal calls
    calls += 1
    if calls >= 2:
      raise OSError("disk failure")
    store_navigation_database_restore_boot_state(state, path)

  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: None,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    state_storer=fail_claim,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  assert not value.claim_acquisition_start()
  assert value.acquisition_started
  assert value.execution.state_persistence_error is not None


def test_yuma_is_blocked_until_dbd_is_terminal_then_persists(
  tmp_path: Path,
) -> None:
  first = runtime(tmp_path)
  first.prepare()
  assert first.database_restore_pending
  assert not first.claim_yuma_transmission()
  assert first.close_restore_window_no_trusted_time()
  assert not first.database_restore_pending
  assert first.claim_yuma_transmission()
  second = runtime(tmp_path)
  writes: list[tuple[bytes, int]] = []
  result = evaluate(
    second,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )
  assert second.yuma_sent
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME
  assert writes == []

# COMMIT7_DBD_FRAME_BOUNDARY_TESTS

def test_missing_observation_boottime_fails_closed(tmp_path: Path) -> None:
  writes: list[tuple[bytes, int]] = []
  value = runtime(tmp_path)
  authorized = AuthorizedTime(
    utc=NOW,
    uncertainty_seconds=1.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    independent=True,
    evidence=TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED,
    observed_boottime_seconds=None,
  )
  result = evaluate(value, authorized_time=authorized, send=lambda frame, index: writes.append((frame, index)))
  assert result.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  assert writes == []


def test_cache_age_is_rechecked_after_restore_claim_before_frame_zero(tmp_path: Path) -> None:
  boottimes = [TEST_BOOTTIME_SECONDS, TEST_BOOTTIME_SECONDS + 2.0]
  def read_boottime() -> float:
    return boottimes.pop(0) if boottimes else TEST_BOOTTIME_SECONDS + 2.0
  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS - 1.0),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=read_boottime,
  )
  authorized = AuthorizedTime(
    utc=NOW,
    uncertainty_seconds=0.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    independent=True,
    evidence=TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED,
    observed_boottime_seconds=TEST_BOOTTIME_SECONDS,
  )
  receiver_writes: list[tuple[bytes, int]] = []
  def guarded_send(frame: bytes, index: int) -> None:
    value.validate_database_write_boundary(index)
    receiver_writes.append((frame, index))
  result = evaluate(value, authorized_time=authorized, send=guarded_send)
  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.RESTORE_CACHE_EXPIRED
  )
  assert receiver_writes == []
  assert result.permanent_failures
  assert all(
    failure.kind
    is NavigationDatabaseRestoreFrameFailureKind.VALIDATION_ERROR
    for failure in result.permanent_failures
  )

# COMMIT8_DBD_PENDING_RESTART_TEST

def test_failed_acquisition_persistence_cannot_reopen_after_restart(
  tmp_path: Path,
) -> None:
  path = tmp_path / "dbd_state.json"

  def fail_acquisition_state(
    state: NavigationDatabaseRestoreBootState,
    state_path: Path,
  ) -> None:
    if state.acquisition_started:
      raise OSError("disk failure")
    store_navigation_database_restore_boot_state(state, state_path)

  first = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=path,
    boot_id_reader=lambda: BOOT_ID,
    state_storer=fail_acquisition_state,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  assert not first.claim_acquisition_start()
  assert first.acquisition_started

  stale = load_navigation_database_restore_boot_state(path)
  assert stale is not None
  assert stale.disposition is NavigationDatabaseRestoreDisposition.PENDING
  assert not stale.acquisition_started

  writes: list[tuple[bytes, int]] = []
  second = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  result = evaluate(
    second,
    authorized_time=network_time(),
    send=lambda frame, index: writes.append((frame, index)),
  )

  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )
  assert writes == []

  recovered = load_navigation_database_restore_boot_state(path)
  assert recovered is not None
  assert (
    recovered.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )

# COMMIT9_COMPLETE_DURABLE_BOOT_BASELINE_TESTS


def test_boot_id_reader_exception_aborts_initialization(
  tmp_path: Path,
) -> None:
  def fail_boot_id() -> str:
    raise OSError("boot identity unavailable")

  with pytest.raises(
    NavigationDatabaseRestoreInitializationError,
    match="boot_id_read_failed",
  ):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      state_path=tmp_path / "state.json",
      boot_id_reader=fail_boot_id,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    )


def test_state_loader_exception_aborts_without_overwriting_state(
  tmp_path: Path,
) -> None:
  path = tmp_path / "dbd_state.json"
  path.write_text("unreadable-state-sentinel", encoding="utf-8")

  def fail_load(_path: Path) -> NavigationDatabaseRestoreBootState | None:
    raise OSError("state unavailable")

  with pytest.raises(
    NavigationDatabaseRestoreInitializationError,
    match="state_load_failed",
  ):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      state_path=path,
      boot_id_reader=lambda: BOOT_ID,
      state_loader=fail_load,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    )

  assert path.read_text(encoding="utf-8") == "unreadable-state-sentinel"


def test_invalid_state_loader_result_aborts_initialization(
  tmp_path: Path,
) -> None:
  with pytest.raises(
    NavigationDatabaseRestoreInitializationError,
    match="state_load_returned_invalid_type",
  ):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      state_path=tmp_path / "state.json",
      boot_id_reader=lambda: BOOT_ID,
      state_loader=lambda _path: object(),  # type: ignore[arg-type, return-value, ty:invalid-argument-type]
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    )


def test_missing_state_baseline_write_failure_aborts_initialization(
  tmp_path: Path,
) -> None:
  path = tmp_path / "dbd_state.json"

  def fail_write(
    _state: NavigationDatabaseRestoreBootState,
    _path: Path,
  ) -> None:
    raise OSError("storage unavailable")

  with pytest.raises(
    NavigationDatabaseRestoreInitializationError,
    match="current_boot_baseline_persist_failed",
  ):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      state_path=path,
      boot_id_reader=lambda: BOOT_ID,
      state_storer=fail_write,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    )

  assert not path.exists()


def test_previous_boot_replacement_failure_aborts_initialization(
  tmp_path: Path,
) -> None:
  path = tmp_path / "dbd_state.json"
  previous = NavigationDatabaseRestoreBootState(
    version=NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
    boot_id=OTHER_BOOT_ID,
    receiver_fingerprint=TEST_RECEIVER_FINGERPRINT,
    disposition=NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED,
    restore_attempted=False,
    position_assistance_claimed=True,
    acquisition_started=True,
    yuma_sent=True,
  )
  store_navigation_database_restore_boot_state(previous, path)

  def fail_write(
    _state: NavigationDatabaseRestoreBootState,
    _path: Path,
  ) -> None:
    raise OSError("storage unavailable")

  with pytest.raises(
    NavigationDatabaseRestoreInitializationError,
    match="current_boot_baseline_persist_failed",
  ):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      state_path=path,
      boot_id_reader=lambda: BOOT_ID,
      state_storer=fail_write,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    )

  assert load_navigation_database_restore_boot_state(path) == previous


def test_storage_recovery_later_process_establishes_fresh_baseline(
  tmp_path: Path,
) -> None:
  path = tmp_path / "dbd_state.json"
  storage_available = False

  def conditional_write(
    state: NavigationDatabaseRestoreBootState,
    state_path: Path,
  ) -> None:
    if not storage_available:
      raise OSError("storage unavailable")
    store_navigation_database_restore_boot_state(state, state_path)

  with pytest.raises(NavigationDatabaseRestoreInitializationError):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      state_path=path,
      boot_id_reader=lambda: BOOT_ID,
      state_storer=conditional_write,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    )
  assert not path.exists()

  storage_available = True
  recovered = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    state_path=path,
    boot_id_reader=lambda: BOOT_ID,
    state_storer=conditional_write,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  assert recovered.controller.pending
  persisted = load_navigation_database_restore_boot_state(path)
  assert persisted is not None
  assert persisted.boot_id == BOOT_ID
  assert persisted.receiver_fingerprint == TEST_RECEIVER_FINGERPRINT
  assert persisted.disposition is NavigationDatabaseRestoreDisposition.PENDING


def test_previous_boot_replacement_succeeds_after_storage_recovers(
  tmp_path: Path,
) -> None:
  path = tmp_path / "dbd_state.json"
  previous = NavigationDatabaseRestoreBootState(
    version=NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
    boot_id=OTHER_BOOT_ID,
    receiver_fingerprint=TEST_RECEIVER_FINGERPRINT,
    disposition=NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED,
    restore_attempted=False,
    position_assistance_claimed=True,
    acquisition_started=True,
    yuma_sent=True,
  )
  store_navigation_database_restore_boot_state(previous, path)
  storage_available = False

  def conditional_write(
    state: NavigationDatabaseRestoreBootState,
    state_path: Path,
  ) -> None:
    if not storage_available:
      raise OSError("storage unavailable")
    store_navigation_database_restore_boot_state(state, state_path)

  with pytest.raises(NavigationDatabaseRestoreInitializationError):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      state_path=path,
      boot_id_reader=lambda: BOOT_ID,
      state_storer=conditional_write,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    )
  assert load_navigation_database_restore_boot_state(path) == previous

  storage_available = True
  recovered = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    state_path=path,
    boot_id_reader=lambda: BOOT_ID,
    state_storer=conditional_write,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  assert recovered.controller.pending
  persisted = load_navigation_database_restore_boot_state(path)
  assert persisted is not None
  assert persisted.boot_id == BOOT_ID
  assert persisted.disposition is NavigationDatabaseRestoreDisposition.PENDING



@pytest.mark.parametrize(
  ("method_name", "expected"),
  (
    (
      "close_restore_window_no_trusted_time",
      NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME,
    ),
    (
      "close_restore_window_wait_timeout",
      NavigationDatabaseRestoreDisposition.SKIPPED_WAIT_TIMEOUT,
    ),
  ),
)
def test_trusted_time_terminal_outcomes_persist_across_restart(
  tmp_path: Path,
  method_name: str,
  expected: NavigationDatabaseRestoreDisposition,
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
  first.prepare()

  assert getattr(first, method_name)()
  assert first.controller.disposition is expected
  assert not first.controller.restore_attempted

  second = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  second.prepare()

  assert second.controller.disposition is expected
  assert not second.controller.restore_attempted


# PR64_COMMIT2_EARLY_ACQUISITION_DBD_POLICY


def test_bootstrap_acquisition_is_durable_and_preserves_one_position_write(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  first = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: fresh_position_snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  first.prepare()

  assert first.note_early_acquisition_started()
  assert first.acquisition_started
  assert (
    first.controller.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_EARLY_ACQUISITION
  )

  position_writes: list[bytes] = []
  authorize_position(first)
  first_result = first.send_position_once(position_writes.append)
  assert len(position_writes) == 1
  assert first_result.position_assistance_succeeded

  second = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: fresh_position_snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  second.prepare()

  assert second.acquisition_started
  assert (
    second.controller.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_EARLY_ACQUISITION
  )
  database_writes: list[tuple[bytes, int]] = []
  result = second.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=lambda frame, index, _mark: database_writes.append((frame, index)),
  )
  second_position_writes: list[bytes] = []
  authorize_position(second)
  second.send_position_once(second_position_writes.append)

  assert result.database_write_attempt_count == 0
  assert database_writes == []
  assert second_position_writes == []


def test_pre_restore_drain_failure_is_durable_transport_error(
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
  first.prepare()

  result = first.record_pre_restore_transport_error(
    authorized_time=network_time(),
    error=OSError("drain failed"),
    phase="pre_restore_drain",
  )

  assert (
    result.disposition
    is NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR
  )
  assert result.database_write_attempt_count == 0
  assert result.accepted_frame_count == 0
  assert result.execution_error == "OSError:drain failed"
  assert result.failure_phase == "pre_restore_drain"

  second = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  second.prepare()
  restored = second.execution

  assert restored.disposition is result.disposition
  assert restored.database_write_attempt_count == 0
  assert restored.execution_error == result.execution_error
  assert restored.failure_phase == result.failure_phase


def test_early_acquisition_closes_only_database_restore_window(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  first = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: fresh_position_snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  first.prepare()

  assert first.close_restore_window_for_early_acquisition()
  assert (
    first.controller.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_EARLY_ACQUISITION
  )
  assert not first.acquisition_started

  first_position_writes: list[bytes] = []
  authorize_position(first)
  first_result = first.send_position_once(
    first_position_writes.append
  )

  assert first_result.position_assistance_attempted
  assert first_result.position_assistance_succeeded
  assert len(first_position_writes) == 1
  assert not first.acquisition_started

  second = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: fresh_position_snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )
  second.prepare()

  assert (
    second.controller.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_EARLY_ACQUISITION
  )
  assert not second.acquisition_started

  database_writes: list[tuple[bytes, int]] = []
  second_result = second.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=(
      lambda frame, index, _mark: database_writes.append((frame, index))
    ),
  )

  assert (
    second_result.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_EARLY_ACQUISITION
  )
  assert database_writes == []
  assert second_result.database_write_attempt_count == 0

  second_position_writes: list[bytes] = []
  authorize_position(second)
  second_result = second.send_position_once(
    second_position_writes.append
  )

  assert second_position_writes == []
  assert not second_result.position_assistance_attempted
  assert not second.acquisition_started

def test_unavailable_runtime_blocks_assistance_but_allows_acquisition() -> None:
  value = NavigationDatabaseRestoreUnavailableRuntime(
    TEST_RECEIVER_FINGERPRINT,
    "boot_state:storage_unavailable",
  )
  database_writes: list[tuple[bytes, int]] = []
  position_writes: list[bytes] = []

  execution = value.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=(
      lambda message, index, _mark: database_writes.append((message, index))
    ),
  )
  value.send_position_once(position_writes.append)

  assert not value.state_available
  assert (
    execution.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_STATE_UNAVAILABLE
  )
  assert execution.state_persistence_error == (
    "boot_state:storage_unavailable"
  )
  assert database_writes == []
  assert position_writes == []
  assert not value.claim_yuma_transmission()
  assert not value.claim_acquisition_start()
  assert value.acquisition_started


def test_state_failure_remains_latched_after_storage_recovers(
  tmp_path: Path,
) -> None:
  store_calls = 0

  def fail_once_then_recover(
    state: NavigationDatabaseRestoreBootState,
    path: Path,
  ) -> None:
    nonlocal store_calls
    store_calls += 1
    if store_calls == 2:
      raise OSError("transient disk failure")
    store_navigation_database_restore_boot_state(state, path)

  value = NavigationDatabaseRestoreRuntime(
    TEST_RECEIVER_FINGERPRINT,
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    state_storer=fail_once_then_recover,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  value.prepare()
  assert not value.state_available
  assert value.assistance_state_disabled_reason == "OSError:transient disk failure"

  database_writes: list[tuple[bytes, int]] = []
  position_writes: list[bytes] = []
  value.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=(
      lambda message, index, _mark: database_writes.append((message, index))
    ),
  )
  value.send_position_once(position_writes.append)

  assert database_writes == []
  assert position_writes == []
  assert not value.claim_yuma_transmission()
  assert not value.claim_acquisition_start()
  assert value.acquisition_started
  assert store_calls == 2


@pytest.mark.parametrize(
  "transfer_budget_seconds",
  (0.0, -1.0, float("nan"), float("inf"), True),
)
def test_runtime_rejects_invalid_transfer_budget(
  tmp_path: Path,
  transfer_budget_seconds: object,
) -> None:
  with pytest.raises(ValueError):
    NavigationDatabaseRestoreRuntime(
      TEST_RECEIVER_FINGERPRINT,
      snapshot_loader=lambda _fingerprint: snapshot(),
      transfer_budget_seconds=transfer_budget_seconds,  # type: ignore[arg-type, ty:invalid-argument-type]
      state_path=tmp_path / "state.json",
      boot_id_reader=lambda: BOOT_ID,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
    )
