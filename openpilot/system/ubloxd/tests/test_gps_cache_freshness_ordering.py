from datetime import timedelta

import pytest

from openpilot.system.ubloxd.gps_assistance import (
  CacheAgeEvidence,
  NavigationCacheStore,
  compare_cache_quality,
  save_cache,
)
from openpilot.system.ubloxd.tests.test_gps_startup_quality_policy import (
  NOW,
  RECEIVER,
  cache,
  startup_ready_quality,
  weak_usable_quality,
)


FRESHNESS_CASES = (
  (
    timedelta(hours=3, minutes=59, seconds=59),
    False,
  ),
  (
    timedelta(hours=4),
    True,
  ),
  (
    timedelta(hours=4, seconds=1),
    True,
  ),
  (
    timedelta(hours=5),
    True,
  ),
  (
    timedelta(hours=23),
    True,
  ),
)


@pytest.mark.parametrize(
  ("newer_by", "materially_fresher"),
  FRESHNESS_CASES,
)
@pytest.mark.parametrize(
  "age_evidence",
  (
    CacheAgeEvidence.TRUSTED_UTC,
    CacheAgeEvidence.UNVERIFIED,
  ),
)
def test_startup_freshness_precedes_readiness(
  tmp_path,
  newer_by,
  materially_fresher,
  age_evidence,
):
  store = NavigationCacheStore(
    tmp_path / "navigation_cache.json"
  )
  primary = cache(weak_usable_quality(), NOW)
  previous = cache(
    startup_ready_quality(),
    NOW - newer_by,
  )
  save_cache(store.primary_path, primary)
  save_cache(store.previous_path, previous)

  selection, _ = store.select_best(
    RECEIVER,
    (
      NOW
      if age_evidence is CacheAgeEvidence.TRUSTED_UTC
      else None
    ),
    age_evidence=age_evidence,
  )

  assert selection is not None
  if materially_fresher:
    assert selection.generation == "primary"
    assert selection.reason == "primary_materially_fresher"
  else:
    assert selection.generation == "previous"
    assert selection.reason == "previous_gps_startup_ready"


@pytest.mark.parametrize(
  ("newer_by", "materially_fresher"),
  FRESHNESS_CASES,
)
def test_promotion_freshness_precedes_readiness(
  newer_by,
  materially_fresher,
):
  existing = cache(
    startup_ready_quality(),
    NOW - newer_by,
  )
  candidate = cache(weak_usable_quality(), NOW)

  replace, reason = compare_cache_quality(
    existing,
    candidate,
    NOW,
  )

  if materially_fresher:
    assert replace
    assert reason == "candidate_materially_fresher"
  else:
    assert not replace
    assert reason == "existing_gps_startup_ready"


def test_material_freshness_precedes_quality_tier():
  existing = cache(
    startup_ready_quality(reliable_seconds=60.0),
    NOW - timedelta(hours=23),
  )
  candidate = cache(weak_usable_quality(), NOW)

  replace, reason = compare_cache_quality(
    existing,
    candidate,
    NOW,
  )

  assert replace
  assert reason == "candidate_materially_fresher"
