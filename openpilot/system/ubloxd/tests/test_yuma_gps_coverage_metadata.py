import pytest

from openpilot.system.ubloxd import gps_assistance
from openpilot.system.ubloxd.gps_assistance import (
  NavigationQuality,
  conservative_navigation_quality,
)


def quality(
  *,
  almanac_ids: tuple[int, ...] | None,
  almanac_count: int,
) -> NavigationQuality:
  return NavigationQuality(
    quality_version=gps_assistance.QUALITY_VERSION,
    policy_version=gps_assistance.QUALITY_POLICY_VERSION,
    capture_context="onroad",
    continuous_reliable_fix_seconds=30.0,
    continuous_orbit_quality_seconds=30.0,
    gps_satellites_known=32,
    glonass_satellites_known=8,
    gps_ephemeris_available=6,
    glonass_ephemeris_available=5,
    satellites_used=10,
    gps_almanac_available=almanac_count,
    glonass_almanac_available=5,
    assistnow_offline_available=0,
    orbit_source_counts={"ephemeris": 11, "almanac": 29},
    gps_almanac_satellite_ids=almanac_ids,
  )


def test_conservative_quality_intersects_request_and_completion_prns():
  request = quality(
    almanac_ids=(1, 2, 3, 4),
    almanac_count=4,
  )
  completion = quality(
    almanac_ids=(2, 3, 4, 5),
    almanac_count=4,
  )

  result = conservative_navigation_quality(
    request,
    completion,
  )

  assert result is not None
  assert result.gps_almanac_satellite_ids == (2, 3, 4)
  assert result.gps_almanac_available == 4


def test_quality_json_round_trip_preserves_optional_prn_metadata():
  original = quality(
    almanac_ids=(1, 2, 4),
    almanac_count=3,
  )

  payload = gps_assistance._quality_to_json(original)
  restored = gps_assistance._quality_from_json(payload)

  assert restored.gps_almanac_satellite_ids == (1, 2, 4)


def test_legacy_quality_without_prn_metadata_remains_readable():
  payload = gps_assistance._quality_to_json(
    quality(
      almanac_ids=None,
      almanac_count=10,
    )
  )

  assert "gps_almanac_satellite_ids" not in payload
  restored = gps_assistance._quality_from_json(payload)

  assert restored.gps_almanac_satellite_ids is None


def test_quality_rejects_prn_metadata_larger_than_count():
  payload = gps_assistance._quality_to_json(
    quality(
      almanac_ids=(1,),
      almanac_count=1,
    )
  )
  payload["gps_almanac_satellite_ids"] = [1, 2]

  with pytest.raises(
    gps_assistance.CacheValidationError,
    match="quality metadata is invalid",
  ):
    gps_assistance._quality_from_json(payload)
