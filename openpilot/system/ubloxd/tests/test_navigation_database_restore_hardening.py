from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest

from openpilot.system.ubloxd.gps_assistance import (
  NavigationQuality,
  QUALITY_POLICY_VERSION,
  QUALITY_VERSION,
)

from openpilot.system.ubloxd.navigation_database_restore import (
  NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS,
  NavigationDatabaseRestoreDisposition,
)
from openpilot.system.ubloxd.navigation_database_restore_runtime import (
  NavigationDatabaseRestoreFrameFailureKind,
  NavigationDatabaseRestoreInitializationError,
  NavigationDatabaseRestoreRuntime,
  NavigationDatabaseRestoreSnapshot,
  load_navigation_database_restore_boot_state,
  navigation_database_restore_state_quarantine_exists,
  quarantine_navigation_database_restore_boot_state,
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
OTHER_BOOT_ID = "87654321-4321-6789-9234-567812345678"
NOW = datetime(2026, 7, 30, 18, 0, tzinfo=UTC)
TEST_BOOTTIME_SECONDS = 100.0


def network_time() -> AuthorizedTime:
  return AuthorizedTime(
    utc=NOW,
    uncertainty_seconds=0.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    independent=True,
    evidence=TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED,
    observed_boottime_seconds=TEST_BOOTTIME_SECONDS,
  )


def snapshot(
  *,
  frame_count: int = 2,
  age_seconds: float = 1800.0,
) -> NavigationDatabaseRestoreSnapshot:
  return NavigationDatabaseRestoreSnapshot(
    saved_at_utc=NOW - timedelta(seconds=age_seconds),
    database_frames=tuple(
      index.to_bytes(2, "big")
      for index in range(frame_count)
    ),
    latitude_e7=320_000_000,
    longitude_e7=-960_000_000,
    altitude_cm=20_000,
    position_accuracy_cm=10_000,
    quality=NavigationQuality(
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
    ),
    generation="primary",
    selection_reason="hardening_test",
  )


def future_version_state() -> str:
  return json.dumps(
    {
      "version": 999,
      "boot_id": BOOT_ID,
      "receiver_fingerprint": "receiver",
      "disposition": NavigationDatabaseRestoreDisposition.PENDING.value,
      "restore_attempted": False,
      "position_assistance_claimed": False,
      "acquisition_started": False,
      "yuma_sent": False,
      "candidate_identities": [],
      "cache_generation": None,
      "cache_saved_at_utc": None,
    }
  )



def deeply_nested_state() -> str:
  depth = 10_000
  return "[" * depth + "0" + "]" * depth


def oversized_integer_state() -> str:
  return "9" * 5000


@pytest.mark.parametrize(
  "invalid_contents",
  (
    "not-json",
    future_version_state(),
    deeply_nested_state(),
    oversized_integer_state(),
  ),
  ids=(
    "malformed_json",
    "unsupported_future_version",
    "excessive_json_nesting",
    "oversized_json_integer",
  ),
)
def test_invalid_state_is_quarantined_and_same_boot_stays_closed(
  tmp_path: Path,
  invalid_contents: str,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  state_path.write_text(invalid_contents, encoding="utf-8")

  first = NavigationDatabaseRestoreRuntime(
    "receiver",
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  writes: list[tuple[bytes, int]] = []
  first_result = first.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=lambda frame, index, _mark: writes.append((frame, index)),
  )

  assert (
    first_result.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )
  assert writes == []

  persisted = load_navigation_database_restore_boot_state(state_path)
  assert persisted is not None
  assert persisted.boot_id == BOOT_ID
  assert (
    persisted.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )

  quarantined = list(tmp_path.glob(f"{state_path.name}.invalid-*"))
  assert len(quarantined) == 1
  assert quarantined[0].read_text(encoding="utf-8") == invalid_contents

  second = NavigationDatabaseRestoreRuntime(
    "receiver",
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  second_writes: list[tuple[bytes, int]] = []
  second_result = second.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=lambda frame, index, _mark: second_writes.append(
      (frame, index)
    ),
  )

  assert (
    second_result.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )
  assert second_writes == []

  next_boot = NavigationDatabaseRestoreRuntime(
    "receiver",
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: OTHER_BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  assert next_boot.controller.pending


def test_acquisition_boundary_stops_after_first_frame(
  tmp_path: Path,
) -> None:
  value = NavigationDatabaseRestoreRuntime(
    "receiver",
    snapshot_loader=lambda _fingerprint: snapshot(frame_count=4096),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  attempted_indexes: list[int] = []
  receiver_writes: list[tuple[bytes, int]] = []

  def guarded_send(frame: bytes, index: int, mark_write_attempt) -> None:
    attempted_indexes.append(index)

    if index == 0:
      assert value.note_acquisition_started()

    value.validate_database_write_boundary(index)
    mark_write_attempt()
    receiver_writes.append((frame, index))

  result = value.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=guarded_send,
  )

  assert result.disposition is NavigationDatabaseRestoreDisposition.WRITE_FAILED
  assert attempted_indexes == [0]
  assert receiver_writes == []
  assert result.database_write_attempt_count == 0
  assert len(result.permanent_failures) == 1
  assert (
    result.permanent_failures[0].kind
    is NavigationDatabaseRestoreFrameFailureKind.VALIDATION_ERROR
  )


def test_expired_cache_boundary_stops_after_first_frame(
  tmp_path: Path,
) -> None:
  boottimes = [
    TEST_BOOTTIME_SECONDS,
    TEST_BOOTTIME_SECONDS,
    TEST_BOOTTIME_SECONDS + 2.0,
  ]

  def read_boottime() -> float:
    if boottimes:
      return boottimes.pop(0)

    return TEST_BOOTTIME_SECONDS + 2.0

  value = NavigationDatabaseRestoreRuntime(
    "receiver",
    snapshot_loader=lambda _fingerprint: snapshot(
      frame_count=4096,
      age_seconds=NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS - 1.0,
    ),
    retry_delay_seconds=0.0,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=read_boottime,
  )

  attempted_indexes: list[int] = []
  receiver_writes: list[tuple[bytes, int]] = []

  def guarded_send(frame: bytes, index: int, mark_write_attempt) -> None:
    attempted_indexes.append(index)
    value.validate_database_write_boundary(index)
    mark_write_attempt()
    receiver_writes.append((frame, index))

  result = value.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=guarded_send,
  )

  assert result.disposition is NavigationDatabaseRestoreDisposition.RESTORE_CACHE_EXPIRED
  assert attempted_indexes == [0]
  assert receiver_writes == []
  assert result.database_write_attempt_count == 0
  assert len(result.permanent_failures) == 1
  assert (
    result.permanent_failures[0].kind
    is NavigationDatabaseRestoreFrameFailureKind.VALIDATION_ERROR
  )






def test_terminal_boundary_clears_previously_queued_retries(
  tmp_path: Path,
) -> None:
  sleep_delays: list[float] = []
  value = NavigationDatabaseRestoreRuntime(
    "receiver",
    snapshot_loader=lambda _fingerprint: snapshot(frame_count=3),
    retry_delay_seconds=0.25,
    sleeper=sleep_delays.append,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  attempted_indexes: list[int] = []

  def guarded_send(_frame: bytes, index: int, mark_write_attempt) -> None:
    attempted_indexes.append(index)

    if index == 0:
      mark_write_attempt()
      raise TimeoutError("retryable frame-zero timeout")
    if index == 1:
      assert value.note_acquisition_started()
      value.validate_database_write_boundary(index)

    mark_write_attempt()
    raise AssertionError("terminal boundary did not stop the initial pass")

  result = value.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=guarded_send,
  )

  assert result.disposition is NavigationDatabaseRestoreDisposition.WRITE_FAILED
  assert attempted_indexes == [0, 1]
  assert sleep_delays == []
  assert result.database_write_attempt_count == 1
  assert tuple(
    failure.frame_index
    for failure in result.initial_failures
  ) == (0, 1)
  assert result.initial_failures[0].kind is (
    NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT
  )
  assert result.initial_failures[1].kind is (
    NavigationDatabaseRestoreFrameFailureKind.VALIDATION_ERROR
  )
  assert tuple(
    failure.frame_index
    for failure in result.permanent_failures
  ) == (1,)
  assert result.first_failure is not None
  assert result.first_failure.frame_index == 0
  assert result.first_failure.attempt == 1
  assert (
    result.first_failure.kind
    is NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT
  )


def test_terminal_boundary_stops_remaining_retry_frames(
  tmp_path: Path,
) -> None:
  def start_acquisition_after_retry_delay(_delay: float) -> None:
    value.note_acquisition_started()

  value = NavigationDatabaseRestoreRuntime(
    "receiver",
    snapshot_loader=lambda _fingerprint: snapshot(frame_count=2),
    retry_delay_seconds=0.25,
    sleeper=start_acquisition_after_retry_delay,
    state_path=tmp_path / "dbd_state.json",
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  attempted_indexes: list[int] = []

  def retryable_send(_frame: bytes, index: int, mark_write_attempt) -> None:
    attempted_indexes.append(index)
    mark_write_attempt()
    raise TimeoutError(f"frame {index} timeout")

  result = value.evaluate(
    authorized_time=network_time(),
    reliable_fix_available=False,
    yuma_already_sent=False,
    send_database_message=retryable_send,
  )

  assert result.disposition is NavigationDatabaseRestoreDisposition.WRITE_FAILED
  assert attempted_indexes == [0, 1]
  assert result.database_write_attempt_count == 2
  assert result.failure_phase == "retry_pass"
  assert tuple(
    failure.frame_index
    for failure in result.initial_failures
  ) == (0, 1)
  assert len(result.permanent_failures) == 1
  assert result.permanent_failures[0].frame_index == 0
  assert result.permanent_failures[0].attempt == 2
  assert result.permanent_failures[0].kind is (
    NavigationDatabaseRestoreFrameFailureKind.VALIDATION_ERROR
  )


def test_state_quarantine_is_atomic_and_boot_scoped(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  state_path.write_text("invalid-state", encoding="utf-8")

  quarantine_path = (
    quarantine_navigation_database_restore_boot_state(
      state_path,
      BOOT_ID,
    )
  )

  assert not state_path.exists()
  assert quarantine_path.parent == tmp_path
  assert quarantine_path.read_text(
    encoding="utf-8"
  ) == "invalid-state"

  assert navigation_database_restore_state_quarantine_exists(
    state_path,
    BOOT_ID,
  )
  assert not navigation_database_restore_state_quarantine_exists(
    state_path,
    OTHER_BOOT_ID,
  )



def test_invalid_state_quarantine_failure_remains_fatal(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  state_path.write_text("not-json", encoding="utf-8")

  def fail_quarantine(
    _path: Path,
    _boot_id: str,
  ) -> Path:
    raise OSError("quarantine unavailable")

  with pytest.raises(
    NavigationDatabaseRestoreInitializationError,
    match="state_quarantine_failed",
  ):
    NavigationDatabaseRestoreRuntime(
      "receiver",
      snapshot_loader=lambda _fingerprint: snapshot(),
      retry_delay_seconds=0.0,
      state_path=state_path,
      boot_id_reader=lambda: BOOT_ID,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
      state_quarantiner=fail_quarantine,
    )

  assert state_path.read_text(
    encoding="utf-8"
  ) == "not-json"


def test_terminal_state_write_failure_stays_closed_on_restart(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "dbd_state.json"
  state_path.write_text("not-json", encoding="utf-8")

  def fail_store(
    _state: object,
    _path: Path,
  ) -> None:
    raise OSError("terminal state unavailable")

  with pytest.raises(
    NavigationDatabaseRestoreInitializationError,
    match="terminal_state_persist_failed",
  ):
    NavigationDatabaseRestoreRuntime(
      "receiver",
      snapshot_loader=lambda _fingerprint: snapshot(),
      retry_delay_seconds=0.0,
      state_path=state_path,
      boot_id_reader=lambda: BOOT_ID,
      boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
      state_storer=fail_store,
    )

  quarantined = list(
    tmp_path.glob(f"{state_path.name}.invalid-*")
  )
  assert len(quarantined) == 1
  assert not state_path.exists()

  recovered = NavigationDatabaseRestoreRuntime(
    "receiver",
    snapshot_loader=lambda _fingerprint: snapshot(),
    retry_delay_seconds=0.0,
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
    boottime_reader=lambda: TEST_BOOTTIME_SECONDS,
  )

  assert (
    recovered.controller.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )

  persisted = load_navigation_database_restore_boot_state(
    state_path
  )
  assert persisted is not None
  assert (
    persisted.disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
  )
