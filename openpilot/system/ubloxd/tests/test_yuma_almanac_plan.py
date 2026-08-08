import pytest

from openpilot.system.ubloxd.gps_assistance import (
  CACHE_TIER_FRESHNESS_WINDOW_SECONDS,
  NavSatQuality,
)
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaDatabaseRestoreState,
  YumaSupplementationAction,
  YumaSupplementationReason,
  plan_yuma_supplementation,
)


YUMA_PRNS = frozenset((*range(1, 13), *range(14, 33)))


def nav_sat(
  *,
  healthy: frozenset[int] = frozenset(),
  almanac: frozenset[int] = frozenset(),
) -> NavSatQuality:
  return NavSatQuality(
    8,
    8,
    4,
    6,
    8,
    len(almanac),
    5,
    4,
    {"ephemeris": 16},
    gps_satellite_ids=healthy,
    gps_healthy_satellite_ids=healthy,
    gps_almanac_satellite_ids=almanac,
  )


def plan(**overrides):
  arguments = {
    "database_state": YumaDatabaseRestoreState.COMPLETE,
    "database_age_seconds": 60.0,
    "restored_gps_almanac_available": len(YUMA_PRNS),
    "restored_gps_startup_ready": True,
    "restored_gps_almanac_satellite_ids": tuple(sorted(YUMA_PRNS)),
    "yuma_reference_age_seconds": 60.0,
    "nav_sat": None,
    "yuma_satellite_ids": YUMA_PRNS,
    "trusted_time_available": True,
    "reliable_fix_available": False,
    "trusted_time_wait_expired": False,
    "cache_wait_expired": False,
    "nav_sat_observation_expired": False,
  }
  arguments.update(overrides)
  return plan_yuma_supplementation(**arguments)


def test_reliable_fix_stops_supplementation():
  result = plan(
    reliable_fix_available=True,
    trusted_time_available=False,
    yuma_satellite_ids=None,
  )

  assert result.action is YumaSupplementationAction.SKIP
  assert result.reason is YumaSupplementationReason.RELIABLE_FIX_AVAILABLE


def test_route_8b_late_trusted_time_remains_eligible_after_deadline():
  waiting = plan(
    trusted_time_available=False,
    trusted_time_wait_expired=True,
  )

  assert waiting.action is YumaSupplementationAction.WAIT
  assert waiting.reason is YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME


def test_late_yuma_cache_remains_eligible_after_deadline():
  waiting = plan(
    yuma_satellite_ids=None,
    yuma_reference_age_seconds=None,
    cache_wait_expired=True,
  )

  assert waiting.action is YumaSupplementationAction.WAIT
  assert waiting.reason is YumaSupplementationReason.WAITING_FOR_YUMA_CACHE


def test_route_88_exact_ten_almanacs_sends_missing_prns():
  restored = tuple(range(1, 11))
  result = plan(
    restored_gps_almanac_available=10,
    restored_gps_startup_ready=False,
    restored_gps_almanac_satellite_ids=restored,
  )

  assert result.action is YumaSupplementationAction.SEND_MISSING
  assert result.reason is YumaSupplementationReason.RESTORED_GPS_ALMANAC_PRNS_MISSING
  assert result.satellite_ids == YUMA_PRNS - frozenset(restored)


def test_incomplete_restored_count_without_nav_sat_sends_all():
  result = plan(
    restored_gps_almanac_available=10,
    restored_gps_startup_ready=False,
    restored_gps_almanac_satellite_ids=None,
    nav_sat_observation_expired=True,
  )

  assert result.action is YumaSupplementationAction.SEND_ALL
  assert result.reason is YumaSupplementationReason.RESTORED_GPS_ALMANAC_INCOMPLETE
  assert result.satellite_ids == YUMA_PRNS


def test_unknown_restored_count_sends_all_without_nav_sat_wait():
  result = plan(
    restored_gps_almanac_available=None,
    restored_gps_startup_ready=None,
    restored_gps_almanac_satellite_ids=None,
  )

  assert result.action is YumaSupplementationAction.SEND_ALL
  assert result.reason is YumaSupplementationReason.RESTORED_GPS_ALMANAC_UNKNOWN
  assert result.satellite_ids == YUMA_PRNS


def test_unknown_restored_count_with_visible_gap_sends_all_immediately():
  result = plan(
    restored_gps_almanac_available=None,
    restored_gps_startup_ready=None,
    restored_gps_almanac_satellite_ids=None,
    nav_sat=nav_sat(
      healthy=frozenset((1, 2, 3)),
      almanac=frozenset((1, 3)),
    ),
  )

  assert result.action is YumaSupplementationAction.SEND_ALL
  assert result.reason is YumaSupplementationReason.RESTORED_GPS_ALMANAC_UNKNOWN
  assert result.satellite_ids == YUMA_PRNS


def test_count_complete_but_not_startup_ready_sends_all_immediately():
  result = plan(
    restored_gps_almanac_available=len(YUMA_PRNS),
    restored_gps_startup_ready=False,
    restored_gps_almanac_satellite_ids=None,
  )

  assert result.action is YumaSupplementationAction.SEND_ALL
  assert result.reason is YumaSupplementationReason.RESTORED_CACHE_NOT_STARTUP_READY
  assert result.satellite_ids == YUMA_PRNS


def test_legacy_complete_count_without_exact_membership_sends_all():
  visible = frozenset(YUMA_PRNS)
  result = plan(
    restored_gps_almanac_available=len(YUMA_PRNS),
    restored_gps_startup_ready=True,
    restored_gps_almanac_satellite_ids=None,
    nav_sat=nav_sat(healthy=visible, almanac=visible),
    nav_sat_observation_expired=True,
  )

  assert result.action is YumaSupplementationAction.SEND_ALL
  assert result.reason is YumaSupplementationReason.RESTORED_GPS_ALMANAC_UNKNOWN
  assert result.satellite_ids == YUMA_PRNS


def test_sufficient_restored_count_targets_visible_missing_prns():
  result = plan(
    nav_sat=nav_sat(
      healthy=frozenset((1, 2, 3, 4)),
      almanac=frozenset((1, 3)),
    ),
  )

  assert result.action is YumaSupplementationAction.SEND_MISSING
  assert result.reason is YumaSupplementationReason.MISSING_VISIBLE_GPS_ALMANAC
  assert result.satellite_ids == frozenset((2, 4))
  assert result.unavailable_satellite_ids == frozenset()


def test_unhealthy_or_untracked_prns_are_not_targeted():
  result = plan(
    nav_sat=nav_sat(
      healthy=frozenset((1, 2)),
      almanac=frozenset((1,)),
    ),
  )

  assert result.action is YumaSupplementationAction.SEND_MISSING
  assert result.satellite_ids == frozenset((2,))


def test_missing_prns_absent_from_yuma_are_reported():
  result = plan(
    restored_gps_almanac_available=2,
    restored_gps_almanac_satellite_ids=(1, 2),
    nav_sat=nav_sat(
      healthy=frozenset((1, 2, 3)),
      almanac=frozenset((1,)),
    ),
    yuma_satellite_ids=frozenset((1, 2)),
  )

  assert result.action is YumaSupplementationAction.SEND_MISSING
  assert result.satellite_ids == frozenset((2,))
  assert result.unavailable_satellite_ids == frozenset((3,))


def test_no_sendable_visible_missing_prns_skips_with_sufficient_coverage():
  result = plan(
    restored_gps_almanac_available=2,
    restored_gps_almanac_satellite_ids=(1, 2),
    nav_sat=nav_sat(
      healthy=frozenset((31, 32)),
      almanac=frozenset(),
    ),
    yuma_satellite_ids=frozenset((1, 2)),
  )

  assert result.action is YumaSupplementationAction.SKIP
  assert result.reason is YumaSupplementationReason.MISSING_VISIBLE_PRNS_NOT_IN_YUMA
  assert result.unavailable_satellite_ids == frozenset((31, 32))


def test_sufficient_restored_count_and_visible_coverage_skips():
  visible = frozenset((1, 2, 3))
  result = plan(
    nav_sat=nav_sat(healthy=visible, almanac=visible),
  )

  assert result.action is YumaSupplementationAction.SKIP
  assert result.reason is YumaSupplementationReason.VISIBLE_GPS_ALMANAC_COMPLETE


@pytest.mark.parametrize(
  ("database_state", "expected_reason"),
  (
    (
      YumaDatabaseRestoreState.FAILED,
      YumaSupplementationReason.DATABASE_RESTORE_INCOMPLETE,
    ),
    (
      YumaDatabaseRestoreState.PARTIAL,
      YumaSupplementationReason.DATABASE_RESTORE_PARTIAL,
    ),
    (
      YumaDatabaseRestoreState.SKIPPED,
      YumaSupplementationReason.DATABASE_RESTORE_SKIPPED,
    ),
    (
      YumaDatabaseRestoreState.REJECTED,
      YumaSupplementationReason.DATABASE_RESTORE_REJECTED,
    ),
    (
      YumaDatabaseRestoreState.RESPONSE_TIMEOUT,
      YumaSupplementationReason.DATABASE_RESTORE_RESPONSE_TIMEOUT,
    ),
    (
      YumaDatabaseRestoreState.TRANSFER_DEADLINE,
      YumaSupplementationReason.DATABASE_RESTORE_TRANSFER_DEADLINE,
    ),
    (
      YumaDatabaseRestoreState.TRANSPORT_ERROR,
      YumaSupplementationReason.DATABASE_RESTORE_TRANSPORT_ERROR,
    ),
    (
      YumaDatabaseRestoreState.EXPIRED,
      YumaSupplementationReason.DATABASE_RESTORE_EXPIRED,
    ),
  ),
)
def test_noncomplete_database_restore_sends_all_with_exact_reason(
  database_state,
  expected_reason,
):
  result = plan(database_state=database_state)

  assert result.action is YumaSupplementationAction.SEND_ALL
  assert result.reason is expected_reason
  assert result.satellite_ids == YUMA_PRNS

def test_recent_complete_database_without_nav_sat_skips():
  result = plan(
    database_age_seconds=CACHE_TIER_FRESHNESS_WINDOW_SECONDS,
    nav_sat_observation_expired=True,
  )

  assert result.action is YumaSupplementationAction.SKIP
  assert result.reason is YumaSupplementationReason.COMPLETE_DATABASE_IS_RECENT


def test_five_hour_database_uses_newer_yuma_immediately():
  result = plan(
    database_age_seconds=5 * 60 * 60,
    yuma_reference_age_seconds=2 * 60 * 60,
  )

  assert result.action is YumaSupplementationAction.SEND_ALL
  assert result.reason is YumaSupplementationReason.STALE_DATABASE_WITH_NEWER_YUMA


def test_route_8f_stale_unknown_cache_sends_all_without_nav_sat_wait():
  result = plan(
    database_age_seconds=16 * 60 * 60 + 47 * 60,
    yuma_reference_age_seconds=2 * 60 * 60,
    restored_gps_almanac_available=14,
    restored_gps_startup_ready=False,
    restored_gps_almanac_satellite_ids=None,
  )

  assert result.action is YumaSupplementationAction.SEND_ALL
  assert result.reason is YumaSupplementationReason.STALE_DATABASE_WITH_NEWER_YUMA
  assert result.satellite_ids == YUMA_PRNS


def test_five_hour_database_keeps_newer_database_almanac():
  result = plan(
    database_age_seconds=5 * 60 * 60,
    yuma_reference_age_seconds=24 * 60 * 60,
    nav_sat_observation_expired=True,
  )

  assert result.action is YumaSupplementationAction.SKIP
  assert result.reason is YumaSupplementationReason.YUMA_NOT_NEWER_THAN_DATABASE


def test_complete_database_with_unverified_age_skips_blind_overwrite():
  result = plan(
    database_age_seconds=None,
    nav_sat_observation_expired=True,
  )

  assert result.action is YumaSupplementationAction.SKIP
  assert result.reason is YumaSupplementationReason.DATABASE_AGE_UNVERIFIED


@pytest.mark.parametrize(
  ("field", "value", "message"),
  (
    ("database_age_seconds", -1.0, "non-negative finite"),
    ("database_age_seconds", float("inf"), "non-negative finite"),
    ("yuma_reference_age_seconds", float("nan"), "non-negative finite"),
    ("yuma_reference_age_seconds", True, "non-negative finite"),
    ("restored_gps_almanac_available", -1, "0 through 32"),
    ("restored_gps_almanac_available", 33, "0 through 32"),
    ("restored_gps_almanac_available", True, "0 through 32"),
    ("restored_gps_startup_ready", 1, "bool or None"),
  ),
)
def test_invalid_numeric_inputs_are_rejected(field, value, message):
  with pytest.raises(ValueError, match=message):
    plan(**{field: value})


def test_invalid_yuma_prns_are_rejected():
  with pytest.raises(ValueError, match="1 through 32"):
    plan(yuma_satellite_ids=frozenset((0, 1)))


def test_nav_sat_expiry_does_not_expire_trusted_time_wait():
  result = plan(
    trusted_time_available=False,
    trusted_time_wait_expired=True,
    nav_sat_observation_expired=True,
    cache_wait_expired=True,
  )

  assert result.action is YumaSupplementationAction.WAIT
  assert result.reason is YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME


def test_nav_sat_expiry_does_not_expire_cache_wait():
  result = plan(
    yuma_satellite_ids=None,
    yuma_reference_age_seconds=None,
    cache_wait_expired=True,
    nav_sat_observation_expired=True,
  )

  assert result.action is YumaSupplementationAction.WAIT
  assert result.reason is YumaSupplementationReason.WAITING_FOR_YUMA_CACHE


def test_31_restored_almanacs_with_wrong_prn_set_sends_missing():
  restored = tuple(range(1, 32))
  result = plan(
    restored_gps_almanac_available=31,
    restored_gps_startup_ready=True,
    restored_gps_almanac_satellite_ids=restored,
  )

  assert result.action is YumaSupplementationAction.SEND_MISSING
  assert result.reason is YumaSupplementationReason.RESTORED_GPS_ALMANAC_PRNS_MISSING
  assert result.satellite_ids == frozenset((32,))


def test_invalid_restored_prn_metadata_is_rejected():
  with pytest.raises(ValueError, match="unique sorted"):
    plan(restored_gps_almanac_satellite_ids=(2, 1))


def test_pending_database_restore_waits_without_yuma_transmission():
  result = plan(database_state=YumaDatabaseRestoreState.PENDING)

  assert result.action is YumaSupplementationAction.WAIT
  assert result.reason is YumaSupplementationReason.WAITING_FOR_DATABASE_RESTORE
  assert result.satellite_ids == frozenset()
