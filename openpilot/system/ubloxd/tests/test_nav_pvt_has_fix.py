from types import SimpleNamespace

import pytest

from openpilot.system.ubloxd.ubloxd import UbloxMsgParser


def _nav_pvt(*, fix_type: int, flags: int) -> SimpleNamespace:
  return SimpleNamespace(
    flags=flags,
    valid=0x07,
    fix_type=fix_type,
    lat=280_000_000,
    lon=-820_000_000,
    height=1_500,
    g_speed=0,
    head_mot=0,
    h_acc=1_000,
    num_sv=8,
    year=2024,
    month=1,
    day=2,
    hour=3,
    min=4,
    sec=5,
    nano=0,
    vel_n=100,
    vel_e=200,
    vel_d=0,
    v_acc=2_000,
    s_acc=100,
    head_acc=100,
  )


@pytest.mark.parametrize(
  ("fix_type", "flags", "expected"),
  [
    (0, 0x01, False),  # no fix
    (1, 0x01, False),  # dead reckoning
    (2, 0x01, False),  # 2D
    (3, 0x01, True),  # 3D
    (4, 0x01, True),  # GNSS+DR
    (5, 0x01, False),  # time only
    (3, 0x00, False),  # 3D without gnssFixOk
    (4, 0x00, False),  # GNSS+DR without gnssFixOk
    (2, 0x00, False),
    (5, 0x00, False),
  ],
)
def test_gen_nav_pvt_has_fix_policy(fix_type: int, flags: int, expected: bool) -> None:
  parser = UbloxMsgParser()
  service, dat = parser._gen_nav_pvt(
    _nav_pvt(fix_type=fix_type, flags=flags),  # type: ignore[arg-type, ty:invalid-argument-type]
    measurement_mono_ns=1_000_000_000,
  )
  assert service == "gpsLocationExternal"
  assert dat.gpsLocationExternal.hasFix is expected
  assert dat.gpsLocationExternal.measurementMonoNs == 1_000_000_000
  # Position fields are still populated for consumers that ignore hasFix.
  assert dat.gpsLocationExternal.latitude == pytest.approx(28.0)
  assert dat.gpsLocationExternal.longitude == pytest.approx(-82.0)


def test_gen_nav_pvt_3d_gnss_fix_ok_publishes_normally() -> None:
  parser = UbloxMsgParser()
  _, dat = parser._gen_nav_pvt(
    _nav_pvt(fix_type=3, flags=0x01),  # type: ignore[arg-type, ty:invalid-argument-type]
    measurement_mono_ns=2_000_000_000,
  )
  gps = dat.gpsLocationExternal
  assert gps.hasFix is True
  assert gps.measurementMonoNs == 2_000_000_000
  assert gps.satelliteCount == 8
  assert list(gps.vNED) == pytest.approx([0.1, 0.2, 0.0])
