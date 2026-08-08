from dataclasses import replace
from datetime import UTC, datetime, timedelta

from openpilot.common.time_helpers import (
  HostTimeObservation,
  HostTimeSource,
)

import pytest

from openpilot.system.ubloxd.rtc_time_observation import (
  RtcObservationReason,
  RtcObservationState,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  RtcVoltageStatus,
  TimeProvenance,
  TrustedTimeAnchor,
  TrustedTimeAnchorStore,
  TrustedTimeSource,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AnchorWriteStatus,
  TimeAuthorizationEvidence,
  TimeAuthority,
  TimeAuthorityRejectionReason,
)

BOOT_ID = "12345678-1234-5678-9234-567812345678"
OTHER_BOOT_ID = "87654321-4321-6789-9234-567812345678"
NOW = datetime(2026, 7, 22, 21, tzinfo=UTC)


def network_host(
  *,
  utc: datetime = NOW,
  boottime: float = 100.0,
  generation: str = "network:1",
) -> HostTimeObservation:
  return HostTimeObservation(
    utc=utc,
    observed_boottime_seconds=boottime,
    uncertainty_seconds=30.0,
    source=HostTimeSource.NETWORK_SYNCHRONIZED,
    independent=True,
    generation=generation,
  )


def anchor(
  *,
  boot_id: str = BOOT_ID,
  trusted_utc: datetime = NOW,
  boottime_seconds: float = 100.0,
  uncertainty_seconds: float = 30.0,
  sequence: int = 1,
) -> TrustedTimeAnchor:
  return TrustedTimeAnchor(
    version=1,
    trusted_utc=trusted_utc,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    authorized=True,
    independent=True,
    uncertainty_seconds=uncertainty_seconds,
    boot_id=boot_id,
    boottime_seconds=boottime_seconds,
    rtc_epoch_seconds=1_784_754_260,
    rtc_voltage_status_supported=False,
    rtc_voltage_status_flags=None,
    sequence=sequence,
  )


def authority(
  tmp_path,
  *,
  boot_id=BOOT_ID,
  boottime=100.0,
  utc_now=NOW,
  max_elapsed=7 * 24 * 60 * 60,
):
  store = TrustedTimeAnchorStore(
    tmp_path / "trusted_time_anchor.json"
  )
  return TimeAuthority(
    store,
    boot_id_reader=lambda: boot_id,
    boottime_reader=lambda: boottime,
    rtc_epoch_reader=lambda: 1_784_754_260,
    rtc_voltage_reader=lambda: RtcVoltageStatus(
      False,
      None,
    ),
    utc_now=lambda: utc_now,
    max_same_boot_elapsed_seconds=max_elapsed,
  ), store


def test_synchronized_system_time_has_priority_and_creates_anchor(
  tmp_path,
):
  evaluator, store = authority(
    tmp_path,
    boottime=125.0,
  )

  result = evaluator.current_authorized_time(
    host_time_observation=network_host(boottime=125.0)
  )

  authorized = result.authorized_time
  assert authorized is not None
  assert authorized.utc == NOW
  assert authorized.independent is True
  assert authorized.evidence is (
    TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED
  )
  assert authorized.source is (
    TrustedTimeSource.SYSTEM_SYNCHRONIZED
  )
  assert authorized.mga_accuracy_seconds == 30
  assert result.anchor_write_status is (
    AnchorWriteStatus.SAVED
  )

  selection, _ = store.load_best()
  assert selection is not None
  assert selection.anchor.trusted_utc == NOW
  assert selection.anchor.boot_id == BOOT_ID
  assert selection.anchor.boottime_seconds == 125.0
  assert selection.anchor.rtc_epoch_seconds == 1_784_754_260


def test_synchronized_time_remains_authorized_when_anchor_write_fails(
  tmp_path,
  monkeypatch,
):
  evaluator, store = authority(tmp_path)

  def fail_save(value):
    raise OSError("storage unavailable")

  monkeypatch.setattr(store, "save", fail_save)

  result = evaluator.current_authorized_time(
    host_time_observation=network_host()
  )

  assert result.authorized_time is not None
  assert result.rejection_reason is None
  assert result.anchor_write_status is AnchorWriteStatus.FAILED
  assert result.anchor_write_error is not None
  assert "OSError" in result.anchor_write_error


def test_synchronized_time_does_not_rewrite_current_boot_anchor(
  tmp_path,
):
  evaluator, store = authority(tmp_path)
  store.save(anchor())

  result = evaluator.current_authorized_time(
    host_time_observation=network_host()
  )

  assert result.authorized_time is not None
  assert result.anchor_write_status is (
    AnchorWriteStatus.PRESERVED_CURRENT_BOOT
  )
  selection, inventory = store.load_best()
  assert selection is not None
  assert selection.anchor.sequence == 1
  assert inventory.previous.anchor is None


def test_synchronized_time_operates_when_continuity_readers_missing(
  tmp_path,
):
  evaluator, store = authority(
    tmp_path,
    boot_id=None,
    boottime=None,
  )

  result = evaluator.current_authorized_time(
    host_time_observation=network_host()
  )

  assert result.authorized_time is not None
  assert result.anchor_write_status is (
    AnchorWriteStatus.READERS_UNAVAILABLE
  )
  selection, _ = store.load_best()
  assert selection is None


def test_invalid_host_observation_fails_closed(tmp_path):
  evaluator, _ = authority(tmp_path)

  result = evaluator.current_authorized_time(
    host_time_observation=object()
  )

  assert result.authorized_time is None
  assert result.rejection_reason is (
    TimeAuthorityRejectionReason.INVALID_HOST_TIME
  )


def test_same_boot_continuity_uses_boottime_and_retains_provenance(
  tmp_path,
):
  evaluator, store = authority(
    tmp_path,
    boottime=160.0,
  )
  store.save(anchor())

  result = evaluator.current_authorized_time(
    host_time_observation=None
  )

  authorized = result.authorized_time
  assert authorized is not None
  assert authorized.utc == NOW + timedelta(seconds=60)
  assert authorized.evidence is (
    TimeAuthorizationEvidence.SAME_BOOT_BOOTTIME
  )
  assert authorized.independent is False
  assert authorized.source is (
    TrustedTimeSource.SYSTEM_SYNCHRONIZED
  )
  assert authorized.provenance is (
    TimeProvenance.NETWORK_INDEPENDENT
  )
  assert authorized.anchor_generation == "primary"
  assert authorized.anchor_sequence == 1
  assert authorized.elapsed_seconds == 60.0


def test_same_boot_uncertainty_grows_conservatively(tmp_path):
  evaluator, store = authority(
    tmp_path,
    boottime=10_100.0,
  )
  store.save(anchor(uncertainty_seconds=30.0))

  result = evaluator.current_authorized_time(
    host_time_observation=None
  )

  authorized = result.authorized_time
  assert authorized is not None
  assert authorized.uncertainty_seconds == 31.0
  assert authorized.mga_accuracy_seconds == 31


def test_different_boot_never_authorizes_rtc_subtraction(
  tmp_path,
):
  evaluator, store = authority(
    tmp_path,
    boot_id=OTHER_BOOT_ID,
  )
  store.save(anchor())

  result = evaluator.current_authorized_time(
    host_time_observation=None
  )

  assert result.authorized_time is None
  assert result.rejection_reason is (
    TimeAuthorityRejectionReason
    .CROSS_BOOT_CONTINUITY_UNPROVABLE
  )


@pytest.mark.parametrize(
  ("boot_id", "boottime", "reason"),
  (
    (
      None,
      100.0,
      TimeAuthorityRejectionReason.BOOT_ID_UNAVAILABLE,
    ),
    (
      BOOT_ID,
      None,
      TimeAuthorityRejectionReason.BOOTTIME_UNAVAILABLE,
    ),
  ),
)
def test_missing_continuity_reader_rejects_anchor(
  tmp_path,
  boot_id,
  boottime,
  reason,
):
  evaluator, store = authority(
    tmp_path,
    boot_id=boot_id,
    boottime=boottime,
  )
  store.save(anchor())

  result = evaluator.current_authorized_time(
    host_time_observation=None
  )

  assert result.authorized_time is None
  assert result.rejection_reason is reason


def test_missing_anchor_rejects_same_boot_continuity(tmp_path):
  evaluator, _ = authority(tmp_path)

  result = evaluator.current_authorized_time(
    host_time_observation=None
  )

  assert result.authorized_time is None
  assert result.rejection_reason is (
    TimeAuthorityRejectionReason.ANCHOR_UNAVAILABLE
  )


def test_boottime_rollback_rejects_same_boot_continuity(
  tmp_path,
):
  evaluator, store = authority(
    tmp_path,
    boottime=99.0,
  )
  store.save(anchor())

  result = evaluator.current_authorized_time(
    host_time_observation=None
  )

  assert result.authorized_time is None
  assert result.rejection_reason is (
    TimeAuthorityRejectionReason.BOOTTIME_ROLLBACK
  )


def test_same_boot_elapsed_limit_is_enforced(tmp_path):
  evaluator, store = authority(
    tmp_path,
    boottime=111.0,
    max_elapsed=10.0,
  )
  store.save(anchor())

  result = evaluator.current_authorized_time(
    host_time_observation=None
  )

  assert result.authorized_time is None
  assert result.rejection_reason is (
    TimeAuthorityRejectionReason
    .ELAPSED_TIME_ABOVE_MAXIMUM
  )


def test_new_boot_synchronized_time_replaces_old_boot_anchor(
  tmp_path,
):
  evaluator, store = authority(
    tmp_path,
    boot_id=OTHER_BOOT_ID,
    boottime=25.0,
  )
  store.save(anchor())

  result = evaluator.current_authorized_time(
    host_time_observation=network_host(boottime=25.0)
  )

  assert result.authorized_time is not None
  assert result.anchor_write_status is AnchorWriteStatus.SAVED
  selection, inventory = store.load_best()
  assert selection is not None
  assert selection.anchor.boot_id == OTHER_BOOT_ID
  assert selection.anchor.sequence == 2
  assert inventory.previous.anchor is not None
  assert inventory.previous.anchor.boot_id == BOOT_ID


def test_previous_generation_same_boot_anchor_is_selected(
  tmp_path,
):
  evaluator, store = authority(
    tmp_path,
    boottime=200.0,
  )
  store.save(anchor(sequence=1))
  store.save(replace(
    anchor(sequence=2),
    boot_id=OTHER_BOOT_ID,
  ))

  result = evaluator.current_authorized_time(
    host_time_observation=None
  )

  authorized = result.authorized_time
  assert authorized is not None
  assert authorized.anchor_generation == "previous"
  assert authorized.anchor_sequence == 1



def test_cross_boot_observation_never_authorizes_time(
  tmp_path,
):
  store = TrustedTimeAnchorStore(
    tmp_path / "trusted_time_anchor.json"
  )
  store.save(anchor())
  boottimes = iter((10.0, 10.0, 12.0))
  rtc_values = iter((1_784_754_360, 1_784_754_362))
  evaluator = TimeAuthority(
    store,
    boot_id_reader=lambda: OTHER_BOOT_ID,
    boottime_reader=lambda: next(boottimes),
    rtc_epoch_reader=lambda: next(rtc_values),
    rtc_voltage_reader=lambda: RtcVoltageStatus(False, None),
    utc_now=lambda: NOW,
  )
  primary_before = store.primary_path.read_bytes()

  authorization = evaluator.current_authorized_time(
    host_time_observation=None
  )
  observer = evaluator.create_cross_boot_rtc_observer(
    tick_interval_seconds=2.0
  )
  pending = observer.current_observation(50.0)
  observed = observer.current_observation(52.0)

  assert authorization.authorized_time is None
  assert authorization.rejection_reason is (
    TimeAuthorityRejectionReason
    .CROSS_BOOT_CONTINUITY_UNPROVABLE
  )
  assert pending.state is RtcObservationState.PENDING_TICK
  assert observed.state is RtcObservationState.OBSERVED
  assert observed.candidate is not None
  assert not observed.authorized
  assert not observed.operational
  assert not observed.candidate.authorized
  assert not observed.candidate.operational
  assert store.primary_path.read_bytes() == primary_before
  assert not store.previous_path.exists()


def test_same_boot_authorization_has_no_cross_boot_candidate(
  tmp_path,
):
  evaluator, store = authority(
    tmp_path,
    boottime=160.0,
  )
  store.save(anchor())

  authorized = evaluator.current_authorized_time(
    host_time_observation=None
  )
  observation = (
    evaluator.create_cross_boot_rtc_observer()
    .current_observation(50.0)
  )

  assert authorized.authorized_time is not None
  assert observation.state is RtcObservationState.NOT_APPLICABLE
  assert observation.reason is RtcObservationReason.SAME_BOOT_ONLY
  assert observation.candidate is None


def test_receiver_independent_utc_is_authorized_and_saved(tmp_path):
  evaluator, store = authority(tmp_path, boottime=200.0)

  result = evaluator.observe_independent_time(
    utc=NOW + timedelta(seconds=100),
    uncertainty_seconds=0.025,
    source=(
      TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
    ),
    provenance=TimeProvenance.GNSS_INDEPENDENT,
    observed_boottime_seconds=200.0,
  )

  authorized = result.authorized_time
  assert authorized is not None
  assert authorized.independent
  assert authorized.source is (
    TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
  )
  assert authorized.evidence is (
    TimeAuthorizationEvidence.RECEIVER_UTC_UNASSISTED_GNSS
  )
  assert authorized.observed_boottime_seconds == 200.0
  assert result.anchor_write_status is AnchorWriteStatus.SAVED
  selection, _ = store.load_best()
  assert selection is not None
  assert selection.anchor.source is (
    TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
  )
  assert selection.anchor.provenance is (
    TimeProvenance.GNSS_INDEPENDENT
  )


def test_gnss_replaces_current_system_anchor(tmp_path):
  evaluator, store = authority(tmp_path, boottime=200.0)
  store.save(anchor())

  result = evaluator.observe_independent_time(
    utc=NOW + timedelta(seconds=100),
    uncertainty_seconds=0.025,
    source=(
      TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
    ),
    provenance=TimeProvenance.GNSS_INDEPENDENT,
    observed_boottime_seconds=200.0,
  )

  assert result.anchor_write_status is AnchorWriteStatus.SAVED
  assert result.anchor_write_reason is not None
  assert result.anchor_write_reason.value == (
    "more_authoritative_provenance"
  )
  selection, inventory = store.load_best()
  assert selection is not None
  assert selection.anchor.provenance is (
    TimeProvenance.GNSS_INDEPENDENT
  )
  assert inventory.previous.anchor is not None
  assert inventory.previous.anchor.provenance is (
    TimeProvenance.NETWORK_INDEPENDENT
  )


def test_repeated_system_synchronization_preserves_anchor(tmp_path):
  evaluator, store = authority(
    tmp_path,
    boottime=200.0,
    utc_now=NOW + timedelta(seconds=100),
  )
  store.save(anchor())
  before = store.primary_path.read_bytes()

  result = evaluator.current_authorized_time(
    host_time_observation=network_host(
      utc=NOW + timedelta(seconds=100),
      boottime=200.0,
    )
  )

  assert result.authorized_time is not None
  assert result.authorized_time.observed_boottime_seconds == 200.0
  assert result.anchor_write_status is (
    AnchorWriteStatus.PRESERVED_CURRENT_BOOT
  )
  assert result.anchor_write_reason is not None
  assert result.anchor_write_reason.value == (
    "existing_anchor_preserved"
  )
  assert store.primary_path.read_bytes() == before
  assert not store.previous_path.exists()


def test_lower_rank_system_does_not_replace_current_gnss_anchor(
  tmp_path,
):
  evaluator, store = authority(
    tmp_path,
    boottime=200.0,
    utc_now=NOW + timedelta(seconds=100),
  )
  store.save(replace(
    anchor(),
    source=(
      TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
    ),
    provenance=TimeProvenance.GNSS_INDEPENDENT,
    uncertainty_seconds=0.025,
  ))
  before = store.primary_path.read_bytes()

  result = evaluator.current_authorized_time(
    host_time_observation=network_host(
      utc=NOW + timedelta(seconds=100),
      boottime=200.0,
    )
  )

  assert result.authorized_time is not None
  assert result.anchor_write_status is (
    AnchorWriteStatus.PRESERVED_CURRENT_BOOT
  )
  assert store.primary_path.read_bytes() == before


def nonindependent_host(
  source: HostTimeSource,
  *,
  generation: str,
) -> HostTimeObservation:
  return HostTimeObservation(
    utc=NOW,
    observed_boottime_seconds=100.0,
    uncertainty_seconds=30.0,
    source=source,
    independent=False,
    generation=generation,
  )


def test_receiver_derived_host_time_is_not_independent(tmp_path):
  evaluator, store = authority(tmp_path)

  result = evaluator.current_authorized_time(
    host_time_observation=nonindependent_host(
      HostTimeSource.RECEIVER_DERIVED,
      generation="receiver:1",
    )
  )

  assert result.authorized_time is None
  assert result.rejection_reason is (
    TimeAuthorityRejectionReason.HOST_TIME_NOT_INDEPENDENT
  )
  assert result.anchor_write_status is (
    AnchorWriteStatus.NOT_REQUIRED
  )
  selection, _ = store.load_best()
  assert selection is None


def test_unknown_host_time_is_not_independent(tmp_path):
  evaluator, store = authority(tmp_path)

  result = evaluator.current_authorized_time(
    host_time_observation=nonindependent_host(
      HostTimeSource.UNKNOWN,
      generation="unknown:1",
    )
  )

  assert result.authorized_time is None
  assert result.rejection_reason is (
    TimeAuthorityRejectionReason.HOST_TIME_NOT_INDEPENDENT
  )
  selection, _ = store.load_best()
  assert selection is None


def test_receiver_host_copy_does_not_replace_direct_gnss_anchor(
  tmp_path,
):
  evaluator, store = authority(tmp_path, boottime=200.0)
  store.save(replace(
    anchor(),
    source=(
      TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
    ),
    provenance=TimeProvenance.GNSS_INDEPENDENT,
    uncertainty_seconds=0.025,
  ))
  before = store.primary_path.read_bytes()

  result = evaluator.current_authorized_time(
    host_time_observation=nonindependent_host(
      HostTimeSource.RECEIVER_DERIVED,
      generation="receiver:2",
    )
  )

  authorized = result.authorized_time
  assert authorized is not None
  assert not authorized.independent
  assert authorized.source is (
    TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
  )
  assert authorized.provenance is (
    TimeProvenance.GNSS_INDEPENDENT
  )
  assert result.anchor_write_status is (
    AnchorWriteStatus.NOT_REQUIRED
  )
  assert store.primary_path.read_bytes() == before
