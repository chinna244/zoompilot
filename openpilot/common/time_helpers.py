from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path

from openpilot.common.gps_time import representable_gps_utc_maximum

MIN_DATE = datetime.datetime(year=2025, month=2, day=21)
# Exclusive UTC ceiling for host/trusted-time authority.
# Representation-derived from GPS week UInt16 capacity, NOT a product sunset year.
# Replaces the former hard-coded 2035-01-01 bomb.
MAX_DATE = representable_gps_utc_maximum().astimezone(datetime.UTC).replace(tzinfo=None)

TIME_SYNC_MARKER = Path("/dev/shm/openpilot/time_synced")
NTP_SYNC_MARKER = Path("/run/systemd/timesync/synchronized")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
HOST_TIME_RECORD_VERSION = 1
HOST_TIME_UNCERTAINTY_SECONDS = 30.0


class HostTimeSource(StrEnum):
  NETWORK_SYNCHRONIZED = "network_synchronized"
  RECEIVER_DERIVED = "receiver_derived"
  UNKNOWN = "unknown"


@dataclass(frozen=True)
class HostTimeObservation:
  utc: datetime.datetime
  observed_boottime_seconds: float
  uncertainty_seconds: float
  source: HostTimeSource
  independent: bool
  generation: str

  def __post_init__(self) -> None:
    if not isinstance(self.utc, datetime.datetime):
      raise ValueError("Host UTC is invalid")
    if self.utc.tzinfo is None or self.utc.utcoffset() is None:
      raise ValueError("Host UTC must be timezone-aware")
    normalized = self.utc.astimezone(datetime.UTC)
    if not (
      MIN_DATE.replace(tzinfo=datetime.UTC)
      < normalized
      < MAX_DATE.replace(tzinfo=datetime.UTC)
    ):
      raise ValueError("Host UTC is outside the supported range")
    if (
      type(self.observed_boottime_seconds) not in (int, float)
      or isinstance(self.observed_boottime_seconds, bool)
      or not isfinite(self.observed_boottime_seconds)
      or self.observed_boottime_seconds < 0.0
    ):
      raise ValueError("Host boottime is invalid")
    if (
      type(self.uncertainty_seconds) not in (int, float)
      or isinstance(self.uncertainty_seconds, bool)
      or not isfinite(self.uncertainty_seconds)
      or self.uncertainty_seconds < 0.0
    ):
      raise ValueError("Host uncertainty is invalid")
    if not isinstance(self.source, HostTimeSource):
      raise ValueError("Host time source is invalid")
    if type(self.independent) is not bool:
      raise ValueError("Host independence is invalid")
    if (
      self.independent
      != (self.source is HostTimeSource.NETWORK_SYNCHRONIZED)
    ):
      raise ValueError("Host source and independence do not match")
    if type(self.generation) is not str or not self.generation:
      raise ValueError("Host generation is invalid")

    object.__setattr__(self, "utc", normalized)
    object.__setattr__(
      self,
      "observed_boottime_seconds",
      float(self.observed_boottime_seconds),
    )
    object.__setattr__(
      self,
      "uncertainty_seconds",
      float(self.uncertainty_seconds),
    )


def min_date():
  # on systemd systems, the default time is the systemd build time
  systemd_path = Path("/lib/systemd/systemd")
  if systemd_path.exists():
    d = datetime.datetime.fromtimestamp(systemd_path.stat().st_mtime)
    return max(MIN_DATE, d + datetime.timedelta(days=1))
  return MIN_DATE


def system_time_valid():
  return min_date() < datetime.datetime.now() < MAX_DATE


def set_system_time(new_time: datetime.datetime) -> bool:
  """Set the system clock in UTC.

  Returns True when the command succeeded or no adjustment was needed.
  """
  if new_time.tzinfo is None:
    new_time_utc = new_time.replace(tzinfo=datetime.UTC)
  else:
    new_time_utc = new_time.astimezone(datetime.UTC)

  now_utc = datetime.datetime.now(datetime.UTC)
  if abs(now_utc - new_time_utc) < datetime.timedelta(seconds=10):
    return True

  subprocess.run(
    [
      "date",
      "-u",
      "-s",
      new_time_utc.strftime("%Y-%m-%d %H:%M:%S"),
    ],
    check=True,
    capture_output=True,
    text=True,
  )
  return True


def _read_boottime_seconds() -> float | None:
  try:
    value = time.clock_gettime(time.CLOCK_BOOTTIME)
  except (AttributeError, OSError, ValueError):
    return None
  if not isfinite(value) or value < 0.0:
    return None
  return value


def _read_boot_id() -> str | None:
  try:
    value = BOOT_ID_PATH.read_text(encoding="utf-8").strip()
  except OSError:
    return None
  return value or None


def _normalize_marker_source(source: HostTimeSource | str) -> HostTimeSource:
  if isinstance(source, HostTimeSource):
    return source
  if type(source) is not str:
    return HostTimeSource.UNKNOWN
  normalized = source.strip().casefold().replace("-", "_")
  if normalized in {
    "gps",
    "gnss",
    "ublox",
    "qcom",
    "receiver",
    "receiver_derived",
  }:
    return HostTimeSource.RECEIVER_DERIVED
  if normalized in {
    "network",
    "ntp",
    "network_synchronized",
  }:
    return HostTimeSource.NETWORK_SYNCHRONIZED
  return HostTimeSource.UNKNOWN


def _atomic_write(path: Path, data: bytes) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  temporary = path.with_name(
    f".{path.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
  )
  descriptor = os.open(
    temporary,
    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
    0o600,
  )
  try:
    with os.fdopen(descriptor, "wb") as output:
      output.write(data)
      output.flush()
      os.fsync(output.fileno())
    os.replace(temporary, path)
  finally:
    try:
      temporary.unlink()
    except FileNotFoundError:
      pass


def mark_time_synced(source: HostTimeSource | str) -> bool:
  """Record the explicit source that set the host clock this boot."""
  normalized = _normalize_marker_source(source)
  if (
    TIME_SYNC_MARKER.exists()
    and _receiver_marker_source() is normalized
  ):
    return True

  boottime = _read_boottime_seconds()
  payload = {
    "version": HOST_TIME_RECORD_VERSION,
    "source": normalized.value,
    "written_boottime_seconds": boottime,
    "boot_id": _read_boot_id(),
  }
  encoded = (
    json.dumps(payload, sort_keys=True, separators=(",", ":"))
    + "\n"
  ).encode("utf-8")
  try:
    _atomic_write(TIME_SYNC_MARKER, encoded)
  except OSError:
    return False
  return True


def _marker_generation(
  path: Path,
  source: HostTimeSource,
) -> str | None:
  try:
    information = path.stat()
  except OSError:
    return None
  return ":".join((
    source.value,
    str(information.st_dev),
    str(information.st_ino),
    str(information.st_mtime_ns),
    str(information.st_size),
  ))


def _receiver_marker_source() -> HostTimeSource:
  try:
    raw = TIME_SYNC_MARKER.read_text(encoding="utf-8")
  except OSError:
    return HostTimeSource.UNKNOWN

  try:
    parsed = json.loads(raw)
  except json.JSONDecodeError:
    return _normalize_marker_source(raw)

  if (
    not isinstance(parsed, dict)
    or parsed.get("version") != HOST_TIME_RECORD_VERSION
  ):
    return HostTimeSource.UNKNOWN
  return _normalize_marker_source(parsed.get("source", "unknown"))


def read_host_time_observation() -> HostTimeObservation | None:
  """Return a typed host-clock observation with explicit provenance."""
  if not system_time_valid():
    return None

  if NTP_SYNC_MARKER.exists():
    source = HostTimeSource.NETWORK_SYNCHRONIZED
    marker = NTP_SYNC_MARKER
    independent = True
  elif TIME_SYNC_MARKER.exists():
    source = _receiver_marker_source()
    marker = TIME_SYNC_MARKER
    # Only systemd's synchronization marker can prove network independence.
    independent = False
    if source is HostTimeSource.NETWORK_SYNCHRONIZED:
      source = HostTimeSource.UNKNOWN
  else:
    return None

  boottime = _read_boottime_seconds()
  generation = _marker_generation(marker, source)
  if boottime is None or generation is None:
    return None

  try:
    return HostTimeObservation(
      utc=datetime.datetime.now(datetime.UTC),
      observed_boottime_seconds=boottime,
      uncertainty_seconds=HOST_TIME_UNCERTAINTY_SECONDS,
      source=source,
      independent=independent,
      generation=generation,
    )
  except ValueError:
    return None


def trusted_time_synced() -> bool:
  """Return whether this boot has GPS-derived or network-synchronized time."""
  return read_host_time_observation() is not None
