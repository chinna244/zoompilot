from datetime import UTC, datetime, timedelta

import pytest

from openpilot.system.ubloxd.gps_assistance import (
  FRESH_POSITION_ASSISTANCE_MAX_AGE_SECONDS,
  LEGACY_RECEIVER_FINGERPRINT_SUFFIX,
  MAX_RESTORE_POSITION_ACCURACY_CM,
  MIN_RESTORE_POSITION_ACCURACY_CM,
  MonVerInfo,
  age_safe_restore_position_accuracy_cm,
  build_durable_receiver_fingerprint,
  build_position_assistance_message,
  create_cache,
  load_cache,
  parse_durable_receiver_fingerprint,
  parse_legacy_receiver_fingerprint,
  receiver_fingerprints_compatible,
  save_cache,
)
from openpilot.system.ubloxd.tests.test_gps_assistance import (
  build_dbd_frame,
  reliable_fix,
)


def _mon_ver(
  *,
  software: str = "EXT CORE 3.01 (111111)",
  hardware: str = "00080000",
  protocol: str = "20.30",
  firmware: str = "HPG 1.40ROV",
  firmware_extra: str | None = None,
) -> MonVerInfo:
  extensions = [f"PROTVER={protocol}", f"FWVER={firmware}", "GPS;GLO"]
  if firmware_extra is not None:
    extensions.insert(1, f"FWVER={firmware_extra}")
  return MonVerInfo(
    software_version=software,
    hardware_version=hardware,
    extensions=tuple(extensions),
  )


def test_durable_fingerprint_identical_identity() -> None:
  info = _mon_ver()
  a = build_durable_receiver_fingerprint("serial-a", info)
  b = build_durable_receiver_fingerprint("serial-a", info)
  assert a == b
  assert a.startswith("v1|serial-a|")
  assert "sw=" in a and "hw=" in a and "prot=20.30" in a and "fw=" in a
  assert receiver_fingerprints_compatible(a, b)


def test_durable_fingerprint_fwver_is_deterministic_with_multiple_values() -> None:
  first = build_durable_receiver_fingerprint(
    "serial-a",
    _mon_ver(firmware="B", firmware_extra="A"),
  )
  second = build_durable_receiver_fingerprint(
    "serial-a",
    _mon_ver(firmware="A", firmware_extra="B"),
  )
  assert first == second
  assert first.endswith("fw=a;b")


def test_durable_fingerprint_changes_with_software() -> None:
  base = build_durable_receiver_fingerprint("serial-a", _mon_ver())
  changed = build_durable_receiver_fingerprint(
    "serial-a",
    _mon_ver(software="EXT CORE 3.02 (222222)"),
  )
  assert base != changed
  assert not receiver_fingerprints_compatible(base, changed)


def test_durable_fingerprint_changes_with_hardware() -> None:
  base = build_durable_receiver_fingerprint("serial-a", _mon_ver())
  changed = build_durable_receiver_fingerprint(
    "serial-a",
    _mon_ver(hardware="00090000"),
  )
  assert base != changed
  assert not receiver_fingerprints_compatible(base, changed)


def test_durable_fingerprint_changes_with_protocol() -> None:
  base = build_durable_receiver_fingerprint("serial-a", _mon_ver())
  changed = build_durable_receiver_fingerprint(
    "serial-a",
    _mon_ver(protocol="27.00"),
  )
  assert base != changed
  assert not receiver_fingerprints_compatible(base, changed)


def test_durable_fingerprint_changes_with_firmware_only() -> None:
  base = build_durable_receiver_fingerprint("serial-a", _mon_ver())
  changed = build_durable_receiver_fingerprint(
    "serial-a",
    _mon_ver(firmware="HPG 1.41ROV"),
  )
  assert base != changed
  assert not receiver_fingerprints_compatible(base, changed)


def test_durable_fingerprint_changes_with_serial() -> None:
  info = _mon_ver()
  a = build_durable_receiver_fingerprint("serial-a", info)
  b = build_durable_receiver_fingerprint("serial-b", info)
  assert a != b
  assert not receiver_fingerprints_compatible(a, b)


def test_missing_fwver_is_fail_closed() -> None:
  incomplete = build_durable_receiver_fingerprint(
    "serial-a",
    MonVerInfo(
      software_version="EXT CORE 3.01",
      hardware_version="00080000",
      extensions=("PROTVER=20.30", "GPS;GLO"),
    ),
  )
  assert incomplete.endswith("mon_ver_incomplete")
  assert parse_durable_receiver_fingerprint(incomplete) is None
  assert not receiver_fingerprints_compatible(incomplete, incomplete)


def test_missing_protver_is_fail_closed() -> None:
  incomplete = build_durable_receiver_fingerprint(
    "serial-a",
    MonVerInfo(
      software_version="EXT CORE 3.01",
      hardware_version="00080000",
      extensions=("FWVER=HPG 1.40ROV",),
    ),
  )
  assert incomplete.endswith("mon_ver_incomplete")
  assert not receiver_fingerprints_compatible(incomplete, incomplete)


def test_fail_safe_sentinels_never_match_themselves() -> None:
  unavailable = build_durable_receiver_fingerprint("serial-a", None)
  incomplete = build_durable_receiver_fingerprint(
    "serial-a",
    MonVerInfo(software_version="", hardware_version="00080000", extensions=()),
  )
  durable = build_durable_receiver_fingerprint("serial-a", _mon_ver())
  malformed = "v1|serial-a|not-a-complete-identity"

  assert unavailable.endswith("mon_ver_unavailable")
  assert incomplete.endswith("mon_ver_incomplete")
  assert not receiver_fingerprints_compatible(unavailable, unavailable)
  assert not receiver_fingerprints_compatible(incomplete, incomplete)
  assert not receiver_fingerprints_compatible(unavailable, durable)
  assert not receiver_fingerprints_compatible(durable, unavailable)
  assert not receiver_fingerprints_compatible(incomplete, durable)
  assert not receiver_fingerprints_compatible(malformed, malformed)
  assert receiver_fingerprints_compatible(durable, durable)


def test_legacy_fingerprint_fail_closed_for_receiver_state() -> None:
  legacy = f"serial-a|{LEGACY_RECEIVER_FINGERPRINT_SUFFIX}"
  expected = build_durable_receiver_fingerprint("serial-a", _mon_ver(protocol="20.30"))
  assert parse_legacy_receiver_fingerprint(legacy) is not None
  assert not receiver_fingerprints_compatible(legacy, expected)
  assert not receiver_fingerprints_compatible(legacy, legacy)
  assert not receiver_fingerprints_compatible(expected, legacy)


def test_load_cache_rejects_legacy_fingerprint(tmp_path) -> None:
  path = tmp_path / "gps_assistance.json"
  legacy = f"device123|{LEGACY_RECEIVER_FINGERPRINT_SUFFIX}"
  cache = create_cache(
    receiver_fingerprint=legacy,
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 1, tzinfo=UTC),
  )
  save_cache(path, cache)
  expected = build_durable_receiver_fingerprint("device123", _mon_ver(protocol="20.30"))
  with pytest.raises(Exception, match="different receiver"):
    load_cache(
      path,
      expected_receiver_fingerprint=expected,
      now_utc=datetime(2026, 7, 2, tzinfo=UTC),
    )


def test_load_cache_accepts_matching_durable_fingerprint(tmp_path) -> None:
  path = tmp_path / "gps_assistance.json"
  fingerprint = build_durable_receiver_fingerprint("device123", _mon_ver())
  cache = create_cache(
    receiver_fingerprint=fingerprint,
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 1, tzinfo=UTC),
  )
  save_cache(path, cache)
  loaded = load_cache(
    path,
    expected_receiver_fingerprint=fingerprint,
    now_utc=datetime(2026, 7, 2, tzinfo=UTC),
  )
  assert loaded.receiver_fingerprint == fingerprint


def test_load_cache_rejects_firmware_change(tmp_path) -> None:
  path = tmp_path / "gps_assistance.json"
  old = build_durable_receiver_fingerprint("device123", _mon_ver())
  cache = create_cache(
    receiver_fingerprint=old,
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 1, tzinfo=UTC),
  )
  save_cache(path, cache)
  new = build_durable_receiver_fingerprint(
    "device123",
    _mon_ver(firmware="HPG 1.41ROV"),
  )
  with pytest.raises(Exception, match="different receiver"):
    load_cache(
      path,
      expected_receiver_fingerprint=new,
      now_utc=datetime(2026, 7, 2, tzinfo=UTC),
    )


def test_fresh_verified_position_may_send() -> None:
  accuracy, reason = age_safe_restore_position_accuracy_cm(
    250,
    age_seconds=60.0,
    age_verified=True,
  )
  assert accuracy == MIN_RESTORE_POSITION_ACCURACY_CM
  assert reason == "verified_fresh_floor"


def test_verified_age_beyond_fresh_window_skips() -> None:
  accuracy, reason = age_safe_restore_position_accuracy_cm(
    250,
    age_seconds=FRESH_POSITION_ASSISTANCE_MAX_AGE_SECONDS + 1,
    age_verified=True,
  )
  assert accuracy is None
  assert reason == "position_uncertainty_unrepresentable"


def test_unverified_age_skips() -> None:
  accuracy, reason = age_safe_restore_position_accuracy_cm(
    250,
    age_seconds=10.0,
    age_verified=False,
  )
  assert accuracy is None
  assert reason == "position_age_unverified"


def test_unknown_age_skips() -> None:
  accuracy, reason = age_safe_restore_position_accuracy_cm(
    MIN_RESTORE_POSITION_ACCURACY_CM,
    age_seconds=None,
    age_verified=True,
  )
  assert accuracy is None
  assert reason == "position_age_unverified"


def test_negative_age_skips() -> None:
  accuracy, reason = age_safe_restore_position_accuracy_cm(
    250,
    age_seconds=-1.0,
    age_verified=True,
  )
  assert accuracy is None
  assert reason == "position_age_unverified"


def test_stored_accuracy_above_policy_limit_skips_not_clamped() -> None:
  accuracy, reason = age_safe_restore_position_accuracy_cm(
    MAX_RESTORE_POSITION_ACCURACY_CM + 1,
    age_seconds=60.0,
    age_verified=True,
  )
  assert accuracy is None
  assert reason == "position_uncertainty_unrepresentable"


def test_build_position_assistance_never_clamps_downward() -> None:
  # Floor may increase uncertainty, but values above the policy limit must raise.
  message = build_position_assistance_message(
    latitude_e7=280_000_000,
    longitude_e7=-820_000_000,
    altitude_cm=1_500,
    position_accuracy_cm=250,
  )
  import struct

  payload = message[6:-2]
  _, _, _, _, _, accuracy = struct.unpack("<BBxxiiiI", payload)
  assert accuracy == MIN_RESTORE_POSITION_ACCURACY_CM

  with pytest.raises(Exception, match="usefulness limit|valid range"):
    build_position_assistance_message(
      latitude_e7=280_000_000,
      longitude_e7=-820_000_000,
      altitude_cm=1_500,
      position_accuracy_cm=MAX_RESTORE_POSITION_ACCURACY_CM + 1,
    )


def test_load_cache_rejects_protocol_change(tmp_path) -> None:
  path = tmp_path / "gps_assistance.json"
  old = build_durable_receiver_fingerprint("device123", _mon_ver(protocol="20.30"))
  cache = create_cache(
    receiver_fingerprint=old,
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 1, tzinfo=UTC),
  )
  save_cache(path, cache)
  new = build_durable_receiver_fingerprint("device123", _mon_ver(protocol="27.00"))
  with pytest.raises(Exception, match="different receiver"):
    load_cache(
      path,
      expected_receiver_fingerprint=new,
      now_utc=datetime(2026, 7, 2, tzinfo=UTC),
    )


def test_load_cache_rejects_serial_change(tmp_path) -> None:
  path = tmp_path / "gps_assistance.json"
  old = build_durable_receiver_fingerprint("device123", _mon_ver())
  cache = create_cache(
    receiver_fingerprint=old,
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 1, tzinfo=UTC),
  )
  save_cache(path, cache)
  new = build_durable_receiver_fingerprint("device999", _mon_ver())
  with pytest.raises(Exception, match="different receiver"):
    load_cache(
      path,
      expected_receiver_fingerprint=new,
      now_utc=datetime(2026, 7, 2, tzinfo=UTC),
    )


def _legacy_restore_with_logging_failure(
  monkeypatch: pytest.MonkeyPatch,
  *,
  trusted_now: datetime | None,
  time_assistance_source: str | None,
  age_seconds: float,
) -> tuple[object, list[int | None]]:
  from types import SimpleNamespace

  from openpilot.system.ubloxd import pigeond

  saved_at = datetime(2026, 7, 10, tzinfo=UTC)
  cache = SimpleNamespace(
    saved_at_utc=saved_at,
    rtc_counter_seconds=100,
    database_frames=(bytes((0,)), bytes((1,))),
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    position_accuracy_cm=1_000,
    quality=None,
  )
  calls: list[int | None] = []

  def fake_send_mga(_pigeon, _message, timeout=None, database_frame_index=None):
    calls.append(database_frame_index)

  def boom_info(message, *args, **kwargs):
    if "GPS legacy position assistance skipped" in str(message):
      raise RuntimeError("cloudlog failed during position skip")
    return None

  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(pigeond, "load_cache", lambda *args, **kwargs: cache)
  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", fake_send_mga)
  monkeypatch.setattr(pigeond.cloudlog, "info", boom_info)
  monkeypatch.setattr(pigeond.cloudlog, "warning", lambda *_a, **_k: None)
  monkeypatch.setattr(pigeond.cloudlog, "error", lambda *_a, **_k: None)
  monkeypatch.setattr(pigeond.time, "sleep", lambda *_a, **_k: None)

  if trusted_now is None:
    result = pigeond.restore_navigation_assistance(
      object(),  # type: ignore[arg-type, ty:invalid-argument-type]
      "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
      allow_legacy_direct_restore=True,
    )
  else:
    cache.saved_at_utc = trusted_now - timedelta(seconds=age_seconds)
    result = pigeond.restore_navigation_assistance(
      object(),  # type: ignore[arg-type, ty:invalid-argument-type]
      "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
      trusted_now=trusted_now,
      time_assistance_source=time_assistance_source,
      allow_legacy_direct_restore=True,
    )
  return result, calls


def test_position_skip_logging_failure_does_not_block_dbd_unverified(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  from openpilot.system.ubloxd import pigeond

  result, calls = _legacy_restore_with_logging_failure(
    monkeypatch,
    trusted_now=None,
    time_assistance_source=None,
    age_seconds=0.0,
  )

  assert None not in calls
  assert calls == [0, 1]
  assert result.status is pigeond.NavigationAssistanceRestoreStatus.COMPLETE
  assert result.accepted_frame_count == 2
  assert result.usable


def test_position_skip_logging_failure_does_not_block_dbd_stale(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  from openpilot.system.ubloxd import pigeond

  now = datetime(2026, 7, 10, 1, 0, tzinfo=UTC)
  result, calls = _legacy_restore_with_logging_failure(
    monkeypatch,
    trusted_now=now,
    time_assistance_source="system_synchronized",
    age_seconds=FRESH_POSITION_ASSISTANCE_MAX_AGE_SECONDS + 60,
  )

  assert None not in calls
  assert calls == [0, 1]
  assert result.status is pigeond.NavigationAssistanceRestoreStatus.COMPLETE
  assert result.accepted_frame_count == 2
  assert result.usable
