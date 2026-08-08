from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import ceil, isfinite

from openpilot.common.time_helpers import MAX_DATE, MIN_DATE
from openpilot.system.ubloxd.receiver_time_provenance import (
  ReceiverTimeAssistanceObservation,
)
from openpilot.system.ubloxd.rtc_time_observation import (
  CrossBootRtcObservation,
  RtcObservationState,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  MAX_TRUSTED_TIME_UNCERTAINTY_SECONDS,
  TimeProvenance,
  TrustedTimeAnchorSelection,
  TrustedTimeSource,
)

RTC_VALIDATION_MINIMUM_ALLOWED_ERROR_SECONDS = 120.0
RTC_VALIDATION_MARGIN_SECONDS = 30.0
ANCHOR_VALIDATION_MINIMUM_ALLOWED_ERROR_SECONDS = 2.0
ANCHOR_VALIDATION_MARGIN_SECONDS = 1.0
MINIMUM_ANCHOR_UNCERTAINTY_IMPROVEMENT_SECONDS = 5.0
MINIMUM_RECEIVER_CORRECTION_DELTA_SECONDS = 2.0


class SameInstantComparisonStatus(StrEnum):
  AGREES = "agrees"
  DISAGREES = "disagrees"


class AnchorReplacementReason(StrEnum):
  ANCHOR_MISSING_OR_INVALID = "anchor_missing_or_invalid"
  NEW_CURRENT_BOOT_INDEPENDENT_SOURCE = (
    "new_current_boot_independent_source"
  )
  MORE_AUTHORITATIVE_PROVENANCE = (
    "more_authoritative_provenance"
  )
  MATERIALLY_LOWER_UNCERTAINTY = (
    "materially_lower_uncertainty"
  )
  INACCURATE_CURRENT_BOOT_ANCHOR = (
    "inaccurate_current_boot_anchor"
  )
  OBSERVATION_BEFORE_CURRENT_ANCHOR = (
    "observation_before_current_anchor"
  )
  EXISTING_ANCHOR_PRESERVED = "existing_anchor_preserved"


class CrossBootRtcValidationStatus(StrEnum):
  UNAVAILABLE = "unavailable"
  AGREES = "agrees"
  DISAGREES = "disagrees"


class ReceiverCorrectionReason(StrEnum):
  CORRECTION_REQUIRED = "correction_required"
  NO_ASSISTANCE_WRITTEN = "no_assistance_written"
  CORRECTION_ALREADY_WRITTEN = "correction_already_written"
  ASSISTANCE_TIME_UNAVAILABLE = "assistance_time_unavailable"
  INDEPENDENT_SOURCE_INVALID = "independent_source_invalid"
  RECEIVER_SELF_SOURCE = "receiver_self_source"
  OBSERVATION_BEFORE_ASSISTANCE = (
    "observation_before_assistance"
  )
  NOT_MATERIALLY_BETTER = "not_materially_better"
  DELTA_BELOW_THRESHOLD = "delta_below_threshold"


@dataclass(frozen=True)
class IndependentTimeObservation:
  utc: datetime
  observed_boottime_seconds: float
  uncertainty_seconds: float
  source: TrustedTimeSource
  provenance: TimeProvenance
  authorized: bool = field(default=True, init=False)
  independent: bool = field(default=True, init=False)

  def __post_init__(self) -> None:
    normalized = _normalize_utc(self.utc)
    if normalized is None:
      raise ValueError("Independent UTC is invalid")
    if not _valid_nonnegative_float(
      self.observed_boottime_seconds
    ):
      raise ValueError(
        "Independent UTC boottime is invalid"
      )
    if not _valid_uncertainty(self.uncertainty_seconds):
      raise ValueError(
        "Independent UTC uncertainty is invalid"
      )
    valid_pairs = {
      (
        TrustedTimeSource.SYSTEM_SYNCHRONIZED,
        TimeProvenance.NETWORK_INDEPENDENT,
      ),
      (
        TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS,
        TimeProvenance.GNSS_INDEPENDENT,
      ),
    }
    if (self.source, self.provenance) not in valid_pairs:
      raise ValueError(
        "Independent UTC source and provenance do not match"
      )
    object.__setattr__(self, "utc", normalized)
    object.__setattr__(
      self,
      "observed_boottime_seconds",
      float(self.observed_boottime_seconds),
    )
    object.__setattr__(
      self,
      "uncertainty_seconds",
      float(self.uncertainty_seconds),
    )


@dataclass(frozen=True)
class SameInstantTimeComparison:
  status: SameInstantComparisonStatus
  reference_utc: datetime
  candidate_utc_at_reference: datetime
  reference_boottime_seconds: float
  candidate_boottime_seconds: float
  reference_uncertainty_seconds: float
  candidate_uncertainty_seconds: float
  error_seconds: float
  allowed_error_seconds: float


@dataclass(frozen=True)
class AnchorReplacementDecision:
  replace: bool
  reason: AnchorReplacementReason
  comparison: SameInstantTimeComparison | None
  existing_effective_uncertainty_seconds: float | None


@dataclass(frozen=True)
class CrossBootRtcValidation:
  status: CrossBootRtcValidationStatus
  reason: str
  validation_source: TrustedTimeSource
  validation_provenance: TimeProvenance
  validation_utc: datetime
  validation_boottime_seconds: float
  validation_uncertainty_seconds: float
  candidate_utc_at_validation: datetime | None
  candidate_error_seconds: float | None
  allowed_error_seconds: float | None
  anchor_generation: str | None
  anchor_sequence: int | None
  anchor_boot_id: str | None
  current_boot_id: str | None
  rtc_elapsed_seconds: int | None
  current_uptime_seconds: float | None
  rtc_tick_delta_seconds: int | None
  boottime_tick_delta_seconds: float | None
  tick_consistent: bool | None
  authorized: bool = field(default=False, init=False)
  operational: bool = field(default=False, init=False)


@dataclass(frozen=True)
class ReceiverCorrectionDecision:
  should_correct: bool
  reason: ReceiverCorrectionReason
  receiver_cycle: int
  source: TrustedTimeSource
  target_utc: datetime
  target_boottime_seconds: float
  target_uncertainty_seconds: float
  predicted_receiver_utc: datetime | None
  delta_seconds: float | None
  minimum_delta_seconds: float
  materially_better: bool


def _valid_nonnegative_float(value: object) -> bool:
  return (
    type(value) in (int, float)
    and not isinstance(value, bool)
    and isfinite(value)
    and value >= 0.0
  )


def _valid_uncertainty(value: object) -> bool:
  return (
    _valid_nonnegative_float(value)
    and float(value)
    <= MAX_TRUSTED_TIME_UNCERTAINTY_SECONDS
  )


def _normalize_utc(value: object) -> datetime | None:
  try:
    if not isinstance(value, datetime):
      return None
    if value.tzinfo is None or value.utcoffset() is None:
      return None
    normalized = value.astimezone(UTC)
  except Exception:
    return None
  if not (
    MIN_DATE.replace(tzinfo=UTC)
    < normalized
    < MAX_DATE.replace(tzinfo=UTC)
  ):
    return None
  return normalized


def provenance_rank(provenance: TimeProvenance | None) -> int:
  return {
    TimeProvenance.EXTERNAL_OR_UNKNOWN: 0,
    TimeProvenance.NETWORK_INDEPENDENT: 1,
    TimeProvenance.GNSS_INDEPENDENT: 2,
  }.get(provenance, 0)


def compare_same_instant(
  reference: IndependentTimeObservation,
  *,
  candidate_utc: datetime,
  candidate_boottime_seconds: float,
  candidate_uncertainty_seconds: float,
  minimum_allowed_error_seconds: float,
  margin_seconds: float,
) -> SameInstantTimeComparison:
  normalized_candidate = _normalize_utc(candidate_utc)
  if normalized_candidate is None:
    raise ValueError("Comparison candidate UTC is invalid")
  if not _valid_nonnegative_float(
    candidate_boottime_seconds
  ):
    raise ValueError(
      "Comparison candidate boottime is invalid"
    )
  if not _valid_uncertainty(
    candidate_uncertainty_seconds
  ):
    raise ValueError(
      "Comparison candidate uncertainty is invalid"
    )
  if (
    not _valid_nonnegative_float(
      minimum_allowed_error_seconds
    )
    or not _valid_nonnegative_float(margin_seconds)
  ):
    raise ValueError("Comparison policy is invalid")

  candidate_at_reference = (
    normalized_candidate
    + timedelta(
      seconds=(
        reference.observed_boottime_seconds
        - float(candidate_boottime_seconds)
      )
    )
  )
  error_seconds = abs(
    (
      reference.utc
      - candidate_at_reference
    ).total_seconds()
  )
  allowed_error_seconds = max(
    float(minimum_allowed_error_seconds),
    reference.uncertainty_seconds
    + float(candidate_uncertainty_seconds)
    + float(margin_seconds),
  )
  return SameInstantTimeComparison(
    status=(
      SameInstantComparisonStatus.AGREES
      if error_seconds <= allowed_error_seconds
      else SameInstantComparisonStatus.DISAGREES
    ),
    reference_utc=reference.utc,
    candidate_utc_at_reference=candidate_at_reference,
    reference_boottime_seconds=(
      reference.observed_boottime_seconds
    ),
    candidate_boottime_seconds=float(
      candidate_boottime_seconds
    ),
    reference_uncertainty_seconds=(
      reference.uncertainty_seconds
    ),
    candidate_uncertainty_seconds=float(
      candidate_uncertainty_seconds
    ),
    error_seconds=error_seconds,
    allowed_error_seconds=allowed_error_seconds,
  )


def evaluate_anchor_replacement(
  existing: TrustedTimeAnchorSelection | None,
  current_boot_id: str | None,
  independent: IndependentTimeObservation,
  *,
  drift_parts_per_million: int,
) -> AnchorReplacementDecision:
  if (
    type(drift_parts_per_million) is not int
    or drift_parts_per_million < 0
  ):
    raise ValueError("Anchor drift policy is invalid")

  if existing is None:
    return AnchorReplacementDecision(
      True,
      AnchorReplacementReason.ANCHOR_MISSING_OR_INVALID,
      None,
      None,
    )

  anchor = existing.anchor
  if (
    type(current_boot_id) is not str
    or not current_boot_id
    or anchor.boot_id != current_boot_id
  ):
    return AnchorReplacementDecision(
      True,
      (
        AnchorReplacementReason
        .NEW_CURRENT_BOOT_INDEPENDENT_SOURCE
      ),
      None,
      None,
    )

  elapsed_seconds = (
    independent.observed_boottime_seconds
    - anchor.boottime_seconds
  )
  if elapsed_seconds < 0.0:
    return AnchorReplacementDecision(
      False,
      (
        AnchorReplacementReason
        .OBSERVATION_BEFORE_CURRENT_ANCHOR
      ),
      None,
      None,
    )

  existing_effective_uncertainty = min(
    MAX_TRUSTED_TIME_UNCERTAINTY_SECONDS,
    anchor.uncertainty_seconds
    + ceil(
      elapsed_seconds
      * drift_parts_per_million
      / 1_000_000
    ),
  )
  comparison = compare_same_instant(
    independent,
    candidate_utc=anchor.trusted_utc,
    candidate_boottime_seconds=anchor.boottime_seconds,
    candidate_uncertainty_seconds=(
      existing_effective_uncertainty
    ),
    minimum_allowed_error_seconds=(
      ANCHOR_VALIDATION_MINIMUM_ALLOWED_ERROR_SECONDS
    ),
    margin_seconds=ANCHOR_VALIDATION_MARGIN_SECONDS,
  )
  candidate_rank = provenance_rank(independent.provenance)
  existing_rank = provenance_rank(anchor.provenance)
  more_authoritative = candidate_rank > existing_rank
  materially_lower_uncertainty = (
    independent.uncertainty_seconds
    + MINIMUM_ANCHOR_UNCERTAINTY_IMPROVEMENT_SECONDS
    <= existing_effective_uncertainty
  )
  inaccurate = (
    comparison.status
    is SameInstantComparisonStatus.DISAGREES
    and candidate_rank >= existing_rank
  )

  if more_authoritative:
    reason = (
      AnchorReplacementReason
      .MORE_AUTHORITATIVE_PROVENANCE
    )
  elif materially_lower_uncertainty:
    reason = (
      AnchorReplacementReason
      .MATERIALLY_LOWER_UNCERTAINTY
    )
  elif inaccurate:
    reason = (
      AnchorReplacementReason
      .INACCURATE_CURRENT_BOOT_ANCHOR
    )
  else:
    return AnchorReplacementDecision(
      False,
      AnchorReplacementReason.EXISTING_ANCHOR_PRESERVED,
      comparison,
      existing_effective_uncertainty,
    )

  return AnchorReplacementDecision(
    True,
    reason,
    comparison,
    existing_effective_uncertainty,
  )


def validate_cross_boot_rtc(
  observation: CrossBootRtcObservation,
  independent: IndependentTimeObservation,
) -> CrossBootRtcValidation:
  candidate = observation.candidate
  if (
    observation.state is not RtcObservationState.OBSERVED
    or candidate is None
  ):
    return CrossBootRtcValidation(
      status=CrossBootRtcValidationStatus.UNAVAILABLE,
      reason="cross_boot_candidate_not_observed",
      validation_source=independent.source,
      validation_provenance=independent.provenance,
      validation_utc=independent.utc,
      validation_boottime_seconds=(
        independent.observed_boottime_seconds
      ),
      validation_uncertainty_seconds=(
        independent.uncertainty_seconds
      ),
      candidate_utc_at_validation=None,
      candidate_error_seconds=None,
      allowed_error_seconds=None,
      anchor_generation=None,
      anchor_sequence=None,
      anchor_boot_id=None,
      current_boot_id=None,
      rtc_elapsed_seconds=None,
      current_uptime_seconds=None,
      rtc_tick_delta_seconds=(
        observation.rtc_tick_delta_seconds
      ),
      boottime_tick_delta_seconds=(
        observation.boottime_tick_delta_seconds
      ),
      tick_consistent=observation.tick_consistent,
    )

  comparison = compare_same_instant(
    independent,
    candidate_utc=candidate.candidate_utc,
    candidate_boottime_seconds=(
      candidate.current_boottime_seconds
    ),
    candidate_uncertainty_seconds=(
      candidate.uncertainty_seconds
    ),
    minimum_allowed_error_seconds=(
      RTC_VALIDATION_MINIMUM_ALLOWED_ERROR_SECONDS
    ),
    margin_seconds=RTC_VALIDATION_MARGIN_SECONDS,
  )
  agrees = (
    comparison.status
    is SameInstantComparisonStatus.AGREES
  )
  return CrossBootRtcValidation(
    status=(
      CrossBootRtcValidationStatus.AGREES
      if agrees
      else CrossBootRtcValidationStatus.DISAGREES
    ),
    reason=(
      "cross_boot_candidate_within_allowed_error"
      if agrees
      else "cross_boot_candidate_exceeds_allowed_error"
    ),
    validation_source=independent.source,
    validation_provenance=independent.provenance,
    validation_utc=independent.utc,
    validation_boottime_seconds=(
      independent.observed_boottime_seconds
    ),
    validation_uncertainty_seconds=(
      independent.uncertainty_seconds
    ),
    candidate_utc_at_validation=(
      comparison.candidate_utc_at_reference
    ),
    candidate_error_seconds=comparison.error_seconds,
    allowed_error_seconds=(
      comparison.allowed_error_seconds
    ),
    anchor_generation=candidate.anchor_generation,
    anchor_sequence=candidate.anchor_sequence,
    anchor_boot_id=candidate.anchor_boot_id,
    current_boot_id=candidate.current_boot_id,
    rtc_elapsed_seconds=candidate.rtc_elapsed_seconds,
    current_uptime_seconds=(
      candidate.current_boottime_seconds
    ),
    rtc_tick_delta_seconds=(
      observation.rtc_tick_delta_seconds
    ),
    boottime_tick_delta_seconds=(
      observation.boottime_tick_delta_seconds
    ),
    tick_consistent=observation.tick_consistent,
  )


def evaluate_receiver_correction(
  assistance: ReceiverTimeAssistanceObservation,
  independent: IndependentTimeObservation,
  *,
  minimum_delta_seconds: float = (
    MINIMUM_RECEIVER_CORRECTION_DELTA_SECONDS
  ),
) -> ReceiverCorrectionDecision:
  if not _valid_nonnegative_float(minimum_delta_seconds):
    raise ValueError(
      "Receiver correction delta threshold is invalid"
    )

  common = {
    "receiver_cycle": assistance.cycle_id,
    "source": independent.source,
    "target_utc": independent.utc,
    "target_boottime_seconds": (
      independent.observed_boottime_seconds
    ),
    "target_uncertainty_seconds": (
      independent.uncertainty_seconds
    ),
    "minimum_delta_seconds": float(
      minimum_delta_seconds
    ),
  }

  if not assistance.written:
    return ReceiverCorrectionDecision(
      False,
      ReceiverCorrectionReason.NO_ASSISTANCE_WRITTEN,
      predicted_receiver_utc=None,
      delta_seconds=None,
      materially_better=False,
      **common,
    )
  if assistance.correction_written:
    return ReceiverCorrectionDecision(
      False,
      ReceiverCorrectionReason.CORRECTION_ALREADY_WRITTEN,
      predicted_receiver_utc=None,
      delta_seconds=None,
      materially_better=False,
      **common,
    )
  if (
    assistance.utc is None
    or assistance.written_boottime_seconds is None
    or assistance.uncertainty_seconds is None
  ):
    return ReceiverCorrectionDecision(
      False,
      ReceiverCorrectionReason.ASSISTANCE_TIME_UNAVAILABLE,
      predicted_receiver_utc=None,
      delta_seconds=None,
      materially_better=False,
      **common,
    )
  if not independent.independent:
    return ReceiverCorrectionDecision(
      False,
      ReceiverCorrectionReason.INDEPENDENT_SOURCE_INVALID,
      predicted_receiver_utc=None,
      delta_seconds=None,
      materially_better=False,
      **common,
    )
  if independent.source is (
    TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
  ):
    return ReceiverCorrectionDecision(
      False,
      ReceiverCorrectionReason.RECEIVER_SELF_SOURCE,
      predicted_receiver_utc=None,
      delta_seconds=None,
      materially_better=False,
      **common,
    )

  elapsed_seconds = (
    independent.observed_boottime_seconds
    - assistance.written_boottime_seconds
  )
  if elapsed_seconds < 0.0:
    return ReceiverCorrectionDecision(
      False,
      (
        ReceiverCorrectionReason
        .OBSERVATION_BEFORE_ASSISTANCE
      ),
      predicted_receiver_utc=None,
      delta_seconds=None,
      materially_better=False,
      **common,
    )

  predicted_receiver_utc = (
    assistance.utc
    + timedelta(seconds=elapsed_seconds)
  )
  delta_seconds = abs(
    (
      independent.utc
      - predicted_receiver_utc
    ).total_seconds()
  )
  candidate_rank = provenance_rank(independent.provenance)
  assistance_rank = provenance_rank(assistance.provenance)
  new_independent = (
    assistance.independent is not True
    and candidate_rank >= assistance_rank
  )
  more_authoritative = candidate_rank > assistance_rank
  materially_lower_uncertainty = (
    independent.uncertainty_seconds
    + MINIMUM_ANCHOR_UNCERTAINTY_IMPROVEMENT_SECONDS
    <= assistance.uncertainty_seconds
  )
  materially_better = (
    new_independent
    or more_authoritative
    or materially_lower_uncertainty
  )
  if not materially_better:
    return ReceiverCorrectionDecision(
      False,
      ReceiverCorrectionReason.NOT_MATERIALLY_BETTER,
      predicted_receiver_utc=predicted_receiver_utc,
      delta_seconds=delta_seconds,
      materially_better=False,
      **common,
    )
  if delta_seconds < float(minimum_delta_seconds):
    return ReceiverCorrectionDecision(
      False,
      ReceiverCorrectionReason.DELTA_BELOW_THRESHOLD,
      predicted_receiver_utc=predicted_receiver_utc,
      delta_seconds=delta_seconds,
      materially_better=True,
      **common,
    )

  return ReceiverCorrectionDecision(
    True,
    ReceiverCorrectionReason.CORRECTION_REQUIRED,
    predicted_receiver_utc=predicted_receiver_utc,
    delta_seconds=delta_seconds,
    materially_better=True,
    **common,
  )
