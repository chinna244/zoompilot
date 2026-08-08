from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import ceil, isfinite

from openpilot.common.time_helpers import (
  HostTimeObservation,
  HostTimeSource,
  MAX_DATE,
  MIN_DATE,
)
from openpilot.system.ubloxd.rtc_time_observation import (
  RTC_OBSERVATION_TICK_INTERVAL_SECONDS,
  CrossBootRtcObserver,
)
from openpilot.system.ubloxd.trusted_time_validation import (
  AnchorReplacementDecision,
  AnchorReplacementReason,
  IndependentTimeObservation,
  SameInstantTimeComparison,
  evaluate_anchor_replacement,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  MAX_TRUSTED_TIME_UNCERTAINTY_SECONDS,
  TRUSTED_TIME_ANCHOR_VERSION,
  RtcVoltageStatus,
  TimeProvenance,
  TrustedTimeAnchor,
  TrustedTimeAnchorInventory,
  TrustedTimeAnchorSelection,
  TrustedTimeAnchorStore,
  TrustedTimeSource,
  read_boot_id,
  read_boottime_seconds,
  read_rtc_epoch_seconds,
  read_rtc_voltage_status,
)

SYSTEM_SYNCHRONIZED_UNCERTAINTY_SECONDS = 30.0
MAX_SAME_BOOT_CONTINUITY_SECONDS = 7 * 24 * 60 * 60
SAME_BOOT_DRIFT_PARTS_PER_MILLION = 100


class TimeAuthorizationEvidence(StrEnum):
  SYSTEM_SYNCHRONIZED = "system_synchronized"
  SAME_BOOT_BOOTTIME = "same_boot_boottime"
  RECEIVER_UTC_UNASSISTED_GNSS = (
    "receiver_utc_unassisted_gnss"
  )


class TimeAuthorityRejectionReason(StrEnum):
  ANCHOR_UNAVAILABLE = "anchor_unavailable"
  BOOT_ID_UNAVAILABLE = "boot_id_unavailable"
  BOOTTIME_UNAVAILABLE = "boottime_unavailable"
  CROSS_BOOT_CONTINUITY_UNPROVABLE = (
    "cross_boot_continuity_unprovable"
  )
  BOOTTIME_ROLLBACK = "boottime_rollback"
  ELAPSED_TIME_ABOVE_MAXIMUM = (
    "elapsed_time_above_maximum"
  )
  UTC_OUTSIDE_SUPPORTED_RANGE = (
    "utc_outside_supported_range"
  )
  INVALID_HOST_TIME = "invalid_host_time"
  HOST_TIME_NOT_INDEPENDENT = "host_time_not_independent"
  INVALID_INDEPENDENT_TIME = "invalid_independent_time"


class AnchorWriteStatus(StrEnum):
  NOT_REQUIRED = "not_required"
  SAVED = "saved"
  PRESERVED_CURRENT_BOOT = "preserved_current_boot"
  READERS_UNAVAILABLE = "readers_unavailable"
  FAILED = "failed"


@dataclass(frozen=True)
class AuthorizedTime:
  utc: datetime
  uncertainty_seconds: float
  source: TrustedTimeSource
  provenance: TimeProvenance
  independent: bool
  evidence: TimeAuthorizationEvidence
  anchor_generation: str | None = None
  anchor_sequence: int | None = None
  elapsed_seconds: float = 0.0
  observed_boottime_seconds: float | None = None

  @property
  def mga_accuracy_seconds(self) -> int:
    return min(
      65_535,
      max(0, ceil(self.uncertainty_seconds)),
    )


@dataclass(frozen=True)
class TimeAuthorityEvaluation:
  authorized_time: AuthorizedTime | None
  rejection_reason: TimeAuthorityRejectionReason | None
  anchor_write_status: AnchorWriteStatus
  anchor_write_error: str | None = None
  selected_anchor_generation: str | None = None
  selected_anchor_sequence: int | None = None
  anchor_write_reason: AnchorReplacementReason | None = None
  anchor_comparison: SameInstantTimeComparison | None = None


def _normalize_utc(value: datetime) -> datetime | None:
  try:
    if not isinstance(value, datetime):
      return None
    if value.tzinfo is None or value.utcoffset() is None:
      return None
    normalized = value.astimezone(UTC)
  except Exception:
    return None
  supported_minimum = MIN_DATE.replace(tzinfo=UTC)
  supported_maximum = MAX_DATE.replace(tzinfo=UTC)
  if not supported_minimum < normalized < supported_maximum:
    return None
  return normalized


def _bounded_error(exc: BaseException) -> str:
  return f"{type(exc).__name__}:{exc}"[:240]


class TimeAuthority:
  def __init__(
    self,
    store: TrustedTimeAnchorStore | None = None,
    *,
    boot_id_reader: Callable[[], str | None] = read_boot_id,
    boottime_reader: Callable[[], float | None] = (
      read_boottime_seconds
    ),
    rtc_epoch_reader: Callable[[], int | None] = (
      read_rtc_epoch_seconds
    ),
    rtc_voltage_reader: Callable[[], RtcVoltageStatus] = (
      read_rtc_voltage_status
    ),
    utc_now: Callable[[], datetime] = (
      lambda: datetime.now(UTC)
    ),
    max_same_boot_elapsed_seconds: float = (
      MAX_SAME_BOOT_CONTINUITY_SECONDS
    ),
    drift_parts_per_million: int = (
      SAME_BOOT_DRIFT_PARTS_PER_MILLION
    ),
  ) -> None:
    if (
      type(max_same_boot_elapsed_seconds)
      not in (int, float)
      or isinstance(max_same_boot_elapsed_seconds, bool)
      or not isfinite(max_same_boot_elapsed_seconds)
      or max_same_boot_elapsed_seconds < 0.0
    ):
      raise ValueError(
        "Maximum same-boot elapsed time is invalid"
      )
    if (
      type(drift_parts_per_million) is not int
      or drift_parts_per_million < 0
    ):
      raise ValueError(
        "Same-boot drift allowance is invalid"
      )
    self._store = store or TrustedTimeAnchorStore()
    self._boot_id_reader = boot_id_reader
    self._boottime_reader = boottime_reader
    self._rtc_epoch_reader = rtc_epoch_reader
    self._rtc_voltage_reader = rtc_voltage_reader
    self._utc_now = utc_now
    self._max_same_boot_elapsed_seconds = float(
      max_same_boot_elapsed_seconds
    )
    self._drift_parts_per_million = (
      drift_parts_per_million
    )

  @staticmethod
  def _safe_read[Value](
    reader: Callable[[], Value],
  ) -> Value | None:
    try:
      return reader()
    except Exception:
      return None

  def _load_inventory(
    self,
  ) -> tuple[
    TrustedTimeAnchorSelection | None,
    TrustedTimeAnchorInventory | None,
  ]:
    try:
      selection, inventory = self._store.load_best()
    except Exception:
      return None, None
    return selection, inventory

  @staticmethod
  def _matching_boot_selection(
    inventory: TrustedTimeAnchorInventory | None,
    boot_id: str | None,
  ) -> TrustedTimeAnchorSelection | None:
    if inventory is None or boot_id is None:
      return None
    candidates = [
      inspection
      for inspection in (
        inventory.primary,
        inventory.previous,
      )
      if (
        inspection.anchor is not None
        and inspection.anchor.boot_id == boot_id
      )
    ]
    if not candidates:
      return None
    selected = max(
      candidates,
      key=lambda inspection: (
        inspection.anchor.sequence,
        inspection.generation == "primary",
      ),
    )
    assert selected.anchor is not None
    return TrustedTimeAnchorSelection(
      selected.generation,
      selected.anchor,
      "matching_boot_newest_sequence",
    )

  def _persist_independent_anchor(
    self,
    independent: IndependentTimeObservation,
    current_boot_id: str | None,
    existing: TrustedTimeAnchorSelection | None,
  ) -> tuple[
    AnchorWriteStatus,
    str | None,
    AnchorReplacementDecision | None,
  ]:
    if current_boot_id is None:
      return (
        AnchorWriteStatus.READERS_UNAVAILABLE,
        None,
        None,
      )

    decision = evaluate_anchor_replacement(
      existing,
      current_boot_id,
      independent,
      drift_parts_per_million=(
        self._drift_parts_per_million
      ),
    )
    if not decision.replace:
      return (
        AnchorWriteStatus.PRESERVED_CURRENT_BOOT,
        None,
        decision,
      )

    rtc_epoch = self._safe_read(self._rtc_epoch_reader)
    voltage = self._safe_read(self._rtc_voltage_reader)
    if not isinstance(voltage, RtcVoltageStatus):
      voltage = RtcVoltageStatus(
        supported=False,
        flags=None,
        error="invalid_voltage_status_reader_result",
      )

    try:
      sequence = self._store.next_sequence()
      self._store.save(TrustedTimeAnchor(
        version=TRUSTED_TIME_ANCHOR_VERSION,
        trusted_utc=independent.utc,
        source=independent.source,
        provenance=independent.provenance,
        authorized=True,
        independent=True,
        uncertainty_seconds=(
          independent.uncertainty_seconds
        ),
        boot_id=current_boot_id,
        boottime_seconds=(
          independent.observed_boottime_seconds
        ),
        rtc_epoch_seconds=(
          rtc_epoch
          if type(rtc_epoch) is int and rtc_epoch >= 0
          else None
        ),
        rtc_voltage_status_supported=voltage.supported,
        rtc_voltage_status_flags=voltage.flags,
        sequence=sequence,
      ))
    except Exception as exc:
      return (
        AnchorWriteStatus.FAILED,
        _bounded_error(exc),
        decision,
      )

    return AnchorWriteStatus.SAVED, None, decision

  @staticmethod
  def _evidence_for_source(
    source: TrustedTimeSource,
  ) -> TimeAuthorizationEvidence:
    if source is TrustedTimeSource.SYSTEM_SYNCHRONIZED:
      return TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED
    if source is (
      TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
    ):
      return (
        TimeAuthorizationEvidence
        .RECEIVER_UTC_UNASSISTED_GNSS
      )
    raise ValueError("Unsupported independent time source")

  def _independent_evaluation(
    self,
    independent: IndependentTimeObservation,
    *,
    selected: TrustedTimeAnchorSelection | None,
    inventory: TrustedTimeAnchorInventory | None,
    current_boot_id: str | None,
  ) -> TimeAuthorityEvaluation:
    matching_boot = self._matching_boot_selection(
      inventory,
      current_boot_id,
    )
    existing = matching_boot or selected
    write_status, write_error, decision = (
      self._persist_independent_anchor(
        independent,
        current_boot_id,
        existing,
      )
    )
    return TimeAuthorityEvaluation(
      authorized_time=AuthorizedTime(
        utc=independent.utc,
        uncertainty_seconds=(
          independent.uncertainty_seconds
        ),
        source=independent.source,
        provenance=independent.provenance,
        independent=True,
        evidence=self._evidence_for_source(
          independent.source
        ),
        observed_boottime_seconds=(
          independent.observed_boottime_seconds
        ),
      ),
      rejection_reason=None,
      anchor_write_status=write_status,
      anchor_write_error=write_error,
      selected_anchor_generation=(
        selected.generation
        if selected is not None
        else None
      ),
      selected_anchor_sequence=(
        selected.anchor.sequence
        if selected is not None
        else None
      ),
      anchor_write_reason=(
        decision.reason
        if decision is not None
        else None
      ),
      anchor_comparison=(
        decision.comparison
        if decision is not None
        else None
      ),
    )

  def observe_independent_time(
    self,
    *,
    utc: datetime,
    uncertainty_seconds: float,
    source: TrustedTimeSource,
    provenance: TimeProvenance,
    observed_boottime_seconds: float | None = None,
  ) -> TimeAuthorityEvaluation:
    boottime = (
      self._safe_read(self._boottime_reader)
      if observed_boottime_seconds is None
      else observed_boottime_seconds
    )
    try:
      independent = IndependentTimeObservation(
        utc=utc,
        observed_boottime_seconds=boottime,
        uncertainty_seconds=uncertainty_seconds,
        source=source,
        provenance=provenance,
      )
    except (TypeError, ValueError):
      return TimeAuthorityEvaluation(
        authorized_time=None,
        rejection_reason=(
          TimeAuthorityRejectionReason
          .INVALID_INDEPENDENT_TIME
        ),
        anchor_write_status=(
          AnchorWriteStatus.NOT_REQUIRED
        ),
      )

    current_boot_id = self._safe_read(
      self._boot_id_reader
    )
    selected, inventory = self._load_inventory()
    return self._independent_evaluation(
      independent,
      selected=selected,
      inventory=inventory,
      current_boot_id=current_boot_id,
    )

  def create_cross_boot_rtc_observer(
    self,
    *,
    tick_interval_seconds: float = (
      RTC_OBSERVATION_TICK_INTERVAL_SECONDS
    ),
  ) -> CrossBootRtcObserver:
    return CrossBootRtcObserver(
      self._store,
      boot_id_reader=self._boot_id_reader,
      boottime_reader=self._boottime_reader,
      rtc_epoch_reader=self._rtc_epoch_reader,
      rtc_voltage_reader=self._rtc_voltage_reader,
      tick_interval_seconds=tick_interval_seconds,
      drift_parts_per_million=self._drift_parts_per_million,
    )

  def current_authorized_time(
    self,
    *,
    host_time_observation: HostTimeObservation | None,
  ) -> TimeAuthorityEvaluation:
    current_boot_id = self._safe_read(
      self._boot_id_reader
    )
    current_boottime = self._safe_read(
      self._boottime_reader
    )
    selected, inventory = self._load_inventory()
    matching_boot = self._matching_boot_selection(
      inventory,
      current_boot_id,
    )

    host_rejection: TimeAuthorityRejectionReason | None = None
    if host_time_observation is not None:
      if not isinstance(
        host_time_observation,
        HostTimeObservation,
      ):
        host_rejection = (
          TimeAuthorityRejectionReason.INVALID_HOST_TIME
        )
      elif (
        host_time_observation.independent
        and host_time_observation.source
        is HostTimeSource.NETWORK_SYNCHRONIZED
      ):
        independent = IndependentTimeObservation(
          utc=host_time_observation.utc,
          observed_boottime_seconds=(
            host_time_observation
            .observed_boottime_seconds
          ),
          uncertainty_seconds=(
            host_time_observation.uncertainty_seconds
          ),
          source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
          provenance=TimeProvenance.NETWORK_INDEPENDENT,
        )
        return self._independent_evaluation(
          independent,
          selected=selected,
          inventory=inventory,
          current_boot_id=current_boot_id,
        )
      else:
        host_rejection = (
          TimeAuthorityRejectionReason
          .HOST_TIME_NOT_INDEPENDENT
        )

    if selected is None:
      return TimeAuthorityEvaluation(
        authorized_time=None,
        rejection_reason=(
          host_rejection
          or TimeAuthorityRejectionReason.ANCHOR_UNAVAILABLE
        ),
        anchor_write_status=AnchorWriteStatus.NOT_REQUIRED,
      )

    selected_common = {
      "anchor_write_status": AnchorWriteStatus.NOT_REQUIRED,
      "selected_anchor_generation": selected.generation,
      "selected_anchor_sequence": selected.anchor.sequence,
    }
    if current_boot_id is None:
      return TimeAuthorityEvaluation(
        authorized_time=None,
        rejection_reason=(
          TimeAuthorityRejectionReason.BOOT_ID_UNAVAILABLE
        ),
        **selected_common,
      )
    if current_boottime is None:
      return TimeAuthorityEvaluation(
        authorized_time=None,
        rejection_reason=(
          TimeAuthorityRejectionReason.BOOTTIME_UNAVAILABLE
        ),
        **selected_common,
      )

    continuity_anchor = matching_boot
    if continuity_anchor is None:
      return TimeAuthorityEvaluation(
        authorized_time=None,
        rejection_reason=(
          TimeAuthorityRejectionReason
          .CROSS_BOOT_CONTINUITY_UNPROVABLE
        ),
        **selected_common,
      )

    common = {
      "anchor_write_status": AnchorWriteStatus.NOT_REQUIRED,
      "selected_anchor_generation": (
        continuity_anchor.generation
      ),
      "selected_anchor_sequence": (
        continuity_anchor.anchor.sequence
      ),
    }
    elapsed_seconds = (
      current_boottime
      - continuity_anchor.anchor.boottime_seconds
    )
    if elapsed_seconds < 0.0:
      return TimeAuthorityEvaluation(
        authorized_time=None,
        rejection_reason=(
          TimeAuthorityRejectionReason.BOOTTIME_ROLLBACK
        ),
        **common,
      )
    if (
      elapsed_seconds
      > self._max_same_boot_elapsed_seconds
    ):
      return TimeAuthorityEvaluation(
        authorized_time=None,
        rejection_reason=(
          TimeAuthorityRejectionReason
          .ELAPSED_TIME_ABOVE_MAXIMUM
        ),
        **common,
      )

    try:
      estimated_utc = _normalize_utc(
        continuity_anchor.anchor.trusted_utc
        + timedelta(seconds=elapsed_seconds)
      )
    except (OverflowError, TypeError, ValueError):
      estimated_utc = None
    if estimated_utc is None:
      return TimeAuthorityEvaluation(
        authorized_time=None,
        rejection_reason=(
          TimeAuthorityRejectionReason
          .UTC_OUTSIDE_SUPPORTED_RANGE
        ),
        **common,
      )

    drift_uncertainty = ceil(
      elapsed_seconds
      * self._drift_parts_per_million
      / 1_000_000
    )
    uncertainty_seconds = min(
      MAX_TRUSTED_TIME_UNCERTAINTY_SECONDS,
      continuity_anchor.anchor.uncertainty_seconds
      + drift_uncertainty,
    )

    return TimeAuthorityEvaluation(
      authorized_time=AuthorizedTime(
        utc=estimated_utc,
        uncertainty_seconds=uncertainty_seconds,
        source=continuity_anchor.anchor.source,
        provenance=continuity_anchor.anchor.provenance,
        independent=False,
        evidence=(
          TimeAuthorizationEvidence.SAME_BOOT_BOOTTIME
        ),
        anchor_generation=continuity_anchor.generation,
        anchor_sequence=continuity_anchor.anchor.sequence,
        elapsed_seconds=elapsed_seconds,
        observed_boottime_seconds=float(
          current_boottime
        ),
      ),
      rejection_reason=None,
      **common,
    )
