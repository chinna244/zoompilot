from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from openpilot.system.ubloxd.gps_assistance import (
  CACHE_TIER_FRESHNESS_WINDOW_SECONDS,
  NavSatQuality,
)


class YumaDatabaseRestoreState(StrEnum):
  PENDING = "pending"
  COMPLETE = "complete"
  PARTIAL = "partial"
  SKIPPED = "skipped"
  REJECTED = "rejected"
  RESPONSE_TIMEOUT = "response_timeout"
  TRANSFER_DEADLINE = "transfer_deadline"
  TRANSPORT_ERROR = "transport_error"
  EXPIRED = "expired"
  FAILED = "failed"


class YumaSupplementationAction(StrEnum):
  WAIT = "wait"
  SKIP = "skip"
  SEND_MISSING = "send_missing"
  SEND_ALL = "send_all"


class YumaSupplementationReason(StrEnum):
  RELIABLE_FIX_AVAILABLE = "reliable_fix_available"
  FEATURE_DISABLED = "feature_disabled"
  RECEIVER_CYCLE_RESET = "receiver_cycle_reset"
  WAITING_FOR_TRUSTED_TIME = "waiting_for_trusted_time"
  TRUSTED_TIME_UNAVAILABLE = "trusted_time_unavailable"
  WAITING_FOR_YUMA_CACHE = "waiting_for_yuma_cache"
  YUMA_CACHE_UNAVAILABLE = "yuma_cache_unavailable"
  WAITING_FOR_NAV_SAT = "waiting_for_nav_sat"
  VISIBLE_GPS_ALMANAC_COMPLETE = "visible_gps_almanac_complete"
  MISSING_VISIBLE_GPS_ALMANAC = "missing_visible_gps_almanac"
  MISSING_VISIBLE_PRNS_NOT_IN_YUMA = "missing_visible_prns_not_in_yuma"
  WAITING_FOR_DATABASE_RESTORE = "waiting_for_database_restore"
  DATABASE_RESTORE_INCOMPLETE = "database_restore_incomplete"
  DATABASE_RESTORE_PARTIAL = "database_restore_partial"
  DATABASE_RESTORE_SKIPPED = "database_restore_skipped"
  DATABASE_RESTORE_REJECTED = "database_restore_rejected"
  DATABASE_RESTORE_RESPONSE_TIMEOUT = "database_restore_response_timeout"
  DATABASE_RESTORE_TRANSFER_DEADLINE = "database_restore_transfer_deadline"
  DATABASE_RESTORE_TRANSPORT_ERROR = "database_restore_transport_error"
  DATABASE_RESTORE_EXPIRED = "database_restore_expired"
  RESTORED_GPS_ALMANAC_INCOMPLETE = "restored_gps_almanac_incomplete"
  RESTORED_GPS_ALMANAC_UNKNOWN = "restored_gps_almanac_unknown"
  RESTORED_GPS_ALMANAC_PRNS_MISSING = "restored_gps_almanac_prns_missing"
  RESTORED_CACHE_NOT_STARTUP_READY = "restored_cache_not_startup_ready"
  COMPLETE_DATABASE_IS_RECENT = "complete_database_is_recent"
  DATABASE_AGE_UNVERIFIED = "database_age_unverified"
  YUMA_NOT_NEWER_THAN_DATABASE = "yuma_not_newer_than_database"
  STALE_DATABASE_WITH_NEWER_YUMA = "stale_database_with_newer_yuma"


@dataclass(frozen=True)
class YumaSupplementationPlan:
  action: YumaSupplementationAction
  reason: YumaSupplementationReason
  satellite_ids: frozenset[int] = frozenset()
  unavailable_satellite_ids: frozenset[int] = frozenset()

  @property
  def terminal(self) -> bool:
    return self.action is not YumaSupplementationAction.WAIT


def _validated_age(
  value: float | None,
  field: str,
) -> float | None:
  if value is None:
    return None
  if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value) or value < 0:
    raise ValueError(f"{field} must be a non-negative finite number or None")
  return float(value)


def _validated_optional_count(
  value: int | None,
  field: str,
) -> int | None:
  if value is None:
    return None
  if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 32:
    raise ValueError(f"{field} must be an integer from 0 through 32 or None")
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
  if satellite_ids != tuple(sorted(set(satellite_ids))) or any(
    isinstance(satellite_id, bool) or not isinstance(satellite_id, int) or not 1 <= satellite_id <= 32 for satellite_id in satellite_ids
  ):
    raise ValueError(f"{field} must contain unique sorted integers from 1 through 32")
  return satellite_ids


def _validated_prns(
  satellite_ids: frozenset[int] | None,
) -> frozenset[int] | None:
  if satellite_ids is None:
    return None
  if not isinstance(satellite_ids, frozenset):
    raise ValueError("yuma_satellite_ids must be a frozenset or None")
  if any(isinstance(satellite_id, bool) or not isinstance(satellite_id, int) or not 1 <= satellite_id <= 32 for satellite_id in satellite_ids):
    raise ValueError("YUMA satellite IDs must be integers from 1 through 32")
  return satellite_ids


def plan_yuma_supplementation(
  *,
  database_state: YumaDatabaseRestoreState,
  database_age_seconds: float | None,
  yuma_reference_age_seconds: float | None,
  nav_sat: NavSatQuality | None,
  yuma_satellite_ids: frozenset[int] | None,
  trusted_time_available: bool,
  reliable_fix_available: bool,
  trusted_time_wait_expired: bool,
  cache_wait_expired: bool,
  nav_sat_observation_expired: bool,
  restored_gps_almanac_available: int | None = None,
  restored_gps_startup_ready: bool | None = None,
  restored_gps_almanac_satellite_ids: tuple[int, ...] | None = None,
) -> YumaSupplementationPlan:
  if not isinstance(database_state, YumaDatabaseRestoreState):
    raise ValueError("database_state must be a YumaDatabaseRestoreState")
  if not isinstance(trusted_time_wait_expired, bool):
    raise ValueError("trusted_time_wait_expired must be a bool")
  if not isinstance(cache_wait_expired, bool):
    raise ValueError("cache_wait_expired must be a bool")
  if not isinstance(nav_sat_observation_expired, bool):
    raise ValueError("nav_sat_observation_expired must be a bool")

  database_age = _validated_age(
    database_age_seconds,
    "database_age_seconds",
  )
  restored_gps_almanac = _validated_optional_count(
    restored_gps_almanac_available,
    "restored_gps_almanac_available",
  )
  restored_startup_ready = _validated_optional_bool(
    restored_gps_startup_ready,
    "restored_gps_startup_ready",
  )
  restored_almanac_ids = _validated_optional_prns(
    restored_gps_almanac_satellite_ids,
    "restored_gps_almanac_satellite_ids",
  )
  if restored_almanac_ids is not None and restored_gps_almanac is not None and len(restored_almanac_ids) > restored_gps_almanac:
    raise ValueError("restored GPS almanac PRN count exceeds restored availability")
  yuma_reference_age = _validated_age(
    yuma_reference_age_seconds,
    "yuma_reference_age_seconds",
  )
  yuma_prns = _validated_prns(yuma_satellite_ids)

  if reliable_fix_available:
    return YumaSupplementationPlan(
      YumaSupplementationAction.SKIP,
      YumaSupplementationReason.RELIABLE_FIX_AVAILABLE,
    )

  # The deadline is diagnostic only. A receiver that obtains trusted time
  # later in the same cycle must remain eligible for supplementation.
  if not trusted_time_available:
    return YumaSupplementationPlan(
      YumaSupplementationAction.WAIT,
      YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME,
    )

  # The cache deadline is also diagnostic only. The downloader can publish a
  # valid cache later, and waiting has no receiver-write side effects.
  if yuma_prns is None or yuma_reference_age is None:
    return YumaSupplementationPlan(
      YumaSupplementationAction.WAIT,
      YumaSupplementationReason.WAITING_FOR_YUMA_CACHE,
    )

  if database_state is YumaDatabaseRestoreState.PENDING:
    return YumaSupplementationPlan(
      YumaSupplementationAction.WAIT,
      YumaSupplementationReason.WAITING_FOR_DATABASE_RESTORE,
    )

  incomplete_database_reasons = {
    YumaDatabaseRestoreState.PARTIAL: (
      YumaSupplementationReason.DATABASE_RESTORE_PARTIAL
    ),
    YumaDatabaseRestoreState.SKIPPED: (
      YumaSupplementationReason.DATABASE_RESTORE_SKIPPED
    ),
    YumaDatabaseRestoreState.REJECTED: (
      YumaSupplementationReason.DATABASE_RESTORE_REJECTED
    ),
    YumaDatabaseRestoreState.RESPONSE_TIMEOUT: (
      YumaSupplementationReason.DATABASE_RESTORE_RESPONSE_TIMEOUT
    ),
    YumaDatabaseRestoreState.TRANSFER_DEADLINE: (
      YumaSupplementationReason.DATABASE_RESTORE_TRANSFER_DEADLINE
    ),
    YumaDatabaseRestoreState.TRANSPORT_ERROR: (
      YumaSupplementationReason.DATABASE_RESTORE_TRANSPORT_ERROR
    ),
    YumaDatabaseRestoreState.EXPIRED: (
      YumaSupplementationReason.DATABASE_RESTORE_EXPIRED
    ),
    YumaDatabaseRestoreState.FAILED: (
      YumaSupplementationReason.DATABASE_RESTORE_INCOMPLETE
    ),
  }
  incomplete_reason = incomplete_database_reasons.get(database_state)
  if incomplete_reason is not None:
    return YumaSupplementationPlan(
      YumaSupplementationAction.SEND_ALL,
      incomplete_reason,
      satellite_ids=yuma_prns,
    )

  if database_age is not None and database_age > CACHE_TIER_FRESHNESS_WINDOW_SECONDS and yuma_reference_age <= database_age:
    return YumaSupplementationPlan(
      YumaSupplementationAction.SEND_ALL,
      YumaSupplementationReason.STALE_DATABASE_WITH_NEWER_YUMA,
      satellite_ids=yuma_prns,
    )

  if restored_almanac_ids is None:
    if restored_gps_almanac is None:
      return YumaSupplementationPlan(
        YumaSupplementationAction.SEND_ALL,
        YumaSupplementationReason.RESTORED_GPS_ALMANAC_UNKNOWN,
        satellite_ids=yuma_prns,
      )
    if restored_gps_almanac < len(yuma_prns):
      return YumaSupplementationPlan(
        YumaSupplementationAction.SEND_ALL,
        YumaSupplementationReason.RESTORED_GPS_ALMANAC_INCOMPLETE,
        satellite_ids=yuma_prns,
      )
    if restored_startup_ready is not True:
      return YumaSupplementationPlan(
        YumaSupplementationAction.SEND_ALL,
        YumaSupplementationReason.RESTORED_CACHE_NOT_STARTUP_READY,
        satellite_ids=yuma_prns,
      )

  if restored_almanac_ids is not None:
    restored_prns = frozenset(restored_almanac_ids)
    missing_restored_prns = yuma_prns - restored_prns
    if missing_restored_prns:
      return YumaSupplementationPlan(
        YumaSupplementationAction.SEND_MISSING,
        YumaSupplementationReason.RESTORED_GPS_ALMANAC_PRNS_MISSING,
        satellite_ids=missing_restored_prns,
      )

    if nav_sat is not None:
      healthy_prns = nav_sat.gps_healthy_satellite_ids
      missing_prns = healthy_prns - nav_sat.gps_almanac_satellite_ids
      if missing_prns:
        sendable_prns = missing_prns & yuma_prns
        unavailable_prns = missing_prns - yuma_prns
        if sendable_prns:
          return YumaSupplementationPlan(
            YumaSupplementationAction.SEND_MISSING,
            YumaSupplementationReason.MISSING_VISIBLE_GPS_ALMANAC,
            satellite_ids=sendable_prns,
            unavailable_satellite_ids=unavailable_prns,
          )
        return YumaSupplementationPlan(
          YumaSupplementationAction.SKIP,
          YumaSupplementationReason.MISSING_VISIBLE_PRNS_NOT_IN_YUMA,
          unavailable_satellite_ids=unavailable_prns,
        )
      if healthy_prns:
        return YumaSupplementationPlan(
          YumaSupplementationAction.SKIP,
          YumaSupplementationReason.VISIBLE_GPS_ALMANAC_COMPLETE,
        )

    if not nav_sat_observation_expired:
      return YumaSupplementationPlan(
        YumaSupplementationAction.WAIT,
        YumaSupplementationReason.WAITING_FOR_NAV_SAT,
      )

  else:
    if nav_sat is not None:
      healthy_prns = nav_sat.gps_healthy_satellite_ids
      missing_prns = healthy_prns - nav_sat.gps_almanac_satellite_ids
      if missing_prns:
        unavailable_prns = missing_prns - yuma_prns
        if restored_gps_almanac is None or restored_gps_almanac < len(yuma_prns) or restored_startup_ready is not True:
          return YumaSupplementationPlan(
            YumaSupplementationAction.SEND_ALL,
            YumaSupplementationReason.RESTORED_GPS_ALMANAC_UNKNOWN,
            satellite_ids=yuma_prns,
            unavailable_satellite_ids=unavailable_prns,
          )
        sendable_prns = missing_prns & yuma_prns
        if sendable_prns:
          return YumaSupplementationPlan(
            YumaSupplementationAction.SEND_MISSING,
            YumaSupplementationReason.MISSING_VISIBLE_GPS_ALMANAC,
            satellite_ids=sendable_prns,
            unavailable_satellite_ids=unavailable_prns,
          )

    if not nav_sat_observation_expired:
      return YumaSupplementationPlan(
        YumaSupplementationAction.WAIT,
        YumaSupplementationReason.WAITING_FOR_NAV_SAT,
      )

    # A legacy cache count cannot prove membership. After the post-ready
    # observation window, supplement the complete validated YUMA snapshot
    # rather than allowing an age-only skip.
    return YumaSupplementationPlan(
      YumaSupplementationAction.SEND_ALL,
      YumaSupplementationReason.RESTORED_GPS_ALMANAC_UNKNOWN,
      satellite_ids=yuma_prns,
    )

  if database_age is None:
    return YumaSupplementationPlan(
      YumaSupplementationAction.SKIP,
      YumaSupplementationReason.DATABASE_AGE_UNVERIFIED,
    )

  if database_age <= CACHE_TIER_FRESHNESS_WINDOW_SECONDS:
    return YumaSupplementationPlan(
      YumaSupplementationAction.SKIP,
      YumaSupplementationReason.COMPLETE_DATABASE_IS_RECENT,
    )

  if yuma_reference_age > database_age:
    return YumaSupplementationPlan(
      YumaSupplementationAction.SKIP,
      YumaSupplementationReason.YUMA_NOT_NEWER_THAN_DATABASE,
    )

  return YumaSupplementationPlan(
    YumaSupplementationAction.SEND_ALL,
    YumaSupplementationReason.STALE_DATABASE_WITH_NEWER_YUMA,
    satellite_ids=yuma_prns,
  )
