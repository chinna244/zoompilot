from datetime import UTC, datetime, timedelta, timezone

import pytest

from openpilot.system.ubloxd.receiver_time_provenance import (
  ReceiverTimeAssistanceObservation,
)
from openpilot.system.ubloxd.rtc_time_observation import (
  CrossBootRtcObservation,
  RtcObservationCandidate,
  RtcObservationReason,
  RtcObservationState,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
  TrustedTimeAnchor,
  TrustedTimeAnchorSelection,
  TrustedTimeSource,
)
from openpilot.system.ubloxd.trusted_time_validation import (
  AnchorReplacementReason,
  CrossBootRtcValidationStatus,
  IndependentTimeObservation,
  ReceiverCorrectionReason,
  SameInstantComparisonStatus,
  evaluate_anchor_replacement,
  evaluate_receiver_correction,
  validate_cross_boot_rtc,
)


BOOT_ID = "12345678-1234-5678-9234-567812345678"
OTHER_BOOT_ID = "87654321-4321-6789-9234-567812345678"
NOW = datetime(2026, 7, 23, 12, tzinfo=UTC)


def independent(
  *,
  utc: datetime = NOW,
  boottime: float = 200.0,
  uncertainty: float = 0.025,
  source: TrustedTimeSource = (
    TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
  ),
  provenance: TimeProvenance = (
    TimeProvenance.GNSS_INDEPENDENT
  ),
) -> IndependentTimeObservation:
  return IndependentTimeObservation(
    utc=utc,
    observed_boottime_seconds=boottime,
    uncertainty_seconds=uncertainty,
    source=source,
    provenance=provenance,
  )


def anchor(
  *,
  boot_id: str = BOOT_ID,
  utc: datetime = NOW - timedelta(seconds=100),
  boottime: float = 100.0,
  uncertainty: float = 30.0,
  source: TrustedTimeSource = (
    TrustedTimeSource.SYSTEM_SYNCHRONIZED
  ),
  provenance: TimeProvenance = (
    TimeProvenance.NETWORK_INDEPENDENT
  ),
) -> TrustedTimeAnchorSelection:
  value = TrustedTimeAnchor(
    version=1,
    trusted_utc=utc,
    source=source,
    provenance=provenance,
    authorized=True,
    independent=True,
    uncertainty_seconds=uncertainty,
    boot_id=boot_id,
    boottime_seconds=boottime,
    rtc_epoch_seconds=1_000,
    rtc_voltage_status_supported=False,
    rtc_voltage_status_flags=None,
    sequence=1,
  )
  return TrustedTimeAnchorSelection(
    "primary",
    value,
    "primary_only",
  )


def rtc_observation(
  *,
  candidate_utc: datetime = NOW - timedelta(seconds=2),
  candidate_boottime: float = 198.0,
  uncertainty: float = 31.0,
) -> CrossBootRtcObservation:
  candidate = RtcObservationCandidate(
    candidate_utc=candidate_utc,
    uncertainty_seconds=uncertainty,
    anchor_generation="previous",
    anchor_sequence=7,
    anchor_boot_id=OTHER_BOOT_ID,
    current_boot_id=BOOT_ID,
    anchor_trusted_utc=NOW - timedelta(seconds=102),
    anchor_rtc_epoch_seconds=1_000,
    current_rtc_epoch_seconds=1_102,
    rtc_elapsed_seconds=102,
    current_boottime_seconds=candidate_boottime,
    rtc_advanced=True,
    elapsed_covers_uptime=True,
    rtc_voltage_status_supported=False,
    rtc_voltage_status_flags=None,
  )
  return CrossBootRtcObservation(
    state=RtcObservationState.OBSERVED,
    reason=(
      RtcObservationReason.CROSS_BOOT_CANDIDATE_OBSERVED
    ),
    candidate=candidate,
    first_rtc_epoch_seconds=1_100,
    second_rtc_epoch_seconds=1_102,
    first_boottime_seconds=196.0,
    second_boottime_seconds=198.0,
    first_observed_at=50.0,
    second_observed_at=52.0,
    tick_elapsed_seconds=2.0,
    rtc_tick_delta_seconds=2,
    boottime_tick_delta_seconds=2.0,
    tick_consistent=True,
  )


def assistance(
  *,
  written: bool = True,
  utc: datetime | None = NOW - timedelta(seconds=105),
  boottime: float | None = 100.0,
  uncertainty: float | None = 31.0,
  independent_value: bool | None = False,
  provenance: TimeProvenance | None = (
    TimeProvenance.NETWORK_INDEPENDENT
  ),
  correction_written: bool = False,
) -> ReceiverTimeAssistanceObservation:
  return ReceiverTimeAssistanceObservation(
    cycle_id=3,
    written=written,
    source="same_boot_boottime" if written else None,
    utc=utc,
    uncertainty_seconds=uncertainty,
    written_at=10.0 if written else None,
    written_boottime_seconds=boottime,
    independent=independent_value,
    provenance=provenance,
    correction_written=correction_written,
  )


def test_independent_observation_is_typed_and_normalized():
  value = independent(
    utc=datetime(
      2026,
      7,
      23,
      7,
      tzinfo=timezone(timedelta(hours=-5)),
    )
  )

  assert value.utc == NOW
  assert value.authorized
  assert value.independent


def test_independent_observation_rejects_mismatched_source():
  with pytest.raises(ValueError):
    independent(
      source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
      provenance=TimeProvenance.GNSS_INDEPENDENT,
    )


def test_cross_boot_candidate_is_compared_at_same_boottime():
  result = validate_cross_boot_rtc(
    rtc_observation(),
    independent(),
  )

  assert result.status is CrossBootRtcValidationStatus.AGREES
  assert result.candidate_utc_at_validation == NOW
  assert result.candidate_error_seconds == 0.0
  assert result.allowed_error_seconds == 120.0
  assert not result.authorized
  assert not result.operational


def test_cross_boot_disagreement_remains_telemetry_only():
  result = validate_cross_boot_rtc(
    rtc_observation(
      candidate_utc=NOW - timedelta(minutes=10)
    ),
    independent(),
  )

  assert result.status is (
    CrossBootRtcValidationStatus.DISAGREES
  )
  assert result.candidate_error_seconds is not None
  assert result.candidate_error_seconds > (
    result.allowed_error_seconds
  )
  assert not result.authorized
  assert not result.operational


def test_anchor_missing_or_previous_boot_is_replaced():
  missing = evaluate_anchor_replacement(
    None,
    BOOT_ID,
    independent(),
    drift_parts_per_million=100,
  )
  previous_boot = evaluate_anchor_replacement(
    anchor(boot_id=OTHER_BOOT_ID),
    BOOT_ID,
    independent(),
    drift_parts_per_million=100,
  )

  assert missing.replace
  assert missing.reason is (
    AnchorReplacementReason.ANCHOR_MISSING_OR_INVALID
  )
  assert previous_boot.replace
  assert previous_boot.reason is (
    AnchorReplacementReason
    .NEW_CURRENT_BOOT_INDEPENDENT_SOURCE
  )


def test_gnss_replaces_current_system_anchor_by_provenance():
  decision = evaluate_anchor_replacement(
    anchor(),
    BOOT_ID,
    independent(),
    drift_parts_per_million=100,
  )

  assert decision.replace
  assert decision.reason is (
    AnchorReplacementReason.MORE_AUTHORITATIVE_PROVENANCE
  )
  assert decision.comparison is not None
  assert decision.comparison.status is (
    SameInstantComparisonStatus.AGREES
  )


def test_minor_repeated_system_observation_preserves_anchor():
  system = independent(
    uncertainty=30.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
  )
  decision = evaluate_anchor_replacement(
    anchor(),
    BOOT_ID,
    system,
    drift_parts_per_million=100,
  )

  assert not decision.replace
  assert decision.reason is (
    AnchorReplacementReason.EXISTING_ANCHOR_PRESERVED
  )


def test_materially_lower_uncertainty_replaces_equal_provenance():
  system = independent(
    uncertainty=5.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
  )
  decision = evaluate_anchor_replacement(
    anchor(uncertainty=30.0),
    BOOT_ID,
    system,
    drift_parts_per_million=100,
  )

  assert decision.replace
  assert decision.reason is (
    AnchorReplacementReason.MATERIALLY_LOWER_UNCERTAINTY
  )


def test_same_rank_disagreement_replaces_inaccurate_anchor():
  system = independent(
    utc=NOW,
    uncertainty=30.0,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
  )
  decision = evaluate_anchor_replacement(
    anchor(utc=NOW - timedelta(minutes=10)),
    BOOT_ID,
    system,
    drift_parts_per_million=100,
  )

  assert decision.replace
  assert decision.reason is (
    AnchorReplacementReason.INACCURATE_CURRENT_BOOT_ANCHOR
  )


def test_correction_requires_prior_nonindependent_assistance():
  no_assistance = evaluate_receiver_correction(
    assistance(written=False),
    independent(
      source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
      provenance=TimeProvenance.NETWORK_INDEPENDENT,
      uncertainty=30.0,
    ),
  )
  required = evaluate_receiver_correction(
    assistance(),
    independent(
      source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
      provenance=TimeProvenance.NETWORK_INDEPENDENT,
      uncertainty=30.0,
    ),
  )

  assert not no_assistance.should_correct
  assert no_assistance.reason is (
    ReceiverCorrectionReason.NO_ASSISTANCE_WRITTEN
  )
  assert required.should_correct
  assert required.reason is (
    ReceiverCorrectionReason.CORRECTION_REQUIRED
  )
  assert required.delta_seconds == 5.0


def test_correction_is_not_sent_back_to_receiver_source():
  decision = evaluate_receiver_correction(
    assistance(),
    independent(),
  )

  assert not decision.should_correct
  assert decision.reason is (
    ReceiverCorrectionReason.RECEIVER_SELF_SOURCE
  )


def test_correction_delta_threshold_and_cycle_guard():
  system = independent(
    utc=NOW,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    uncertainty=30.0,
  )
  small = evaluate_receiver_correction(
    assistance(
      utc=NOW - timedelta(seconds=101),
    ),
    system,
  )
  repeated = evaluate_receiver_correction(
    assistance(correction_written=True),
    system,
  )

  assert not small.should_correct
  assert small.reason is (
    ReceiverCorrectionReason.DELTA_BELOW_THRESHOLD
  )
  assert not repeated.should_correct
  assert repeated.reason is (
    ReceiverCorrectionReason.CORRECTION_ALREADY_WRITTEN
  )


def test_system_source_does_not_override_same_boot_gnss_assistance():
  system = independent(
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    uncertainty=30.0,
  )
  decision = evaluate_receiver_correction(
    assistance(
      independent_value=False,
      provenance=TimeProvenance.GNSS_INDEPENDENT,
    ),
    system,
  )

  assert not decision.should_correct
  assert decision.reason is (
    ReceiverCorrectionReason.NOT_MATERIALLY_BETTER
  )


def test_independent_system_can_correct_same_boot_system_assistance():
  system = independent(
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    uncertainty=30.0,
  )
  decision = evaluate_receiver_correction(
    assistance(
      independent_value=False,
      provenance=TimeProvenance.EXTERNAL_OR_UNKNOWN,
    ),
    system,
  )

  assert decision.should_correct
  assert decision.reason is (
    ReceiverCorrectionReason.CORRECTION_REQUIRED
  )
