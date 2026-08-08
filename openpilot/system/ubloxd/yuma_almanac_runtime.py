from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite
from pathlib import Path
import time

from openpilot.common.swaglog import cloudlog
from openpilot.system.ubloxd.gps_assistance import (
  CacheAgeEvidence,
  NavSatQuality,
  RestoredNavigationQuality,
  refresh_restored_navigation_quality,
)
from openpilot.system.ubloxd.yuma_almanac_controller import (
  YumaCacheLoader,
  YumaReferenceValidator,
  YumaSupplementationController,
)
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaDatabaseRestoreState,
  YumaSupplementationAction,
  YumaSupplementationPlan,
  YumaSupplementationReason,
)
from openpilot.system.ubloxd.yuma_almanac_store import (
  YUMA_ALMANAC_CACHE_PATH,
  load_yuma_almanac,
)
from openpilot.system.ubloxd.yuma_almanac import (
  validate_yuma_reference_time,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  YumaAlmanacTransmitResult,
  YumaAlmanacTransmitStatus,
  transmit_public_yuma_almanac,
)


YUMA_PIGEOND_TRANSMIT_BUDGET_SECONDS = 8.0
# Exceeds the 0.75-second GPS_ASSISTANCE_ACK_TIMEOUT used by
# send_mga_with_strict_ack to cover serial-write and scheduling overhead.
YUMA_PIGEOND_TRANSMIT_MARGIN_SECONDS = 1.0
YUMA_PIGEOND_RETRY_DELAY_SECONDS = 2.0
YUMA_RETRYABLE_TRANSMIT_STATUSES = frozenset((
  YumaAlmanacTransmitStatus.PARTIAL,
  YumaAlmanacTransmitStatus.FAILED,
  YumaAlmanacTransmitStatus.BUDGET_EXPIRED,
))
YumaMessageSender = Callable[[bytes], None]
MonotonicClock = Callable[[], float]


@dataclass(frozen=True)
class YumaTransmissionAttemptOutcome:
  attempt: int
  elapsed_ms: float
  transmit_result: YumaAlmanacTransmitResult | None = None
  error: str | None = None


@dataclass(frozen=True)
class YumaSupplementationRuntimeOutcome:
  plan: YumaSupplementationPlan
  transmit_result: YumaAlmanacTransmitResult | None = None
  error: str | None = None
  database_state: YumaDatabaseRestoreState | None = None
  database_age_seconds: float | None = None
  restored_cache_age_evidence: str | None = None
  restored_cache_age_verified: bool | None = None
  captured_gps_ephemeris_available: int | None = None
  captured_glonass_ephemeris_available: int | None = None
  captured_gps_startup_ready: bool | None = None
  restored_gps_ephemeris_fresh: bool | None = None
  restored_glonass_ephemeris_fresh: bool | None = None
  restored_quality_expiration_reasons: tuple[str, ...] = ()
  restored_cache_generation: str | None = None
  restored_cache_selection_reason: str | None = None
  restored_gps_almanac_available: int | None = None
  restored_glonass_almanac_available: int | None = None
  restored_gps_ephemeris_available: int | None = None
  restored_glonass_ephemeris_available: int | None = None
  restored_satellites_used: int | None = None
  restored_gps_startup_ready: bool | None = None
  restored_gps_almanac_satellite_ids: tuple[int, ...] | None = None
  yuma_reference_utc: datetime | None = None
  yuma_snapshot_sha256: str | None = None
  yuma_reference_age_seconds: float | None = None
  downloaded_at_utc: datetime | None = None
  cache_error: str | None = None
  transmission_attempt: int = 0
  transmission_elapsed_ms: float | None = None
  attempt_history: tuple[YumaTransmissionAttemptOutcome, ...] = ()
  terminal: bool = False
  retry_pending: bool = False
  time_anchor_source: str | None = None
  time_anchor_utc: datetime | None = None
  trusted_now_utc: datetime | None = None
  trusted_time_wait_expired: bool = False
  cache_wait_expired: bool = False
  nav_sat_observation_expired: bool = False
  runtime_elapsed_seconds: float | None = None
  time_anchor_elapsed_seconds: float | None = None
  decision_ready_elapsed_seconds: float | None = None
  nav_sat_observed_elapsed_seconds: float | None = None
  nav_sat_wait_seconds: float | None = None
  completion_elapsed_seconds: float | None = None
  completion_monotonic: float | None = None
  completion_utc: datetime | None = None
  gnss_start_sent_at_monotonic: float | None = None
  completed_before_gnss_start: bool | None = None
  cancellation_reason: YumaSupplementationReason | None = None
  receiver_cycle: int | None = None
  feature_enabled: bool = True


def _validated_monotonic(
  value: float,
  field: str,
) -> float:
  if (
    isinstance(value, bool)
    or not isinstance(value, (int, float))
    or not isfinite(value)
    or value < 0
  ):
    raise ValueError(
      f"{field} must be a non-negative finite number"
    )
  return float(value)


def _validated_utc(
  value: datetime | None,
) -> datetime | None:
  if value is None:
    return None
  if value.tzinfo is None or value.utcoffset() is None:
    raise ValueError("time anchor must be timezone-aware")
  return value.astimezone(UTC)


def _utc_age_seconds(
  newer: datetime | None,
  older: datetime | None,
) -> float | None:
  if newer is None or older is None:
    return None
  age = (newer - older).total_seconds()
  return age if age >= 0 else None


class YumaSupplementationRuntime:
  def __init__(
    self,
    *,
    database_state: YumaDatabaseRestoreState,
    database_saved_at_utc: datetime | None,
    started_at: float,
    time_anchor_utc: datetime | None,
    time_anchor_source: str | None = None,
    restored_cache_generation: str | None = None,
    restored_cache_selection_reason: str | None = None,
    restored_gps_almanac_available: int | None = None,
    restored_glonass_almanac_available: int | None = None,
    restored_gps_ephemeris_available: int | None = None,
    restored_glonass_ephemeris_available: int | None = None,
    restored_satellites_used: int | None = None,
    restored_gps_startup_ready: bool | None = None,
    restored_gps_almanac_satellite_ids: tuple[int, ...] | None = None,
    restored_navigation_quality: RestoredNavigationQuality | None = None,
    time_anchor_monotonic: float | None = None,
    path: Path = YUMA_ALMANAC_CACHE_PATH,
    cache_loader: YumaCacheLoader = load_yuma_almanac,
    reference_validator: YumaReferenceValidator = (
      validate_yuma_reference_time
    ),
    monotonic: MonotonicClock = time.monotonic,
  ) -> None:
    normalized_started_at = _validated_monotonic(
      started_at,
      "started_at",
    )
    self.controller = YumaSupplementationController(
      database_state=database_state,
      database_saved_at_utc=database_saved_at_utc,
      started_at=normalized_started_at,
      restored_cache_generation=restored_cache_generation,
      restored_cache_selection_reason=(
        restored_cache_selection_reason
      ),
      restored_gps_almanac_available=(
        restored_gps_almanac_available
      ),
      restored_glonass_almanac_available=(
        restored_glonass_almanac_available
      ),
      restored_gps_ephemeris_available=(
        restored_gps_ephemeris_available
      ),
      restored_glonass_ephemeris_available=(
        restored_glonass_ephemeris_available
      ),
      restored_satellites_used=restored_satellites_used,
      restored_gps_startup_ready=restored_gps_startup_ready,
      restored_gps_almanac_satellite_ids=(
        restored_gps_almanac_satellite_ids
      ),
      path=path,
      cache_loader=cache_loader,
      reference_validator=reference_validator,
    )
    if (
      restored_navigation_quality is not None
      and not isinstance(
        restored_navigation_quality,
        RestoredNavigationQuality,
      )
    ):
      raise ValueError(
        "restored_navigation_quality must be a RestoredNavigationQuality or None"
      )
    self._restored_navigation_quality = (
      restored_navigation_quality
    )
    self._restored_quality_policy_key = (
      self._quality_policy_key(restored_navigation_quality)
    )
    self._time_anchor_utc = _validated_utc(
      time_anchor_utc,
    )
    if time_anchor_source is not None and (
      not isinstance(time_anchor_source, str)
      or not time_anchor_source.strip()
    ):
      raise ValueError(
        "time_anchor_source must be a non-empty string or None"
      )
    self._time_anchor_source = (
      None
      if time_anchor_source is None
      else time_anchor_source.strip()
    )
    self._time_anchor_monotonic = _validated_monotonic(
      (
        normalized_started_at
        if time_anchor_monotonic is None
        else time_anchor_monotonic
      ),
      "time_anchor_monotonic",
    )
    self._started_at = normalized_started_at
    self._monotonic = monotonic
    self._completed = False
    self._outcome: YumaSupplementationRuntimeOutcome | None = None
    self._retry_plan: YumaSupplementationPlan | None = None
    self._retry_satellite_ids: frozenset[int] = frozenset()
    self._retry_at: float | None = None
    self._transmission_attempts = 0
    self._attempt_history: list[YumaTransmissionAttemptOutcome] = []
    self._last_wait_report_key: tuple[object, ...] | None = None

  @property
  def completed(self) -> bool:
    return self._completed

  @property
  def outcome(self) -> YumaSupplementationRuntimeOutcome | None:
    return self._outcome

  @property
  def retry_pending(self) -> bool:
    return self._retry_at is not None

  @property
  def transmission_attempts(self) -> int:
    return self._transmission_attempts

  @property
  def time_anchor_source(self) -> str | None:
    return self._time_anchor_source

  @property
  def restored_navigation_quality(
    self,
  ) -> RestoredNavigationQuality | None:
    return self._restored_navigation_quality

  @staticmethod
  def _quality_policy_key(
    quality: RestoredNavigationQuality | None,
  ) -> tuple[object, ...] | None:
    if quality is None:
      return None
    return (
      quality.age_verified,
      quality.effective_gps_ephemeris_available,
      quality.effective_glonass_ephemeris_available,
      quality.effective_gps_startup_ready,
      quality.gps_ephemeris_fresh,
      quality.glonass_ephemeris_fresh,
      quality.expiration_reasons,
    )

  def _cache_age_evidence(self) -> CacheAgeEvidence:
    normalized_source = (
      self._time_anchor_source or ""
    ).casefold().replace("-", "_")
    if normalized_source == "rtc_estimate":
      return CacheAgeEvidence.RTC_ESTIMATE
    return CacheAgeEvidence.TRUSTED_UTC

  def _refresh_restored_quality(
    self,
    trusted_now: datetime | None,
  ) -> None:
    quality = self._restored_navigation_quality
    saved_at_utc = self.controller.database_saved_at_utc
    if (
      quality is None
      or saved_at_utc is None
      or trusted_now is None
    ):
      return

    refreshed = refresh_restored_navigation_quality(
      quality,
      saved_at_utc,
      trusted_now,
      self._cache_age_evidence(),
    )
    policy_key = self._quality_policy_key(refreshed)
    policy_changed = policy_key != self._restored_quality_policy_key
    self._restored_navigation_quality = refreshed
    self._restored_quality_policy_key = policy_key
    self.controller.set_restored_quality(
      gps_ephemeris_available=(
        refreshed.effective_gps_ephemeris_available
      ),
      glonass_ephemeris_available=(
        refreshed.effective_glonass_ephemeris_available
      ),
      gps_startup_ready=(
        refreshed.effective_gps_startup_ready
      ),
    )
    if not policy_changed:
      return

    self._last_wait_report_key = None
    cloudlog.info(", ".join((
      "GPS restored navigation quality recomputed",
      f"cache_age_seconds={refreshed.cache_age_seconds}",
      f"age_evidence={refreshed.age_evidence.value}",
      f"age_verified={str(refreshed.age_verified).lower()}",
      f"captured_gps_ephemeris={refreshed.captured_gps_ephemeris_available}",
      f"captured_glonass_ephemeris={refreshed.captured_glonass_ephemeris_available}",
      f"effective_gps_ephemeris={refreshed.effective_gps_ephemeris_available}",
      f"effective_glonass_ephemeris={refreshed.effective_glonass_ephemeris_available}",
      f"effective_startup_ready={refreshed.effective_gps_startup_ready}",
      f"gps_ephemeris_fresh={refreshed.gps_ephemeris_fresh}",
      f"glonass_ephemeris_fresh={refreshed.glonass_ephemeris_fresh}",
      f"expiration_reasons={list(refreshed.expiration_reasons)}",
    )))

  def set_time_anchor(
    self,
    utc_time: datetime,
    monotonic_time: float,
    source: str | None = None,
  ) -> None:
    if self._completed:
      return
    if source is not None and (
      not isinstance(source, str)
      or not source.strip()
    ):
      raise ValueError(
        "source must be a non-empty string or None"
      )
    self._time_anchor_utc = _validated_utc(utc_time)
    self._time_anchor_monotonic = _validated_monotonic(
      monotonic_time,
      "monotonic_time",
    )
    if source is not None:
      self._time_anchor_source = source.strip()

  def trusted_now(
    self,
    monotonic_time: float,
  ) -> datetime | None:
    now = _validated_monotonic(
      monotonic_time,
      "monotonic_time",
    )
    if self._time_anchor_utc is None:
      return None
    elapsed = max(
      0.0,
      now - self._time_anchor_monotonic,
    )
    return self._time_anchor_utc + timedelta(
      seconds=elapsed,
    )

  def _clear_retry(self) -> None:
    self._retry_plan = None
    self._retry_satellite_ids = frozenset()
    self._retry_at = None

  def _complete(
    self,
    outcome: YumaSupplementationRuntimeOutcome | None = None,
  ) -> YumaSupplementationRuntimeOutcome | None:
    self._completed = True
    self._clear_retry()
    if outcome is not None:
      self._outcome = outcome
    return outcome

  def _retry_ids(
    self,
    result: YumaAlmanacTransmitResult,
  ) -> frozenset[int]:
    if result.status not in YUMA_RETRYABLE_TRANSMIT_STATUSES:
      return frozenset()
    return frozenset(
      result.failed_satellite_ids
      + result.deferred_satellite_ids
    )

  def _build_outcome(
    self,
    *,
    plan: YumaSupplementationPlan,
    now: float,
    transmit_result: YumaAlmanacTransmitResult | None = None,
    error: str | None = None,
    transmission_attempt: int = 0,
    transmission_elapsed_ms: float | None = None,
    terminal: bool,
    retry_pending: bool = False,
  ) -> YumaSupplementationRuntimeOutcome:
    trusted_now = self.trusted_now(now)
    observation = self.controller.cache_observation
    reference_time = (
      getattr(transmit_result, "reference_time_utc", None)
      if transmit_result is not None
      else None
    )
    downloaded_at = (
      getattr(transmit_result, "downloaded_at_utc", None)
      if transmit_result is not None
      else None
    )
    if reference_time is None and observation is not None:
      reference_time = observation.reference_time_utc
    if downloaded_at is None and observation is not None:
      downloaded_at = observation.downloaded_at_utc

    decision_ready_at = self.controller.decision_ready_at
    nav_sat_observed_at = self.controller.last_decision_nav_sat_time
    runtime_elapsed_seconds = max(0.0, now - self._started_at)
    time_anchor_elapsed_seconds = (
      None
      if self._time_anchor_utc is None
      else max(0.0, self._time_anchor_monotonic - self._started_at)
    )
    decision_ready_elapsed_seconds = (
      None
      if decision_ready_at is None
      else max(0.0, decision_ready_at - self._started_at)
    )
    nav_sat_observed_elapsed_seconds = (
      None
      if nav_sat_observed_at is None
      else max(0.0, nav_sat_observed_at - self._started_at)
    )
    nav_sat_wait_seconds = (
      None
      if decision_ready_at is None
      else max(
        0.0,
        (
          nav_sat_observed_at
          if nav_sat_observed_at is not None
          else now
        )
        - decision_ready_at,
      )
    )
    cancellation_reasons = (
      YumaSupplementationReason.RELIABLE_FIX_AVAILABLE,
      YumaSupplementationReason.FEATURE_DISABLED,
      YumaSupplementationReason.RECEIVER_CYCLE_RESET,
    )

    restored_quality = self._restored_navigation_quality
    return YumaSupplementationRuntimeOutcome(
      plan=plan,
      transmit_result=transmit_result,
      error=error,
      database_state=self.controller.database_state,
      database_age_seconds=_utc_age_seconds(
        trusted_now,
        self.controller.database_saved_at_utc,
      ),
      restored_cache_age_evidence=(
        None
        if restored_quality is None
        else restored_quality.age_evidence.value
      ),
      restored_cache_age_verified=(
        None
        if restored_quality is None
        else restored_quality.age_verified
      ),
      captured_gps_ephemeris_available=(
        None
        if restored_quality is None
        else restored_quality.captured_gps_ephemeris_available
      ),
      captured_glonass_ephemeris_available=(
        None
        if restored_quality is None
        else restored_quality.captured_glonass_ephemeris_available
      ),
      captured_gps_startup_ready=(
        None
        if restored_quality is None
        else restored_quality.captured_gps_startup_ready
      ),
      restored_gps_ephemeris_fresh=(
        None
        if restored_quality is None
        else restored_quality.gps_ephemeris_fresh
      ),
      restored_glonass_ephemeris_fresh=(
        None
        if restored_quality is None
        else restored_quality.glonass_ephemeris_fresh
      ),
      restored_quality_expiration_reasons=(
        ()
        if restored_quality is None
        else restored_quality.expiration_reasons
      ),
      restored_cache_generation=(
        self.controller.restored_cache_generation
      ),
      restored_cache_selection_reason=(
        self.controller.restored_cache_selection_reason
      ),
      restored_gps_almanac_available=(
        self.controller.restored_gps_almanac_available
      ),
      restored_glonass_almanac_available=(
        self.controller.restored_glonass_almanac_available
      ),
      restored_gps_ephemeris_available=(
        self.controller.restored_gps_ephemeris_available
      ),
      restored_glonass_ephemeris_available=(
        self.controller.restored_glonass_ephemeris_available
      ),
      restored_satellites_used=(
        self.controller.restored_satellites_used
      ),
      restored_gps_startup_ready=(
        self.controller.restored_gps_startup_ready
      ),
      restored_gps_almanac_satellite_ids=(
        self.controller.restored_gps_almanac_satellite_ids
      ),
      yuma_reference_utc=reference_time,
      yuma_snapshot_sha256=(
        None
        if observation is None
        else observation.snapshot_sha256
      ),
      yuma_reference_age_seconds=_utc_age_seconds(
        trusted_now,
        reference_time,
      ),
      downloaded_at_utc=downloaded_at,
      cache_error=self.controller.last_cache_error,
      transmission_attempt=transmission_attempt,
      transmission_elapsed_ms=transmission_elapsed_ms,
      attempt_history=tuple(self._attempt_history),
      terminal=terminal,
      retry_pending=retry_pending,
      time_anchor_source=self._time_anchor_source,
      time_anchor_utc=self._time_anchor_utc,
      trusted_now_utc=trusted_now,
      trusted_time_wait_expired=(
        now >= self.controller.trusted_time_deadline
      ),
      cache_wait_expired=(
        self.controller.cache_deadline is not None
        and now >= self.controller.cache_deadline
      ),
      nav_sat_observation_expired=(
        self.controller.nav_sat_observation_expired(now)
      ),
      runtime_elapsed_seconds=runtime_elapsed_seconds,
      time_anchor_elapsed_seconds=time_anchor_elapsed_seconds,
      decision_ready_elapsed_seconds=(
        decision_ready_elapsed_seconds
      ),
      nav_sat_observed_elapsed_seconds=(
        nav_sat_observed_elapsed_seconds
      ),
      nav_sat_wait_seconds=nav_sat_wait_seconds,
      completion_elapsed_seconds=(
        runtime_elapsed_seconds if terminal else None
      ),
      completion_monotonic=(now if terminal else None),
      completion_utc=(trusted_now if terminal else None),
      cancellation_reason=(
        plan.reason if plan.reason in cancellation_reasons else None
      ),
    )

  def _transmit(
    self,
    send_message: YumaMessageSender,
    *,
    plan: YumaSupplementationPlan,
    satellite_ids: frozenset[int],
    now: float,
  ) -> YumaSupplementationRuntimeOutcome:
    self._transmission_attempts += 1
    attempt = self._transmission_attempts
    started_at = _validated_monotonic(
      self._monotonic(),
      "monotonic",
    )

    try:
      observation = self.controller.cache_observation
      if observation is None:
        raise RuntimeError(
          "YUMA transmission requested without a frozen cache snapshot"
        )
      transmit_result = transmit_public_yuma_almanac(
        send_message,
        trusted_now=self.trusted_now(now),
        satellite_ids=satellite_ids,
        path=self.controller.path,
        stored_almanac=observation.stored,
        max_duration_seconds=(
          YUMA_PIGEOND_TRANSMIT_BUDGET_SECONDS
        ),
        minimum_remaining_seconds=(
          YUMA_PIGEOND_TRANSMIT_MARGIN_SECONDS
        ),
      )
    except Exception as exc:
      completed_at = _validated_monotonic(
        self._monotonic(),
        "monotonic",
      )
      cloudlog.exception(
        "Unexpected public YUMA transmission failure"
      )
      elapsed_ms = max(
        0.0,
        completed_at - started_at,
      ) * 1000.0
      error = f"{type(exc).__name__}: {exc}"
      self._attempt_history.append(
        YumaTransmissionAttemptOutcome(
          attempt=attempt,
          elapsed_ms=elapsed_ms,
          error=error,
        )
      )
      outcome = self._build_outcome(
        plan=plan,
        now=max(now, completed_at),
        error=error,
        transmission_attempt=attempt,
        transmission_elapsed_ms=elapsed_ms,
        terminal=True,
      )
      self._complete(outcome)
      return outcome

    completed_at = _validated_monotonic(
      self._monotonic(),
      "monotonic",
    )
    elapsed_ms = max(
      0.0,
      completed_at - started_at,
    ) * 1000.0
    self._attempt_history.append(
      YumaTransmissionAttemptOutcome(
        attempt=attempt,
        elapsed_ms=elapsed_ms,
        transmit_result=transmit_result,
      )
    )

    retry_ids = self._retry_ids(transmit_result)
    retry_pending = attempt == 1 and bool(retry_ids)
    if retry_pending:
      self._retry_plan = plan
      self._retry_satellite_ids = retry_ids
      self._retry_at = (
        max(now, completed_at)
        + YUMA_PIGEOND_RETRY_DELAY_SECONDS
      )

    outcome = self._build_outcome(
      plan=plan,
      now=max(now, completed_at),
      transmit_result=transmit_result,
      transmission_attempt=attempt,
      transmission_elapsed_ms=elapsed_ms,
      terminal=not retry_pending,
      retry_pending=retry_pending,
    )
    self._outcome = outcome

    if not retry_pending:
      self._complete(outcome)
    return outcome

  def cancel(
    self,
    *,
    now: float,
    reason: YumaSupplementationReason,
  ) -> YumaSupplementationRuntimeOutcome | None:
    if self._completed:
      return None
    if reason not in (
      YumaSupplementationReason.RELIABLE_FIX_AVAILABLE,
      YumaSupplementationReason.FEATURE_DISABLED,
      YumaSupplementationReason.RECEIVER_CYCLE_RESET,
    ):
      raise ValueError("Unsupported YUMA cancellation reason")

    normalized_now = _validated_monotonic(now, "now")
    latest_result = (
      self._attempt_history[-1].transmit_result
      if self._attempt_history
      else None
    )
    latest_attempt = (
      self._attempt_history[-1].attempt
      if self._attempt_history
      else 0
    )
    latest_elapsed = (
      self._attempt_history[-1].elapsed_ms
      if self._attempt_history
      else None
    )
    outcome = self._build_outcome(
      plan=YumaSupplementationPlan(
        YumaSupplementationAction.SKIP,
        reason,
      ),
      now=normalized_now,
      transmit_result=latest_result,
      transmission_attempt=latest_attempt,
      transmission_elapsed_ms=latest_elapsed,
      terminal=True,
    )
    self._complete(outcome)
    return outcome

  def evaluate(
    self,
    send_message: YumaMessageSender,
    *,
    now: float,
    nav_sat: NavSatQuality | None,
    nav_sat_time: float | None,
    reliable_fix_available: bool,
  ) -> YumaSupplementationRuntimeOutcome | None:
    if self._completed:
      return None
    if not isinstance(reliable_fix_available, bool):
      raise ValueError(
        "reliable_fix_available must be a bool"
      )

    normalized_now = _validated_monotonic(
      now,
      "now",
    )

    if self._retry_at is not None:
      if reliable_fix_available:
        return self.cancel(
          now=normalized_now,
          reason=(
            YumaSupplementationReason.RELIABLE_FIX_AVAILABLE
          ),
        )
      if normalized_now < self._retry_at:
        return None

      assert self._retry_plan is not None
      return self._transmit(
        send_message,
        plan=self._retry_plan,
        satellite_ids=self._retry_satellite_ids,
        now=normalized_now,
      )

    trusted_now = self.trusted_now(normalized_now)
    self._refresh_restored_quality(trusted_now)
    plan = self.controller.evaluate(
      now=normalized_now,
      trusted_now=trusted_now,
      nav_sat=nav_sat,
      nav_sat_time=nav_sat_time,
      reliable_fix_available=reliable_fix_available,
    )
    if plan.action is YumaSupplementationAction.WAIT:
      wait_report_key = (
        plan.reason,
        normalized_now >= self.controller.trusted_time_deadline,
        (
          self.controller.cache_deadline is not None
          and normalized_now >= self.controller.cache_deadline
        ),
        self.controller.nav_sat_observation_expired(
          normalized_now
        ),
        self.controller.last_cache_error,
      )
      if wait_report_key == self._last_wait_report_key:
        return None
      self._last_wait_report_key = wait_report_key
      outcome = self._build_outcome(
        plan=plan,
        now=normalized_now,
        terminal=False,
      )
      self._outcome = outcome
      return outcome

    if plan.action in (
      YumaSupplementationAction.SEND_ALL,
      YumaSupplementationAction.SEND_MISSING,
    ):
      return self._transmit(
        send_message,
        plan=plan,
        satellite_ids=plan.satellite_ids,
        now=normalized_now,
      )

    outcome = self._build_outcome(
      plan=plan,
      now=normalized_now,
      terminal=True,
    )
    self._complete(outcome)
    return outcome
