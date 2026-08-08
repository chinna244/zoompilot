import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from pathlib import Path

from openpilot.common.swaglog import cloudlog
from openpilot.system.ubloxd.gps_assistance import (
  MAXIMUM_NAV_SAT_AGE_SECONDS,
  NavSatQuality,
)
from openpilot.system.ubloxd.yuma_almanac import (
  YumaAlmanac,
  YumaAlmanacError,
  validate_yuma_reference_time,
)
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaDatabaseRestoreState,
  YumaSupplementationPlan,
  plan_yuma_supplementation,
)
from openpilot.system.ubloxd.yuma_almanac_store import (
  YUMA_ALMANAC_CACHE_PATH,
  StoredYumaAlmanac,
  load_yuma_almanac,
)


YUMA_NAV_SAT_OBSERVATION_SECONDS = 15.0
YUMA_CACHE_WAIT_SECONDS = 30.0
YUMA_TRUSTED_TIME_WAIT_SECONDS = 180.0
YUMA_CACHE_RETRY_SECONDS = 1.0
YUMA_CACHE_SLOW_RETRY_SECONDS = 30.0

YumaCacheLoader = Callable[[Path], StoredYumaAlmanac]
YumaReferenceValidator = Callable[[YumaAlmanac, datetime], datetime]


@dataclass(frozen=True)
class YumaCacheObservation:
  stored: StoredYumaAlmanac
  satellite_ids: frozenset[int]
  reference_time_utc: datetime
  downloaded_at_utc: datetime
  snapshot_sha256: str


def _validated_nonnegative_finite(
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


def _validated_positive_finite(
  value: float,
  field: str,
) -> float:
  normalized = _validated_nonnegative_finite(value, field)
  if normalized == 0:
    raise ValueError(f"{field} must be positive")
  return normalized


def _validated_optional_text(
  value: str | None,
  field: str,
) -> str | None:
  if value is None:
    return None
  if not isinstance(value, str) or not value.strip():
    raise ValueError(f"{field} must be a non-empty string or None")
  return value.strip()


def _validated_optional_count(
  value: int | None,
  field: str,
  *,
  maximum: int | None = None,
) -> int | None:
  if value is None:
    return None
  if (
    isinstance(value, bool)
    or not isinstance(value, int)
    or value < 0
    or (maximum is not None and value > maximum)
  ):
    suffix = (
      f" from 0 through {maximum}"
      if maximum is not None
      else " that is non-negative"
    )
    raise ValueError(f"{field} must be an integer{suffix} or None")
  return value


def _validated_optional_bool(
  value: bool | None,
  field: str,
) -> bool | None:
  if value is None:
    return None
  if not isinstance(value, bool):
    raise ValueError(f"{field} must be a bool or None")
  return value


def _validated_optional_prns(
  satellite_ids: tuple[int, ...] | None,
  field: str,
) -> tuple[int, ...] | None:
  if satellite_ids is None:
    return None
  if not isinstance(satellite_ids, tuple):
    raise ValueError(f"{field} must be a tuple or None")
  if (
    satellite_ids != tuple(sorted(set(satellite_ids)))
    or any(
      isinstance(satellite_id, bool)
      or not isinstance(satellite_id, int)
      or not 1 <= satellite_id <= 32
      for satellite_id in satellite_ids
    )
  ):
    raise ValueError(
      f"{field} must contain unique sorted integers from 1 through 32"
    )
  return satellite_ids


def _trusted_utc_or_none(
  value: datetime | None,
) -> datetime | None:
  if value is None:
    return None
  if value.tzinfo is None or value.utcoffset() is None:
    raise ValueError("trusted_now must be timezone-aware")
  return value.astimezone(UTC)


def _aware_utc_or_none(
  value: datetime | None,
  field: str,
) -> datetime | None:
  if value is None:
    return None
  if value.tzinfo is None or value.utcoffset() is None:
    raise ValueError(f"{field} must be timezone-aware")
  return value.astimezone(UTC)


def _age_seconds(
  newer: datetime | None,
  older: datetime | None,
) -> float | None:
  if newer is None or older is None:
    return None
  age = (newer - older).total_seconds()
  return age if age >= 0 else None


class YumaSupplementationController:
  def __init__(
    self,
    *,
    database_state: YumaDatabaseRestoreState,
    database_saved_at_utc: datetime | None,
    started_at: float,
    restored_cache_generation: str | None = None,
    restored_cache_selection_reason: str | None = None,
    restored_gps_almanac_available: int | None = None,
    restored_glonass_almanac_available: int | None = None,
    restored_gps_ephemeris_available: int | None = None,
    restored_glonass_ephemeris_available: int | None = None,
    restored_satellites_used: int | None = None,
    restored_gps_startup_ready: bool | None = None,
    restored_gps_almanac_satellite_ids: tuple[int, ...] | None = None,
    path: Path = YUMA_ALMANAC_CACHE_PATH,
    nav_sat_observation_seconds: float = (
      YUMA_NAV_SAT_OBSERVATION_SECONDS
    ),
    cache_wait_seconds: float = YUMA_CACHE_WAIT_SECONDS,
    trusted_time_wait_seconds: float = (
      YUMA_TRUSTED_TIME_WAIT_SECONDS
    ),
    cache_retry_seconds: float = YUMA_CACHE_RETRY_SECONDS,
    cache_slow_retry_seconds: float = (
      YUMA_CACHE_SLOW_RETRY_SECONDS
    ),
    cache_loader: YumaCacheLoader = load_yuma_almanac,
    reference_validator: YumaReferenceValidator = (
      validate_yuma_reference_time
    ),
  ) -> None:
    if not isinstance(
      database_state,
      YumaDatabaseRestoreState,
    ):
      raise ValueError(
        "database_state must be a YumaDatabaseRestoreState"
      )

    self.database_state = database_state
    self.database_saved_at_utc = _aware_utc_or_none(
      database_saved_at_utc,
      "database_saved_at_utc",
    )
    self.restored_cache_generation = _validated_optional_text(
      restored_cache_generation,
      "restored_cache_generation",
    )
    self.restored_cache_selection_reason = _validated_optional_text(
      restored_cache_selection_reason,
      "restored_cache_selection_reason",
    )
    self.restored_gps_almanac_available = _validated_optional_count(
      restored_gps_almanac_available,
      "restored_gps_almanac_available",
      maximum=32,
    )
    self.restored_glonass_almanac_available = _validated_optional_count(
      restored_glonass_almanac_available,
      "restored_glonass_almanac_available",
    )
    self.restored_gps_ephemeris_available = _validated_optional_count(
      restored_gps_ephemeris_available,
      "restored_gps_ephemeris_available",
      maximum=32,
    )
    self.restored_glonass_ephemeris_available = _validated_optional_count(
      restored_glonass_ephemeris_available,
      "restored_glonass_ephemeris_available",
    )
    self.restored_satellites_used = _validated_optional_count(
      restored_satellites_used,
      "restored_satellites_used",
    )
    self.restored_gps_startup_ready = _validated_optional_bool(
      restored_gps_startup_ready,
      "restored_gps_startup_ready",
    )
    self.restored_gps_almanac_satellite_ids = _validated_optional_prns(
      restored_gps_almanac_satellite_ids,
      "restored_gps_almanac_satellite_ids",
    )
    self.started_at = _validated_nonnegative_finite(
      started_at,
      "started_at",
    )
    self.path = path
    self.nav_sat_observation_seconds = (
      _validated_positive_finite(
        nav_sat_observation_seconds,
        "nav_sat_observation_seconds",
      )
    )
    self.cache_wait_seconds = _validated_positive_finite(
      cache_wait_seconds,
      "cache_wait_seconds",
    )
    self.trusted_time_wait_seconds = (
      _validated_positive_finite(
        trusted_time_wait_seconds,
        "trusted_time_wait_seconds",
      )
    )
    self.cache_retry_seconds = _validated_positive_finite(
      cache_retry_seconds,
      "cache_retry_seconds",
    )
    self.cache_slow_retry_seconds = _validated_positive_finite(
      cache_slow_retry_seconds,
      "cache_slow_retry_seconds",
    )
    self._cache_loader = cache_loader
    self._reference_validator = reference_validator
    self._next_cache_attempt_at = self.started_at
    self._cache_wait_started_at: float | None = None
    self._cache_observation: YumaCacheObservation | None = None
    self._decision_ready_at: float | None = None
    self._last_decision_nav_sat_time: float | None = None
    self._terminal_plan: YumaSupplementationPlan | None = None
    self._last_cache_error: str | None = None

  @property
  def nav_sat_deadline(self) -> float | None:
    if self._decision_ready_at is None:
      return None
    return self._decision_ready_at + self.nav_sat_observation_seconds

  @property
  def decision_ready_at(self) -> float | None:
    return self._decision_ready_at

  @property
  def last_decision_nav_sat_time(self) -> float | None:
    return self._last_decision_nav_sat_time

  def nav_sat_observation_expired(self, now: float) -> bool:
    deadline = self.nav_sat_deadline
    return deadline is not None and now >= deadline

  @property
  def trusted_time_deadline(self) -> float:
    return self.started_at + self.trusted_time_wait_seconds

  @property
  def cache_deadline(self) -> float | None:
    if self._cache_wait_started_at is None:
      return None
    return self._cache_wait_started_at + self.cache_wait_seconds

  @property
  def cache_observation(self) -> YumaCacheObservation | None:
    return self._cache_observation

  @property
  def last_cache_error(self) -> str | None:
    return self._last_cache_error

  @property
  def terminal_plan(self) -> YumaSupplementationPlan | None:
    return self._terminal_plan

  def set_restored_quality(
    self,
    *,
    gps_ephemeris_available: int | None,
    glonass_ephemeris_available: int | None,
    gps_startup_ready: bool | None,
  ) -> None:
    if self._terminal_plan is not None:
      return
    self.restored_gps_ephemeris_available = (
      _validated_optional_count(
        gps_ephemeris_available,
        "gps_ephemeris_available",
        maximum=32,
      )
    )
    self.restored_glonass_ephemeris_available = (
      _validated_optional_count(
        glonass_ephemeris_available,
        "glonass_ephemeris_available",
      )
    )
    self.restored_gps_startup_ready = _validated_optional_bool(
      gps_startup_ready,
      "gps_startup_ready",
    )

  def _observe_cache(
    self,
    now: float,
    trusted_now: datetime | None,
  ) -> None:
    if (
      trusted_now is None
      or self._cache_observation is not None
      or now < self._next_cache_attempt_at
    ):
      return

    retry_seconds = (
      self.cache_slow_retry_seconds
      if (
        self.cache_deadline is not None
        and now >= self.cache_deadline
      )
      else self.cache_retry_seconds
    )
    self._next_cache_attempt_at = now + retry_seconds

    try:
      stored = self._cache_loader(self.path)
      reference_time = self._reference_validator(
        stored.almanac,
        trusted_now,
      )
      satellite_ids = frozenset(
        frame[8]
        for frame in stored.almanac.frames
      )
      if not satellite_ids:
        raise YumaAlmanacError(
          "YUMA cache contains no satellite frames"
        )
    except (OSError, YumaAlmanacError) as exc:
      self._last_cache_error = f"{type(exc).__name__}: {exc}"
      return
    except Exception as exc:
      self._last_cache_error = f"{type(exc).__name__}: {exc}"
      cloudlog.exception(
        "Unexpected public YUMA cache observation failure"
      )
      return

    self._cache_observation = YumaCacheObservation(
      stored=stored,
      satellite_ids=satellite_ids,
      reference_time_utc=reference_time,
      downloaded_at_utc=stored.downloaded_at_utc,
      snapshot_sha256=hashlib.sha256(
        stored.almanac.ubx_data
      ).hexdigest(),
    )
    self._last_cache_error = None

  def _fresh_nav_sat(
    self,
    now: float,
    nav_sat: NavSatQuality | None,
    nav_sat_time: float | None,
  ) -> NavSatQuality | None:
    if (
      nav_sat is None
      or nav_sat_time is None
      or self._decision_ready_at is None
    ):
      return None

    observed_at = _validated_nonnegative_finite(
      nav_sat_time,
      "nav_sat_time",
    )
    if (
      observed_at < self._decision_ready_at
      or now < observed_at
      or now - observed_at > MAXIMUM_NAV_SAT_AGE_SECONDS
    ):
      return None
    self._last_decision_nav_sat_time = observed_at
    return nav_sat

  def evaluate(
    self,
    *,
    now: float,
    trusted_now: datetime | None,
    nav_sat: NavSatQuality | None,
    nav_sat_time: float | None,
    reliable_fix_available: bool,
  ) -> YumaSupplementationPlan:
    if self._terminal_plan is not None:
      return self._terminal_plan
    if not isinstance(reliable_fix_available, bool):
      raise ValueError(
        "reliable_fix_available must be a bool"
      )

    normalized_now = _validated_nonnegative_finite(
      now,
      "now",
    )
    normalized_trusted_now = _trusted_utc_or_none(
      trusted_now,
    )

    if reliable_fix_available:
      plan = plan_yuma_supplementation(
        database_state=self.database_state,
        database_age_seconds=None,
        restored_gps_almanac_available=(
          self.restored_gps_almanac_available
        ),
        restored_gps_startup_ready=(
          self.restored_gps_startup_ready
        ),
        restored_gps_almanac_satellite_ids=(
          self.restored_gps_almanac_satellite_ids
        ),
        yuma_reference_age_seconds=None,
        nav_sat=None,
        yuma_satellite_ids=None,
        trusted_time_available=(
          normalized_trusted_now is not None
        ),
        reliable_fix_available=True,
        trusted_time_wait_expired=(
          normalized_now >= self.trusted_time_deadline
        ),
        cache_wait_expired=(
          self.cache_deadline is not None
          and normalized_now >= self.cache_deadline
        ),
        nav_sat_observation_expired=(
          self.nav_sat_observation_expired(normalized_now)
        ),
      )
      self._terminal_plan = plan
      return plan

    if (
      normalized_trusted_now is not None
      and self._cache_wait_started_at is None
    ):
      self._cache_wait_started_at = normalized_now

    self._observe_cache(
      normalized_now,
      normalized_trusted_now,
    )

    observation = self._cache_observation
    if (
      observation is not None
      and normalized_trusted_now is not None
      and self._decision_ready_at is None
    ):
      self._decision_ready_at = normalized_now

    reference_age = None
    if observation is not None and normalized_trusted_now is not None:
      reference_age = max(
        0.0,
        (
          normalized_trusted_now
          - observation.reference_time_utc
        ).total_seconds(),
      )

    plan = plan_yuma_supplementation(
      database_state=self.database_state,
      database_age_seconds=_age_seconds(
        normalized_trusted_now,
        self.database_saved_at_utc,
      ),
      restored_gps_almanac_available=(
        self.restored_gps_almanac_available
      ),
      restored_gps_startup_ready=(
        self.restored_gps_startup_ready
      ),
      restored_gps_almanac_satellite_ids=(
        self.restored_gps_almanac_satellite_ids
      ),
      yuma_reference_age_seconds=reference_age,
      nav_sat=self._fresh_nav_sat(
        normalized_now,
        nav_sat,
        nav_sat_time,
      ),
      yuma_satellite_ids=(
        observation.satellite_ids
        if observation is not None
        else None
      ),
      trusted_time_available=(
        normalized_trusted_now is not None
      ),
      reliable_fix_available=reliable_fix_available,
      trusted_time_wait_expired=(
        normalized_now >= self.trusted_time_deadline
      ),
      cache_wait_expired=(
        self.cache_deadline is not None
        and normalized_now >= self.cache_deadline
      ),
      nav_sat_observation_expired=(
        self.nav_sat_observation_expired(normalized_now)
      ),
    )

    if plan.terminal:
      self._terminal_plan = plan
    return plan
