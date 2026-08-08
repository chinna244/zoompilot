"""Helpers for GPS time, NAV-PVT validity, week era resolution, and leap seconds.

PR82 contracts:
  FULL GPS WEEK: unambiguous absolute week in [0, 0xFFFE]
  ROLLED/MODULO WEEK (10-bit): requires trustworthy era evidence
  UNKNOWN/UNTRUSTED WEEK: fail closed

  Leap-second policy:
    Maintain known IERS GPS-UTC offset data explicitly.
    Historical eras without a known mapping fail closed under the default path.
    Callers with independent authority (receiver leapSecValid, explicit leap_seconds)
    may still convert. Do NOT invent future leaps. Do NOT treat GPS week rollover
    boundaries as leap-second authority. Update the maintained table/constant when
    an actual leap-second change is announced.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from math import isfinite

UBLOX_NAV_PVT_VALID_SHIFT = 8
UBLOX_NAV_PVT_TRUSTED_TIME_MASK = 0x07

# NAV-PVT fixType (u-blox Interface Description).
UBLOX_FIX_TYPE_NO_FIX = 0
UBLOX_FIX_TYPE_DEAD_RECKONING = 1
UBLOX_FIX_TYPE_2D = 2
UBLOX_FIX_TYPE_3D = 3
UBLOX_FIX_TYPE_GNSS_DR = 4
UBLOX_FIX_TYPE_TIME_ONLY = 5

# gpsLocationExternal.hasFix policy for downstream KF consumers (locationd_llk):
# advertise only GNSS-anchored solutions that safely support lat/lon/alt + NED.
# 2D is rejected (altitude unconstrained). DR-only and time-only are rejected.
UBLOX_HAS_FIX_TYPES = (
  UBLOX_FIX_TYPE_3D,
  UBLOX_FIX_TYPE_GNSS_DR,
)

# GPS epoch and week representation.
GPS_EPOCH_UTC = datetime(1980, 1, 6, tzinfo=UTC)
GPS_WEEK_SECONDS = 7 * 24 * 60 * 60
GPS_WEEK_MILLISECONDS = GPS_WEEK_SECONDS * 1000
GPS_WEEK_ROLLOVER_MODULO = 1024
# cereal MeasurementReport.gpsWeek / conversion helpers accept weeks in [0, 0xFFFF).
GPS_WEEK_FULL_MAX_INCLUSIVE = 0xFFFE

# Maintained known GPS-UTC leap-second data (IERS). Update when a new leap is announced.
# GPS week 1930 begins 2017-01-01; the 18s offset applies for week >= 1930.
# Entries are (valid_from_week_inclusive, leap_seconds). There is no artificial
# upper GPS-rollover cutoff — future weeks use the latest known maintained offset
# until the table is updated for a real leap-second change.
GPS_UTC_LEAP_SECONDS = 18
GPS_UTC_LEAP_SECONDS_VALID_FROM_WEEK = 1930
GPS_UTC_LEAP_SECOND_TABLE: tuple[tuple[int, int], ...] = ((GPS_UTC_LEAP_SECONDS_VALID_FROM_WEEK, GPS_UTC_LEAP_SECONDS),)


def encode_ublox_gps_flags(fix_flags: int, time_valid_flags: int) -> int:
  """Pack existing u-blox fix flags and NAV-PVT validity into UInt16."""
  return (fix_flags & 0xFF) | ((time_valid_flags & 0xFF) << UBLOX_NAV_PVT_VALID_SHIFT)


def ublox_gps_time_valid(flags: int) -> bool:
  """Return whether NAV-PVT date/time are valid and fully resolved."""
  validity = (flags >> UBLOX_NAV_PVT_VALID_SHIFT) & 0xFF
  return (validity & UBLOX_NAV_PVT_TRUSTED_TIME_MASK) == UBLOX_NAV_PVT_TRUSTED_TIME_MASK


def ublox_nav_pvt_has_fix(flags: int, fix_type: int) -> bool:
  """Return whether NAV-PVT should set gpsLocationExternal.hasFix.

  Requires gnssFixOk (flags bit 0) and fixType in {3D, GNSS+DR}.
  """
  if type(flags) is not int or type(fix_type) is not int:
    return False
  gnss_fix_ok = (flags & 0x01) != 0
  return gnss_fix_ok and fix_type in UBLOX_HAS_FIX_TYPES


def gps_week_is_full(gps_week: int) -> bool:
  return type(gps_week) is int and not isinstance(gps_week, bool) and 0 <= gps_week <= GPS_WEEK_FULL_MAX_INCLUSIVE


def gps_week_is_modulo_1024(gps_week: int) -> bool:
  return type(gps_week) is int and not isinstance(gps_week, bool) and 0 <= gps_week < GPS_WEEK_ROLLOVER_MODULO


def representable_gps_utc_maximum() -> datetime:
  """Exclusive UTC ceiling representable as GPS week+TOW under UInt16 week.

  Week 0xFFFF is rejected by converters; the last inclusive full week is 0xFFFE.
  The exclusive ceiling is the start of week (0xFFFE + 1).
  """
  return GPS_EPOCH_UTC + timedelta(weeks=GPS_WEEK_FULL_MAX_INCLUSIVE + 1)


def utc_to_gps_week_tow(utc: datetime, *, leap_seconds: int) -> tuple[int, float]:
  """Convert timezone-aware UTC to (full GPS week, TOW milliseconds)."""
  if utc.tzinfo is None or utc.utcoffset() is None:
    raise ValueError("utc must be timezone-aware")
  if type(leap_seconds) is not int or leap_seconds < 0:
    raise ValueError("leap_seconds must be a non-negative int")
  gps_time = utc.astimezone(UTC) + timedelta(seconds=leap_seconds)
  elapsed = (gps_time - GPS_EPOCH_UTC).total_seconds()
  if elapsed < 0:
    raise ValueError("utc predates the GPS epoch")
  week = int(elapsed // GPS_WEEK_SECONDS)
  if week > GPS_WEEK_FULL_MAX_INCLUSIVE:
    raise ValueError("utc exceeds representable GPS week range")
  tow_ms = (elapsed - week * GPS_WEEK_SECONDS) * 1000.0
  return week, tow_ms


def resolve_gps_week_mod_1024(
  week_mod: int,
  *,
  trusted_utc: datetime | None = None,
  trusted_full_week: int | None = None,
) -> int:
  """Resolve a 10-bit (mod-1024) GPS week to an absolute full week.

  Requires trustworthy era evidence: a full receiver week and/or trusted UTC.
  Does not use an untrusted RTC. Fails closed when era evidence is absent or
  when two candidate eras are exactly equally close to the reference.
  """
  if not gps_week_is_modulo_1024(week_mod):
    raise ValueError("week_mod must be an int in [0, 1024)")

  reference_week: int | None = None
  if trusted_full_week is not None:
    if not gps_week_is_full(trusted_full_week):
      raise ValueError("trusted_full_week must be a representable full GPS week")
    reference_week = trusted_full_week
  elif trusted_utc is not None:
    if trusted_utc.tzinfo is None or trusted_utc.utcoffset() is None:
      raise ValueError("trusted_utc must be timezone-aware")
    elapsed = (trusted_utc.astimezone(UTC) - GPS_EPOCH_UTC).total_seconds()
    if elapsed < 0:
      raise ValueError("trusted_utc predates the GPS epoch")
    reference_week = int(elapsed // GPS_WEEK_SECONDS)
  else:
    raise ValueError("modulo GPS week requires trusted era evidence")

  assert reference_week is not None
  base_cycle = (reference_week - week_mod) // GPS_WEEK_ROLLOVER_MODULO
  candidates = {
    week_mod + (base_cycle + cycle_offset) * GPS_WEEK_ROLLOVER_MODULO
    for cycle_offset in (-1, 0, 1, 2)
    if (week_mod + (base_cycle + cycle_offset) * GPS_WEEK_ROLLOVER_MODULO) >= 0
  }
  if not candidates:
    raise ValueError("unable to resolve modulo GPS week")

  ranked = sorted((abs(absolute - reference_week), absolute) for absolute in candidates)
  if len(ranked) >= 2 and ranked[0][0] == ranked[1][0]:
    raise ValueError("ambiguous GPS week era resolution")
  resolved = ranked[0][1]
  if not gps_week_is_full(resolved):
    raise ValueError("resolved GPS week is outside representable range")
  return resolved


# Live SFRBX ephemeris may differ from the trusted RAWX current week by at most
# one full week (week-boundary / buffered-frame race). Farther nearest-era
# results are not valid live current ephemeris even when generic resolution succeeds.
LIVE_EPHEMERIS_RAWX_WEEK_MAX_DELTA = 1


def live_ephemeris_week_matches_rawx_current(
  resolved_week: int,
  rawx_current_week: int,
  *,
  max_delta: int = LIVE_EPHEMERIS_RAWX_WEEK_MAX_DELTA,
) -> bool:
  """True when a live-resolved SFRBX week is compatible with RAWX current week."""
  if not gps_week_is_full(resolved_week) or not gps_week_is_full(rawx_current_week):
    return False
  if type(max_delta) is not int or isinstance(max_delta, bool) or max_delta < 0:
    return False
  return abs(resolved_week - rawx_current_week) <= max_delta


def rawx_full_week_is_trusted_era_evidence(*, week: int, num_meas: int) -> bool:
  """True when a RAWX report may latch full-week era evidence for modulo resolution.

  Matches receiver_time_provenance week validity: nonzero full week plus nonempty
  measurements. Empty RAWX headers are never trusted era authority.
  """
  if not gps_week_is_full(week) or week <= 0:
    return False
  if type(num_meas) is not int or isinstance(num_meas, bool) or num_meas <= 0:
    return False
  return True


def default_leap_seconds_for_week(gps_week: int) -> int:
  """Return the maintained known leap offset for gps_week, or raise."""
  if not gps_week_is_full(gps_week):
    raise ValueError("gps_week must be a valid GPS week")
  applied: int | None = None
  for from_week, leap in GPS_UTC_LEAP_SECOND_TABLE:
    if gps_week >= from_week:
      applied = leap
  if applied is None:
    raise ValueError("gps_week is outside the maintained GPS_UTC_LEAP_SECONDS era; pass an explicit leap_seconds for historical conversions")
  return applied


def gps_week_tow_to_unix_millis(
  gps_week: int,
  gps_tow_ms: float,
  *,
  leap_seconds: int | None = None,
) -> float:
  """Convert GPS week + time-of-week milliseconds to Unix epoch milliseconds (UTC).

  Modem timestamps are GPS time. UTC = GPS - leap_seconds.

  Default leap_seconds uses the maintained known leap table/constant for weeks
  at/after the earliest known modern era. Pass an explicit leap_seconds when the
  caller has independent authority for that epoch.
  """
  if not gps_week_is_full(gps_week):
    raise ValueError("gps_week must be a valid GPS week")
  if isinstance(gps_tow_ms, bool) or not isinstance(gps_tow_ms, (int, float)):
    raise ValueError("gps_tow_ms must be numeric")
  tow_ms = float(gps_tow_ms)
  if not isfinite(tow_ms) or tow_ms < 0.0 or tow_ms >= GPS_WEEK_MILLISECONDS:
    raise ValueError("gps_tow_ms must be finite within one GPS week")

  if leap_seconds is None:
    applied_leap = default_leap_seconds_for_week(gps_week)
  else:
    if type(leap_seconds) is not int or leap_seconds < 0:
      raise ValueError("leap_seconds must be a non-negative int")
    applied_leap = leap_seconds

  utc = GPS_EPOCH_UTC + timedelta(weeks=gps_week) + timedelta(milliseconds=tow_ms) - timedelta(seconds=applied_leap)
  return utc.timestamp() * 1e3
