import base64
import hashlib
import json
import os
import stat
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpilot.system.ubloxd.yuma_almanac import (
  MAXIMUM_YUMA_GPS_SATELLITES,
  MINIMUM_YUMA_GPS_SATELLITES,
  YumaAlmanac,
  YumaAlmanacError,
  split_yuma_ubx_frames,
)


YUMA_ALMANAC_CACHE_VERSION = 1
YUMA_ALMANAC_CACHE_PATH = Path(
  "/data/gps_assistance/public_yuma_almanac.json"
)
YUMA_ALMANAC_MAX_FILE_BYTES = 64 * 1024


class YumaAlmanacStoreError(YumaAlmanacError):
  pass


@dataclass(frozen=True)
class StoredYumaAlmanac:
  downloaded_at_utc: datetime
  almanac: YumaAlmanac

def _aware_utc(value: datetime, field: str) -> datetime:
  if value.tzinfo is None or value.utcoffset() is None:
    raise YumaAlmanacStoreError(
      f"{field} must be timezone-aware"
    )
  return value.astimezone(UTC)


def _parse_utc_datetime(value: Any, field: str) -> datetime:
  if not isinstance(value, str):
    raise YumaAlmanacStoreError(
      f"{field} must be an ISO-8601 string"
    )

  try:
    parsed = datetime.fromisoformat(value)
  except ValueError as exc:
    raise YumaAlmanacStoreError(
      f"{field} is not a valid ISO-8601 timestamp"
    ) from exc

  return _aware_utc(parsed, field)


def _require_int(
  value: Any,
  field: str,
  minimum: int,
  maximum: int,
) -> int:
  if isinstance(value, bool) or not isinstance(value, int):
    raise YumaAlmanacStoreError(
      f"{field} must be an integer"
    )
  if not minimum <= value <= maximum:
    raise YumaAlmanacStoreError(
      f"{field} is out of range: {value}"
    )
  return value


def _require_satellite_ids(value: Any) -> tuple[int, ...]:
  if not isinstance(value, list):
    raise YumaAlmanacStoreError(
      "satellite_ids must be a list"
    )

  satellite_ids = tuple(
    _require_int(
      satellite_id,
      "satellite_id",
      1,
      32,
    )
    for satellite_id in value
  )
  if satellite_ids != tuple(sorted(set(satellite_ids))):
    raise YumaAlmanacStoreError(
      "satellite_ids must be unique and sorted"
    )
  if not (
    MINIMUM_YUMA_GPS_SATELLITES
    <= len(satellite_ids)
    <= MAXIMUM_YUMA_GPS_SATELLITES
  ):
    raise YumaAlmanacStoreError(
      f"Unexpected number of GPS satellites: {len(satellite_ids)}"
    )

  return satellite_ids


def _serialize(
  almanac: YumaAlmanac,
  downloaded_at_utc: datetime,
) -> bytes:
  normalized_downloaded_at = _aware_utc(
    downloaded_at_utc,
    "downloaded_at_utc",
  )
  ubx_data = almanac.ubx_data
  frames = split_yuma_ubx_frames(ubx_data)

  if frames != almanac.frames:
    raise YumaAlmanacStoreError(
      "YUMA almanac frames failed round-trip validation"
    )

  payload = {
    "version": YUMA_ALMANAC_CACHE_VERSION,
    "downloaded_at_utc": normalized_downloaded_at.isoformat(),
    "gps_week_mod_1024": almanac.gps_week_mod_1024,
    "time_of_applicability_seconds": (
      almanac.time_of_applicability_seconds
    ),
    "satellite_ids": list(almanac.satellite_ids),
    "ubx_sha256": hashlib.sha256(ubx_data).hexdigest(),
    "ubx_data_base64": base64.b64encode(ubx_data).decode("ascii"),
  }
  encoded = (
    json.dumps(
      payload,
      sort_keys=True,
      separators=(",", ":"),
    )
    + "\n"
  ).encode("utf-8")

  if len(encoded) > YUMA_ALMANAC_MAX_FILE_BYTES:
    raise YumaAlmanacStoreError(
      f"Serialized YUMA almanac is too large: {len(encoded)} bytes"
    )

  return encoded


def save_yuma_almanac(
  path: Path,
  almanac: YumaAlmanac,
  *,
  downloaded_at_utc: datetime,
) -> None:
  encoded = _serialize(almanac, downloaded_at_utc)
  path.parent.mkdir(parents=True, exist_ok=True)

  fd, temporary_name = tempfile.mkstemp(
    dir=path.parent,
    prefix=f".{path.name}.",
  )
  temporary_path = Path(temporary_name)

  try:
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as output:
      fd = -1
      output.write(encoded)
      output.flush()
      os.fsync(output.fileno())

    os.replace(temporary_path, path)

    directory_fd = os.open(
      path.parent,
      os.O_RDONLY | os.O_DIRECTORY,
    )
    try:
      os.fsync(directory_fd)
    finally:
      os.close(directory_fd)

  finally:
    if fd >= 0:
      os.close(fd)
    try:
      temporary_path.unlink()
    except FileNotFoundError:
      pass


def _read_regular_file(path: Path) -> bytes:
  flags = os.O_RDONLY
  if hasattr(os, "O_NOFOLLOW"):
    flags |= os.O_NOFOLLOW

  try:
    fd = os.open(path, flags)
  except OSError as exc:
    raise YumaAlmanacStoreError(
      f"Unable to open YUMA almanac cache: {exc}"
    ) from exc

  try:
    information = os.fstat(fd)
    if not stat.S_ISREG(information.st_mode):
      raise YumaAlmanacStoreError(
        "YUMA almanac cache is not a regular file"
      )
    if information.st_size > YUMA_ALMANAC_MAX_FILE_BYTES:
      raise YumaAlmanacStoreError(
        f"YUMA almanac cache is too large: {information.st_size} bytes"
      )

    chunks: list[bytes] = []
    remaining = YUMA_ALMANAC_MAX_FILE_BYTES + 1
    while remaining > 0:
      chunk = os.read(fd, min(remaining, 8192))
      if not chunk:
        break
      chunks.append(chunk)
      remaining -= len(chunk)

    data = b"".join(chunks)
    if len(data) > YUMA_ALMANAC_MAX_FILE_BYTES:
      raise YumaAlmanacStoreError(
        f"YUMA almanac cache is too large: {len(data)} bytes"
      )
    return data

  finally:
    os.close(fd)


def _decode_payload(data: bytes) -> dict[str, Any]:
  try:
    decoded = data.decode("utf-8")
  except UnicodeDecodeError as exc:
    raise YumaAlmanacStoreError(
      "YUMA almanac cache is not UTF-8"
    ) from exc

  try:
    payload = json.loads(decoded)
  except (json.JSONDecodeError, RecursionError) as exc:
    raise YumaAlmanacStoreError(
      "YUMA almanac cache is not valid JSON"
    ) from exc

  if not isinstance(payload, dict):
    raise YumaAlmanacStoreError(
      "YUMA almanac cache must contain a JSON object"
    )
  return payload


def load_yuma_almanac(
  path: Path,
) -> StoredYumaAlmanac:
  payload = _decode_payload(_read_regular_file(path))

  version = _require_int(
    payload.get("version"),
    "version",
    YUMA_ALMANAC_CACHE_VERSION,
    YUMA_ALMANAC_CACHE_VERSION,
  )
  if version != YUMA_ALMANAC_CACHE_VERSION:
    raise YumaAlmanacStoreError(
      f"Unsupported YUMA almanac cache version: {version}"
    )

  downloaded_at_utc = _parse_utc_datetime(
    payload.get("downloaded_at_utc"),
    "downloaded_at_utc",
  )
  gps_week = _require_int(
    payload.get("gps_week_mod_1024"),
    "gps_week_mod_1024",
    0,
    1023,
  )
  time_of_applicability_seconds = _require_int(
    payload.get("time_of_applicability_seconds"),
    "time_of_applicability_seconds",
    0,
    604800,
  )
  satellite_ids = _require_satellite_ids(
    payload.get("satellite_ids")
  )

  encoded_ubx = payload.get("ubx_data_base64")
  if not isinstance(encoded_ubx, str):
    raise YumaAlmanacStoreError(
      "ubx_data_base64 must be a string"
    )

  try:
    ubx_data = base64.b64decode(
      encoded_ubx,
      validate=True,
    )
  except (ValueError, TypeError) as exc:
    raise YumaAlmanacStoreError(
      "ubx_data_base64 is invalid"
    ) from exc

  expected_hash = payload.get("ubx_sha256")
  if not isinstance(expected_hash, str) or len(expected_hash) != 64:
    raise YumaAlmanacStoreError(
      "ubx_sha256 must be a 64-character string"
    )
  actual_hash = hashlib.sha256(ubx_data).hexdigest()
  if actual_hash != expected_hash:
    raise YumaAlmanacStoreError(
      "YUMA almanac UBX hash mismatch"
    )

  try:
    frames = split_yuma_ubx_frames(ubx_data)
  except YumaAlmanacError as exc:
    raise YumaAlmanacStoreError(str(exc)) from exc

  frame_satellite_ids = tuple(frame[8] for frame in frames)
  if frame_satellite_ids != satellite_ids:
    raise YumaAlmanacStoreError(
      "YUMA almanac satellite metadata mismatch"
    )

  frame_week_mod_256 = {frame[12] for frame in frames}
  if frame_week_mod_256 != {gps_week % 256}:
    raise YumaAlmanacStoreError(
      "YUMA almanac GPS week metadata mismatch"
    )

  frame_toa_seconds = {
    frame[13] * 2**12
    for frame in frames
  }
  if frame_toa_seconds != {time_of_applicability_seconds}:
    raise YumaAlmanacStoreError(
      "YUMA almanac time-of-applicability metadata mismatch"
    )

  almanac = YumaAlmanac(
    gps_week_mod_1024=gps_week,
    time_of_applicability_seconds=time_of_applicability_seconds,
    satellite_ids=satellite_ids,
    frames=frames,
  )
  stored = StoredYumaAlmanac(
    downloaded_at_utc=downloaded_at_utc,
    almanac=almanac,
  )

  return stored
