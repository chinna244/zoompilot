"""PR80 qcomgpsd coexistence with ublox power rail."""

from __future__ import annotations


def test_ublox_hardware_available_helper(monkeypatch, tmp_path):
  from openpilot.common import gps as gps_mod

  tty = tmp_path / "ttyHS0"
  persist = tmp_path / "use-quectel-gps"

  def fake_exists(path: str) -> bool:
    if path == "/dev/ttyHS0":
      return tty.exists()
    if path == "/persist/comma/use-quectel-gps":
      return persist.exists()
    return False

  monkeypatch.setattr(gps_mod.os.path, "exists", fake_exists)
  assert gps_mod.ublox_hardware_available() is False
  tty.write_text("x")
  assert gps_mod.ublox_hardware_available() is True
  persist.write_text("1")
  assert gps_mod.ublox_hardware_available() is False


def test_qcomgpsd_skips_rail_when_ublox_present():
  import openpilot.system.qcomgpsd.qcomgpsd as qcom

  with open(qcom.__file__, encoding="utf-8") as f:
    src = f.read()
  assert "manage_ublox_rail" in src
  assert "ublox_hardware_available" in src
  assert "skipping GNSS_PWR_EN" in src
