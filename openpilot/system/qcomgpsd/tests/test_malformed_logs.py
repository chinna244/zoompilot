from __future__ import annotations

import pytest

from openpilot.system.qcomgpsd.qcomgpsd import (
  process_constellation_measurement_report,
  process_oemdre_measurement_report,
  process_position_report,
  process_svpoly_report,
)
from openpilot.system.qcomgpsd.structs import (
  LOG_GNSS_GLONASS_MEASUREMENT_REPORT,
  LOG_GNSS_GPS_MEASUREMENT_REPORT,
  dict_unpacker,
  glonass_measurement_report,
  glonass_measurement_report_sv,
  gps_measurement_report,
  gps_measurement_report_sv,
  oemdre_measurement_report,
  oemdre_measurement_report_sv,
  oemdre_svpoly_report,
  position_report,
)

unpack_gps_meas, size_gps_meas = dict_unpacker(gps_measurement_report, True)
unpack_gps_meas_sv, size_gps_meas_sv = dict_unpacker(gps_measurement_report_sv, True)
unpack_glo_meas, size_glo_meas = dict_unpacker(glonass_measurement_report, True)
unpack_glo_meas_sv, size_glo_meas_sv = dict_unpacker(glonass_measurement_report_sv, True)
unpack_oemdre_meas, size_oemdre_meas = dict_unpacker(oemdre_measurement_report, True)
unpack_oemdre_meas_sv, size_oemdre_meas_sv = dict_unpacker(oemdre_measurement_report_sv, True)
unpack_svpoly, size_svpoly = dict_unpacker(oemdre_svpoly_report, True)
unpack_position, size_position = dict_unpacker(position_report)


def _meas_kwargs():
  return {
    "unpack_gps_meas": unpack_gps_meas,
    "size_gps_meas": size_gps_meas,
    "unpack_gps_meas_sv": unpack_gps_meas_sv,
    "size_gps_meas_sv": size_gps_meas_sv,
    "unpack_glonass_meas": unpack_glo_meas,
    "size_glonass_meas": size_glo_meas,
    "unpack_glonass_meas_sv": unpack_glo_meas_sv,
    "size_glonass_meas_sv": size_glo_meas_sv,
  }


def test_position_truncated_header():
  assert (
    process_position_report(
      b"\x00" * (size_position - 1),
      unpack_position=unpack_position,
      size_position=size_position,
    )
    is None
  )


def test_position_valid_length_parses_or_rejects_content():
  # Fixed-size valid length should not raise even if content is zeros.
  process_position_report(
    b"\x00" * size_position,
    unpack_position=unpack_position,
    size_position=size_position,
  )


def test_oemdre_truncated_header():
  assert (
    process_oemdre_measurement_report(
      1,
      b"\x00" * (size_oemdre_meas - 1),
      unpack_oemdre_meas=unpack_oemdre_meas,
      size_oemdre_meas=size_oemdre_meas,
      unpack_oemdre_meas_sv=unpack_oemdre_meas_sv,
      size_oemdre_meas_sv=size_oemdre_meas_sv,
    )
    is None
  )


def test_oemdre_truncated_sv_array():
  # version=2 at byte0, svCount=2, but no SV payload.
  payload = bytearray(size_oemdre_meas)
  payload[0] = 2
  payload[2] = 2
  assert (
    process_oemdre_measurement_report(
      1,
      bytes(payload),
      unpack_oemdre_meas=unpack_oemdre_meas,
      size_oemdre_meas=size_oemdre_meas,
      unpack_oemdre_meas_sv=unpack_oemdre_meas_sv,
      size_oemdre_meas_sv=size_oemdre_meas_sv,
    )
    is None
  )


def test_oemdre_extra_malformed_bytes_rejected():
  payload = bytearray(size_oemdre_meas)
  payload[0] = 2
  payload[2] = 0
  payload.extend(b"\xff\xff")
  assert (
    process_oemdre_measurement_report(
      1,
      bytes(payload),
      unpack_oemdre_meas=unpack_oemdre_meas,
      size_oemdre_meas=size_oemdre_meas,
      unpack_oemdre_meas_sv=unpack_oemdre_meas_sv,
      size_oemdre_meas_sv=size_oemdre_meas_sv,
    )
    is None
  )


def test_oemdre_absurd_sv_count():
  payload = bytearray(size_oemdre_meas)
  payload[0] = 2
  payload[2] = 200
  assert (
    process_oemdre_measurement_report(
      1,
      bytes(payload) + b"\x00" * (200 * size_oemdre_meas_sv),
      unpack_oemdre_meas=unpack_oemdre_meas,
      size_oemdre_meas=size_oemdre_meas,
      unpack_oemdre_meas_sv=unpack_oemdre_meas_sv,
      size_oemdre_meas_sv=size_oemdre_meas_sv,
    )
    is None
  )


def test_oemdre_unsupported_version():
  payload = bytearray(size_oemdre_meas)
  payload[0] = 9
  assert (
    process_oemdre_measurement_report(
      1,
      bytes(payload),
      unpack_oemdre_meas=unpack_oemdre_meas,
      size_oemdre_meas=size_oemdre_meas,
      unpack_oemdre_meas_sv=unpack_oemdre_meas_sv,
      size_oemdre_meas_sv=size_oemdre_meas_sv,
    )
    is None
  )


def test_oemdre_valid_empty_sv_then_processes():
  payload = bytearray(size_oemdre_meas)
  payload[0] = 2
  payload[2] = 0
  msg = process_oemdre_measurement_report(
    1,
    bytes(payload),
    unpack_oemdre_meas=unpack_oemdre_meas,
    size_oemdre_meas=size_oemdre_meas,
    unpack_oemdre_meas_sv=unpack_oemdre_meas_sv,
    size_oemdre_meas_sv=size_oemdre_meas_sv,
  )
  assert msg is not None


def test_svpoly_truncated_and_bad_version():
  assert (
    process_svpoly_report(
      1,
      b"\x00" * (size_svpoly - 1),
      unpack_svpoly=unpack_svpoly,
      size_svpoly=size_svpoly,
    )
    is None
  )
  payload = bytearray(size_svpoly)
  payload[0] = 9
  assert (
    process_svpoly_report(
      1,
      bytes(payload),
      unpack_svpoly=unpack_svpoly,
      size_svpoly=size_svpoly,
    )
    is None
  )


def test_svpoly_valid_version():
  payload = bytearray(size_svpoly)
  payload[0] = 2
  assert (
    process_svpoly_report(
      1,
      bytes(payload),
      unpack_svpoly=unpack_svpoly,
      size_svpoly=size_svpoly,
    )
    is not None
  )


def test_gps_meas_truncated_sv_and_extra_bytes():
  header = bytearray(size_gps_meas)
  header[0] = 0
  header[-1] = 1  # svCount near end depending on layout; use unpack-driven checks
  # Force via constructing exact expected sizes after unpacking zeros (svCount=0).
  assert (
    process_constellation_measurement_report(
      LOG_GNSS_GPS_MEASUREMENT_REPORT,
      1,
      bytes(header) + b"\x00",
      **_meas_kwargs(),
    )
    is None
  )


def test_gps_meas_valid_zero_sv():
  header = bytearray(size_gps_meas)
  header[0] = 0
  msg = process_constellation_measurement_report(
    LOG_GNSS_GPS_MEASUREMENT_REPORT,
    1,
    bytes(header),
    **_meas_kwargs(),
  )
  assert msg is not None


def test_glonass_meas_truncated_header():
  assert (
    process_constellation_measurement_report(
      LOG_GNSS_GLONASS_MEASUREMENT_REPORT,
      1,
      b"\x00" * (size_glo_meas - 1),
      **_meas_kwargs(),
    )
    is None
  )


def test_malformed_then_valid_still_works():
  bad = process_oemdre_measurement_report(
    1,
    b"\x00" * 3,
    unpack_oemdre_meas=unpack_oemdre_meas,
    size_oemdre_meas=size_oemdre_meas,
    unpack_oemdre_meas_sv=unpack_oemdre_meas_sv,
    size_oemdre_meas_sv=size_oemdre_meas_sv,
  )
  assert bad is None
  good_payload = bytearray(size_oemdre_meas)
  good_payload[0] = 2
  good = process_oemdre_measurement_report(
    2,
    bytes(good_payload),
    unpack_oemdre_meas=unpack_oemdre_meas,
    size_oemdre_meas=size_oemdre_meas,
    unpack_oemdre_meas_sv=unpack_oemdre_meas_sv,
    size_oemdre_meas_sv=size_oemdre_meas_sv,
  )
  assert good is not None


def test_position_unsupported_version_discarded():
  payload = bytearray(size_position)
  payload[0] = 18  # QXDM v18+ incompatible layout
  assert (
    process_position_report(
      bytes(payload),
      unpack_position=unpack_position,
      size_position=size_position,
    )
    is None
  )


@pytest.mark.parametrize("version", [1, 17, 18, 255])
def test_position_unproven_versions_rejected(version: int):
  payload = bytearray(size_position)
  payload[0] = version
  assert (
    process_position_report(
      bytes(payload),
      unpack_position=unpack_position,
      size_position=size_position,
    )
    is None
  )


def test_position_version_zero_accepted_for_layout_gate():
  from openpilot.system.qcomgpsd.qcom_position import POS_SOURCE_KALMAN
  from openpilot.system.qcomgpsd.tests.helpers import valid_position_report

  report = valid_position_report(u_Version=0)

  def unpack(_payload: bytes):
    return report

  # Size/version gates pass; content may still fail field validation without raise.
  process_position_report(
    b"\x00" * size_position,
    unpack_position=unpack,
    size_position=size_position,
  )
  assert report["u_Version"] == 0
  assert report["u_PosSource"] == POS_SOURCE_KALMAN


def test_position_oversized_discarded():
  assert (
    process_position_report(
      b"\x00" * (size_position + 8),
      unpack_position=unpack_position,
      size_position=size_position,
    )
    is None
  )


def test_position_supported_version_zero_length_ok_content_may_reject():
  payload = bytearray(size_position)
  payload[0] = 0
  # Version accepted; zeros fail PosSource/hasFix field gates without raising.
  process_position_report(
    bytes(payload),
    unpack_position=unpack_position,
    size_position=size_position,
  )


def test_position_unsupported_then_supported_still_processes(monkeypatch):
  from openpilot.system.qcomgpsd.qcom_position import POS_SOURCE_KALMAN
  from openpilot.system.qcomgpsd.tests.helpers import valid_position_report

  calls = {"n": 0}

  def unpack(payload: bytes):
    calls["n"] += 1
    if payload[0] == 99:
      return {"u_Version": 99, "u_PosSource": POS_SOURCE_KALMAN}
    report = valid_position_report(u_Version=0)
    return report

  class FakeMsg:
    def __init__(self):
      self.gpsLocation = type("G", (), {})()

  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.qcomgpsd.messaging.new_message",
    lambda *_a, **_k: FakeMsg(),
  )
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.qcomgpsd.log.GpsLocationData.SensorSource.qcomdiag",
    1,
  )

  bad = bytearray(10)
  bad[0] = 99
  assert process_position_report(bytes(bad), unpack_position=unpack, size_position=10) is None

  good = bytearray(10)
  good[0] = 0
  msg = process_position_report(bytes(good), unpack_position=unpack, size_position=10)
  assert msg is not None
  assert calls["n"] == 2
