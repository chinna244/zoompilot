"""PR81 GPS measurement timing and NED→ECEF covariance math tests."""

from __future__ import annotations

import numpy as np
import pytest

from openpilot.common.gps_measurement import (
  GPS_MEASUREMENT_MAX_STALE_SECONDS,
  covariance_is_psd,
  ecef_position_covariance_from_hv,
  gps_measurement_time_reject_reason,
  gps_observation_time_s,
  local_ned_position_covariance,
  measurement_mono_ns_valid,
)
from openpilot.common.transformations.transformations import LocalCoord


class TestMeasurementTimingContract:
  def test_valid_measurement_mono(self):
    assert measurement_mono_ns_valid(1)
    assert not measurement_mono_ns_valid(0)
    assert not measurement_mono_ns_valid(None)
    assert not measurement_mono_ns_valid(False)  # type: ignore[arg-type]

  def test_observation_prefers_measurement_mono_not_publication(self):
    t = gps_observation_time_s(
      event_mono_s=100.5,
      measurement_mono_ns=100_000_000_000,
      legacy_sensor_time_offset_s=0.095,
    )
    assert t == pytest.approx(100.0)

  def test_legacy_fallback_when_unset(self):
    t = gps_observation_time_s(
      event_mono_s=10.0,
      measurement_mono_ns=0,
      legacy_sensor_time_offset_s=0.095,
    )
    assert t == pytest.approx(9.905)

  def test_processing_delay_preserves_earlier_measurement(self):
    # Publish at 5.0, measurement stamped at receive 4.2 → observation stays 4.2
    t = gps_observation_time_s(event_mono_s=5.0, measurement_mono_ns=4_200_000_000)
    assert t == pytest.approx(4.2)
    assert t is not None
    assert gps_measurement_time_reject_reason(event_mono_s=5.0, observation_time_s=t) is None

  def test_future_rejected(self):
    assert gps_measurement_time_reject_reason(event_mono_s=1.0, observation_time_s=1.1) == "future"

  def test_stale_rejected(self):
    obs = 1.0
    event = 1.0 + GPS_MEASUREMENT_MAX_STALE_SECONDS + 0.05
    assert gps_measurement_time_reject_reason(event_mono_s=event, observation_time_s=obs) == "stale"

  def test_filter_rewind_stale_even_if_event_fresh(self):
    # event delay 0.1s but filter is 1.2s ahead of measurement
    assert (
      gps_measurement_time_reject_reason(
        event_mono_s=100.1,
        observation_time_s=100.0,
        filter_time_s=101.2,
      )
      == "stale"
    )

  def test_within_filter_rewind_accepted(self):
    assert (
      gps_measurement_time_reject_reason(
        event_mono_s=100.6,
        observation_time_s=100.0,
        filter_time_s=100.7,
      )
      is None
    )

  def test_pre_transition_rejected(self):
    assert (
      gps_measurement_time_reject_reason(
        event_mono_s=5.5,
        observation_time_s=4.9,
        transition_mono_s=5.0,
      )
      == "pre_transition"
    )

  def test_fresh_accepted(self):
    assert gps_measurement_time_reject_reason(event_mono_s=10.0, observation_time_s=9.9) is None


class TestLocalCovariance:
  def test_axes_order_ned(self):
    c = local_ned_position_covariance(2.0, 5.0)
    assert c.shape == (3, 3)
    assert c[0, 0] == pytest.approx(4.0)
    assert c[1, 1] == pytest.approx(4.0)
    assert c[2, 2] == pytest.approx(25.0)
    assert c[0, 1] == 0.0

  def test_invalid_rejected(self):
    with pytest.raises(ValueError):
      local_ned_position_covariance(0.0, 1.0)
    with pytest.raises(ValueError):
      local_ned_position_covariance(1.0, float("nan"))
    with pytest.raises(ValueError):
      ecef_position_covariance_from_hv(
        latitude_deg=0.0,
        longitude_deg=0.0,
        horizontal_std_m=1.0,
        vertical_std_m=-1.0,
      )


class TestEcefCovarianceRotation:
  @pytest.mark.parametrize(
    ("lat", "lon"),
    [
      (0.0, 0.0),
      (0.0, 90.0),
      (37.0, -122.0),
      (-33.0, 151.0),
      (80.0, 10.0),
    ],
  )
  def test_symmetry_psd_trace(self, lat: float, lon: float):
    h, v = 3.0, 8.0
    c_ned = local_ned_position_covariance(h, v)
    c_ecef = ecef_position_covariance_from_hv(
      latitude_deg=lat,
      longitude_deg=lon,
      horizontal_std_m=h,
      vertical_std_m=v,
    )
    assert np.allclose(c_ecef, c_ecef.T, atol=1e-12)
    assert covariance_is_psd(c_ecef)
    assert np.trace(c_ecef) == pytest.approx(np.trace(c_ned), rel=1e-9, abs=1e-9)
    assert np.all(np.isfinite(c_ecef))

  def test_isotropic_h_equals_v_remains_isotropic(self):
    sigma = 4.0
    c = ecef_position_covariance_from_hv(
      latitude_deg=45.0,
      longitude_deg=30.0,
      horizontal_std_m=sigma,
      vertical_std_m=sigma,
    )
    # Rotation of σ² I is σ² I
    assert np.allclose(c, (sigma**2) * np.eye(3), atol=1e-9)

  def test_equator_lon0_vertical_along_ecef_z(self):
    # At lat=0,lon=0: N→-Z, E→+Y, D→-X (from LocalCoord matrix).
    h, v = 1.0, 10.0
    c = ecef_position_covariance_from_hv(
      latitude_deg=0.0,
      longitude_deg=0.0,
      horizontal_std_m=h,
      vertical_std_m=v,
    )
    converter = LocalCoord.from_geodetic([0.0, 0.0, 0.0])
    r = np.asarray(converter.ned2ecef_matrix)
    expected = r @ np.diag([h * h, h * h, v * v]) @ r.T
    assert np.allclose(c, expected, atol=1e-12)
    # Down maps to -X at equator/lon0 → large variance on ECEF X.
    assert c[0, 0] == pytest.approx(v * v, abs=1e-9)
    assert c[1, 1] == pytest.approx(h * h, abs=1e-9)
    assert c[2, 2] == pytest.approx(h * h, abs=1e-9)

  def test_vertical_dominant_orientation_follows_latitude(self):
    c_eq = ecef_position_covariance_from_hv(
      latitude_deg=0.0,
      longitude_deg=0.0,
      horizontal_std_m=1.0,
      vertical_std_m=100.0,
    )
    c_mid = ecef_position_covariance_from_hv(
      latitude_deg=45.0,
      longitude_deg=0.0,
      horizontal_std_m=1.0,
      vertical_std_m=100.0,
    )
    # Equator: vertical ≈ ECEF X; mid-lat mixes X/Z.
    assert c_eq[0, 0] > c_eq[2, 2]
    assert c_mid[0, 0] != pytest.approx(c_eq[0, 0], rel=0.01)
    assert covariance_is_psd(c_eq) and covariance_is_psd(c_mid)

  def test_horizontal_dominant(self):
    c = ecef_position_covariance_from_hv(
      latitude_deg=0.0,
      longitude_deg=0.0,
      horizontal_std_m=50.0,
      vertical_std_m=1.0,
    )
    assert c[1, 1] == pytest.approx(50.0**2)
    assert c[2, 2] == pytest.approx(50.0**2)
    assert c[0, 0] == pytest.approx(1.0)

  def test_std_factor_scales_variance(self):
    base = ecef_position_covariance_from_hv(
      latitude_deg=10.0,
      longitude_deg=20.0,
      horizontal_std_m=2.0,
      vertical_std_m=3.0,
      std_factor=1.0,
    )
    scaled = ecef_position_covariance_from_hv(
      latitude_deg=10.0,
      longitude_deg=20.0,
      horizontal_std_m=2.0,
      vertical_std_m=3.0,
      std_factor=2.0,
    )
    assert np.allclose(scaled, 4.0 * base, atol=1e-9)


class TestProducerTimingStamps:
  def test_ublox_main_loop_wires_ublox_raw_log_mono_into_parse_frame(self):
    """Source-level real wiring: ubloxd stamps from ubloxRaw Event.logMonoTime."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "system" / "ubloxd" / "ubloxd.py"
    text = src.read_text()
    call = "parser.parse_frame(frame, measurement_mono_ns=int(msg.logMonoTime))"
    assert call in text
    assert text.index("Stamp measurement epoch at ubloxRaw") < text.index(call)

  def test_ublox_parse_frame_stamps_from_ublox_raw_transport_time(self, monkeypatch):
    """Real parse_frame wiring: measurementMonoNs equals ubloxRaw framing Event time."""
    import time

    import openpilot.cereal.messaging as messaging
    from openpilot.system.ubloxd.tests.test_gps_assistance import build_nav_pvt_frame
    from openpilot.system.ubloxd.ubloxd import UbloxMsgParser

    clock = {"mono_s": 5.0}

    def fake_monotonic() -> float:
      return clock["mono_s"]

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(messaging.time, "monotonic", fake_monotonic)

    raw_log_mono_ns = 4_200_000_000  # transport/framing Event.logMonoTime
    frame = build_nav_pvt_frame()

    # Parse/publish happens later than framing completion.
    clock["mono_s"] = 5.5
    res = UbloxMsgParser().parse_frame(frame, measurement_mono_ns=raw_log_mono_ns)
    assert res is not None
    service, dat = res
    assert service == "gpsLocationExternal"
    assert dat.gpsLocationExternal.measurementMonoNs == raw_log_mono_ns
    assert dat.logMonoTime == 5_500_000_000
    assert dat.gpsLocationExternal.measurementMonoNs < dat.logMonoTime

  def test_qcom_diag_loop_stamps_before_process_position_report(self):
    """Source-level real wiring: DIAG path captures mono before parse/publish."""
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "system" / "qcomgpsd" / "qcomgpsd.py"
    text = src.read_text()
    stamp = text.index("measurement_mono_ns = time.monotonic_ns()")
    process = text.index("process_position_report(", stamp)
    assert stamp < process
    assert "measurement_mono_ns=measurement_mono_ns" in text[process : process + 400]

  def test_qcom_diag_boundary_stamp_survives_publication_delay(self, monkeypatch):
    """DIAG payload-availability stamp survives simulated parse/publish delay."""
    import time

    import openpilot.cereal.messaging as messaging
    from openpilot.system.qcomgpsd import qcomgpsd
    from openpilot.system.qcomgpsd.tests.helpers import valid_position_report

    clock = {"mono_s": 1.0}

    def fake_monotonic() -> float:
      return clock["mono_s"]

    def fake_monotonic_ns() -> int:
      return int(clock["mono_s"] * 1e9)

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    monkeypatch.setattr(time, "monotonic_ns", fake_monotonic_ns)
    monkeypatch.setattr(messaging.time, "monotonic", fake_monotonic)

    report = valid_position_report(u_PosSource=qcomgpsd.POS_SOURCE_KALMAN)

    def unpack(_payload):
      return report

    # Mirror DIAG loop: stamp at payload availability, then delay before publish.
    measurement_mono_ns = time.monotonic_ns()
    assert measurement_mono_ns == 1_000_000_000
    clock["mono_s"] = 1.5

    payload = b"\x00" * 16
    msg = qcomgpsd.process_position_report(
      payload,
      unpack_position=unpack,
      size_position=len(payload),
      measurement_mono_ns=measurement_mono_ns,
    )
    assert msg is not None
    assert msg.gpsLocation.measurementMonoNs == 1_000_000_000
    assert msg.logMonoTime == 1_500_000_000
    assert msg.gpsLocation.measurementMonoNs < msg.logMonoTime
    # Publication clock must not overwrite the earlier DIAG-boundary stamp.
    assert msg.gpsLocation.measurementMonoNs != msg.logMonoTime
