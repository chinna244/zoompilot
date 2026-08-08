import errno
import json
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from openpilot.system.ubloxd.trusted_time_anchor import (
  RTC_VL_READ,
  AnchorFileState,
  RtcVoltageStatus,
  TimeProvenance,
  TrustedTimeAnchor,
  TrustedTimeAnchorError,
  TrustedTimeAnchorStore,
  TrustedTimeSource,
  load_trusted_time_anchor,
  read_boot_id,
  read_boottime_seconds,
  read_rtc_epoch_seconds,
  read_rtc_voltage_status,
  validate_trusted_time_anchor,
)


BOOT_ID = "12345678-1234-5678-9234-567812345678"
NOW = datetime(2026, 7, 22, 21, tzinfo=UTC)


def anchor(
  sequence: int = 1,
  *,
  source: TrustedTimeSource = (
    TrustedTimeSource.SYSTEM_SYNCHRONIZED
  ),
  provenance: TimeProvenance = (
    TimeProvenance.NETWORK_INDEPENDENT
  ),
) -> TrustedTimeAnchor:
  return TrustedTimeAnchor(
    version=1,
    trusted_utc=NOW + timedelta(seconds=sequence),
    source=source,
    provenance=provenance,
    authorized=True,
    independent=True,
    uncertainty_seconds=30.0,
    boot_id=BOOT_ID,
    boottime_seconds=100.0 + sequence,
    rtc_epoch_seconds=1_784_754_260 + sequence,
    rtc_voltage_status_supported=False,
    rtc_voltage_status_flags=None,
    sequence=sequence,
  )


def write_anchor_json(path, payload):
  path.write_text(
    json.dumps(payload, separators=(",", ":")) + "\n",
    encoding="utf-8",
  )
  path.chmod(0o600)


def test_anchor_validation_normalizes_utc_and_numeric_fields():
  value = anchor()
  normalized = validate_trusted_time_anchor(
    replace(
      value,
      trusted_utc=value.trusted_utc.astimezone(
        timezone(timedelta(hours=-5))
      ),
      uncertainty_seconds=30,
      boottime_seconds=101,
    )
  )

  assert normalized.trusted_utc.tzinfo is UTC
  assert normalized.trusted_utc == value.trusted_utc
  assert normalized.uncertainty_seconds == 30.0
  assert normalized.boottime_seconds == 101.0


@pytest.mark.parametrize(
  "changes",
  (
    {"version": 2},
    {"trusted_utc": datetime(2026, 7, 22, 21)},
    {"authorized": False},
    {"independent": False},
    {"uncertainty_seconds": -1.0},
    {"uncertainty_seconds": float("nan")},
    {"boot_id": "not-a-uuid"},
    {"boottime_seconds": -1.0},
    {"rtc_epoch_seconds": -1},
    {
      "rtc_voltage_status_supported": False,
      "rtc_voltage_status_flags": 0,
    },
    {
      "rtc_voltage_status_supported": True,
      "rtc_voltage_status_flags": None,
    },
    {"sequence": 0},
  ),
)
def test_invalid_anchor_fields_are_rejected(changes):
  with pytest.raises(TrustedTimeAnchorError):
    validate_trusted_time_anchor(
      replace(anchor(), **changes)
    )


def test_invalid_source_provenance_pair_is_rejected():
  with pytest.raises(
    TrustedTimeAnchorError,
    match="do not match",
  ):
    validate_trusted_time_anchor(
      anchor(
        source=(
          TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
        ),
        provenance=TimeProvenance.EXTERNAL_OR_UNKNOWN,
      )
    )


def test_store_round_trip_rotation_permissions_and_fallback(tmp_path):
  store = TrustedTimeAnchorStore(
    tmp_path / "trusted_time_anchor.json"
  )
  first = anchor(1)
  second = anchor(2)

  assert store.next_sequence() == 1
  saved_first = store.save(first)
  assert saved_first.anchor == first
  assert store.next_sequence() == 2

  saved_second = store.save(second)
  assert saved_second.anchor == second

  selection, inventory = store.load_best()
  assert selection is not None
  assert selection.generation == "primary"
  assert selection.anchor == second
  assert inventory.primary.anchor == second
  assert inventory.previous.anchor == first
  assert stat.S_IMODE(
    store.primary_path.stat().st_mode
  ) == 0o600
  assert stat.S_IMODE(
    store.previous_path.stat().st_mode
  ) == 0o600

  store.primary_path.write_text(
    "{broken",
    encoding="utf-8",
  )
  store.primary_path.chmod(0o600)

  selection, inventory = store.load_best()
  assert selection is not None
  assert selection.generation == "previous"
  assert selection.anchor == first
  assert (
    inventory.primary.state
    is AnchorFileState.INVALID
  )


def test_store_preserves_valid_previous_when_primary_is_corrupt(
  tmp_path,
):
  store = TrustedTimeAnchorStore(
    tmp_path / "trusted_time_anchor.json"
  )
  store.save(anchor(1))
  store.save(anchor(2))

  store.primary_path.write_text(
    "{broken",
    encoding="utf-8",
  )
  store.primary_path.chmod(0o600)

  store.save(anchor(3))

  assert load_trusted_time_anchor(
    store.primary_path
  ) == anchor(3)
  assert load_trusted_time_anchor(
    store.previous_path
  ) == anchor(1)


def test_store_preserves_newer_previous_generation(tmp_path):
  store = TrustedTimeAnchorStore(
    tmp_path / "trusted_time_anchor.json"
  )
  store.save(anchor(1))
  store.save(anchor(2))

  primary_contents = store.primary_path.read_bytes()
  previous_contents = store.previous_path.read_bytes()
  store.primary_path.write_bytes(previous_contents)
  store.previous_path.write_bytes(primary_contents)
  store.primary_path.chmod(0o600)
  store.previous_path.chmod(0o600)

  selection, _ = store.load_best()
  assert selection is not None
  assert selection.generation == "previous"
  assert selection.anchor == anchor(2)

  store.save(anchor(3))

  assert load_trusted_time_anchor(
    store.primary_path
  ) == anchor(3)
  assert load_trusted_time_anchor(
    store.previous_path
  ) == anchor(2)


def test_store_rejects_nonadvancing_sequence(tmp_path):
  store = TrustedTimeAnchorStore(
    tmp_path / "trusted_time_anchor.json"
  )
  store.save(anchor(2))

  with pytest.raises(
    TrustedTimeAnchorError,
    match="did not advance",
  ):
    store.save(anchor(2))


def test_store_rejects_symbolic_link_target(tmp_path):
  target = tmp_path / "target"
  target.write_text("unchanged", encoding="utf-8")
  path = tmp_path / "trusted_time_anchor.json"
  path.symlink_to(target)
  store = TrustedTimeAnchorStore(path)

  with pytest.raises(
    TrustedTimeAnchorError,
    match="symbolic link",
  ):
    store.save(anchor())


def test_loader_rejects_duplicate_fields(tmp_path):
  path = tmp_path / "trusted_time_anchor.json"
  encoded = (
    '{"version":1,"version":1}\n'
  )
  path.write_text(encoded, encoding="utf-8")
  path.chmod(0o600)

  with pytest.raises(
    TrustedTimeAnchorError,
    match="Duplicate",
  ):
    load_trusted_time_anchor(path)


def test_loader_ignores_unknown_optional_fields(tmp_path):
  store = TrustedTimeAnchorStore(
    tmp_path / "trusted_time_anchor.json"
  )
  store.save(anchor())
  payload = json.loads(
    store.primary_path.read_text(encoding="utf-8")
  )
  payload["future_optional_evidence"] = {
    "value": 1,
  }
  write_anchor_json(store.primary_path, payload)

  assert load_trusted_time_anchor(
    store.primary_path
  ) == anchor()


def test_loader_rejects_permissions_that_are_too_broad(
  tmp_path,
):
  store = TrustedTimeAnchorStore(
    tmp_path / "trusted_time_anchor.json"
  )
  store.save(anchor())
  store.primary_path.chmod(0o644)

  with pytest.raises(
    TrustedTimeAnchorError,
    match="permissions",
  ):
    load_trusted_time_anchor(
      store.primary_path
    )


def test_boot_id_and_rtc_epoch_readers(tmp_path):
  boot_id_path = tmp_path / "boot_id"
  boot_id_path.write_text(
    f"{BOOT_ID}\n",
    encoding="utf-8",
  )
  rtc_path = tmp_path / "since_epoch"
  rtc_path.write_text("1784754260\n", encoding="utf-8")

  assert read_boot_id(boot_id_path) == BOOT_ID
  assert read_rtc_epoch_seconds(rtc_path) == 1_784_754_260

  boot_id_path.write_text("invalid\n", encoding="utf-8")
  rtc_path.write_text("-1\n", encoding="utf-8")

  assert read_boot_id(boot_id_path) is None
  assert read_rtc_epoch_seconds(rtc_path) is None


@pytest.mark.parametrize(
  ("value", "expected"),
  (
    (123.5, 123.5),
    (-1.0, None),
    (float("nan"), None),
    (True, None),
  ),
)
def test_boottime_reader_validates_values(value, expected):
  assert read_boottime_seconds(
    lambda clock_id: value
  ) == expected


def test_rtc_voltage_status_supported(tmp_path):
  path = tmp_path / "rtc0"
  path.touch()

  def ioctl_call(
    descriptor,
    request,
    buffer,
    mutate,
  ):
    assert descriptor >= 0
    assert request == RTC_VL_READ
    assert mutate is True
    buffer[:] = (5).to_bytes(4, "little")

  assert read_rtc_voltage_status(
    path,
    ioctl_call=ioctl_call,
  ) == RtcVoltageStatus(True, 5)


def test_rtc_voltage_status_unsupported(tmp_path):
  path = tmp_path / "rtc0"
  path.touch()

  def ioctl_call(*args):
    raise OSError(errno.ENOTTY, "unsupported")

  assert read_rtc_voltage_status(
    path,
    ioctl_call=ioctl_call,
  ) == RtcVoltageStatus(False, None)


def test_rtc_voltage_status_unexpected_failure_is_preserved(
  tmp_path,
):
  path = tmp_path / "rtc0"
  path.touch()

  def ioctl_call(*args):
    raise OSError(errno.EIO, "failed")

  result = read_rtc_voltage_status(
    path,
    ioctl_call=ioctl_call,
  )

  assert result.supported is False
  assert result.flags is None
  assert result.error is not None
  assert "OSError" in result.error
