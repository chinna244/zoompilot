from datetime import UTC, datetime, timedelta

import pytest

from openpilot.common.gps_time import (
  GPS_EPOCH_UTC,
  GPS_UTC_LEAP_SECONDS,
  GPS_UTC_LEAP_SECONDS_VALID_FROM_WEEK,
  GPS_WEEK_FULL_MAX_INCLUSIVE,
  GPS_WEEK_MILLISECONDS,
  GPS_WEEK_ROLLOVER_MODULO,
  UBLOX_FIX_TYPE_2D,
  UBLOX_FIX_TYPE_3D,
  UBLOX_FIX_TYPE_DEAD_RECKONING,
  UBLOX_FIX_TYPE_GNSS_DR,
  UBLOX_FIX_TYPE_NO_FIX,
  UBLOX_FIX_TYPE_TIME_ONLY,
  encode_ublox_gps_flags,
  gps_week_tow_to_unix_millis,
  rawx_full_week_is_trusted_era_evidence,
  representable_gps_utc_maximum,
  resolve_gps_week_mod_1024,
  ublox_gps_time_valid,
  ublox_nav_pvt_has_fix,
  utc_to_gps_week_tow,
)
from openpilot.common.time_helpers import MAX_DATE, MIN_DATE


def test_encode_preserves_fix_flags():
  flags = encode_ublox_gps_flags(0xA5, 0x07)

  assert flags & 0xFF == 0xA5
  assert (flags >> 8) & 0xFF == 0x07


def test_time_requires_fully_resolved_date_and_time():
  assert ublox_gps_time_valid(encode_ublox_gps_flags(1, 0x07))
  assert not ublox_gps_time_valid(encode_ublox_gps_flags(1, 0x00))
  assert not ublox_gps_time_valid(encode_ublox_gps_flags(1, 0x01))
  assert not ublox_gps_time_valid(encode_ublox_gps_flags(1, 0x02))
  assert not ublox_gps_time_valid(encode_ublox_gps_flags(1, 0x03))


def test_nav_pvt_has_fix_matrix():
  for fix_type in (
    UBLOX_FIX_TYPE_NO_FIX,
    UBLOX_FIX_TYPE_DEAD_RECKONING,
    UBLOX_FIX_TYPE_2D,
    UBLOX_FIX_TYPE_3D,
    UBLOX_FIX_TYPE_GNSS_DR,
    UBLOX_FIX_TYPE_TIME_ONLY,
  ):
    assert ublox_nav_pvt_has_fix(0x00, fix_type) is False

  assert ublox_nav_pvt_has_fix(0x01, UBLOX_FIX_TYPE_NO_FIX) is False
  assert ublox_nav_pvt_has_fix(0x01, UBLOX_FIX_TYPE_DEAD_RECKONING) is False
  assert ublox_nav_pvt_has_fix(0x01, UBLOX_FIX_TYPE_2D) is False
  assert ublox_nav_pvt_has_fix(0x01, UBLOX_FIX_TYPE_TIME_ONLY) is False
  assert ublox_nav_pvt_has_fix(0x01, UBLOX_FIX_TYPE_3D) is True
  assert ublox_nav_pvt_has_fix(0x01, UBLOX_FIX_TYPE_GNSS_DR) is True
  assert ublox_nav_pvt_has_fix(0x02, UBLOX_FIX_TYPE_3D) is False


def test_gps_tow_bounds():
  week = GPS_UTC_LEAP_SECONDS_VALID_FROM_WEEK
  assert gps_week_tow_to_unix_millis(week, 0.0) > 0
  assert gps_week_tow_to_unix_millis(week, GPS_WEEK_MILLISECONDS - 1) > 0
  with pytest.raises(ValueError):
    gps_week_tow_to_unix_millis(week, GPS_WEEK_MILLISECONDS)
  with pytest.raises(ValueError):
    gps_week_tow_to_unix_millis(week, GPS_WEEK_MILLISECONDS + 1)
  with pytest.raises(ValueError):
    gps_week_tow_to_unix_millis(week, -1.0)
  with pytest.raises(ValueError):
    gps_week_tow_to_unix_millis(week, float("nan"))
  with pytest.raises(ValueError):
    gps_week_tow_to_unix_millis(week, float("inf"))


def test_historical_week_rejected_under_default_leap_authority():
  with pytest.raises(ValueError):
    gps_week_tow_to_unix_millis(0, 0.0)
  expected = GPS_EPOCH_UTC.timestamp() * 1e3
  assert gps_week_tow_to_unix_millis(0, 0.0, leap_seconds=0) == pytest.approx(expected)


def test_current_era_known_conversion():
  week = 2300
  expected = (GPS_EPOCH_UTC + timedelta(weeks=week) - timedelta(seconds=GPS_UTC_LEAP_SECONDS)).timestamp() * 1e3
  assert gps_week_tow_to_unix_millis(week, 0.0) == pytest.approx(expected)


def test_gps_week_tow_leap_offset_applied_once():
  week = GPS_UTC_LEAP_SECONDS_VALID_FROM_WEEK
  with_default = gps_week_tow_to_unix_millis(week, 0.0)
  with_zero_leap = gps_week_tow_to_unix_millis(week, 0.0, leap_seconds=0)
  assert with_zero_leap - with_default == pytest.approx(GPS_UTC_LEAP_SECONDS * 1000.0)


def test_maintained_leap_has_no_artificial_rollover_cutoff():
  # Week 3072 (~2038-11-21) and 2040 must NOT be rejected solely for GPS rollover.
  for year, month, day in (
    (2026, 8, 8),
    (2035, 1, 1),
    (2038, 11, 20),  # before week 3072
    (2038, 11, 21),  # GPS week 3072 boundary
    (2040, 1, 1),
  ):
    utc = datetime(year, month, day, tzinfo=UTC)
    week, tow_ms = utc_to_gps_week_tow(utc, leap_seconds=GPS_UTC_LEAP_SECONDS)
    assert week >= GPS_UTC_LEAP_SECONDS_VALID_FROM_WEEK
    roundtrip = gps_week_tow_to_unix_millis(week, tow_ms)
    assert roundtrip == pytest.approx(utc.timestamp() * 1e3, abs=1.0)

  # Explicit leap overrides maintained default.
  week = 3072
  with_default = gps_week_tow_to_unix_millis(week, 0.0)
  with_explicit = gps_week_tow_to_unix_millis(week, 0.0, leap_seconds=18)
  assert with_default == pytest.approx(with_explicit)
  with_other = gps_week_tow_to_unix_millis(week, 0.0, leap_seconds=19)
  assert abs(with_other - with_default) == pytest.approx(1000.0)


def test_max_date_is_representation_derived_not_2035_sunset():
  assert MAX_DATE.year != 2035
  assert MAX_DATE == representable_gps_utc_maximum().astimezone(UTC).replace(tzinfo=None)
  assert MIN_DATE < datetime(2035, 1, 1) < MAX_DATE
  assert datetime(2035, 1, 1, tzinfo=UTC) < representable_gps_utc_maximum()


def test_resolve_modulo_week_requires_era_evidence():
  with pytest.raises(ValueError):
    resolve_gps_week_mod_1024(100)
  with pytest.raises(ValueError):
    resolve_gps_week_mod_1024(-1)
  with pytest.raises(ValueError):
    resolve_gps_week_mod_1024(GPS_WEEK_ROLLOVER_MODULO)


def test_resolve_modulo_week_with_full_week_reference():
  assert resolve_gps_week_mod_1024(100, trusted_full_week=2200) == 2148
  assert resolve_gps_week_mod_1024(853, trusted_full_week=2900) == 2901
  assert resolve_gps_week_mod_1024(1023, trusted_full_week=2048) == 2047


def test_resolve_modulo_week_ambiguous_midpoint_fail_closed():
  with pytest.raises(ValueError, match="ambiguous"):
    resolve_gps_week_mod_1024(0, trusted_full_week=512)
  # Equivalent midpoint in a later era.
  with pytest.raises(ValueError, match="ambiguous"):
    resolve_gps_week_mod_1024(0, trusted_full_week=512 + GPS_WEEK_ROLLOVER_MODULO)


def test_resolve_modulo_week_with_trusted_utc():
  trusted = datetime(2026, 8, 8, tzinfo=UTC)
  resolved = resolve_gps_week_mod_1024(100, trusted_utc=trusted)
  assert resolved % GPS_WEEK_ROLLOVER_MODULO == 100
  assert abs(resolved - 2200) < GPS_WEEK_ROLLOVER_MODULO


def test_rawx_trusted_era_requires_nonempty_measurements():
  assert rawx_full_week_is_trusted_era_evidence(week=2411, num_meas=1)
  assert not rawx_full_week_is_trusted_era_evidence(week=2411, num_meas=0)
  assert not rawx_full_week_is_trusted_era_evidence(week=0, num_meas=4)
  assert not rawx_full_week_is_trusted_era_evidence(week=-1, num_meas=1)


def test_representable_ceiling_and_invalid_weeks():
  assert representable_gps_utc_maximum() == GPS_EPOCH_UTC + timedelta(weeks=GPS_WEEK_FULL_MAX_INCLUSIVE + 1)
  with pytest.raises(ValueError):
    gps_week_tow_to_unix_millis(-1, 0.0)
  with pytest.raises(ValueError):
    gps_week_tow_to_unix_millis(0xFFFF, 0.0)
  with pytest.raises(ValueError):
    utc_to_gps_week_tow(datetime(1970, 1, 1, tzinfo=UTC), leap_seconds=0)
