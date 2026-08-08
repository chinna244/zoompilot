from datetime import UTC, datetime, timedelta

import pytest

from openpilot.system.ubloxd.gps_assistance import (
  CacheAgeEvidence,
  NavigationCacheStore,
  NavigationQuality,
  NavPvtFix,
  QUALITY_POLICY_VERSION,
  QUALITY_VERSION,
  compare_cache_quality,
  create_cache,
  save_cache,
)
from openpilot.system.ubloxd.tests.test_gps_assistance import (
  build_dbd_frame,
)


NOW = datetime(2026, 7, 21, 18, tzinfo=UTC)
RECEIVER = "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov"


def quality(
  *,
  gps: int,
  glo: int,
  used: int,
  reliable_seconds: float = 20.0,
  orbit_seconds: float = 0.0,
) -> NavigationQuality:
  total_known = 16
  total_ephemeris = gps + glo
  return NavigationQuality(
    quality_version=QUALITY_VERSION,
    policy_version=QUALITY_POLICY_VERSION,
    capture_context="onroad",
    continuous_reliable_fix_seconds=reliable_seconds,
    continuous_orbit_quality_seconds=orbit_seconds,
    gps_satellites_known=8,
    glonass_satellites_known=8,
    gps_ephemeris_available=gps,
    glonass_ephemeris_available=glo,
    satellites_used=used,
    gps_almanac_available=5,
    glonass_almanac_available=5,
    assistnow_offline_available=0,
    orbit_source_counts={
      "ephemeris": total_ephemeris,
      "almanac": total_known - total_ephemeris,
    },
  )


def startup_ready_quality(
  *,
  reliable_seconds: float = 20.0,
  orbit_seconds: float = 10.0,
) -> NavigationQuality:
  return quality(
    gps=4,
    glo=6,
    used=8,
    reliable_seconds=reliable_seconds,
    orbit_seconds=orbit_seconds,
  )


def weak_usable_quality() -> NavigationQuality:
  return quality(
    gps=3,
    glo=7,
    used=8,
    reliable_seconds=20.0,
    orbit_seconds=10.0,
  )


def cache(
  cache_quality: NavigationQuality,
  saved_at: datetime,
):
  return create_cache(
    RECEIVER,
    NavPvtFix(
      True,
      10,
      1,
      2,
      3,
      100,
      100,
      saved_at,
    ),
    (build_dbd_frame(1),),
    saved_at,
    quality=cache_quality,
  )


def test_today_glonass_heavy_caches_are_not_gps_startup_ready():
  morning = NavigationQuality(
    QUALITY_VERSION,
    QUALITY_POLICY_VERSION,
    "post_drive",
    20.021936554999968,
    0.0,
    10,
    9,
    0,
    5,
    5,
    10,
    9,
    0,
    {"almanac": 14, "ephemeris": 5},
  )
  afternoon = NavigationQuality(
    QUALITY_VERSION,
    QUALITY_POLICY_VERSION,
    "onroad",
    20.030876347000003,
    0.0,
    9,
    11,
    1,
    4,
    5,
    9,
    11,
    0,
    {"almanac": 15, "ephemeris": 5},
  )

  for cache_quality in (morning, afternoon):
    assert cache_quality.usable_for_capture
    assert not cache_quality.gps_startup_ready
    assert not cache_quality.passes_policy


def test_startup_ready_requires_sustained_strong_orbit_state():
  cache_quality = startup_ready_quality()

  assert cache_quality.usable_for_capture
  assert cache_quality.gps_startup_ready
  assert not cache_quality.passes_policy


def test_qualified_quality_is_always_startup_ready():
  cache_quality = startup_ready_quality(
    reliable_seconds=60.0,
  )

  assert cache_quality.gps_startup_ready
  assert cache_quality.passes_policy


def test_momentary_strong_snapshot_is_not_startup_ready():
  cache_quality = startup_ready_quality(
    orbit_seconds=0.0,
  )

  assert cache_quality.usable_for_capture
  assert not cache_quality.gps_startup_ready


@pytest.mark.parametrize(
  (
    "gps",
    "glo",
    "used",
    "reliable_seconds",
    "orbit_seconds",
  ),
  (
    (3, 7, 8, 20.0, 10.0),
    (4, 5, 8, 20.0, 10.0),
    (4, 6, 7, 20.0, 10.0),
    (4, 6, 8, 19.999, 10.0),
    (4, 6, 8, 20.0, 9.999),
  ),
)
def test_each_startup_boundary_below_minimum_fails(
  gps,
  glo,
  used,
  reliable_seconds,
  orbit_seconds,
):
  assert not quality(
    gps=gps,
    glo=glo,
    used=used,
    reliable_seconds=reliable_seconds,
    orbit_seconds=orbit_seconds,
  ).gps_startup_ready


def test_newer_weak_cache_does_not_replace_startup_ready_cache():
  existing = cache(
    startup_ready_quality(),
    NOW - timedelta(minutes=1),
  )
  candidate = cache(
    weak_usable_quality(),
    NOW,
  )

  replace_cache, reason = compare_cache_quality(
    existing,
    candidate,
    NOW,
  )

  assert not replace_cache
  assert reason == "existing_gps_startup_ready"


def test_startup_ready_candidate_upgrades_weak_cache():
  existing = cache(
    weak_usable_quality(),
    NOW - timedelta(minutes=1),
  )
  candidate = cache(
    startup_ready_quality(),
    NOW,
  )

  replace_cache, reason = compare_cache_quality(
    existing,
    candidate,
    NOW,
  )

  assert replace_cache
  assert reason == "candidate_gps_startup_ready"


def test_startup_selects_ready_previous_over_newer_weak_primary(
  tmp_path,
):
  store = NavigationCacheStore(
    tmp_path / "navigation_cache.json"
  )
  weak_primary = cache(
    weak_usable_quality(),
    NOW,
  )
  ready_previous = cache(
    startup_ready_quality(),
    NOW - timedelta(minutes=1),
  )

  save_cache(store.primary_path, weak_primary)
  save_cache(store.previous_path, ready_previous)

  selection, _ = store.select_best(
    RECEIVER,
    NOW,
    age_evidence=CacheAgeEvidence.TRUSTED_UTC,
  )

  assert selection is not None
  assert selection.generation == "previous"
  assert selection.reason == "previous_gps_startup_ready"


def test_startup_prefers_qualified_previous_over_usable_primary(
  tmp_path,
):
  store = NavigationCacheStore(
    tmp_path / "navigation_cache.json"
  )
  usable_primary = cache(
    weak_usable_quality(),
    NOW,
  )
  qualified_previous = cache(
    startup_ready_quality(reliable_seconds=60.0),
    NOW - timedelta(minutes=1),
  )

  save_cache(store.primary_path, usable_primary)
  save_cache(store.previous_path, qualified_previous)

  selection, _ = store.select_best(
    RECEIVER,
    NOW,
    age_evidence=CacheAgeEvidence.TRUSTED_UTC,
  )

  assert selection is not None
  assert selection.generation == "previous"
  assert selection.reason == "previous_higher_quality_tier"
