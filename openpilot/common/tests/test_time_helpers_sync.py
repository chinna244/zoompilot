import datetime
import json
import pytest

import openpilot.common.time_helpers as time_helpers


UTC_NOW = datetime.datetime(2026, 7, 23, 12, tzinfo=datetime.UTC)


def configure_paths(tmp_path, monkeypatch):
  gps_marker = tmp_path / "gps"
  ntp_marker = tmp_path / "ntp"
  boot_id = tmp_path / "boot_id"
  boot_id.write_text(
    "12345678-1234-5678-9234-567812345678\n",
    encoding="utf-8",
  )
  monkeypatch.setattr(time_helpers, "TIME_SYNC_MARKER", gps_marker)
  monkeypatch.setattr(time_helpers, "NTP_SYNC_MARKER", ntp_marker)
  monkeypatch.setattr(time_helpers, "BOOT_ID_PATH", boot_id)
  monkeypatch.setattr(time_helpers, "system_time_valid", lambda: True)
  monkeypatch.setattr(
    time_helpers,
    "_read_boottime_seconds",
    lambda: 123.5,
  )
  return gps_marker, ntp_marker


def test_receiver_marker_is_explicitly_nonindependent(
  tmp_path,
  monkeypatch,
):
  gps_marker, _ = configure_paths(tmp_path, monkeypatch)

  assert time_helpers.mark_time_synced(
    time_helpers.HostTimeSource.RECEIVER_DERIVED
  )
  payload = json.loads(gps_marker.read_text(encoding="utf-8"))
  assert payload == {
    "boot_id": "12345678-1234-5678-9234-567812345678",
    "source": "receiver_derived",
    "version": 1,
    "written_boottime_seconds": 123.5,
  }

  observation = time_helpers.read_host_time_observation()
  assert observation is not None
  assert observation.utc.tzinfo is datetime.UTC
  assert observation.source is (
    time_helpers.HostTimeSource.RECEIVER_DERIVED
  )
  assert not observation.independent
  assert observation.observed_boottime_seconds == 123.5
  assert observation.generation.startswith("receiver_derived:")


def test_legacy_gps_marker_is_receiver_derived(
  tmp_path,
  monkeypatch,
):
  gps_marker, _ = configure_paths(tmp_path, monkeypatch)
  gps_marker.write_text("gps", encoding="utf-8")

  observation = time_helpers.read_host_time_observation()

  assert observation is not None
  assert observation.source is (
    time_helpers.HostTimeSource.RECEIVER_DERIVED
  )
  assert not observation.independent


def test_unknown_marker_is_never_independent(
  tmp_path,
  monkeypatch,
):
  gps_marker, _ = configure_paths(tmp_path, monkeypatch)
  gps_marker.write_text("unexpected", encoding="utf-8")

  observation = time_helpers.read_host_time_observation()

  assert observation is not None
  assert observation.source is time_helpers.HostTimeSource.UNKNOWN
  assert not observation.independent


def test_generic_marker_cannot_claim_network_independence(
  tmp_path,
  monkeypatch,
):
  gps_marker, _ = configure_paths(tmp_path, monkeypatch)
  gps_marker.write_text(
    json.dumps({
      "version": 1,
      "source": "network_synchronized",
    }),
    encoding="utf-8",
  )

  observation = time_helpers.read_host_time_observation()

  assert observation is not None
  assert observation.source is time_helpers.HostTimeSource.UNKNOWN
  assert not observation.independent


def test_systemd_network_marker_is_independent(
  tmp_path,
  monkeypatch,
):
  _, ntp_marker = configure_paths(tmp_path, monkeypatch)
  ntp_marker.write_text("ntp", encoding="utf-8")

  observation = time_helpers.read_host_time_observation()

  assert observation is not None
  assert observation.source is (
    time_helpers.HostTimeSource.NETWORK_SYNCHRONIZED
  )
  assert observation.independent
  assert observation.generation.startswith("network_synchronized:")


def test_network_marker_supersedes_receiver_marker(
  tmp_path,
  monkeypatch,
):
  gps_marker, ntp_marker = configure_paths(tmp_path, monkeypatch)
  gps_marker.write_text("gps", encoding="utf-8")
  first = time_helpers.read_host_time_observation()
  assert first is not None
  assert first.source is (
    time_helpers.HostTimeSource.RECEIVER_DERIVED
  )

  ntp_marker.write_text("ntp", encoding="utf-8")
  second = time_helpers.read_host_time_observation()

  assert second is not None
  assert second.source is (
    time_helpers.HostTimeSource.NETWORK_SYNCHRONIZED
  )
  assert second.independent
  assert second.generation != first.generation


def test_invalid_system_time_has_no_host_observation(
  tmp_path,
  monkeypatch,
):
  gps_marker, _ = configure_paths(tmp_path, monkeypatch)
  gps_marker.write_text("gps", encoding="utf-8")
  monkeypatch.setattr(
    time_helpers,
    "system_time_valid",
    lambda: False,
  )

  assert time_helpers.read_host_time_observation() is None
  assert not time_helpers.trusted_time_synced()


def test_host_observation_rejects_invalid_independence_pair():
  with pytest.raises(ValueError):
    time_helpers.HostTimeObservation(
      utc=UTC_NOW,
      observed_boottime_seconds=1.0,
      uncertainty_seconds=30.0,
      source=time_helpers.HostTimeSource.RECEIVER_DERIVED,
      independent=True,
      generation="invalid",
    )


def test_repeated_receiver_source_preserves_marker_generation(
  tmp_path,
  monkeypatch,
):
  gps_marker, _ = configure_paths(tmp_path, monkeypatch)
  assert time_helpers.mark_time_synced("gps")
  before = gps_marker.stat().st_mtime_ns

  assert time_helpers.mark_time_synced(
    time_helpers.HostTimeSource.RECEIVER_DERIVED
  )

  assert gps_marker.stat().st_mtime_ns == before
