# Conversion formulas adapted from Jussi Kivilinna's
# ublox8-gps-qzss-yuma-almanac-converter.
# See LICENSE.ublox8-yuma-almanac-converter for the complete MIT notice.

import struct
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import isfinite, pi

from openpilot.common.gps_time import (
  GPS_EPOCH_UTC,
  resolve_gps_week_mod_1024,
)
from openpilot.system.ubloxd.gps_assistance import add_ubx_checksum, validate_ubx_frame


YUMA_GPS_ALMANAC_MESSAGE_CLASS = 0x13
YUMA_GPS_ALMANAC_MESSAGE_ID = 0x00
YUMA_GPS_ALMANAC_PAYLOAD_LENGTH = 36
MINIMUM_YUMA_GPS_SATELLITES = 24
MAXIMUM_YUMA_GPS_SATELLITES = 32
YUMA_ALMANAC_MAX_REFERENCE_AGE_SECONDS = 14 * 24 * 60 * 60
YUMA_ALMANAC_MAX_REFERENCE_FUTURE_SECONDS = 4 * 24 * 60 * 60

_REQUIRED_FIELDS = frozenset((
  "ID",
  "Health",
  "Eccentricity",
  "Time of Applicability(s)",
  "Orbital Inclination(rad)",
  "Rate of Right Ascen(r/s)",
  "SQRT(A) (m 1/2)",
  "Right Ascen at Week(rad)",
  "Argument of Perigee(rad)",
  "Mean Anom(rad)",
  "Af0(s)",
  "Af1(s/s)",
  "week",
))


class YumaAlmanacError(ValueError):
  pass


@dataclass(frozen=True)
class YumaAlmanac:
  gps_week_mod_1024: int
  time_of_applicability_seconds: int
  satellite_ids: tuple[int, ...]
  frames: tuple[bytes, ...]

  @property
  def ubx_data(self) -> bytes:
    return b"".join(self.frames)


def _normalized_key(value: str) -> str:
  return " ".join(value.split())


def _parse_int(value: str, field: str, satellite_id: int | None = None) -> int:
  try:
    return int(value, 10)
  except ValueError as exc:
    suffix = f" for PRN {satellite_id}" if satellite_id is not None else ""
    raise YumaAlmanacError(f"Invalid integer field {field}{suffix}: {value!r}") from exc


def _parse_float(value: str, field: str, satellite_id: int) -> float:
  try:
    parsed = float(value)
  except ValueError as exc:
    raise YumaAlmanacError(
      f"Invalid floating-point field {field} for PRN {satellite_id}: {value!r}"
    ) from exc

  if not isfinite(parsed):
    raise YumaAlmanacError(
      f"Non-finite field {field} for PRN {satellite_id}: {value!r}"
    )

  return parsed


def _require_range(
  value: int,
  minimum: int,
  maximum: int,
  field: str,
  satellite_id: int,
) -> int:
  if not minimum <= value <= maximum:
    raise YumaAlmanacError(
      f"Field {field} is out of range for PRN {satellite_id}: {value}"
    )
  return value


def _parse_blocks(text: str) -> tuple[dict[str, str], ...]:
  blocks: list[dict[str, str]] = []
  current: dict[str, str] = {}

  for line_number, raw_line in enumerate(text.splitlines(), start=1):
    line = raw_line.strip()
    if not line:
      continue

    if line.startswith("*"):
      if current:
        blocks.append(current)
        current = {}
      continue

    if ":" not in line:
      raise YumaAlmanacError(f"Unexpected YUMA line {line_number}: {raw_line!r}")

    raw_key, raw_value = line.split(":", 1)
    key = _normalized_key(raw_key)
    value = raw_value.strip()

    if key in current:
      raise YumaAlmanacError(
        f"Duplicate YUMA field {key!r} on line {line_number}"
      )

    current[key] = value

  if current:
    blocks.append(current)

  if not blocks:
    raise YumaAlmanacError("YUMA almanac contains no satellite blocks")

  return tuple(blocks)


def _build_gps_almanac_frame(
  fields: dict[str, str],
) -> tuple[int, int, int, bytes | None]:
  missing = sorted(_REQUIRED_FIELDS - fields.keys())
  if missing:
    raise YumaAlmanacError(
      f"YUMA satellite block is missing fields: {', '.join(missing)}"
    )

  satellite_id = _parse_int(fields["ID"], "ID")
  _require_range(satellite_id, 1, 32, "ID", satellite_id)

  health = _parse_int(fields["Health"], "Health", satellite_id)
  _require_range(health, 0, 63, "Health", satellite_id)

  gps_week = _parse_int(fields["week"], "week", satellite_id)
  _require_range(gps_week, 0, 1023, "week", satellite_id)

  time_of_applicability = _parse_float(
    fields["Time of Applicability(s)"],
    "Time of Applicability(s)",
    satellite_id,
  )
  time_of_applicability_seconds = round(time_of_applicability)
  _require_range(
    time_of_applicability_seconds,
    0,
    604800,
    "Time of Applicability(s)",
    satellite_id,
  )

  toa = round(time_of_applicability_seconds / 2**12)
  if abs(time_of_applicability - toa * 2**12) > 0.5:
    raise YumaAlmanacError(
      f"Time of applicability is not aligned to 4096 seconds for PRN {satellite_id}"
    )

  if health != 0:
    return satellite_id, gps_week, time_of_applicability_seconds, None

  eccentricity = round(
    _parse_float(fields["Eccentricity"], "Eccentricity", satellite_id)
    / 2**-21
  )
  inclination = (
    _parse_float(
      fields["Orbital Inclination(rad)"],
      "Orbital Inclination(rad)",
      satellite_id,
    )
    / pi
  )
  delta_inclination = round((inclination - 0.30) / 2**-19)
  right_ascension_rate = round(
    _parse_float(
      fields["Rate of Right Ascen(r/s)"],
      "Rate of Right Ascen(r/s)",
      satellite_id,
    )
    / (2**-38 * pi)
  )
  square_root_a = round(
    _parse_float(
      fields["SQRT(A) (m 1/2)"],
      "SQRT(A) (m 1/2)",
      satellite_id,
    )
    / 2**-11
  )
  right_ascension = round(
    _parse_float(
      fields["Right Ascen at Week(rad)"],
      "Right Ascen at Week(rad)",
      satellite_id,
    )
    / (2**-23 * pi)
  )
  argument_of_perigee = round(
    _parse_float(
      fields["Argument of Perigee(rad)"],
      "Argument of Perigee(rad)",
      satellite_id,
    )
    / (2**-23 * pi)
  )
  mean_anomaly = round(
    _parse_float(fields["Mean Anom(rad)"], "Mean Anom(rad)", satellite_id)
    / (2**-23 * pi)
  )
  af0 = round(
    _parse_float(fields["Af0(s)"], "Af0(s)", satellite_id)
    / 2**-20
  )
  af1 = round(
    _parse_float(fields["Af1(s/s)"], "Af1(s/s)", satellite_id)
    / 2**-38
  )

  payload = struct.pack(
    "<BBBBHBBhhIiiihhI",
    0x02,
    0x00,
    satellite_id,
    health,
    _require_range(eccentricity, 0, 0xFFFF, "Eccentricity", satellite_id),
    gps_week % 256,
    _require_range(toa, 0, 0xFF, "toa", satellite_id),
    _require_range(
      delta_inclination,
      -0x8000,
      0x7FFF,
      "deltaI",
      satellite_id,
    ),
    _require_range(
      right_ascension_rate,
      -0x8000,
      0x7FFF,
      "omegaDot",
      satellite_id,
    ),
    _require_range(square_root_a, 0, 0xFFFFFFFF, "sqrtA", satellite_id),
    _require_range(
      right_ascension,
      -0x80000000,
      0x7FFFFFFF,
      "omega0",
      satellite_id,
    ),
    _require_range(
      argument_of_perigee,
      -0x80000000,
      0x7FFFFFFF,
      "omega",
      satellite_id,
    ),
    _require_range(
      mean_anomaly,
      -0x80000000,
      0x7FFFFFFF,
      "m0",
      satellite_id,
    ),
    _require_range(af0, -0x8000, 0x7FFF, "af0", satellite_id),
    _require_range(af1, -0x8000, 0x7FFF, "af1", satellite_id),
    0,
  )
  header = (
    b"\xB5\x62"
    + bytes((
      YUMA_GPS_ALMANAC_MESSAGE_CLASS,
      YUMA_GPS_ALMANAC_MESSAGE_ID,
    ))
    + len(payload).to_bytes(2, "little")
  )
  return (
    satellite_id,
    gps_week,
    time_of_applicability_seconds,
    add_ubx_checksum(header + payload),
  )


def convert_yuma_almanac(text: str) -> YumaAlmanac:
  frames_by_satellite: dict[int, bytes] = {}
  source_satellite_ids: set[int] = set()
  gps_week: int | None = None
  time_of_applicability_seconds: int | None = None

  for fields in _parse_blocks(text):
    satellite_id, block_week, block_toa, frame = _build_gps_almanac_frame(fields)

    if satellite_id in source_satellite_ids:
      raise YumaAlmanacError(f"Duplicate GPS satellite ID: {satellite_id}")
    source_satellite_ids.add(satellite_id)

    if gps_week is None:
      gps_week = block_week
    elif block_week != gps_week:
      raise YumaAlmanacError(
        f"Mixed GPS weeks in YUMA almanac: {gps_week} and {block_week}"
      )

    if time_of_applicability_seconds is None:
      time_of_applicability_seconds = block_toa
    elif block_toa != time_of_applicability_seconds:
      raise YumaAlmanacError(
        f"Mixed times of applicability in YUMA almanac: {time_of_applicability_seconds} and {block_toa}"
      )

    if frame is not None:
      frames_by_satellite[satellite_id] = frame

  satellite_ids = tuple(sorted(frames_by_satellite))
  if not MINIMUM_YUMA_GPS_SATELLITES <= len(satellite_ids) <= MAXIMUM_YUMA_GPS_SATELLITES:
    raise YumaAlmanacError(
      f"Unexpected number of healthy GPS satellites: {len(satellite_ids)}"
    )

  if gps_week is None or time_of_applicability_seconds is None:
    raise YumaAlmanacError("YUMA almanac contains no GPS satellites")

  frames = tuple(
    frames_by_satellite[satellite_id]
    for satellite_id in satellite_ids
  )
  parsed_frames = split_yuma_ubx_frames(b"".join(frames))
  if parsed_frames != frames:
    raise YumaAlmanacError("Generated YUMA UBX frame validation mismatch")

  return YumaAlmanac(
    gps_week_mod_1024=gps_week,
    time_of_applicability_seconds=time_of_applicability_seconds,
    satellite_ids=satellite_ids,
    frames=frames,
  )


def split_yuma_ubx_frames(data: bytes) -> tuple[bytes, ...]:
  frames: list[bytes] = []
  satellite_ids: set[int] = set()
  offset = 0

  while offset < len(data):
    if data[offset:offset + 2] != b"\xB5\x62":
      raise YumaAlmanacError(f"Invalid UBX sync at offset {offset}")
    if offset + 8 > len(data):
      raise YumaAlmanacError(f"Truncated UBX header at offset {offset}")

    payload_length = int.from_bytes(
      data[offset + 4:offset + 6],
      "little",
    )
    frame_length = payload_length + 8
    frame = data[offset:offset + frame_length]

    if len(frame) != frame_length:
      raise YumaAlmanacError(f"Truncated UBX frame at offset {offset}")
    if not validate_ubx_frame(frame):
      raise YumaAlmanacError(f"Invalid UBX checksum at offset {offset}")
    if (
      frame[2] != YUMA_GPS_ALMANAC_MESSAGE_CLASS
      or frame[3] != YUMA_GPS_ALMANAC_MESSAGE_ID
    ):
      raise YumaAlmanacError(
        f"Unexpected UBX message at offset {offset}: class=0x{frame[2]:02X}, id=0x{frame[3]:02X}"
      )
    if payload_length != YUMA_GPS_ALMANAC_PAYLOAD_LENGTH:
      raise YumaAlmanacError(
        f"Unexpected YUMA payload length at offset {offset}: {payload_length}"
      )
    if frame[6] != 0x02 or frame[7] != 0:
      raise YumaAlmanacError(
        f"Unexpected YUMA payload type/version at offset {offset}"
      )

    satellite_id = frame[8]
    if not 1 <= satellite_id <= 32:
      raise YumaAlmanacError(
        f"Invalid YUMA satellite ID at offset {offset}: {satellite_id}"
      )
    if satellite_id in satellite_ids:
      raise YumaAlmanacError(
        f"Duplicate YUMA satellite ID: {satellite_id}"
      )
    if frame[9] != 0:
      raise YumaAlmanacError(
        f"Stored YUMA frame contains unhealthy PRN {satellite_id}"
      )

    satellite_ids.add(satellite_id)
    frames.append(frame)
    offset += frame_length

  if not frames:
    raise YumaAlmanacError("YUMA UBX data contains no frames")

  return tuple(frames)

def _trusted_utc(value: datetime, field: str) -> datetime:
  if value.tzinfo is None or value.utcoffset() is None:
    raise YumaAlmanacError(
      f"{field} must be timezone-aware"
    )
  return value.astimezone(UTC)


def resolve_yuma_reference_time(
  almanac: YumaAlmanac,
  trusted_now: datetime,
) -> datetime:
  """Resolve the 10-bit YUMA week using shared modulo-week era resolution."""
  normalized_now = _trusted_utc(trusted_now, "trusted_now")
  try:
    absolute_week = resolve_gps_week_mod_1024(
      almanac.gps_week_mod_1024,
      trusted_utc=normalized_now,
    )
  except ValueError as exc:
    raise YumaAlmanacError(str(exc)) from exc
  return GPS_EPOCH_UTC + timedelta(
    weeks=absolute_week,
    seconds=almanac.time_of_applicability_seconds,
  )


def validate_yuma_reference_time(
  almanac: YumaAlmanac,
  trusted_now: datetime,
  *,
  maximum_age_seconds: int = (
    YUMA_ALMANAC_MAX_REFERENCE_AGE_SECONDS
  ),
  maximum_future_seconds: int = (
    YUMA_ALMANAC_MAX_REFERENCE_FUTURE_SECONDS
  ),
) -> datetime:
  """Return the resolved reference time when it is safe to use."""
  for field, value in (
    ("maximum_age_seconds", maximum_age_seconds),
    ("maximum_future_seconds", maximum_future_seconds),
  ):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
      raise YumaAlmanacError(
        f"{field} must be a non-negative integer"
      )

  normalized_now = _trusted_utc(trusted_now, "trusted_now")
  reference_time = resolve_yuma_reference_time(
    almanac,
    normalized_now,
  )
  age_seconds = (
    normalized_now - reference_time
  ).total_seconds()

  if age_seconds > maximum_age_seconds:
    raise YumaAlmanacError(
      f"YUMA almanac reference is too old: {age_seconds:.1f}s"
    )
  if age_seconds < -maximum_future_seconds:
    raise YumaAlmanacError(
      f"YUMA almanac reference is too far in the future: {-age_seconds:.1f}s"
    )

  return reference_time
