from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from math import ceil, isfinite

from openpilot.common.time_helpers import MAX_DATE, MIN_DATE
from openpilot.system.ubloxd.trusted_time_anchor import (
  MAX_TRUSTED_TIME_UNCERTAINTY_SECONDS,
  RtcVoltageStatus,
  TimeProvenance,
  TrustedTimeAnchor,
  TrustedTimeAnchorInventory,
  TrustedTimeSource,
  TrustedTimeAnchorSelection,
  TrustedTimeAnchorStore,
  read_boot_id,
  read_boottime_seconds,
  read_rtc_epoch_seconds,
  read_rtc_voltage_status,
)

RTC_OBSERVATION_TICK_INTERVAL_SECONDS = 2.0
RTC_OBSERVATION_TICK_TOLERANCE_SECONDS = 2.0
MAX_CROSS_BOOT_RTC_ELAPSED_SECONDS = 30 * 24 * 60 * 60
CROSS_BOOT_RTC_DRIFT_PARTS_PER_MILLION = 100
RTC_UPTIME_QUANTIZATION_TOLERANCE_SECONDS = 1.0


class RtcObservationState(StrEnum):
  PENDING_TICK = "pending_tick"
  OBSERVED = "observed"
  REJECTED = "rejected"
  NOT_APPLICABLE = "not_applicable"


class RtcObservationReason(StrEnum):
  CROSS_BOOT_CANDIDATE_PENDING_TICK = (
    "cross_boot_candidate_pending_tick"
  )
  CROSS_BOOT_CANDIDATE_OBSERVED = (
    "cross_boot_candidate_observed"
  )
  ANCHOR_UNAVAILABLE = "anchor_unavailable"
  CURRENT_BOOT_ID_UNAVAILABLE = "current_boot_id_unavailable"
  SAME_BOOT_ONLY = "same_boot_only"
  ANCHOR_RTC_UNAVAILABLE = "anchor_rtc_unavailable"
  CURRENT_RTC_UNAVAILABLE = "current_rtc_unavailable"
  CURRENT_BOOTTIME_UNAVAILABLE = "current_boottime_unavailable"
  RTC_ROLLBACK = "rtc_rollback"
  RTC_NOT_ADVANCED = "rtc_not_advanced"
  RTC_ELAPSED_ABOVE_MAXIMUM = "rtc_elapsed_above_maximum"
  RTC_ELAPSED_BELOW_UPTIME = "rtc_elapsed_below_uptime"
  CANDIDATE_UTC_OUTSIDE_SUPPORTED_RANGE = (
    "candidate_utc_outside_supported_range"
  )
  OBSERVATION_TIME_INVALID = "observation_time_invalid"
  OBSERVATION_TIME_ROLLBACK = "observation_time_rollback"
  RTC_TICK_UNAVAILABLE = "rtc_tick_unavailable"
  BOOTTIME_TICK_UNAVAILABLE = "boottime_tick_unavailable"
  RTC_TICK_ROLLBACK = "rtc_tick_rollback"
  RTC_TICK_NOT_ADVANCED = "rtc_tick_not_advanced"
  BOOTTIME_TICK_ROLLBACK = "boottime_tick_rollback"
  RTC_TICK_RATE_INCONSISTENT = "rtc_tick_rate_inconsistent"


@dataclass(frozen=True)
class RtcObservationCandidate:
  candidate_utc: datetime
  uncertainty_seconds: float
  anchor_generation: str
  anchor_sequence: int
  anchor_boot_id: str
  current_boot_id: str
  anchor_trusted_utc: datetime
  anchor_rtc_epoch_seconds: int
  current_rtc_epoch_seconds: int
  rtc_elapsed_seconds: int
  current_boottime_seconds: float
  rtc_advanced: bool
  elapsed_covers_uptime: bool
  rtc_voltage_status_supported: bool
  rtc_voltage_status_flags: int | None
  anchor_source: TrustedTimeSource | None = None
  anchor_provenance: TimeProvenance | None = None
  anchor_authorized: bool = False
  anchor_independent: bool = False
  authorized: bool = field(default=False, init=False)
  operational: bool = field(default=False, init=False)


@dataclass(frozen=True)
class CrossBootRtcObservation:
  state: RtcObservationState
  reason: RtcObservationReason
  candidate: RtcObservationCandidate | None
  first_rtc_epoch_seconds: int | None
  second_rtc_epoch_seconds: int | None
  first_boottime_seconds: float | None
  second_boottime_seconds: float | None
  first_observed_at: float | None
  second_observed_at: float | None
  tick_elapsed_seconds: float | None
  rtc_tick_delta_seconds: int | None
  boottime_tick_delta_seconds: float | None
  tick_consistent: bool | None
  authorized: bool = field(default=False, init=False)
  operational: bool = field(default=False, init=False)


def _valid_nonnegative_float(value: object) -> bool:
  return (
    type(value) in (int, float)
    and not isinstance(value, bool)
    and isfinite(value)
    and value >= 0.0
  )


def _safe_read[Value](
  reader: Callable[[], Value],
) -> Value | None:
  try:
    return reader()
  except Exception:
    return None


def _normalize_utc(value: datetime) -> datetime | None:
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


def _cross_boot_selection(
  inventory: TrustedTimeAnchorInventory,
  current_boot_id: str,
) -> TrustedTimeAnchorSelection | None:
  candidates = [
    inspection
    for inspection in (
      inventory.primary,
      inventory.previous,
    )
    if (
      inspection.anchor is not None
      and inspection.anchor.boot_id != current_boot_id
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
    "cross_boot_newest_sequence",
  )


class CrossBootRtcObserver:
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
    tick_interval_seconds: float = (
      RTC_OBSERVATION_TICK_INTERVAL_SECONDS
    ),
    tick_tolerance_seconds: float = (
      RTC_OBSERVATION_TICK_TOLERANCE_SECONDS
    ),
    max_elapsed_seconds: int = (
      MAX_CROSS_BOOT_RTC_ELAPSED_SECONDS
    ),
    drift_parts_per_million: int = (
      CROSS_BOOT_RTC_DRIFT_PARTS_PER_MILLION
    ),
  ) -> None:
    if (
      not _valid_nonnegative_float(tick_interval_seconds)
      or tick_interval_seconds <= 0.0
    ):
      raise ValueError("RTC tick interval is invalid")
    if not _valid_nonnegative_float(tick_tolerance_seconds):
      raise ValueError("RTC tick tolerance is invalid")
    if type(max_elapsed_seconds) is not int or max_elapsed_seconds < 1:
      raise ValueError("RTC observation elapsed limit is invalid")
    if (
      type(drift_parts_per_million) is not int
      or drift_parts_per_million < 0
    ):
      raise ValueError("RTC observation drift allowance is invalid")

    self._store = store or TrustedTimeAnchorStore()
    self._boot_id_reader = boot_id_reader
    self._boottime_reader = boottime_reader
    self._rtc_epoch_reader = rtc_epoch_reader
    self._rtc_voltage_reader = rtc_voltage_reader
    self._tick_interval_seconds = float(tick_interval_seconds)
    self._tick_tolerance_seconds = float(tick_tolerance_seconds)
    self._max_elapsed_seconds = max_elapsed_seconds
    self._drift_parts_per_million = drift_parts_per_million
    self._selection: TrustedTimeAnchorSelection | None = None
    self._current_boot_id: str | None = None
    self._voltage = RtcVoltageStatus(False, None)
    self._observation: CrossBootRtcObservation | None = None
    self._tick_due_at: float | None = None
    self._last_report_key: tuple[object, ...] | None = None

  @staticmethod
  def _result(
    state: RtcObservationState,
    reason: RtcObservationReason,
    *,
    candidate: RtcObservationCandidate | None = None,
    first_rtc: int | None = None,
    second_rtc: int | None = None,
    first_boottime: float | None = None,
    second_boottime: float | None = None,
    first_observed_at: float | None = None,
    second_observed_at: float | None = None,
    tick_elapsed: float | None = None,
    rtc_tick_delta: int | None = None,
    boottime_tick_delta: float | None = None,
    tick_consistent: bool | None = None,
  ) -> CrossBootRtcObservation:
    return CrossBootRtcObservation(
      state=state,
      reason=reason,
      candidate=candidate,
      first_rtc_epoch_seconds=first_rtc,
      second_rtc_epoch_seconds=second_rtc,
      first_boottime_seconds=first_boottime,
      second_boottime_seconds=second_boottime,
      first_observed_at=first_observed_at,
      second_observed_at=second_observed_at,
      tick_elapsed_seconds=tick_elapsed,
      rtc_tick_delta_seconds=rtc_tick_delta,
      boottime_tick_delta_seconds=boottime_tick_delta,
      tick_consistent=tick_consistent,
    )

  def _candidate(
    self,
    selection: TrustedTimeAnchorSelection,
    current_boot_id: str,
    current_rtc: int | None,
    current_boottime: float | None,
    voltage: RtcVoltageStatus,
  ) -> tuple[
    RtcObservationCandidate | None,
    RtcObservationReason | None,
  ]:
    anchor: TrustedTimeAnchor = selection.anchor
    if anchor.rtc_epoch_seconds is None:
      return None, RtcObservationReason.ANCHOR_RTC_UNAVAILABLE
    if type(current_rtc) is not int or current_rtc < 0:
      return None, RtcObservationReason.CURRENT_RTC_UNAVAILABLE
    if not _valid_nonnegative_float(current_boottime):
      return None, RtcObservationReason.CURRENT_BOOTTIME_UNAVAILABLE

    rtc_elapsed = current_rtc - anchor.rtc_epoch_seconds
    if rtc_elapsed < 0:
      return None, RtcObservationReason.RTC_ROLLBACK
    if rtc_elapsed == 0:
      return None, RtcObservationReason.RTC_NOT_ADVANCED
    if rtc_elapsed > self._max_elapsed_seconds:
      return None, RtcObservationReason.RTC_ELAPSED_ABOVE_MAXIMUM

    assert current_boottime is not None
    elapsed_covers_uptime = (
      rtc_elapsed + RTC_UPTIME_QUANTIZATION_TOLERANCE_SECONDS
      >= current_boottime
    )
    if not elapsed_covers_uptime:
      return None, RtcObservationReason.RTC_ELAPSED_BELOW_UPTIME

    try:
      candidate_utc = _normalize_utc(
        anchor.trusted_utc + timedelta(seconds=rtc_elapsed)
      )
    except (OverflowError, TypeError, ValueError):
      candidate_utc = None
    if candidate_utc is None:
      return (
        None,
        RtcObservationReason.CANDIDATE_UTC_OUTSIDE_SUPPORTED_RANGE,
      )

    uncertainty_seconds = min(
      MAX_TRUSTED_TIME_UNCERTAINTY_SECONDS,
      anchor.uncertainty_seconds
      + ceil(
        rtc_elapsed
        * self._drift_parts_per_million
        / 1_000_000
      ),
    )
    return RtcObservationCandidate(
      candidate_utc=candidate_utc,
      uncertainty_seconds=uncertainty_seconds,
      anchor_generation=selection.generation,
      anchor_sequence=anchor.sequence,
      anchor_boot_id=anchor.boot_id,
      current_boot_id=current_boot_id,
      anchor_trusted_utc=anchor.trusted_utc,
      anchor_rtc_epoch_seconds=anchor.rtc_epoch_seconds,
      current_rtc_epoch_seconds=current_rtc,
      rtc_elapsed_seconds=rtc_elapsed,
      current_boottime_seconds=float(current_boottime),
      rtc_advanced=True,
      elapsed_covers_uptime=True,
      rtc_voltage_status_supported=voltage.supported,
      rtc_voltage_status_flags=voltage.flags,
      anchor_source=anchor.source,
      anchor_provenance=anchor.provenance,
      anchor_authorized=anchor.authorized,
      anchor_independent=anchor.independent,
    ), None

  def _begin(self, now: float) -> CrossBootRtcObservation:
    try:
      _, inventory = self._store.load_best()
    except Exception:
      return self._result(
        RtcObservationState.REJECTED,
        RtcObservationReason.ANCHOR_UNAVAILABLE,
      )

    if not any(
      inspection.anchor is not None
      for inspection in (
        inventory.primary,
        inventory.previous,
      )
    ):
      return self._result(
        RtcObservationState.REJECTED,
        RtcObservationReason.ANCHOR_UNAVAILABLE,
      )

    current_boot_id = _safe_read(self._boot_id_reader)
    if type(current_boot_id) is not str or not current_boot_id:
      return self._result(
        RtcObservationState.REJECTED,
        RtcObservationReason.CURRENT_BOOT_ID_UNAVAILABLE,
      )

    selection = _cross_boot_selection(
      inventory,
      current_boot_id,
    )
    if selection is None:
      return self._result(
        RtcObservationState.NOT_APPLICABLE,
        RtcObservationReason.SAME_BOOT_ONLY,
      )

    first_rtc = _safe_read(self._rtc_epoch_reader)
    first_boottime = _safe_read(self._boottime_reader)
    voltage = _safe_read(self._rtc_voltage_reader)
    if not isinstance(voltage, RtcVoltageStatus):
      voltage = RtcVoltageStatus(
        supported=False,
        flags=None,
        error="invalid_voltage_status_reader_result",
      )

    candidate, rejection = self._candidate(
      selection,
      current_boot_id,
      first_rtc,
      first_boottime,
      voltage,
    )
    if candidate is None:
      assert rejection is not None
      return self._result(
        RtcObservationState.REJECTED,
        rejection,
        first_rtc=(
          first_rtc
          if type(first_rtc) is int
          else None
        ),
        first_boottime=(
          float(first_boottime)
          if _valid_nonnegative_float(first_boottime)
          else None
        ),
        first_observed_at=now,
      )

    self._selection = selection
    self._current_boot_id = current_boot_id
    self._voltage = voltage
    self._tick_due_at = now + self._tick_interval_seconds
    return self._result(
      RtcObservationState.PENDING_TICK,
      RtcObservationReason.CROSS_BOOT_CANDIDATE_PENDING_TICK,
      candidate=candidate,
      first_rtc=candidate.current_rtc_epoch_seconds,
      first_boottime=candidate.current_boottime_seconds,
      first_observed_at=now,
    )

  def _finish(
    self,
    pending: CrossBootRtcObservation,
    now: float,
  ) -> CrossBootRtcObservation:
    candidate = pending.candidate
    selection = self._selection
    current_boot_id = self._current_boot_id
    assert candidate is not None
    assert selection is not None
    assert current_boot_id is not None
    assert pending.first_rtc_epoch_seconds is not None
    assert pending.first_boottime_seconds is not None
    assert pending.first_observed_at is not None

    second_rtc = _safe_read(self._rtc_epoch_reader)
    second_boottime = _safe_read(self._boottime_reader)
    tick_elapsed = now - pending.first_observed_at

    common = {
      "candidate": candidate,
      "first_rtc": pending.first_rtc_epoch_seconds,
      "second_rtc": (
        second_rtc
        if type(second_rtc) is int
        else None
      ),
      "first_boottime": pending.first_boottime_seconds,
      "second_boottime": (
        float(second_boottime)
        if _valid_nonnegative_float(second_boottime)
        else None
      ),
      "first_observed_at": pending.first_observed_at,
      "second_observed_at": now,
      "tick_elapsed": tick_elapsed,
    }

    if type(second_rtc) is not int or second_rtc < 0:
      return self._result(
        RtcObservationState.REJECTED,
        RtcObservationReason.RTC_TICK_UNAVAILABLE,
        **common,
      )
    if not _valid_nonnegative_float(second_boottime):
      return self._result(
        RtcObservationState.REJECTED,
        RtcObservationReason.BOOTTIME_TICK_UNAVAILABLE,
        **common,
      )

    rtc_tick_delta = (
      second_rtc - pending.first_rtc_epoch_seconds
    )
    boottime_tick_delta = (
      float(second_boottime)
      - pending.first_boottime_seconds
    )
    common.update({
      "rtc_tick_delta": rtc_tick_delta,
      "boottime_tick_delta": boottime_tick_delta,
    })

    if rtc_tick_delta < 0:
      return self._result(
        RtcObservationState.REJECTED,
        RtcObservationReason.RTC_TICK_ROLLBACK,
        tick_consistent=False,
        **common,
      )
    if rtc_tick_delta == 0:
      return self._result(
        RtcObservationState.REJECTED,
        RtcObservationReason.RTC_TICK_NOT_ADVANCED,
        tick_consistent=False,
        **common,
      )
    if boottime_tick_delta < 0.0:
      return self._result(
        RtcObservationState.REJECTED,
        RtcObservationReason.BOOTTIME_TICK_ROLLBACK,
        tick_consistent=False,
        **common,
      )

    tick_consistent = (
      abs(rtc_tick_delta - boottime_tick_delta)
      <= self._tick_tolerance_seconds
    )
    if not tick_consistent:
      return self._result(
        RtcObservationState.REJECTED,
        RtcObservationReason.RTC_TICK_RATE_INCONSISTENT,
        tick_consistent=False,
        **common,
      )

    final_candidate, rejection = self._candidate(
      selection,
      current_boot_id,
      second_rtc,
      float(second_boottime),
      self._voltage,
    )
    if final_candidate is None:
      assert rejection is not None
      return self._result(
        RtcObservationState.REJECTED,
        rejection,
        tick_consistent=True,
        **common,
      )

    return self._result(
      RtcObservationState.OBSERVED,
      RtcObservationReason.CROSS_BOOT_CANDIDATE_OBSERVED,
      candidate=final_candidate,
      first_rtc=pending.first_rtc_epoch_seconds,
      second_rtc=second_rtc,
      first_boottime=pending.first_boottime_seconds,
      second_boottime=float(second_boottime),
      first_observed_at=pending.first_observed_at,
      second_observed_at=now,
      tick_elapsed=tick_elapsed,
      rtc_tick_delta=rtc_tick_delta,
      boottime_tick_delta=boottime_tick_delta,
      tick_consistent=True,
    )

  def current_observation(
    self,
    now: float,
  ) -> CrossBootRtcObservation:
    if not _valid_nonnegative_float(now):
      return self._result(
        RtcObservationState.REJECTED,
        RtcObservationReason.OBSERVATION_TIME_INVALID,
      )
    now = float(now)

    if self._observation is None:
      self._observation = self._begin(now)
      return self._observation

    if (
      self._observation.state
      is not RtcObservationState.PENDING_TICK
    ):
      return self._observation

    assert self._observation.first_observed_at is not None
    if now < self._observation.first_observed_at:
      self._observation = replace(
        self._observation,
        state=RtcObservationState.REJECTED,
        reason=RtcObservationReason.OBSERVATION_TIME_ROLLBACK,
      )
      return self._observation

    assert self._tick_due_at is not None
    if now < self._tick_due_at:
      return self._observation

    self._observation = self._finish(
      self._observation,
      now,
    )
    return self._observation

  def changed_observation(
    self,
    now: float,
  ) -> CrossBootRtcObservation | None:
    observation = self.current_observation(now)
    candidate = observation.candidate
    key = (
      observation.state,
      observation.reason,
      (
        candidate.anchor_generation
        if candidate is not None
        else None
      ),
      (
        candidate.anchor_sequence
        if candidate is not None
        else None
      ),
      observation.second_rtc_epoch_seconds,
      observation.tick_consistent,
    )
    if key == self._last_report_key:
      return None
    self._last_report_key = key
    return observation
