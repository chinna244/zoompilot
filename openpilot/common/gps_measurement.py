"""GPS measurement timing, locationd usability contract, and NED→ECEF covariance (PR81).

Measurement time contract:
  measurementMonoNs is host monotonic time at the producer transport boundary
  (ubloxRaw receive / DIAG payload available), approximating the solution epoch
  without inventing an untrusted wall-clock→monotonic mapping.

Locationd usability contract (mirrored constants — keep in sync with locationd.cc):
  A health-qualified sample must be acceptable to Localizer::handle_gps field checks.

Covariance contract:
  C_ned = diag(σ_h², σ_h², σ_v²) in NED (north, east, down)
  C_ecef = R_ned2ecef · C_ned · R_ned2ecefᵀ
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from openpilot.common.transformations.transformations import LocalCoord

# Mirrored from sunnypilot/selfdrive/locationd/locationd.cc — contract-tested.
# MAX_FILTER_REWIND_TIME / LiveKalman max_rewind_age
GPS_MEASUREMENT_MAX_STALE_SECONDS = 0.8
GPS_LOCATIOND_MAX_FILTER_REWIND_SECONDS = 0.8  # == MAX_FILTER_REWIND_TIME
GPS_LOCATIOND_SANE_UNCERTAINTY_M = 1500.0  # == SANE_GPS_UNCERTAINTY
GPS_LOCATIOND_ALTITUDE_SANITY_M = 10000.0  # == ALTITUDE_SANITY_CHECK
GPS_LOCATIOND_TRANS_SANITY_MPS = 200.0  # == TRANS_SANITY_CHECK
# Measurement stamp slightly after Event.logMonoTime is impossible / clock skew.
GPS_MEASUREMENT_FUTURE_TOLERANCE_SECONDS = 0.001


def measurement_mono_ns_valid(measurement_mono_ns: int | None) -> bool:
  if measurement_mono_ns is None:
    return False
  if not isinstance(measurement_mono_ns, int) or isinstance(measurement_mono_ns, bool):
    return False
  return measurement_mono_ns > 0


def gps_observation_time_s(
  *,
  event_mono_s: float,
  measurement_mono_ns: int | None,
  legacy_sensor_time_offset_s: float = 0.0,
) -> float | None:
  """Return KF observation time (seconds, mono) or None if unusable."""
  if not math.isfinite(event_mono_s):
    return None
  if measurement_mono_ns_valid(measurement_mono_ns):
    assert measurement_mono_ns is not None
    t = measurement_mono_ns * 1e-9
    if not math.isfinite(t):
      return None
    return t
  if not math.isfinite(legacy_sensor_time_offset_s):
    return None
  return event_mono_s - legacy_sensor_time_offset_s


def gps_measurement_time_reject_reason(
  *,
  event_mono_s: float,
  observation_time_s: float,
  transition_mono_s: float | None = None,
  filter_time_s: float | None = None,
  max_stale_s: float = GPS_LOCATIOND_MAX_FILTER_REWIND_SECONDS,
  future_tol_s: float = GPS_MEASUREMENT_FUTURE_TOLERANCE_SECONDS,
) -> str | None:
  """Return reject reason name, or None if observation time is acceptable.

  Stale means outside the KF rewind window relative to Event age *or* filter time.
  """
  if not math.isfinite(event_mono_s) or not math.isfinite(observation_time_s):
    return "non_finite"
  if observation_time_s > event_mono_s + future_tol_s:
    return "future"
  if transition_mono_s is not None and math.isfinite(transition_mono_s):
    if observation_time_s <= transition_mono_s:
      return "pre_transition"
  if (event_mono_s - observation_time_s) > max_stale_s:
    return "stale"
  if filter_time_s is not None and math.isfinite(filter_time_s):
    if (filter_time_s - observation_time_s) > max_stale_s:
      return "stale"
  return None


def _finite_number(value: object) -> bool:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return False
  return math.isfinite(float(value))


def locationd_position_fields_usable(
  *,
  has_fix: bool,
  latitude: float,
  longitude: float,
  altitude: float,
  horizontal_accuracy: float,
  vertical_accuracy: float,
  speed_accuracy: float,
  bearing_accuracy_deg: float,
  v_ned: Sequence[float] | None,
  measurement_mono_ns: int | None,
  event_mono_s: float,
  require_measurement_mono: bool = True,
) -> bool:
  """True if and only if fields would pass Localizer::handle_gps acceptance (sans KF state).

  Aligns gpsard HEALTHY qualification with locationd usability. Does not compare
  accuracy magnitudes across sources — only "usable by the position consumer?".
  """
  if not has_fix:
    return False
  if not (
    _finite_number(latitude)
    and _finite_number(longitude)
    and _finite_number(altitude)
    and _finite_number(horizontal_accuracy)
    and _finite_number(vertical_accuracy)
    and _finite_number(speed_accuracy)
    and _finite_number(bearing_accuracy_deg)
  ):
    return False
  if abs(float(latitude)) > 90.0 or abs(float(longitude)) > 180.0:
    return False
  if abs(float(altitude)) > GPS_LOCATIOND_ALTITUDE_SANITY_M:
    return False
  if float(horizontal_accuracy) <= 0.0 or float(vertical_accuracy) <= 0.0:
    return False
  if float(speed_accuracy) <= 0.0 or float(bearing_accuracy_deg) <= 0.0:
    return False
  if math.hypot(float(horizontal_accuracy), float(vertical_accuracy)) >= GPS_LOCATIOND_SANE_UNCERTAINTY_M:
    return False
  if v_ned is None or len(v_ned) < 3:
    return False
  if not all(_finite_number(v) for v in v_ned[:3]):
    return False
  speed = math.sqrt(sum(float(v) ** 2 for v in v_ned[:3]))
  if not math.isfinite(speed) or speed > GPS_LOCATIOND_TRANS_SANITY_MPS:
    return False
  if not math.isfinite(event_mono_s):
    return False
  if require_measurement_mono:
    if not measurement_mono_ns_valid(measurement_mono_ns):
      return False
    obs = gps_observation_time_s(event_mono_s=event_mono_s, measurement_mono_ns=measurement_mono_ns)
    if obs is None:
      return False
    if gps_measurement_time_reject_reason(event_mono_s=event_mono_s, observation_time_s=obs) is not None:
      return False
  return True


def local_ned_position_covariance(horizontal_std_m: float, vertical_std_m: float) -> np.ndarray:
  """Isotropic horizontal + vertical covariance in NED (m²)."""
  if not (math.isfinite(horizontal_std_m) and math.isfinite(vertical_std_m)):
    raise ValueError("non-finite accuracy")
  if horizontal_std_m <= 0.0 or vertical_std_m <= 0.0:
    raise ValueError("non-positive accuracy")
  h2 = float(horizontal_std_m) ** 2
  v2 = float(vertical_std_m) ** 2
  if not (math.isfinite(h2) and math.isfinite(v2)):
    raise ValueError("non-finite variance")
  return np.diag([h2, h2, v2])


def ecef_position_covariance_from_hv(
  *,
  latitude_deg: float,
  longitude_deg: float,
  horizontal_std_m: float,
  vertical_std_m: float,
  std_factor: float = 1.0,
) -> np.ndarray:
  """Build full ECEF position covariance from local H/V stddevs (meters)."""
  if not (math.isfinite(latitude_deg) and math.isfinite(longitude_deg)):
    raise ValueError("non-finite lat/lon")
  if abs(latitude_deg) > 90.0 or abs(longitude_deg) > 180.0:
    raise ValueError("invalid lat/lon")
  if not math.isfinite(std_factor) or std_factor <= 0.0:
    raise ValueError("invalid std_factor")

  c_ned = local_ned_position_covariance(horizontal_std_m * std_factor, vertical_std_m * std_factor)
  converter = LocalCoord.from_geodetic([latitude_deg, longitude_deg, 0.0])
  r = np.asarray(converter.ned2ecef_matrix, dtype=float)
  c_ecef = r @ c_ned @ r.T
  if not np.all(np.isfinite(c_ecef)):
    raise ValueError("non-finite ecef covariance")
  # Symmetrize tiny numerical asymmetry.
  c_ecef = 0.5 * (c_ecef + c_ecef.T)
  return c_ecef


def covariance_is_psd(cov: np.ndarray, *, tol: float = 1e-9) -> bool:
  eig = np.linalg.eigvalsh(0.5 * (cov + cov.T))
  return bool(np.all(np.isfinite(eig)) and np.all(eig >= -tol))
