from __future__ import annotations

import math

import pytest

from openpilot.common.gps_time import GPS_UTC_LEAP_SECONDS, gps_week_tow_to_unix_millis
from openpilot.system.qcomgpsd.qcom_position import (
  POS_SOURCE_EXTERNAL,
  POS_SOURCE_KALMAN,
  POS_SOURCE_NONE,
  RELIABILITY_HIGH,
  RELIABILITY_LOW,
  RELIABILITY_MEDIUM,
  RELIABILITY_VERY_LOW,
  qcom_horizontal_accuracy_m,
  qcom_position_fields,
  qcom_report_has_fix,
  qcom_vertical_accuracy_m,
)
from openpilot.system.qcomgpsd.tests.helpers import valid_position_report


def test_horizontal_uses_ellipse_semimajor_meters():
  report = valid_position_report(
    q_FltEllipseSemimajorAxis=7.25,
    q_FltEllipseSemiminorAxis=1.0,
  )
  assert qcom_horizontal_accuracy_m(report) == pytest.approx(7.25)


def test_vertical_uses_pos_sigma_not_vdop():
  report = valid_position_report(q_FltVdop=99.0, q_FltPosSigmaVertical=2.75)
  assert qcom_vertical_accuracy_m(report) == pytest.approx(2.75)
  fields = qcom_position_fields(report)
  assert fields is not None
  assert fields.vertical_accuracy == pytest.approx(2.75)
  report2 = valid_position_report(q_FltVdop=1.0, q_FltPosSigmaVertical=2.75)
  fields2 = qcom_position_fields(report2)
  assert fields2 is not None
  assert fields2.vertical_accuracy == fields.vertical_accuracy


def test_invalid_nonfinite_uncertainty_fails_safely():
  assert qcom_horizontal_accuracy_m(valid_position_report(q_FltEllipseSemimajorAxis=float("nan"))) is None
  assert qcom_vertical_accuracy_m(valid_position_report(q_FltPosSigmaVertical=float("inf"))) is None
  assert qcom_position_fields(valid_position_report(q_FltEllipseSemimajorAxis=float("nan"))) is None
  assert qcom_position_fields(valid_position_report(q_FltPosSigmaVertical=0.0)) is None


def test_position_fields_map_units_and_time():
  report = valid_position_report()
  fields = qcom_position_fields(report)
  assert fields is not None
  assert fields.horizontal_accuracy == pytest.approx(4.0)
  assert fields.vertical_accuracy == pytest.approx(3.5)
  assert fields.latitude == pytest.approx(0.65 * 180.0 / math.pi)
  assert fields.longitude == pytest.approx(-2.1 * 180.0 / math.pi)
  assert fields.altitude == pytest.approx(120.5)
  expected_ms = gps_week_tow_to_unix_millis(2300, 123456.0)
  assert fields.unix_timestamp_millis == pytest.approx(expected_ms)
  assert fields.has_fix is True
  assert fields.satellite_count == 8


def test_has_fix_valid_gnss_solution():
  assert qcom_report_has_fix(valid_position_report()) is True


def test_has_fix_explicit_failure():
  assert qcom_report_has_fix(valid_position_report(u_FailureCode=1)) is False


def test_has_fix_invalid_reliability():
  assert qcom_report_has_fix(valid_position_report(u_HorizontalReliability=RELIABILITY_LOW)) is False
  assert qcom_report_has_fix(valid_position_report(u_VerticalReliability=RELIABILITY_VERY_LOW)) is False


def test_has_fix_no_usable_position_source():
  assert qcom_report_has_fix(valid_position_report(u_PosSource=POS_SOURCE_NONE)) is False
  assert qcom_report_has_fix(valid_position_report(u_PosSource=POS_SOURCE_EXTERNAL)) is False


def test_has_fix_insufficient_satellites():
  assert qcom_report_has_fix(valid_position_report(u_NumGpsSvsUsed=2, u_NumGloSvsUsed=1, u_NumBdsSvsUsed=0)) is False


def test_has_fix_malformed_report():
  assert qcom_report_has_fix({}) is False
  assert qcom_report_has_fix(None) is False
  assert qcom_report_has_fix(valid_position_report(**{"t_DblFinalPosLatLon[0]": float("nan")})) is False


def test_invalid_lat_lon_range_rejected():
  assert qcom_report_has_fix(valid_position_report(**{"t_DblFinalPosLatLon[0]": 2.0})) is False
  assert qcom_position_fields(valid_position_report(**{"t_DblFinalPosLatLon[0]": 2.0})) is None
  assert qcom_report_has_fix(valid_position_report(**{"t_DblFinalPosLatLon[1]": 4.0})) is False
  assert qcom_position_fields(valid_position_report(**{"t_DblFinalPosLatLon[1]": -4.0})) is None


def test_negative_velocity_sigma_rejected():
  assert qcom_position_fields(valid_position_report(**{"q_FltVelSigmaMps[0]": -0.1})) is None


def test_nonfinite_speed_accuracy_not_converted_to_zero():
  assert qcom_position_fields(valid_position_report(**{"q_FltVelSigmaMps[1]": float("nan")})) is None
  assert qcom_position_fields(valid_position_report(**{"q_FltVelSigmaMps[2]": float("inf")})) is None


def test_heading_uncertainty_sentinel_and_invalid():
  fields = qcom_position_fields(valid_position_report(q_FltHeadingUncRad=0.0))
  assert fields is not None
  assert fields.bearing_accuracy_deg == pytest.approx(180.0)
  assert qcom_position_fields(valid_position_report(q_FltHeadingUncRad=-0.1)) is None
  assert qcom_position_fields(valid_position_report(q_FltHeadingUncRad=float("nan"))) is None
  assert qcom_position_fields(valid_position_report(q_FltHeadingUncRad=float("inf"))) is None


def test_has_fix_poor_but_finite_uncertainty_still_true():
  report = valid_position_report(
    q_FltEllipseSemimajorAxis=250.0,
    q_FltPosSigmaVertical=180.0,
    u_HorizontalReliability=RELIABILITY_HIGH,
    u_VerticalReliability=RELIABILITY_MEDIUM,
  )
  assert qcom_report_has_fix(report) is True


def test_no_magic_vdop_sentinel_has_fix():
  report = valid_position_report(q_FltVdop=500.0, q_FltPosSigmaVertical=4.0)
  assert qcom_report_has_fix(report) is True
  fields = qcom_position_fields(report)
  assert fields is not None
  assert fields.vertical_accuracy != 500.0


def test_kalman_source_constant():
  assert POS_SOURCE_KALMAN == 2


def test_leap_seconds_authority_used():
  assert GPS_UTC_LEAP_SECONDS == 18
