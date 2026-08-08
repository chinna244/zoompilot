"""QCOM position-report → gpsLocation mapping helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite, pi, sqrt
from typing import Any

from openpilot.common.gps_time import gps_week_tow_to_unix_millis

# position_report u_PosSource values from Qualcomm DM log 0x1476 comments.
POS_SOURCE_NONE = 0
POS_SOURCE_WLS = 1
POS_SOURCE_KALMAN = 2
POS_SOURCE_EXTERNAL = 3
POS_SOURCE_DATABASE = 4

# u_HorizontalReliability / u_VerticalReliability
RELIABILITY_NOT_SET = 0
RELIABILITY_VERY_LOW = 1
RELIABILITY_LOW = 2
RELIABILITY_MEDIUM = 3
RELIABILITY_HIGH = 4

# Minimum satellites used across GPS/GLO/BDS for a usable fix.
MIN_SVS_USED_FOR_FIX = 4


@dataclass(frozen=True)
class QcomAccuracy:
  horizontal_m: float
  vertical_m: float


@dataclass(frozen=True)
class QcomGpsLocationFields:
  latitude: float
  longitude: float
  altitude: float
  speed: float
  bearing_deg: float
  unix_timestamp_millis: float
  v_ned: tuple[float, float, float]
  horizontal_accuracy: float
  vertical_accuracy: float
  bearing_accuracy_deg: float
  speed_accuracy: float
  has_fix: bool
  satellite_count: int


def _finite_positive(value: Any) -> float | None:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return None
  number = float(value)
  if not isfinite(number) or number <= 0.0:
    return None
  return number


def _finite_nonnegative(value: Any) -> float | None:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return None
  number = float(value)
  if not isfinite(number) or number < 0.0:
    return None
  return number


def _lat_lon_radians_valid(lat_rad: float, lon_rad: float) -> bool:
  return isfinite(lat_rad) and isfinite(lon_rad) and -pi / 2.0 <= lat_rad <= pi / 2.0 and -pi <= lon_rad <= pi


def _lat_lon_degrees_valid(lat_deg: float, lon_deg: float) -> bool:
  return isfinite(lat_deg) and isfinite(lon_deg) and -90.0 <= lat_deg <= 90.0 and -180.0 <= lon_deg <= 180.0


def qcom_horizontal_accuracy_m(report: Mapping[str, Any]) -> float | None:
  """Return horizontal accuracy meters from the ellipse semimajor axis.

  Struct docs: axes are meters. Confidence is a separate percentage field and
  does not identify 1-sigma vs other confidence, so the semimajor axis is the
  conservative documented horizontal bound without inventing a conversion.
  """
  return _finite_positive(report.get("q_FltEllipseSemimajorAxis"))


def qcom_vertical_accuracy_m(report: Mapping[str, Any]) -> float | None:
  """Return vertical accuracy meters from Gaussian 1-sigma altitude sigma."""
  return _finite_positive(report.get("q_FltPosSigmaVertical"))


def qcom_satellites_used(report: Mapping[str, Any]) -> int:
  total = 0
  for key in ("u_NumGpsSvsUsed", "u_NumGloSvsUsed", "u_NumBdsSvsUsed"):
    value = report.get(key, 0)
    if type(value) is int and value > 0:
      total += value
  return total


def qcom_report_has_fix(report: Mapping[str, Any] | None) -> bool:
  """Return whether a QCOM position_report is a usable GNSS fix.

  Requires Kalman position source, success failure code, valid GPS week,
  medium-or-better horizontal and vertical reliability, enough satellites,
  finite positive horizontal/vertical position uncertainty in meters, and
  geographically valid coordinates.
  """
  if not isinstance(report, Mapping):
    return False
  if report.get("u_PosSource") != POS_SOURCE_KALMAN:
    return False
  failure = report.get("u_FailureCode", None)
  if type(failure) is not int or failure != 0:
    return False
  week = report.get("w_GpsWeekNumber", None)
  if type(week) is not int or week < 0 or week >= 0xFFFF:
    return False
  h_rel = report.get("u_HorizontalReliability", RELIABILITY_NOT_SET)
  v_rel = report.get("u_VerticalReliability", RELIABILITY_NOT_SET)
  if type(h_rel) is not int or type(v_rel) is not int:
    return False
  if h_rel < RELIABILITY_MEDIUM or v_rel < RELIABILITY_MEDIUM:
    return False
  if qcom_satellites_used(report) < MIN_SVS_USED_FOR_FIX:
    return False
  if qcom_horizontal_accuracy_m(report) is None:
    return False
  if qcom_vertical_accuracy_m(report) is None:
    return False
  try:
    lat = float(report["t_DblFinalPosLatLon[0]"])
    lon = float(report["t_DblFinalPosLatLon[1]"])
    alt = float(report["q_FltFinalPosAlt"])
  except (KeyError, TypeError, ValueError):
    return False
  if not isfinite(alt):
    return False
  if not _lat_lon_radians_valid(lat, lon):
    return False
  if not _lat_lon_degrees_valid(lat * 180.0 / pi, lon * 180.0 / pi):
    return False
  return True


def qcom_position_fields(report: Mapping[str, Any]) -> QcomGpsLocationFields | None:
  """Map a modem position_report dict into gpsLocation field values.

  Returns None when the report cannot safely publish a gpsLocation message.
  """
  if not isinstance(report, Mapping):
    return None
  week = report.get("w_GpsWeekNumber")
  if type(week) is not int or week < 0 or week >= 0xFFFF:
    return None

  horizontal = qcom_horizontal_accuracy_m(report)
  vertical = qcom_vertical_accuracy_m(report)
  if horizontal is None or vertical is None:
    return None

  try:
    lat_rad = float(report["t_DblFinalPosLatLon[0]"])
    lon_rad = float(report["t_DblFinalPosLatLon[1]"])
    alt = float(report["q_FltFinalPosAlt"])
    v_e = float(report["q_FltVelEnuMps[0]"])
    v_n = float(report["q_FltVelEnuMps[1]"])
    v_u = float(report["q_FltVelEnuMps[2]"])
    s_e = float(report["q_FltVelSigmaMps[0]"])
    s_n = float(report["q_FltVelSigmaMps[1]"])
    s_u = float(report["q_FltVelSigmaMps[2]"])
    heading_rad = float(report["q_FltHeadingRad"])
    heading_unc = float(report["q_FltHeadingUncRad"])
    tow_ms = float(report["q_GpsFixTimeMs"])
  except (KeyError, TypeError, ValueError):
    return None

  if not _lat_lon_radians_valid(lat_rad, lon_rad):
    return None
  lat = lat_rad * 180.0 / pi
  lon = lon_rad * 180.0 / pi
  if not _lat_lon_degrees_valid(lat, lon):
    return None

  if not all(isfinite(x) for x in (alt, v_e, v_n, v_u, heading_rad, tow_ms)):
    return None
  # Velocity sigmas are Gaussian 1-sigma: must be finite and non-negative.
  if _finite_nonnegative(s_e) is None or _finite_nonnegative(s_n) is None or _finite_nonnegative(s_u) is None:
    return None
  # Heading uncertainty: 0 is the modem sentinel for unknown → 180 deg.
  # Negative / non-finite values are invalid.
  if not isfinite(heading_unc) or heading_unc < 0.0:
    return None

  v_ned = (v_n, v_e, -v_u)
  if not all(isfinite(x) for x in v_ned):
    return None
  speed = sqrt(sum(x * x for x in v_ned))
  if not isfinite(speed):
    return None
  speed_accuracy = sqrt(s_n * s_n + s_e * s_e + s_u * s_u)
  if not isfinite(speed_accuracy) or speed_accuracy < 0.0:
    return None

  heading = heading_rad * 180.0 / pi
  if not isfinite(heading):
    return None
  if heading_unc == 0.0:
    bearing_accuracy = 180.0
  else:
    bearing_accuracy = heading_unc * 180.0 / pi
  if not isfinite(bearing_accuracy) or bearing_accuracy < 0.0:
    return None

  try:
    unix_ms = gps_week_tow_to_unix_millis(week, tow_ms)
  except ValueError:
    return None
  if not isfinite(unix_ms):
    return None

  return QcomGpsLocationFields(
    latitude=lat,
    longitude=lon,
    altitude=alt,
    speed=speed,
    bearing_deg=heading,
    unix_timestamp_millis=unix_ms,
    v_ned=v_ned,
    horizontal_accuracy=horizontal,
    vertical_accuracy=vertical,
    bearing_accuracy_deg=bearing_accuracy,
    speed_accuracy=speed_accuracy,
    has_fix=qcom_report_has_fix(report),
    satellite_count=qcom_satellites_used(report),
  )


def host_time_safe_for_qcom_injection(observation: Any) -> bool:
  """Return True only for independent network-synchronized host time."""
  if observation is None:
    return False
  independent = getattr(observation, "independent", False)
  source = getattr(observation, "source", None)
  source_name = getattr(source, "value", source)
  return independent is True and source_name == "network_synchronized"
