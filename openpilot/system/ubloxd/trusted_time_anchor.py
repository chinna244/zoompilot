from __future__ import annotations

import errno
import fcntl
import json
import os
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum, StrEnum
from math import isfinite
from pathlib import Path

TRUSTED_TIME_ANCHOR_VERSION = 1
MAX_TRUSTED_TIME_ANCHOR_FILE_BYTES = 16 * 1024
MAX_TRUSTED_TIME_UNCERTAINTY_SECONDS = 65_535.0

TRUSTED_TIME_ANCHOR_PATH = Path(
  "/data/gps_assistance/trusted_time_anchor.json"
)
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
RTC_EPOCH_PATH = Path("/sys/class/rtc/rtc0/since_epoch")
RTC_DEVICE_PATH = Path("/dev/rtc0")

# Linux UAPI _IOC/_IOR layout used by the C4's arm64 kernel.
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14
_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS
_IOC_READ = 2
_UNSIGNED_INT_BYTES = 4


def _ior(ioctl_type: int, number: int, size: int) -> int:
  return (
    (_IOC_READ << _IOC_DIRSHIFT)
    | (ioctl_type << _IOC_TYPESHIFT)
    | (number << _IOC_NRSHIFT)
    | (size << _IOC_SIZESHIFT)
  )


RTC_VL_READ = _ior(ord("p"), 0x13, _UNSIGNED_INT_BYTES)


class TrustedTimeAnchorError(ValueError):
  pass


class TrustedTimeSource(StrEnum):
  SYSTEM_SYNCHRONIZED = "system_synchronized"
  RECEIVER_UTC_UNASSISTED_GNSS = (
    "receiver_utc_unassisted_gnss"
  )


class TimeProvenance(StrEnum):
  EXTERNAL_OR_UNKNOWN = "external_or_unknown"
  NETWORK_INDEPENDENT = "network_independent"
  GNSS_INDEPENDENT = "gnss_independent"


@dataclass(frozen=True)
class TrustedTimeAnchor:
  version: int
  trusted_utc: datetime
  source: TrustedTimeSource
  provenance: TimeProvenance
  authorized: bool
  independent: bool
  uncertainty_seconds: float
  boot_id: str
  boottime_seconds: float
  rtc_epoch_seconds: int | None
  rtc_voltage_status_supported: bool
  rtc_voltage_status_flags: int | None
  sequence: int


@dataclass(frozen=True)
class RtcVoltageStatus:
  supported: bool
  flags: int | None
  error: str | None = None


class AnchorFileState(Enum):
  ABSENT = "absent"
  VALID = "valid"
  INVALID = "invalid"


@dataclass(frozen=True)
class AnchorFileInspection:
  generation: str
  path: Path
  state: AnchorFileState
  anchor: TrustedTimeAnchor | None = None
  error: str | None = None


@dataclass(frozen=True)
class TrustedTimeAnchorInventory:
  primary: AnchorFileInspection
  previous: AnchorFileInspection


@dataclass(frozen=True)
class TrustedTimeAnchorSelection:
  generation: str
  anchor: TrustedTimeAnchor
  reason: str


def _validated_utc(value: datetime) -> datetime:
  if not isinstance(value, datetime):
    raise TrustedTimeAnchorError(
      "Trusted time anchor UTC is not a datetime"
    )
  if value.tzinfo is None:
    raise TrustedTimeAnchorError(
      "Trusted time anchor UTC must be timezone-aware"
    )
  try:
    if value.utcoffset() is None:
      raise TrustedTimeAnchorError(
        "Trusted time anchor UTC has no UTC offset"
      )
    return value.astimezone(UTC)
  except TrustedTimeAnchorError:
    raise
  except Exception as exc:
    raise TrustedTimeAnchorError(
      "Trusted time anchor UTC timezone is invalid"
    ) from exc


def _validated_boot_id(value: str) -> str:
  if type(value) is not str:
    raise TrustedTimeAnchorError(
      "Trusted time anchor boot ID is invalid"
    )
  normalized = value.strip().lower()
  try:
    parsed = uuid.UUID(normalized)
  except (AttributeError, ValueError) as exc:
    raise TrustedTimeAnchorError(
      "Trusted time anchor boot ID is invalid"
    ) from exc
  if str(parsed) != normalized:
    raise TrustedTimeAnchorError(
      "Trusted time anchor boot ID is not canonical"
    )
  return normalized


def validate_trusted_time_anchor(
  anchor: TrustedTimeAnchor,
) -> TrustedTimeAnchor:
  if not isinstance(anchor, TrustedTimeAnchor):
    raise TrustedTimeAnchorError(
      "Trusted time anchor has an invalid type"
    )
  if (
    type(anchor.version) is not int
    or anchor.version != TRUSTED_TIME_ANCHOR_VERSION
  ):
    raise TrustedTimeAnchorError(
      "Unsupported trusted time anchor version"
    )
  if not isinstance(anchor.source, TrustedTimeSource):
    raise TrustedTimeAnchorError(
      "Trusted time anchor source is invalid"
    )
  if not isinstance(anchor.provenance, TimeProvenance):
    raise TrustedTimeAnchorError(
      "Trusted time anchor provenance is invalid"
    )
  valid_pairs = {
    (
      TrustedTimeSource.SYSTEM_SYNCHRONIZED,
      TimeProvenance.NETWORK_INDEPENDENT,
    ),
    (
      TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS,
      TimeProvenance.GNSS_INDEPENDENT,
    ),
  }
  if (anchor.source, anchor.provenance) not in valid_pairs:
    raise TrustedTimeAnchorError(
      "Trusted time source and provenance do not match"
    )
  if anchor.authorized is not True:
    raise TrustedTimeAnchorError(
      "Stored trusted time anchor must be authorized"
    )
  if anchor.independent is not True:
    raise TrustedTimeAnchorError(
      "Stored trusted time anchor must be independent"
    )
  if (
    type(anchor.uncertainty_seconds) not in (int, float)
    or isinstance(anchor.uncertainty_seconds, bool)
    or not isfinite(anchor.uncertainty_seconds)
    or not 0.0 <= anchor.uncertainty_seconds
    <= MAX_TRUSTED_TIME_UNCERTAINTY_SECONDS
  ):
    raise TrustedTimeAnchorError(
      "Trusted time anchor uncertainty is invalid"
    )
  if (
    type(anchor.boottime_seconds) not in (int, float)
    or isinstance(anchor.boottime_seconds, bool)
    or not isfinite(anchor.boottime_seconds)
    or anchor.boottime_seconds < 0.0
  ):
    raise TrustedTimeAnchorError(
      "Trusted time anchor boottime is invalid"
    )
  if (
    anchor.rtc_epoch_seconds is not None
    and (
      type(anchor.rtc_epoch_seconds) is not int
      or anchor.rtc_epoch_seconds < 0
    )
  ):
    raise TrustedTimeAnchorError(
      "Trusted time anchor RTC epoch is invalid"
    )
  if type(anchor.rtc_voltage_status_supported) is not bool:
    raise TrustedTimeAnchorError(
      "Trusted time anchor RTC voltage support is invalid"
    )
  if anchor.rtc_voltage_status_supported:
    if (
      type(anchor.rtc_voltage_status_flags) is not int
      or not 0 <= anchor.rtc_voltage_status_flags <= 0xFFFFFFFF
    ):
      raise TrustedTimeAnchorError(
        "Trusted time anchor RTC voltage flags are invalid"
      )
  elif anchor.rtc_voltage_status_flags is not None:
    raise TrustedTimeAnchorError(
      "Unsupported RTC voltage status cannot have flags"
    )
  if type(anchor.sequence) is not int or anchor.sequence < 1:
    raise TrustedTimeAnchorError(
      "Trusted time anchor sequence is invalid"
    )

  return replace(
    anchor,
    trusted_utc=_validated_utc(anchor.trusted_utc),
    boot_id=_validated_boot_id(anchor.boot_id),
    uncertainty_seconds=float(anchor.uncertainty_seconds),
    boottime_seconds=float(anchor.boottime_seconds),
  )


def read_boot_id(path: Path = BOOT_ID_PATH) -> str | None:
  try:
    return _validated_boot_id(
      path.read_text(encoding="utf-8").strip()
    )
  except (
    OSError,
    TrustedTimeAnchorError,
    UnicodeDecodeError,
  ):
    return None


def read_boottime_seconds(
  clock_gettime: Callable[[int], float] = time.clock_gettime,
) -> float | None:
  try:
    value = clock_gettime(time.CLOCK_BOOTTIME)
  except (OSError, OverflowError, TypeError, ValueError):
    return None
  if (
    type(value) not in (int, float)
    or isinstance(value, bool)
    or not isfinite(value)
    or value < 0.0
  ):
    return None
  return float(value)


def read_rtc_epoch_seconds(
  path: Path = RTC_EPOCH_PATH,
) -> int | None:
  try:
    value = int(path.read_text(encoding="utf-8").strip())
  except (OSError, UnicodeDecodeError, ValueError):
    return None
  return value if value >= 0 else None


def read_rtc_voltage_status(
  path: Path = RTC_DEVICE_PATH,
  *,
  ioctl_call: Callable[..., object] = fcntl.ioctl,
) -> RtcVoltageStatus:
  flags = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | getattr(os, "O_NOFOLLOW", 0)
  )
  try:
    descriptor = os.open(path, flags)
  except OSError as exc:
    return RtcVoltageStatus(
      False,
      None,
      f"{type(exc).__name__}:{exc}",
    )

  error: BaseException | None = None
  result: RtcVoltageStatus | None = None
  try:
    buffer = bytearray(_UNSIGNED_INT_BYTES)
    try:
      ioctl_call(
        descriptor,
        RTC_VL_READ,
        buffer,
        True,
      )
    except OSError as exc:
      if exc.errno in (
        errno.EINVAL,
        errno.ENOTTY,
        errno.EOPNOTSUPP,
      ):
        result = RtcVoltageStatus(False, None)
      else:
        result = RtcVoltageStatus(
          False,
          None,
          f"{type(exc).__name__}:{exc}",
        )
    else:
      result = RtcVoltageStatus(
        True,
        int.from_bytes(buffer, "little"),
      )
  except BaseException as exc:
    error = exc

  try:
    os.close(descriptor)
  except BaseException as close_exc:
    if error is None:
      error = close_exc
    else:
      error.add_note(
        f"RTC descriptor close also failed: {type(close_exc).__name__}"
      )

  if error is not None:
    return RtcVoltageStatus(
      False,
      None,
      f"{type(error).__name__}:{error}",
    )
  assert result is not None
  return result


def _reject_duplicate_pairs(
  pairs: list[tuple[str, object]],
) -> dict[str, object]:
  result: dict[str, object] = {}
  for key, value in pairs:
    if key in result:
      raise TrustedTimeAnchorError(
        f"Duplicate trusted time anchor field: {key}"
      )
    result[key] = value
  return result


def _encode_anchor(anchor: TrustedTimeAnchor) -> bytes:
  anchor = validate_trusted_time_anchor(anchor)
  payload = {
    "version": anchor.version,
    "trusted_utc": anchor.trusted_utc.isoformat(),
    "source": anchor.source.value,
    "provenance": anchor.provenance.value,
    "authorized": anchor.authorized,
    "independent": anchor.independent,
    "uncertainty_seconds": anchor.uncertainty_seconds,
    "boot_id": anchor.boot_id,
    "boottime_seconds": anchor.boottime_seconds,
    "rtc_epoch_seconds": anchor.rtc_epoch_seconds,
    "rtc_voltage_status_supported": (
      anchor.rtc_voltage_status_supported
    ),
    "rtc_voltage_status_flags": (
      anchor.rtc_voltage_status_flags
    ),
    "sequence": anchor.sequence,
  }
  try:
    encoded = (
      json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
      ).encode("utf-8")
      + b"\n"
    )
  except (TypeError, ValueError) as exc:
    raise TrustedTimeAnchorError(
      "Trusted time anchor cannot be encoded"
    ) from exc
  if len(encoded) > MAX_TRUSTED_TIME_ANCHOR_FILE_BYTES:
    raise TrustedTimeAnchorError(
      "Trusted time anchor exceeds the size limit"
    )
  return encoded


def _anchor_from_json(raw: object) -> TrustedTimeAnchor:
  if type(raw) is not dict:
    raise TrustedTimeAnchorError(
      "Trusted time anchor root is not an object"
    )
  required = {
    "version",
    "trusted_utc",
    "source",
    "provenance",
    "authorized",
    "independent",
    "uncertainty_seconds",
    "boot_id",
    "boottime_seconds",
    "rtc_epoch_seconds",
    "rtc_voltage_status_supported",
    "rtc_voltage_status_flags",
    "sequence",
  }
  missing = required - raw.keys()
  if missing:
    raise TrustedTimeAnchorError(
      "Trusted time anchor fields are missing: "
      + ",".join(sorted(missing))
    )
  try:
    if type(raw["trusted_utc"]) is not str:
      raise TrustedTimeAnchorError(
        "Trusted time anchor UTC is invalid"
      )
    if type(raw["source"]) is not str:
      raise TrustedTimeAnchorError(
        "Trusted time anchor source is invalid"
      )
    if type(raw["provenance"]) is not str:
      raise TrustedTimeAnchorError(
        "Trusted time anchor provenance is invalid"
      )
    anchor = TrustedTimeAnchor(
      version=raw["version"],
      trusted_utc=datetime.fromisoformat(
        raw["trusted_utc"]
      ),
      source=TrustedTimeSource(raw["source"]),
      provenance=TimeProvenance(raw["provenance"]),
      authorized=raw["authorized"],
      independent=raw["independent"],
      uncertainty_seconds=raw["uncertainty_seconds"],
      boot_id=raw["boot_id"],
      boottime_seconds=raw["boottime_seconds"],
      rtc_epoch_seconds=raw["rtc_epoch_seconds"],
      rtc_voltage_status_supported=(
        raw["rtc_voltage_status_supported"]
      ),
      rtc_voltage_status_flags=(
        raw["rtc_voltage_status_flags"]
      ),
      sequence=raw["sequence"],
    )
  except TrustedTimeAnchorError:
    raise
  except (
    KeyError,
    OverflowError,
    TypeError,
    ValueError,
  ) as exc:
    raise TrustedTimeAnchorError(
      "Trusted time anchor is malformed"
    ) from exc
  return validate_trusted_time_anchor(anchor)


def _require_nofollow() -> int:
  nofollow = getattr(os, "O_NOFOLLOW", None)
  if nofollow is None:
    raise TrustedTimeAnchorError(
      "Secure trusted-time file handling is unavailable"
    )
  return nofollow


def _read_fixed_file(path: Path) -> bytes:
  flags = (
    os.O_RDONLY
    | getattr(os, "O_CLOEXEC", 0)
    | _require_nofollow()
  )
  try:
    descriptor = os.open(path, flags)
  except FileNotFoundError as exc:
    raise TrustedTimeAnchorError(
      "Trusted time anchor does not exist"
    ) from exc
  except (NotImplementedError, OSError) as exc:
    raise TrustedTimeAnchorError(
      "Trusted time anchor cannot be opened safely"
    ) from exc

  contents: bytes | None = None
  operation_error: BaseException | None = None
  try:
    information = os.fstat(descriptor)
    if not stat.S_ISREG(information.st_mode):
      raise TrustedTimeAnchorError(
        "Trusted time anchor is not a regular file"
      )
    if information.st_mode & 0o077:
      raise TrustedTimeAnchorError(
        "Trusted time anchor permissions are too broad"
      )
    if (
      information.st_size <= 0
      or information.st_size
      > MAX_TRUSTED_TIME_ANCHOR_FILE_BYTES
    ):
      raise TrustedTimeAnchorError(
        "Trusted time anchor has an invalid size"
      )
    chunks: list[bytes] = []
    remaining = information.st_size
    while remaining:
      chunk = os.read(
        descriptor,
        min(remaining, 64 * 1024),
      )
      if not chunk:
        raise TrustedTimeAnchorError(
          "Trusted time anchor changed while reading"
        )
      chunks.append(chunk)
      remaining -= len(chunk)
    if os.read(descriptor, 1):
      raise TrustedTimeAnchorError(
        "Trusted time anchor grew while reading"
      )
    contents = b"".join(chunks)
  except BaseException as exc:
    operation_error = exc

  try:
    os.close(descriptor)
  except BaseException as close_exc:
    if operation_error is None:
      raise
    operation_error.add_note(
      f"Trusted time anchor close also failed: {type(close_exc).__name__}"
    )
  if operation_error is not None:
    raise operation_error
  assert contents is not None
  return contents


def load_trusted_time_anchor(path: Path) -> TrustedTimeAnchor:
  try:
    encoded = _read_fixed_file(path)

    def reject_nonfinite(value: str) -> None:
      raise ValueError(
        f"Unsupported JSON constant: {value}"
      )

    raw = json.loads(
      encoded.decode("utf-8"),
      object_pairs_hook=_reject_duplicate_pairs,
      parse_constant=reject_nonfinite,
    )
    return _anchor_from_json(raw)
  except TrustedTimeAnchorError:
    raise
  except (
    json.JSONDecodeError,
    OSError,
    OverflowError,
    RecursionError,
    TypeError,
    UnicodeError,
    ValueError,
  ) as exc:
    raise TrustedTimeAnchorError(
      "Trusted time anchor validation failed safely"
    ) from exc


def _fsync_directory(directory: Path) -> None:
  directory_flag = getattr(os, "O_DIRECTORY", None)
  if directory_flag is None:
    raise TrustedTimeAnchorError(
      "Secure trusted-time directory handling is unavailable"
    )
  descriptor = os.open(
    directory,
    os.O_RDONLY
    | directory_flag
    | getattr(os, "O_CLOEXEC", 0),
  )
  operation_error: BaseException | None = None
  try:
    os.fsync(descriptor)
  except BaseException as exc:
    operation_error = exc
  try:
    os.close(descriptor)
  except BaseException as close_exc:
    if operation_error is None:
      raise
    operation_error.add_note(
      f"Trusted-time directory close also failed: {type(close_exc).__name__}"
    )
  if operation_error is not None:
    raise operation_error


def _assert_safe_known_path(path: Path) -> None:
  try:
    information = path.lstat()
  except FileNotFoundError:
    return
  except OSError as exc:
    raise TrustedTimeAnchorError(
      "Trusted time anchor status is unavailable"
    ) from exc
  if stat.S_ISLNK(information.st_mode):
    raise TrustedTimeAnchorError(
      f"Trusted time path is a symbolic link: {path.name}"
    )
  if not stat.S_ISREG(information.st_mode):
    raise TrustedTimeAnchorError(
      f"Trusted time path is not regular: {path.name}"
    )


class TrustedTimeAnchorStore:
  def __init__(
    self,
    primary_path: Path = TRUSTED_TIME_ANCHOR_PATH,
  ) -> None:
    self.primary_path = primary_path
    self.previous_path = primary_path.with_name(
      f"{primary_path.stem}_previous{primary_path.suffix}"
    )
    self.candidate_path = primary_path.with_name(
      f"{primary_path.stem}_candidate.tmp"
    )

  def _inspect_one(
    self,
    generation: str,
    path: Path,
  ) -> AnchorFileInspection:
    try:
      anchor = load_trusted_time_anchor(path)
    except Exception as exc:
      try:
        path.lstat()
      except FileNotFoundError:
        state = AnchorFileState.ABSENT
      except OSError:
        state = AnchorFileState.INVALID
      else:
        state = AnchorFileState.INVALID
      return AnchorFileInspection(
        generation,
        path,
        state,
        error=f"{type(exc).__name__}:{exc}",
      )
    return AnchorFileInspection(
      generation,
      path,
      AnchorFileState.VALID,
      anchor=anchor,
    )

  def inspect(self) -> TrustedTimeAnchorInventory:
    return TrustedTimeAnchorInventory(
      self._inspect_one(
        "primary",
        self.primary_path,
      ),
      self._inspect_one(
        "previous",
        self.previous_path,
      ),
    )

  @staticmethod
  def select_inventory(
    inventory: TrustedTimeAnchorInventory,
  ) -> TrustedTimeAnchorSelection | None:
    candidates = [
      inspection
      for inspection in (
        inventory.primary,
        inventory.previous,
      )
      if inspection.anchor is not None
    ]
    if not candidates:
      return None
    selected = max(
      candidates,
      key=lambda inspection: (
        inspection.anchor.sequence,
        inspection.generation == "primary",
      ),
    )
    assert selected.anchor is not None
    if len(candidates) == 1:
      reason = f"{selected.generation}_only"
    elif selected.generation == "primary":
      reason = "primary_newest_sequence"
    else:
      reason = "previous_newest_sequence"
    return TrustedTimeAnchorSelection(
      selected.generation,
      selected.anchor,
      reason,
    )

  def load_best(
    self,
  ) -> tuple[
    TrustedTimeAnchorSelection | None,
    TrustedTimeAnchorInventory,
  ]:
    inventory = self.inspect()
    return self.select_inventory(inventory), inventory

  def next_sequence(self) -> int:
    selected, _ = self.load_best()
    return (
      1
      if selected is None
      else selected.anchor.sequence + 1
    )

  def remove_stale_candidate(self) -> bool:
    _assert_safe_known_path(self.candidate_path)
    try:
      self.candidate_path.unlink()
    except FileNotFoundError:
      return False
    _fsync_directory(self.primary_path.parent)
    return True

  def _write_candidate(
    self,
    anchor: TrustedTimeAnchor,
  ) -> TrustedTimeAnchor:
    encoded = _encode_anchor(anchor)
    self.primary_path.parent.mkdir(
      parents=True,
      exist_ok=True,
      mode=0o700,
    )
    _assert_safe_known_path(self.candidate_path)
    try:
      self.candidate_path.unlink()
    except FileNotFoundError:
      pass

    flags = (
      os.O_WRONLY
      | os.O_CREAT
      | os.O_EXCL
      | getattr(os, "O_CLOEXEC", 0)
      | _require_nofollow()
    )
    descriptor = os.open(
      self.candidate_path,
      flags,
      0o600,
    )
    candidate_file = None
    try:
      candidate_file = os.fdopen(descriptor, "wb")
    except BaseException:
      os.close(descriptor)
      raise

    operation_error: BaseException | None = None
    try:
      candidate_file.write(encoded)
      candidate_file.flush()
      os.fsync(candidate_file.fileno())
    except BaseException as exc:
      operation_error = exc
    try:
      candidate_file.close()
    except BaseException as close_exc:
      if operation_error is None:
        raise
      operation_error.add_note(
        f"Trusted time candidate close also failed: {type(close_exc).__name__}"
      )
    if operation_error is not None:
      raise operation_error

    _fsync_directory(self.primary_path.parent)
    return load_trusted_time_anchor(
      self.candidate_path
    )

  def save(
    self,
    anchor: TrustedTimeAnchor,
  ) -> TrustedTimeAnchorSelection:
    anchor = validate_trusted_time_anchor(anchor)
    for path in (
      self.primary_path,
      self.previous_path,
      self.candidate_path,
    ):
      _assert_safe_known_path(path)

    selected, inventory = self.load_best()
    if (
      selected is not None
      and anchor.sequence
      <= selected.anchor.sequence
    ):
      raise TrustedTimeAnchorError(
        "Trusted time anchor sequence did not advance"
      )

    candidate_written = False
    try:
      candidate = self._write_candidate(anchor)
      candidate_written = True
      if candidate != anchor:
        raise TrustedTimeAnchorError(
          "Trusted time candidate readback changed data"
        )

      if (
        inventory.primary.anchor is not None
        and (
          inventory.previous.anchor is None
          or inventory.primary.anchor.sequence
          >= inventory.previous.anchor.sequence
        )
      ):
        os.replace(
          self.primary_path,
          self.previous_path,
        )
        _fsync_directory(self.primary_path.parent)

      os.replace(
        self.candidate_path,
        self.primary_path,
      )
      candidate_written = False
      _fsync_directory(self.primary_path.parent)

      primary = load_trusted_time_anchor(
        self.primary_path
      )
      if primary != anchor:
        raise TrustedTimeAnchorError(
          "Promoted trusted time anchor changed data"
        )
      return TrustedTimeAnchorSelection(
        "primary",
        primary,
        "new_primary_saved",
      )
    finally:
      if candidate_written:
        try:
          self.candidate_path.unlink()
        except FileNotFoundError:
          pass
