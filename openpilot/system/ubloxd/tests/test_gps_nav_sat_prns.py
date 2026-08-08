import struct

from openpilot.system.ubloxd.gps_assistance import (
  NavSatQuality,
  add_ubx_checksum,
  parse_nav_sat,
)


def build_nav_sat_frame(
  satellites: tuple[tuple[int, int, int], ...],
) -> bytes:
  payload = bytearray(8 + len(satellites) * 12)
  payload[4] = 1
  payload[5] = len(satellites)

  for index, (gnss_id, satellite_id, flags) in enumerate(
    satellites
  ):
    offset = 8 + index * 12
    payload[offset] = gnss_id
    payload[offset + 1] = satellite_id
    struct.pack_into("<I", payload, offset + 8, flags)

  return add_ubx_checksum(
    b"\xB5\x62\x01\x35"
    + len(payload).to_bytes(2, "little")
    + bytes(payload)
  )


def test_nav_sat_tracks_exact_gps_prn_sets():
  report = parse_nav_sat(build_nav_sat_frame((
    (0, 1, (1 << 3) | (1 << 11) | (1 << 12)),
    (0, 2, 0),
    (0, 3, (2 << 4) | (1 << 12)),
    (0, 4, 1 << 11),
    (6, 7, (1 << 11) | (1 << 12)),
  )))

  assert report is not None
  assert report.gps_satellites_known == 4
  assert report.glonass_satellites_known == 1
  assert report.gps_ephemeris_available == 2
  assert report.glonass_ephemeris_available == 1
  assert report.gps_almanac_available == 2
  assert report.glonass_almanac_available == 1
  assert report.gps_satellite_ids == frozenset((1, 2, 3, 4))
  assert report.gps_healthy_satellite_ids == frozenset((1, 2, 4))
  assert report.gps_almanac_satellite_ids == frozenset((1, 3))


def test_nav_sat_ignores_invalid_gps_prns_for_exact_sets():
  report = parse_nav_sat(build_nav_sat_frame((
    (0, 0, 1 << 12),
    (0, 33, 1 << 12),
  )))

  assert report is not None
  assert report.gps_satellites_known == 2
  assert report.gps_almanac_available == 2
  assert report.gps_satellite_ids == frozenset()
  assert report.gps_healthy_satellite_ids == frozenset()
  assert report.gps_almanac_satellite_ids == frozenset()


def test_nav_sat_rejects_duplicate_satellite():
  assert parse_nav_sat(build_nav_sat_frame((
    (0, 1, 0),
    (0, 1, 1 << 12),
  ))) is None


def test_nav_sat_quality_positional_constructor_remains_compatible():
  report = NavSatQuality(
    8,
    8,
    4,
    6,
    8,
    5,
    5,
    4,
    {"ephemeris": 16},
  )

  assert report.gps_satellite_ids == frozenset()
  assert report.gps_healthy_satellite_ids == frozenset()
  assert report.gps_almanac_satellite_ids == frozenset()
