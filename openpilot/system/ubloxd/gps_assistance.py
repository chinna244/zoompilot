import base64
import binascii
import hashlib
import json
import os
import stat
import struct
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum, StrEnum, auto
from math import ceil, isfinite
from pathlib import Path
from typing import cast

from openpilot.common.gps_time import ublox_nav_pvt_has_fix
from openpilot.common.time_helpers import MAX_DATE, MIN_DATE


UBX_SYNC = b"\xB5\x62"

UBX_CLASS_NAV = 0x01
UBX_ID_NAV_PVT = 0x07
UBX_ID_NAV_SAT = 0x35
UBX_ID_NAV_AOPSTATUS = 0x60
UBX_CLASS_CFG = 0x06
UBX_ID_CFG_MSG = 0x01
UBX_ID_CFG_RATE = 0x08
UBX_ID_CFG_RXM = 0x11
UBX_ID_CFG_ODO = 0x1E
UBX_ID_CFG_NAVX5 = 0x23
UBX_ID_CFG_NAV5 = 0x24
UBX_ID_CFG_ITFM = 0x39
UBX_ID_CFG_PM2 = 0x3B
UBX_ID_CFG_GNSS = 0x3E
NAVX5_MASK1_ACK_AIDING = 0x0400
NAVX5_MASK1_AOP = 0x4000
UBX_CLASS_MON = 0x0A
UBX_ID_MON_VER = 0x04
UBX_CLASS_UPD = 0x09
UBX_ID_UPD_SOS = 0x14

UBX_CLASS_MGA = 0x13
UBX_ID_MGA_INI = 0x40
UBX_ID_MGA_ACK = 0x60
UBX_ID_MGA_DBD = 0x80

CACHE_VERSION = 1
MAX_DATABASE_BYTES = 64 * 1024
MAX_CACHE_FILE_BYTES = 128 * 1024
MAX_DATABASE_FRAMES = 4096
DEFAULT_MAX_CACHE_AGE_SECONDS = 7 * 24 * 60 * 60

QUALITY_VERSION = 1
QUALITY_POLICY_VERSION = 1
MINIMUM_RELIABLE_FIX_SECONDS = 60.0
MINIMUM_GPS_EPHEMERIS = 4
MINIMUM_GLONASS_EPHEMERIS = 5
MINIMUM_TOTAL_EPHEMERIS = 10
MINIMUM_SATELLITES_USED = 8
MINIMUM_ORBIT_QUALITY_SECONDS = 10.0
MINIMUM_USABLE_RELIABLE_FIX_SECONDS = 20.0
MAXIMUM_NAV_SAT_AGE_SECONDS = 2.0
MAXIMUM_NAV_PVT_GAP_SECONDS = 2.0
MAXIMUM_RECEIVER_UTC_NANOSECONDS = 1_000_000_000
GPS_EPHEMERIS_FRESHNESS_SECONDS = 4 * 60 * 60
GLONASS_EPHEMERIS_FRESHNESS_SECONDS = 60 * 60
CACHE_TIER_FRESHNESS_WINDOW_SECONDS = GPS_EPHEMERIS_FRESHNESS_SECONDS

GPS_ASSISTANCE_CACHE_PATH = Path(
  "/data/gps_assistance/navigation_cache.json"
)
RTC_COUNTER_PATH = Path(
  "/sys/class/rtc/rtc0/since_epoch"
)

MAX_RTC_ASSISTANCE_ELAPSED_SECONDS = (
  DEFAULT_MAX_CACHE_AGE_SECONDS
)
RTC_BASE_TIME_UNCERTAINTY_SECONDS = 60
RTC_DRIFT_PARTS_PER_MILLION = 100

# A deliberately broad radius prevents a recent cached point from being treated
# as exact. This is a restore floor (larger = safer), not an age model.
MIN_RESTORE_POSITION_ACCURACY_CM = 5_000_000  # 50 km
# Policy/usefulness ceiling for assistance we are willing to encode. Not a
# u-blox protocol maximum (posAcc is U4 cm). Required uncertainty above this
# must SKIP assistance rather than clamp downward.
MAX_RESTORE_POSITION_ACCURACY_CM = 50_000_000  # 500 km policy limit
# Without an authoritative mobility model, only genuinely fresh verified-age
# positions may be sent. At highway speeds (~40 m/s), 50 km exceeds ~21 minutes
# of travel; use a shorter window so the 50 km floor remains conservative.
FRESH_POSITION_ASSISTANCE_MAX_AGE_SECONDS = 15 * 60

# Durable assistance fingerprint schema. Legacy persisted caches used
# "{serial}|ublox-m8-prot20.30" without consulting live MON-VER and must fail
# closed for receiver-specific restore.
RECEIVER_FINGERPRINT_SCHEMA_VERSION = 1
LEGACY_RECEIVER_FINGERPRINT_SUFFIX = "ublox-m8-prot20.30"


class CacheValidationError(ValueError):
  pass


def _validated_utc(value: datetime, description: str) -> datetime:
  if not isinstance(value, datetime):
    raise CacheValidationError(f"{description} is not a datetime")
  if value.tzinfo is None:
    raise CacheValidationError(f"{description} must be timezone-aware")
  try:
    if value.utcoffset() is None:
      raise CacheValidationError(f"{description} has no UTC offset")
    return value.astimezone(UTC)
  except CacheValidationError:
    raise
  except Exception as exc:
    raise CacheValidationError(f"{description} timezone is invalid") from exc


def navigation_quality_strictly_better(
  candidate: "NavigationQuality",
  existing: "NavigationQuality",
) -> bool:
  # Eligibility is checked before ordering. Replacement is deliberately
  # conservative: every reviewed navigation-data field must be no worse and
  # at least one must be better. Time alone never weakens this ordering.
  existing_counts = (
    existing.gps_ephemeris_available,
    existing.glonass_ephemeris_available,
    existing.total_ephemeris_available,
    existing.satellites_used,
    existing.gps_almanac_available,
    existing.glonass_almanac_available,
    existing.assistnow_offline_available,
  )
  candidate_counts = (
    candidate.gps_ephemeris_available,
    candidate.glonass_ephemeris_available,
    candidate.total_ephemeris_available,
    candidate.satellites_used,
    candidate.gps_almanac_available,
    candidate.glonass_almanac_available,
    candidate.assistnow_offline_available,
  )
  if not all(new >= old for new, old in zip(candidate_counts, existing_counts, strict=True)):
    return False
  return any(new > old for new, old in zip(candidate_counts, existing_counts, strict=True))


class CacheQualityTier(StrEnum):
  LEGACY = "legacy"
  USABLE = "usable"
  QUALIFIED = "qualified"


class CacheAgeEvidence(StrEnum):
  TRUSTED_UTC = "trusted_utc"
  RTC_ESTIMATE = "rtc_estimate"
  UNVERIFIED = "unverified"

  @property
  def verified(self) -> bool:
    return self is not CacheAgeEvidence.UNVERIFIED


def navigation_quality_tier(
  quality: "NavigationQuality | None",
) -> CacheQualityTier | None:
  if quality is None:
    return CacheQualityTier.LEGACY
  if not quality.usable_for_capture:
    return None
  return (
    CacheQualityTier.QUALIFIED
    if quality.passes_policy
    else CacheQualityTier.USABLE
  )


def compare_cache_quality(
  existing: "GpsAssistanceCache",
  candidate: "GpsAssistanceCache",
  trusted_now: datetime | None,
) -> tuple[bool, str]:
  candidate_quality = candidate.quality
  candidate_tier = navigation_quality_tier(candidate_quality)
  if candidate_tier not in (CacheQualityTier.USABLE, CacheQualityTier.QUALIFIED):
    return False, "candidate_not_usable"
  if existing.receiver_fingerprint != candidate.receiver_fingerprint:
    return True, "different_receiver"
  existing_tier = navigation_quality_tier(existing.quality)
  if existing_tier is None:
    return True, "existing_quality_not_usable"

  if trusted_now is not None:
    age = (
      _validated_utc(trusted_now, "Cache comparison time")
      - existing.saved_at_utc
    ).total_seconds()
    if age > DEFAULT_MAX_CACHE_AGE_SECONDS:
      return True, "existing_cache_expired"

  if existing_tier is CacheQualityTier.LEGACY:
    return True, "candidate_has_current_quality_metadata"

  assert existing.quality is not None

  candidate_startup_ready = candidate_quality.gps_startup_ready
  existing_startup_ready = existing.quality.gps_startup_ready
  same_usable_tier = (
    candidate_tier is CacheQualityTier.USABLE
    and navigation_quality_tier(existing.quality)
    is CacheQualityTier.USABLE
  )

  freshness_delta = (
    candidate.saved_at_utc - existing.saved_at_utc
  ).total_seconds()
  if (
    freshness_delta
    >= CACHE_TIER_FRESHNESS_WINDOW_SECONDS
  ):
    return True, "candidate_materially_fresher"
  if (
    freshness_delta
    <= -CACHE_TIER_FRESHNESS_WINDOW_SECONDS
  ):
    return False, "existing_materially_fresher"

  if (
    same_usable_tier
    and candidate_startup_ready
    and not existing_startup_ready
  ):
    return True, "candidate_gps_startup_ready"

  if (
    same_usable_tier
    and existing_startup_ready
    and not candidate_startup_ready
  ):
    return False, "existing_gps_startup_ready"

  if candidate_tier is CacheQualityTier.USABLE:
    if candidate.saved_at_utc > existing.saved_at_utc:
      return True, "fresh_usable_candidate_preserves_existing_fallback"
    if (
      existing_tier is CacheQualityTier.USABLE
      and navigation_quality_strictly_better(candidate_quality, existing.quality)
    ):
      return True, "usable_candidate_quality_better"
    return False, "existing_cache_preserved"

  if existing_tier is CacheQualityTier.USABLE:
    return True, "candidate_quality_tier_upgrade"

  if navigation_quality_strictly_better(candidate_quality, existing.quality):
    existing_counts = (
      existing.quality.gps_ephemeris_available,
      existing.quality.glonass_ephemeris_available,
      existing.quality.total_ephemeris_available,
    )
    candidate_counts = (
      candidate_quality.gps_ephemeris_available,
      candidate_quality.glonass_ephemeris_available,
      candidate_quality.total_ephemeris_available,
    )
    return (
      (True, "equal_orbits_more_navigation_data")
      if candidate_counts == existing_counts
      else (True, "candidate_orbit_quality_better")
    )
  return False, "existing_cache_preserved"


class RtcEstimateRejectionReason(Enum):
  MISSING_CACHED_RTC_ANCHOR = auto()
  CURRENT_RTC_UNAVAILABLE = auto()
  RTC_ROLLBACK = auto()
  ELAPSED_TIME_ABOVE_MAXIMUM = auto()
  UTC_BEFORE_SUPPORTED_MINIMUM = auto()
  UTC_AFTER_SUPPORTED_MAXIMUM = auto()
  INVALID_RTC_ESTIMATE = auto()


@dataclass(frozen=True)
class RtcEstimateSuccess:
  estimated_utc: datetime
  uncertainty_seconds: int
  elapsed_seconds: int


@dataclass(frozen=True)
class RtcEstimateRejection:
  reason: RtcEstimateRejectionReason
  elapsed_seconds: int | None = None


RtcEstimateResult = RtcEstimateSuccess | RtcEstimateRejection


class _CurrentRtcNotSupplied:
  pass


_CURRENT_RTC_NOT_SUPPLIED = _CurrentRtcNotSupplied()


@dataclass(frozen=True)
class MgaAck:
  accepted: bool
  acknowledgment_type: int
  version: int
  info_code: int
  message_id: int
  message_payload_start: bytes


@dataclass(frozen=True)
class MonVerInfo:
  software_version: str
  hardware_version: str
  extensions: tuple[str, ...]

  @property
  def protocol_versions(self) -> tuple[str, ...]:
    return tuple(value for value in self.extensions if value.upper().startswith("PROTVER="))

  @property
  def firmware_versions(self) -> tuple[str, ...]:
    return tuple(value for value in self.extensions if value.upper().startswith("FWVER="))

  @property
  def module_identifiers(self) -> tuple[str, ...]:
    return tuple(value for value in self.extensions if value.upper().startswith("MOD="))

  @property
  def supported_gnss(self) -> tuple[str, ...]:
    known = {"GPS", "GLO", "GAL", "BDS", "SBAS", "QZSS", "IMES"}
    systems: list[str] = []
    for extension in self.extensions:
      for token in extension.split(";"):
        normalized = token.strip().upper()
        if normalized in known and normalized not in systems:
          systems.append(normalized)
    return tuple(systems)


@dataclass(frozen=True)
class Navx5Config:
  payload: bytes
  version: int
  ack_aiding: bool
  use_aop: bool
  aop_orbit_max_error_m: int


@dataclass(frozen=True)
class NavAopStatus:
  enabled: bool
  status: int

  @property
  def idle(self) -> bool:
    return self.status == 0


@dataclass(frozen=True)
class UpdSosResponse:
  command: int
  response: int


def _decode_ubx_text(field: bytes) -> str:
  return field.split(b"\x00", 1)[0].decode("ascii", errors="replace").strip()


def parse_mon_ver(frame: bytes) -> MonVerInfo | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_MON
    or frame[3] != UBX_ID_MON_VER
  ):
    return None
  payload = frame[6:-2]
  if len(payload) < 40 or (len(payload) - 40) % 30 != 0:
    return None
  return MonVerInfo(
    software_version=_decode_ubx_text(payload[:30]),
    hardware_version=_decode_ubx_text(payload[30:40]),
    extensions=tuple(
      _decode_ubx_text(payload[offset:offset + 30])
      for offset in range(40, len(payload), 30)
    ),
  )


def normalized_receiver_identity(info: MonVerInfo) -> str:
  def normalize(value: str) -> str:
    return " ".join(value.split()).casefold()

  extensions = sorted(normalize(value) for value in info.extensions)
  return "|".join((
    f"sw={normalize(info.software_version)}",
    f"hw={normalize(info.hardware_version)}",
    f"ext={';'.join(extensions)}",
  ))


def _normalize_fingerprint_token(value: str) -> str:
  return " ".join(value.split()).casefold()


def mon_ver_protocol_version(info: MonVerInfo) -> str | None:
  for extension in info.protocol_versions:
    if "=" not in extension:
      continue
    _, _, value = extension.partition("=")
    token = _normalize_fingerprint_token(value)
    if token:
      return token
  return None


def mon_ver_firmware_versions(info: MonVerInfo) -> tuple[str, ...]:
  tokens: list[str] = []
  for extension in info.firmware_versions:
    if "=" not in extension:
      continue
    _, _, value = extension.partition("=")
    token = _normalize_fingerprint_token(value)
    if token and token not in tokens:
      tokens.append(token)
  return tuple(sorted(tokens))


def build_durable_receiver_fingerprint(
  hardware_serial: str,
  info: MonVerInfo | None,
) -> str:
  """Build a versioned receiver fingerprint from serial + MON-VER.

  Incomplete/missing MON-VER yields a sentinel string that is never treated as
  a compatible receiver identity by receiver_fingerprints_compatible().
  """
  serial = _normalize_fingerprint_token(hardware_serial)
  if not serial:
    return f"v{RECEIVER_FINGERPRINT_SCHEMA_VERSION}|unknown_serial|mon_ver_unavailable"
  if info is None:
    return f"v{RECEIVER_FINGERPRINT_SCHEMA_VERSION}|{serial}|mon_ver_unavailable"

  software = _normalize_fingerprint_token(info.software_version)
  hardware = _normalize_fingerprint_token(info.hardware_version)
  protocol = mon_ver_protocol_version(info)
  firmwares = mon_ver_firmware_versions(info)
  if not software or not hardware or protocol is None or not firmwares:
    return f"v{RECEIVER_FINGERPRINT_SCHEMA_VERSION}|{serial}|mon_ver_incomplete"
  firmware = ";".join(firmwares)
  return (
    f"v{RECEIVER_FINGERPRINT_SCHEMA_VERSION}|{serial}|sw={software}|hw={hardware}|prot={protocol}|fw={firmware}"
  )


def parse_legacy_receiver_fingerprint(value: str) -> tuple[str, str] | None:
  parts = value.strip().split("|", 1)
  if len(parts) != 2:
    return None
  serial, suffix = parts
  serial = serial.strip()
  if not serial or suffix.strip() != LEGACY_RECEIVER_FINGERPRINT_SUFFIX:
    return None
  return serial, suffix.strip()


def parse_durable_receiver_fingerprint(
  value: str,
) -> dict[str, str] | None:
  """Parse a complete durable identity. Sentinels/legacy/malformed -> None."""
  if type(value) is not str:
    return None
  raw = value.strip()
  prefix = f"v{RECEIVER_FINGERPRINT_SCHEMA_VERSION}|"
  if not raw.startswith(prefix):
    return None
  parts = raw.split("|")
  if len(parts) != 6:
    return None
  _, serial, sw_part, hw_part, prot_part, fw_part = parts
  if (
    not serial
    or serial in ("unknown_serial",)
    or not sw_part.startswith("sw=")
    or not hw_part.startswith("hw=")
    or not prot_part.startswith("prot=")
    or not fw_part.startswith("fw=")
  ):
    return None
  software = sw_part[3:]
  hardware = hw_part[3:]
  protocol = prot_part[5:]
  firmware = fw_part[3:]
  if not software or not hardware or not protocol or not firmware:
    return None
  if "mon_ver_" in software or "mon_ver_" in hardware:
    return None
  return {
    "serial": serial,
    "software": software,
    "hardware": hardware,
    "protocol": protocol,
    "firmware": firmware,
  }


def receiver_fingerprints_compatible(stored: str, expected: str) -> bool:
  """True only for identical complete durable MON-VER identities.

  Fail-closed for sentinels (including identical unavailable/incomplete
  strings), legacy opaque fingerprints, and malformed v1 strings.
  """
  stored_parsed = parse_durable_receiver_fingerprint(stored)
  expected_parsed = parse_durable_receiver_fingerprint(expected)
  if stored_parsed is None or expected_parsed is None:
    return False
  return stored_parsed == expected_parsed


def evaluate_position_assistance_accuracy_cm(
  stored_position_accuracy_cm: int,
  *,
  age_seconds: float | None,
  age_verified: bool,
) -> tuple[int | None, str]:
  """Decide whether cached position assistance may be sent.

  Returns (accuracy_cm, reason). accuracy_cm is None when assistance must SKIP.
  Unverified/unknown age never invents a radius. Verified age beyond the fresh
  window is skipped rather than grown with an unsupported mobility model.
  Uncertainty is never clamped downward.
  """
  if type(stored_position_accuracy_cm) is not int:
    raise CacheValidationError("Position accuracy must be an exact integer")
  if stored_position_accuracy_cm < 1:
    raise CacheValidationError("Position accuracy is outside the valid range")

  if not age_verified or age_seconds is None or not isfinite(age_seconds):
    return None, "position_age_unverified"
  if age_seconds < 0:
    return None, "position_age_unverified"
  if age_seconds > FRESH_POSITION_ASSISTANCE_MAX_AGE_SECONDS:
    return None, "position_uncertainty_unrepresentable"

  accuracy = max(stored_position_accuracy_cm, MIN_RESTORE_POSITION_ACCURACY_CM)
  if accuracy > MAX_RESTORE_POSITION_ACCURACY_CM:
    return None, "position_uncertainty_unrepresentable"
  if accuracy == MIN_RESTORE_POSITION_ACCURACY_CM:
    return accuracy, "verified_fresh_floor"
  return accuracy, "verified_fresh_stored"


# Backward-compatible name used by older call sites/tests during PR77.
def age_safe_restore_position_accuracy_cm(
  stored_position_accuracy_cm: int,
  *,
  age_seconds: float | None,
  age_verified: bool,
) -> tuple[int | None, str]:
  return evaluate_position_assistance_accuracy_cm(
    stored_position_accuracy_cm,
    age_seconds=age_seconds,
    age_verified=age_verified,
  )


def parse_upd_sos_response(frame: bytes) -> UpdSosResponse | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_UPD
    or frame[3] != UBX_ID_UPD_SOS
  ):
    return None
  payload = frame[6:-2]
  if len(payload) != 8:
    return None
  return UpdSosResponse(
    command=payload[0],
    response=payload[4],
  )


@dataclass(frozen=True)
class GnssConfigBlock:
  gnss_id: int
  reserved_tracking_channels: int
  maximum_tracking_channels: int
  enabled: bool
  signal_configuration_mask: int
  flags: int


@dataclass(frozen=True)
class GnssConfig:
  version: int
  hardware_tracking_channels: int
  configured_tracking_channels: int
  blocks: tuple[GnssConfigBlock, ...]


@dataclass(frozen=True)
class RxmConfig:
  low_power_mode: int


@dataclass(frozen=True)
class Pm2Config:
  version: int
  maximum_startup_state_duration_s: int
  flags: int
  update_period_ms: int
  search_period_ms: int
  grid_offset_ms: int
  on_time_s: int
  minimum_acquisition_time_s: int
  external_interrupt_inactivity_ms: int | None


@dataclass(frozen=True)
class RateConfig:
  measurement_period_ms: int
  navigation_rate: int
  time_reference: int


@dataclass(frozen=True)
class Nav5Config:
  dynamic_model: int
  fix_mode: int


@dataclass(frozen=True)
class OdoConfig:
  version: int
  flags: int
  profile: int


@dataclass(frozen=True)
class ItfmConfig:
  config: int
  config2: int


@dataclass(frozen=True)
class MessageRateConfig:
  message_class: int
  message_id: int
  rates: tuple[int, int, int, int, int, int]


@dataclass(frozen=True)
class PortConfig:
  port_id: int
  tx_ready: int
  mode: int
  baud_rate: int
  input_protocol_mask: int
  output_protocol_mask: int
  flags: int


@dataclass(frozen=True)
class NavPvtFix:
  fix_ok: bool
  satellites: int
  latitude_e7: int
  longitude_e7: int
  altitude_cm: int
  horizontal_accuracy_cm: int
  vertical_accuracy_cm: int
  utc_time: datetime | None = None
  valid_date: bool = False
  valid_time: bool = False
  fully_resolved: bool = False
  time_accuracy_ns: int | None = None

  @property
  def reliable(self) -> bool:
    return (
      self.fix_ok
      and self.satellites >= 4
      and self.horizontal_accuracy_cm <= 2_500
    )


@dataclass(frozen=True)
class NavigationQuality:
  quality_version: int
  policy_version: int
  capture_context: str
  continuous_reliable_fix_seconds: float
  continuous_orbit_quality_seconds: float
  gps_satellites_known: int
  glonass_satellites_known: int
  gps_ephemeris_available: int
  glonass_ephemeris_available: int
  satellites_used: int
  gps_almanac_available: int
  glonass_almanac_available: int
  assistnow_offline_available: int
  orbit_source_counts: dict[str, int]
  gps_almanac_satellite_ids: tuple[int, ...] | None = None

  @property
  def total_ephemeris_available(self) -> int:
    return self.gps_ephemeris_available + self.glonass_ephemeris_available

  @property
  def usable_for_capture(self) -> bool:
    return (
      _quality_is_valid(self)
      and self.continuous_reliable_fix_seconds
      >= MINIMUM_USABLE_RELIABLE_FIX_SECONDS
    )

  @property
  def gps_startup_ready(self) -> bool:
    return (
      self.usable_for_capture
      and self.continuous_orbit_quality_seconds
      >= MINIMUM_ORBIT_QUALITY_SECONDS
      and self.gps_ephemeris_available
      >= MINIMUM_GPS_EPHEMERIS
      and self.glonass_ephemeris_available
      >= MINIMUM_GLONASS_EPHEMERIS
      and self.total_ephemeris_available
      >= MINIMUM_TOTAL_EPHEMERIS
      and self.satellites_used
      >= MINIMUM_SATELLITES_USED
    )

  @property
  def passes_policy(self) -> bool:
    return (
      self.quality_version == QUALITY_VERSION
      and self.policy_version == QUALITY_POLICY_VERSION
      and self.continuous_reliable_fix_seconds >= MINIMUM_RELIABLE_FIX_SECONDS
      and self.continuous_orbit_quality_seconds >= MINIMUM_ORBIT_QUALITY_SECONDS
      and self.gps_ephemeris_available >= MINIMUM_GPS_EPHEMERIS
      and self.glonass_ephemeris_available >= MINIMUM_GLONASS_EPHEMERIS
      and self.total_ephemeris_available >= MINIMUM_TOTAL_EPHEMERIS
      and self.satellites_used >= MINIMUM_SATELLITES_USED
    )


@dataclass(frozen=True)
class RestoredNavigationQuality:
  cache_age_seconds: float | None
  age_evidence: CacheAgeEvidence
  age_verified: bool
  captured_gps_ephemeris_available: int | None
  captured_glonass_ephemeris_available: int | None
  captured_gps_startup_ready: bool | None
  effective_gps_ephemeris_available: int | None
  effective_glonass_ephemeris_available: int | None
  effective_gps_startup_ready: bool | None
  gps_ephemeris_fresh: bool | None
  glonass_ephemeris_fresh: bool | None
  expiration_reasons: tuple[str, ...]


def _restored_navigation_quality_from_captured(
  *,
  captured_gps_ephemeris_available: int | None,
  captured_glonass_ephemeris_available: int | None,
  captured_gps_startup_ready: bool | None,
  cache_saved_at_utc: datetime,
  trusted_now: datetime | None,
  age_evidence: CacheAgeEvidence,
) -> RestoredNavigationQuality:
  if not isinstance(age_evidence, CacheAgeEvidence):
    raise ValueError("age_evidence must be a CacheAgeEvidence")

  saved_at_utc = _validated_utc(
    cache_saved_at_utc,
    "Cache saved time",
  )
  cache_age_seconds = None
  expiration_reasons: list[str] = []

  if not age_evidence.verified or trusted_now is None:
    expiration_reasons.append("cache_age_unverified")
  else:
    age = (
      _validated_utc(trusted_now, "Restore evaluation time")
      - saved_at_utc
    ).total_seconds()
    if age < 0:
      expiration_reasons.append("cache_timestamp_in_future")
    else:
      cache_age_seconds = age

  age_verified = cache_age_seconds is not None
  gps_ephemeris_fresh = (
    None
    if captured_gps_ephemeris_available is None
    else (
      age_verified
      and cache_age_seconds <= GPS_EPHEMERIS_FRESHNESS_SECONDS
    )
  )
  glonass_ephemeris_fresh = (
    None
    if captured_glonass_ephemeris_available is None
    else (
      age_verified
      and cache_age_seconds <= GLONASS_EPHEMERIS_FRESHNESS_SECONDS
    )
  )

  if (
    gps_ephemeris_fresh is False
    and age_verified
  ):
    expiration_reasons.append("gps_ephemeris_expired")
  if (
    glonass_ephemeris_fresh is False
    and age_verified
  ):
    expiration_reasons.append("glonass_ephemeris_expired")

  if captured_gps_ephemeris_available is None:
    effective_gps_ephemeris = None
  else:
    effective_gps_ephemeris = (
      captured_gps_ephemeris_available
      if gps_ephemeris_fresh
      else 0
    )

  if captured_glonass_ephemeris_available is None:
    effective_glonass_ephemeris = None
  else:
    effective_glonass_ephemeris = (
      captured_glonass_ephemeris_available
      if glonass_ephemeris_fresh
      else 0
    )

  effective_startup_ready = (
    None
    if (
      captured_gps_startup_ready is None
      or effective_gps_ephemeris is None
      or effective_glonass_ephemeris is None
    )
    else (
      captured_gps_startup_ready is True
      and effective_gps_ephemeris >= MINIMUM_GPS_EPHEMERIS
      and effective_glonass_ephemeris >= MINIMUM_GLONASS_EPHEMERIS
      and (
        effective_gps_ephemeris
        + effective_glonass_ephemeris
        >= MINIMUM_TOTAL_EPHEMERIS
      )
    )
  )

  return RestoredNavigationQuality(
    cache_age_seconds=cache_age_seconds,
    age_evidence=age_evidence,
    age_verified=age_verified,
    captured_gps_ephemeris_available=(
      captured_gps_ephemeris_available
    ),
    captured_glonass_ephemeris_available=(
      captured_glonass_ephemeris_available
    ),
    captured_gps_startup_ready=captured_gps_startup_ready,
    effective_gps_ephemeris_available=effective_gps_ephemeris,
    effective_glonass_ephemeris_available=(
      effective_glonass_ephemeris
    ),
    effective_gps_startup_ready=effective_startup_ready,
    gps_ephemeris_fresh=gps_ephemeris_fresh,
    glonass_ephemeris_fresh=glonass_ephemeris_fresh,
    expiration_reasons=tuple(expiration_reasons),
  )


def effective_restored_navigation_quality(
  quality: NavigationQuality | None,
  cache_saved_at_utc: datetime,
  trusted_now: datetime | None,
  age_evidence: CacheAgeEvidence,
) -> RestoredNavigationQuality:
  return _restored_navigation_quality_from_captured(
    captured_gps_ephemeris_available=(
      None if quality is None else quality.gps_ephemeris_available
    ),
    captured_glonass_ephemeris_available=(
      None
      if quality is None
      else quality.glonass_ephemeris_available
    ),
    captured_gps_startup_ready=(
      None if quality is None else quality.gps_startup_ready
    ),
    cache_saved_at_utc=cache_saved_at_utc,
    trusted_now=trusted_now,
    age_evidence=age_evidence,
  )


def refresh_restored_navigation_quality(
  restored_quality: RestoredNavigationQuality,
  cache_saved_at_utc: datetime,
  trusted_now: datetime | None,
  age_evidence: CacheAgeEvidence,
) -> RestoredNavigationQuality:
  if not isinstance(
    restored_quality,
    RestoredNavigationQuality,
  ):
    raise ValueError(
      "restored_quality must be a RestoredNavigationQuality"
    )
  return _restored_navigation_quality_from_captured(
    captured_gps_ephemeris_available=(
      restored_quality.captured_gps_ephemeris_available
    ),
    captured_glonass_ephemeris_available=(
      restored_quality.captured_glonass_ephemeris_available
    ),
    captured_gps_startup_ready=(
      restored_quality.captured_gps_startup_ready
    ),
    cache_saved_at_utc=cache_saved_at_utc,
    trusted_now=trusted_now,
    age_evidence=age_evidence,
  )


def conservative_navigation_quality(
  request_quality: NavigationQuality,
  completion_quality: NavigationQuality,
) -> NavigationQuality | None:
  if not _quality_is_valid(request_quality) or not _quality_is_valid(completion_quality):
    return None
  request_almanac_ids = request_quality.gps_almanac_satellite_ids
  completion_almanac_ids = completion_quality.gps_almanac_satellite_ids
  conservative_almanac_ids = (
    None
    if request_almanac_ids is None or completion_almanac_ids is None
    else tuple(sorted(set(request_almanac_ids) & set(completion_almanac_ids)))
  )
  quality = NavigationQuality(
    quality_version=completion_quality.quality_version,
    policy_version=completion_quality.policy_version,
    capture_context=completion_quality.capture_context,
    continuous_reliable_fix_seconds=min(
      request_quality.continuous_reliable_fix_seconds,
      completion_quality.continuous_reliable_fix_seconds,
    ),
    continuous_orbit_quality_seconds=min(
      request_quality.continuous_orbit_quality_seconds,
      completion_quality.continuous_orbit_quality_seconds,
    ),
    gps_satellites_known=completion_quality.gps_satellites_known,
    glonass_satellites_known=completion_quality.glonass_satellites_known,
    gps_ephemeris_available=min(
      request_quality.gps_ephemeris_available,
      completion_quality.gps_ephemeris_available,
    ),
    glonass_ephemeris_available=min(
      request_quality.glonass_ephemeris_available,
      completion_quality.glonass_ephemeris_available,
    ),
    satellites_used=min(
      request_quality.satellites_used,
      completion_quality.satellites_used,
    ),
    gps_almanac_available=min(
      request_quality.gps_almanac_available,
      completion_quality.gps_almanac_available,
    ),
    glonass_almanac_available=min(
      request_quality.glonass_almanac_available,
      completion_quality.glonass_almanac_available,
    ),
    assistnow_offline_available=min(
      request_quality.assistnow_offline_available,
      completion_quality.assistnow_offline_available,
    ),
    orbit_source_counts=dict(completion_quality.orbit_source_counts),
    gps_almanac_satellite_ids=conservative_almanac_ids,
  )
  return quality if _quality_is_valid(quality) else None


def capture_eligible(
  quality: NavigationQuality | None,
  stable_fix: NavPvtFix | None,
  latest_fix: NavPvtFix | None,
) -> bool:
  return (
    quality is not None
    and quality.usable_for_capture
    and stable_fix is not None
    and latest_fix is not None
    and stable_fix == latest_fix
    and latest_fix.reliable
  )


@dataclass(frozen=True)
class GpsAssistanceCache:
  saved_at_utc: datetime
  receiver_fingerprint: str
  latitude_e7: int
  longitude_e7: int
  altitude_cm: int
  position_accuracy_cm: int
  database_frames: tuple[bytes, ...]
  rtc_counter_seconds: int | None = None
  quality: NavigationQuality | None = None
  receiver_cycle: int | None = None


def read_rtc_counter_seconds(
  path: Path = RTC_COUNTER_PATH,
) -> int | None:
  try:
    value = int(path.read_text(encoding="utf-8").strip())
  except (OSError, UnicodeDecodeError, ValueError):
    return None

  return value if value >= 0 else None


def evaluate_utc_from_rtc(
  cache: GpsAssistanceCache,
  current_rtc_seconds: int | None | _CurrentRtcNotSupplied = (
    _CURRENT_RTC_NOT_SUPPLIED
  ),
  max_elapsed_seconds: int = (
    MAX_RTC_ASSISTANCE_ELAPSED_SECONDS
  ),
) -> RtcEstimateResult:
  if cache.rtc_counter_seconds is None:
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.MISSING_CACHED_RTC_ANCHOR
    )

  try:
    current_rtc = cast(int | None, (
      read_rtc_counter_seconds()
      if current_rtc_seconds is _CURRENT_RTC_NOT_SUPPLIED
      else current_rtc_seconds
    ))
  except (OSError, OverflowError, TypeError, ValueError):
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.INVALID_RTC_ESTIMATE
    )

  if current_rtc is None:
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.CURRENT_RTC_UNAVAILABLE
    )
  if type(current_rtc) is not int or current_rtc < 0:
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.INVALID_RTC_ESTIMATE
    )

  if type(cache.rtc_counter_seconds) is not int or cache.rtc_counter_seconds < 0:
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.INVALID_RTC_ESTIMATE
    )

  try:
    elapsed_seconds = current_rtc - cache.rtc_counter_seconds
  except (OverflowError, TypeError, ValueError):
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.INVALID_RTC_ESTIMATE
    )

  if elapsed_seconds < 0:
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.RTC_ROLLBACK,
      elapsed_seconds=elapsed_seconds,
    )

  if elapsed_seconds > max_elapsed_seconds:
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.ELAPSED_TIME_ABOVE_MAXIMUM,
      elapsed_seconds=elapsed_seconds,
    )

  try:
    saved_at_utc = _validated_utc(cache.saved_at_utc, "Cached RTC UTC anchor")
    estimated_utc = saved_at_utc + timedelta(seconds=elapsed_seconds)
    supported_minimum = MIN_DATE.replace(tzinfo=UTC)
    supported_maximum = MAX_DATE.replace(tzinfo=UTC)
  except (CacheValidationError, OSError, OverflowError, TypeError, ValueError):
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.INVALID_RTC_ESTIMATE,
      elapsed_seconds=elapsed_seconds,
    )

  if estimated_utc <= supported_minimum:
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.UTC_BEFORE_SUPPORTED_MINIMUM,
      elapsed_seconds=elapsed_seconds,
    )
  if estimated_utc >= supported_maximum:
    return RtcEstimateRejection(
      RtcEstimateRejectionReason.UTC_AFTER_SUPPORTED_MAXIMUM,
      elapsed_seconds=elapsed_seconds,
    )

  # An RTC-derived UTC that is plausible and inside these absolute bounds can
  # still be wrong; disproving it requires an independent trusted time source.

  drift_uncertainty = ceil(
    elapsed_seconds
    * RTC_DRIFT_PARTS_PER_MILLION
    / 1_000_000
  )
  uncertainty_seconds = min(
    65_535,
    RTC_BASE_TIME_UNCERTAINTY_SECONDS
    + drift_uncertainty,
  )

  return RtcEstimateSuccess(
    estimated_utc=estimated_utc,
    uncertainty_seconds=uncertainty_seconds,
    elapsed_seconds=elapsed_seconds,
  )


def estimate_utc_from_rtc(
  cache: GpsAssistanceCache,
  current_rtc_seconds: int | None = None,
  max_elapsed_seconds: int = (
    MAX_RTC_ASSISTANCE_ELAPSED_SECONDS
  ),
) -> tuple[datetime, int] | None:
  result = evaluate_utc_from_rtc(
    cache,
    # Preserve the legacy meaning: None requests the default RTC read.
    current_rtc_seconds=(
      _CURRENT_RTC_NOT_SUPPLIED
      if current_rtc_seconds is None
      else current_rtc_seconds
    ),
    max_elapsed_seconds=max_elapsed_seconds,
  )

  if isinstance(result, RtcEstimateRejection):
    return None

  return result.estimated_utc, result.uncertainty_seconds


def add_ubx_checksum(message_without_checksum: bytes) -> bytes:
  if not message_without_checksum.startswith(UBX_SYNC):
    raise ValueError("UBX message is missing the sync prefix")

  checksum_a = 0
  checksum_b = 0

  for value in message_without_checksum[2:]:
    checksum_a = (checksum_a + value) & 0xFF
    checksum_b = (checksum_b + checksum_a) & 0xFF

  return message_without_checksum + bytes((checksum_a, checksum_b))


def validate_ubx_frame(frame: bytes) -> bool:
  if len(frame) < 8 or not frame.startswith(UBX_SYNC):
    return False

  payload_length = int.from_bytes(frame[4:6], "little")

  if len(frame) != 6 + payload_length + 2:
    return False

  return add_ubx_checksum(frame[:-2]) == frame


def build_cfg_gnss_poll_message() -> bytes:
  return add_ubx_checksum(b"\xb5\x62\x06\x3e\x00\x00")


def build_cfg_rate_poll_message() -> bytes:
  return add_ubx_checksum(b"\xb5\x62\x06\x08\x00\x00")


def build_cfg_prt_poll_message(port_id: int) -> bytes:
  if port_id not in (0, 1, 2, 3, 4):
    raise ValueError(f"Unsupported CFG-PRT port ID: {port_id}")
  return add_ubx_checksum(b"\xb5\x62\x06\x00\x01\x00" + bytes((port_id,)))


def parse_cfg_prt(frame: bytes) -> PortConfig | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_CFG
    or frame[3] != 0x00
  ):
    return None
  payload = frame[6:-2]
  if len(payload) != 20 or payload[0] not in (0, 1, 2, 3, 4):
    return None
  return PortConfig(
    port_id=payload[0],
    tx_ready=int.from_bytes(payload[2:4], "little"),
    mode=int.from_bytes(payload[4:8], "little"),
    baud_rate=int.from_bytes(payload[8:12], "little"),
    input_protocol_mask=int.from_bytes(payload[12:14], "little"),
    output_protocol_mask=int.from_bytes(payload[14:16], "little"),
    flags=int.from_bytes(payload[16:18], "little"),
  )


def parse_cfg_rate(frame: bytes) -> RateConfig | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_CFG
    or frame[3] != UBX_ID_CFG_RATE
  ):
    return None
  payload = frame[6:-2]
  if len(payload) != 6:
    return None
  return RateConfig(
    measurement_period_ms=int.from_bytes(payload[0:2], "little"),
    navigation_rate=int.from_bytes(payload[2:4], "little"),
    time_reference=int.from_bytes(payload[4:6], "little"),
  )


def build_cfg_nav5_poll_message() -> bytes:
  return add_ubx_checksum(b"\xb5\x62\x06\x24\x00\x00")


def build_cfg_nav5_set_message(
  *,
  dynamic_model: int = 4,
  fix_mode: int = 3,
  mask: int = 0x0005,
) -> bytes:
  """Build a CFG-NAV5 SET with an exact 36-byte payload.

  mask 0x0005 applies dynModel (bit 0) and fixMode (bit 2). Defaults match the
  automotive dynamic model and auto 2D/3D fix mode used at receiver startup.
  """
  if (
    isinstance(dynamic_model, bool)
    or not isinstance(dynamic_model, int)
    or not 0 <= dynamic_model <= 0xFF
  ):
    raise ValueError("dynamic_model must be an integer from 0 through 255")
  if (
    isinstance(fix_mode, bool)
    or not isinstance(fix_mode, int)
    or not 0 <= fix_mode <= 0xFF
  ):
    raise ValueError("fix_mode must be an integer from 0 through 255")
  if (
    isinstance(mask, bool)
    or not isinstance(mask, int)
    or not 0 <= mask <= 0xFFFF
  ):
    raise ValueError("mask must be an integer from 0 through 65535")

  payload = bytearray(36)
  payload[0:2] = mask.to_bytes(2, "little")
  payload[2] = dynamic_model
  payload[3] = fix_mode
  return add_ubx_checksum(
    b"\xb5\x62\x06\x24" + len(payload).to_bytes(2, "little") + bytes(payload)
  )


def parse_cfg_nav5(frame: bytes) -> Nav5Config | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_CFG
    or frame[3] != UBX_ID_CFG_NAV5
  ):
    return None
  payload = frame[6:-2]
  if len(payload) != 36:
    return None
  return Nav5Config(dynamic_model=payload[2], fix_mode=payload[3])


def build_cfg_odo_poll_message() -> bytes:
  return add_ubx_checksum(b"\xb5\x62\x06\x1e\x00\x00")


def parse_cfg_odo(frame: bytes) -> OdoConfig | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_CFG
    or frame[3] != UBX_ID_CFG_ODO
  ):
    return None
  payload = frame[6:-2]
  if len(payload) != 20 or payload[0] != 0:
    return None
  return OdoConfig(
    version=payload[0],
    flags=payload[4],
    profile=payload[5] & 0x07,
  )


def build_cfg_itfm_poll_message() -> bytes:
  return add_ubx_checksum(b"\xb5\x62\x06\x39\x00\x00")


def parse_cfg_itfm(frame: bytes) -> ItfmConfig | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_CFG
    or frame[3] != UBX_ID_CFG_ITFM
  ):
    return None
  payload = frame[6:-2]
  if len(payload) != 8:
    return None
  return ItfmConfig(
    config=int.from_bytes(payload[0:4], "little"),
    config2=int.from_bytes(payload[4:8], "little"),
  )


def build_cfg_msg_poll_message(message_class: int, message_id: int) -> bytes:
  payload = bytes((message_class, message_id))
  return add_ubx_checksum(b"\xb5\x62\x06\x01\x02\x00" + payload)


def parse_cfg_msg(frame: bytes) -> MessageRateConfig | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_CFG
    or frame[3] != UBX_ID_CFG_MSG
  ):
    return None
  payload = frame[6:-2]
  if len(payload) != 8:
    return None
  return MessageRateConfig(
    message_class=payload[0],
    message_id=payload[1],
    rates=tuple(payload[2:8]),
  )


def parse_cfg_gnss(frame: bytes) -> GnssConfig | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_CFG
    or frame[3] != UBX_ID_CFG_GNSS
  ):
    return None

  payload = frame[6:-2]
  if len(payload) < 4 or payload[0] != 0:
    return None

  block_count = payload[3]
  if len(payload) != 4 + block_count * 8:
    return None

  blocks = []
  for offset in range(4, len(payload), 8):
    flags = int.from_bytes(payload[offset + 4:offset + 8], "little")
    blocks.append(GnssConfigBlock(
      gnss_id=payload[offset],
      reserved_tracking_channels=payload[offset + 1],
      maximum_tracking_channels=payload[offset + 2],
      enabled=bool(flags & 0x01),
      signal_configuration_mask=(flags >> 16) & 0xFF,
      flags=flags,
    ))

  return GnssConfig(
    version=payload[0],
    hardware_tracking_channels=payload[1],
    configured_tracking_channels=payload[2],
    blocks=tuple(blocks),
  )


def build_cfg_rxm_poll_message() -> bytes:
  return add_ubx_checksum(b"\xb5\x62\x06\x11\x00\x00")


def parse_cfg_rxm(frame: bytes) -> RxmConfig | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_CFG
    or frame[3] != UBX_ID_CFG_RXM
  ):
    return None

  payload = frame[6:-2]
  if len(payload) != 2:
    return None

  return RxmConfig(low_power_mode=payload[1])


def build_cfg_pm2_poll_message() -> bytes:
  return add_ubx_checksum(b"\xb5\x62\x06\x3b\x00\x00")


def parse_cfg_pm2(frame: bytes) -> Pm2Config | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_CFG
    or frame[3] != UBX_ID_CFG_PM2
  ):
    return None

  payload = frame[6:-2]
  if not payload:
    return None

  version = payload[0]
  expected_length = {1: 44, 2: 48}.get(version)
  if expected_length is None or len(payload) != expected_length:
    return None

  return Pm2Config(
    version=version,
    maximum_startup_state_duration_s=payload[2],
    flags=int.from_bytes(payload[4:8], "little"),
    update_period_ms=int.from_bytes(payload[8:12], "little"),
    search_period_ms=int.from_bytes(payload[12:16], "little"),
    grid_offset_ms=int.from_bytes(payload[16:20], "little"),
    on_time_s=int.from_bytes(payload[20:22], "little"),
    minimum_acquisition_time_s=int.from_bytes(payload[22:24], "little"),
    external_interrupt_inactivity_ms=(
      int.from_bytes(payload[44:48], "little")
      if version == 2
      else None
    ),
  )


def build_navx5_poll_message() -> bytes:
  return add_ubx_checksum(b"\xb5\x62\x06\x23\x00\x00")


def parse_navx5(frame: bytes) -> Navx5Config | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_CFG
    or frame[3] != UBX_ID_CFG_NAVX5
  ):
    return None

  payload = frame[6:-2]
  if len(payload) != 40:
    return None

  version = int.from_bytes(payload[0:2], "little")
  return Navx5Config(
    payload=payload,
    version=version,
    ack_aiding=bool(payload[17]),
    use_aop=bool(payload[27] & 0x01),
    aop_orbit_max_error_m=int.from_bytes(payload[30:32], "little"),
  )


_NAVX5_RESERVED_RANGES = (
  (8, 10),
  (13, 14),
  (15, 17),
  (21, 26),
  (28, 30),
  (32, 39),
)
_NAVX5_UNRELATED_WRITABLE_RANGES = (
  (10, 13),
  (14, 15),
  (17, 21),
  (26, 27),
  (39, 40),
)


def _build_navx5_set_message(config: Navx5Config, mask1: int) -> bytearray:
  payload = bytearray(config.payload)
  payload[2:4] = mask1.to_bytes(2, "little")
  payload[4:8] = b"\x00" * 4
  # The protocol requires reserved bytes to be zero in CFG-NAVX5 set messages.
  for start, end in _NAVX5_RESERVED_RANGES:
    payload[start:end] = b"\x00" * (end - start)
  return payload


def build_navx5_ack_aiding_enable_message(config: Navx5Config) -> bytes:
  payload = _build_navx5_set_message(config, NAVX5_MASK1_ACK_AIDING)
  payload[17] = 0x01
  header = b"\xb5\x62\x06\x23" + len(payload).to_bytes(2, "little")
  return add_ubx_checksum(header + payload)


def build_navx5_aop_enable_message(config: Navx5Config) -> bytes:
  payload = _build_navx5_set_message(config, NAVX5_MASK1_AOP)
  payload[27] = 0x01
  header = b"\xb5\x62\x06\x23" + len(payload).to_bytes(2, "little")
  return add_ubx_checksum(header + payload)


def navx5_unrelated_fields_unchanged(
  previous: Navx5Config,
  resulting: Navx5Config,
  *,
  enabling_ack_aiding: bool = False,
) -> bool:
  if previous.version != resulting.version or len(previous.payload) != len(resulting.payload):
    return False
  ranges = _NAVX5_UNRELATED_WRITABLE_RANGES
  if enabling_ack_aiding:
    ranges = (*ranges[:2], (18, 21), *ranges[3:])
  return all(previous.payload[start:end] == resulting.payload[start:end] for start, end in ranges)


def build_nav_aopstatus_poll_message() -> bytes:
  return add_ubx_checksum(b"\xb5\x62\x01\x60\x00\x00")


def parse_nav_aopstatus(frame: bytes) -> NavAopStatus | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_NAV
    or frame[3] != UBX_ID_NAV_AOPSTATUS
  ):
    return None

  payload = frame[6:-2]
  if len(payload) != 16:
    return None
  return NavAopStatus(
    enabled=bool(payload[4] & 0x01),
    status=payload[5],
  )


def split_ubx_frames(data: bytes) -> tuple[bytes, ...]:
  frames: list[bytes] = []
  offset = 0

  while offset < len(data):
    if data[offset:offset + 2] != UBX_SYNC:
      raise CacheValidationError(
        f"Invalid UBX sync prefix at byte {offset}"
      )

    if len(data) - offset < 8:
      raise CacheValidationError("Truncated UBX frame header")

    payload_length = int.from_bytes(
      data[offset + 4:offset + 6],
      "little",
    )
    frame_length = 6 + payload_length + 2
    frame_end = offset + frame_length

    if frame_end > len(data):
      raise CacheValidationError("Truncated UBX frame payload")

    frame = data[offset:frame_end]

    if not validate_ubx_frame(frame):
      raise CacheValidationError("Invalid UBX frame checksum")

    frames.append(frame)
    offset = frame_end

  return tuple(frames)


class UbxStreamParser:
  def __init__(self) -> None:
    self._buffer = bytearray()

  def reset(self) -> None:
    self._buffer.clear()

  def feed(self, data: bytes) -> list[bytes]:
    self._buffer.extend(data)
    frames: list[bytes] = []

    while True:
      start = self._buffer.find(UBX_SYNC)

      if start < 0:
        if self._buffer[-1:] == UBX_SYNC[:1]:
          del self._buffer[:-1]
        else:
          self._buffer.clear()
        break

      if start:
        del self._buffer[:start]

      if len(self._buffer) < 8:
        break

      payload_length = int.from_bytes(
        self._buffer[4:6],
        "little",
      )
      frame_length = 6 + payload_length + 2

      if frame_length > MAX_DATABASE_BYTES:
        del self._buffer[0]
        continue

      if len(self._buffer) < frame_length:
        break

      frame = bytes(self._buffer[:frame_length])
      del self._buffer[:frame_length]

      if validate_ubx_frame(frame):
        frames.append(frame)

    return frames



def build_database_poll_message() -> bytes:
  return add_ubx_checksum(
    UBX_SYNC
    + bytes((UBX_CLASS_MGA, UBX_ID_MGA_DBD))
    + b"\x00\x00"
  )


def parse_mga_ack(frame: bytes) -> MgaAck | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_MGA
    or frame[3] != UBX_ID_MGA_ACK
  ):
    return None

  payload = frame[6:-2]

  if len(payload) != 8:
    return None

  return MgaAck(
    accepted=payload[0] == 1 and payload[1] == 0 and payload[2] == 0,
    acknowledgment_type=payload[0],
    version=payload[1],
    info_code=payload[2],
    message_id=payload[3],
    message_payload_start=payload[4:8],
  )


def parse_nav_pvt(frame: bytes) -> NavPvtFix | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_NAV
    or frame[3] != UBX_ID_NAV_PVT
  ):
    return None

  payload = frame[6:-2]

  if len(payload) < 92:
    return None

  utc_time = None
  valid_time_flags = payload[11]
  valid_date = bool(valid_time_flags & 0x01)
  valid_time = bool(valid_time_flags & 0x02)
  fully_resolved = bool(valid_time_flags & 0x04)
  time_accuracy_ns = struct.unpack_from(
    "<I",
    payload,
    12,
  )[0]
  nano = struct.unpack_from("<i", payload, 16)[0]

  if (
    valid_date
    and valid_time
    and fully_resolved
    and abs(nano) <= MAXIMUM_RECEIVER_UTC_NANOSECONDS
  ):
    try:
      utc_time = datetime(
        struct.unpack_from("<H", payload, 4)[0],
        payload[6],
        payload[7],
        payload[8],
        payload[9],
        payload[10],
        tzinfo=UTC,
      ) + timedelta(
        microseconds=round(nano / 1_000)
      )
    except (OverflowError, ValueError):
      # Ignore malformed receiver dates while retaining the position fix.
      utc_time = None

  fix_type = payload[20]
  flags = payload[21]

  return NavPvtFix(
    fix_ok=ublox_nav_pvt_has_fix(flags, fix_type),
    satellites=payload[23],
    longitude_e7=struct.unpack_from("<i", payload, 24)[0],
    latitude_e7=struct.unpack_from("<i", payload, 28)[0],
    altitude_cm=round(
      struct.unpack_from("<i", payload, 32)[0] / 10
    ),
    horizontal_accuracy_cm=round(
      struct.unpack_from("<I", payload, 40)[0] / 10
    ),
    vertical_accuracy_cm=round(
      struct.unpack_from("<I", payload, 44)[0] / 10
    ),
    utc_time=utc_time,
    valid_date=valid_date,
    valid_time=valid_time,
    fully_resolved=fully_resolved,
    time_accuracy_ns=time_accuracy_ns,
  )


@dataclass(frozen=True)
class NavSatQuality:
  gps_satellites_known: int
  glonass_satellites_known: int
  gps_ephemeris_available: int
  glonass_ephemeris_available: int
  satellites_used: int
  gps_almanac_available: int
  glonass_almanac_available: int
  assistnow_offline_available: int
  orbit_source_counts: dict[str, int]
  assistnow_autonomous_available: int = 0
  gps_satellite_ids: frozenset[int] = frozenset()
  gps_healthy_satellite_ids: frozenset[int] = frozenset()
  gps_almanac_satellite_ids: frozenset[int] = frozenset()

  @property
  def total_ephemeris_available(self) -> int:
    return self.gps_ephemeris_available + self.glonass_ephemeris_available

  @property
  def passes_thresholds(self) -> bool:
    return (
      self.gps_ephemeris_available >= MINIMUM_GPS_EPHEMERIS
      and self.glonass_ephemeris_available >= MINIMUM_GLONASS_EPHEMERIS
      and self.total_ephemeris_available >= MINIMUM_TOTAL_EPHEMERIS
      and self.satellites_used >= MINIMUM_SATELLITES_USED
    )


_ORBIT_SOURCE_NAMES = {
  0: "none",
  1: "ephemeris",
  2: "almanac",
  3: "assistnow_offline",
  4: "assistnow_autonomous",
  5: "other",
  6: "other_6",
  7: "other_7",
}


def parse_nav_sat(frame: bytes) -> NavSatQuality | None:
  if (
    not validate_ubx_frame(frame)
    or frame[2] != UBX_CLASS_NAV
    or frame[3] != UBX_ID_NAV_SAT
  ):
    return None

  payload = frame[6:-2]
  if len(payload) < 8 or payload[4] != 1:
    return None
  satellite_count = payload[5]
  if len(payload) != 8 + satellite_count * 12:
    return None

  known = {0: 0, 6: 0}
  ephemeris = {0: 0, 6: 0}
  almanac = {0: 0, 6: 0}
  used = 0
  assistnow_offline = 0
  assistnow_autonomous = 0
  orbit_sources: dict[str, int] = {}
  seen_satellites: set[tuple[int, int]] = set()
  gps_satellite_ids: set[int] = set()
  gps_healthy_satellite_ids: set[int] = set()
  gps_almanac_satellite_ids: set[int] = set()
  for offset in range(8, len(payload), 12):
    gnss_id = payload[offset]
    satellite_id = payload[offset + 1]
    satellite = (gnss_id, satellite_id)
    if satellite in seen_satellites:
      return None
    seen_satellites.add(satellite)
    if gnss_id not in known:
      continue
    flags = struct.unpack_from("<I", payload, offset + 8)[0]
    health = (flags >> 4) & 0x03
    explicitly_unhealthy = health == 2
    if gnss_id == 0 and 1 <= satellite_id <= 32:
      gps_satellite_ids.add(satellite_id)
      if not explicitly_unhealthy:
        gps_healthy_satellite_ids.add(satellite_id)
      if flags & (1 << 12):
        gps_almanac_satellite_ids.add(satellite_id)
    known[gnss_id] += 1
    used += bool(flags & (1 << 3))
    ephemeris[gnss_id] += bool(flags & (1 << 11)) and not explicitly_unhealthy
    almanac[gnss_id] += bool(flags & (1 << 12))
    assistnow_offline += bool(flags & (1 << 13))
    assistnow_autonomous += bool(flags & (1 << 14))
    source = _ORBIT_SOURCE_NAMES[(flags >> 8) & 0x07]
    orbit_sources[source] = orbit_sources.get(source, 0) + 1

  return NavSatQuality(
    gps_satellites_known=known[0],
    glonass_satellites_known=known[6],
    gps_ephemeris_available=ephemeris[0],
    glonass_ephemeris_available=ephemeris[6],
    satellites_used=used,
    gps_almanac_available=almanac[0],
    glonass_almanac_available=almanac[6],
    assistnow_offline_available=assistnow_offline,
    orbit_source_counts=orbit_sources,
    assistnow_autonomous_available=assistnow_autonomous,
    gps_satellite_ids=frozenset(gps_satellite_ids),
    gps_healthy_satellite_ids=frozenset(
      gps_healthy_satellite_ids
    ),
    gps_almanac_satellite_ids=frozenset(
      gps_almanac_satellite_ids
    ),
  )


def build_time_assistance_message(
  now: datetime,
  accuracy_seconds: int = 30,
) -> bytes:
  if now.tzinfo is None:
    raise ValueError("Time assistance requires a timezone-aware datetime")

  if not 0 <= accuracy_seconds <= 65_535:
    raise ValueError(
      "Time-assistance accuracy is outside the valid range"
    )

  now_utc = now.astimezone(UTC)

  payload = struct.pack(
    "<BBBBHBBBBBxIHxxI",
    0x10,
    0x00,
    0x00,
    0x80,
    now_utc.year,
    now_utc.month,
    now_utc.day,
    now_utc.hour,
    now_utc.minute,
    now_utc.second,
    now_utc.microsecond * 1_000,
    accuracy_seconds,
    0,
  )

  return add_ubx_checksum(
    UBX_SYNC
    + bytes((UBX_CLASS_MGA, UBX_ID_MGA_INI))
    + len(payload).to_bytes(2, "little")
    + payload
  )


def build_position_assistance_message(
  latitude_e7: int,
  longitude_e7: int,
  altitude_cm: int,
  position_accuracy_cm: int,
) -> bytes:
  _validate_position(
    latitude_e7,
    longitude_e7,
    altitude_cm,
    position_accuracy_cm,
  )

  # Floor upward only (larger uncertainty is safer). Never clamp downward.
  restore_accuracy_cm = max(
    position_accuracy_cm,
    MIN_RESTORE_POSITION_ACCURACY_CM,
  )
  if restore_accuracy_cm > MAX_RESTORE_POSITION_ACCURACY_CM:
    raise CacheValidationError(
      "Position accuracy exceeds the restore policy usefulness limit"
    )

  payload = struct.pack(
    "<BBxxiiiI",
    0x01,
    0x00,
    latitude_e7,
    longitude_e7,
    altitude_cm,
    restore_accuracy_cm,
  )

  return add_ubx_checksum(
    UBX_SYNC
    + bytes((UBX_CLASS_MGA, UBX_ID_MGA_INI))
    + len(payload).to_bytes(2, "little")
    + payload
  )


def create_cache(
  receiver_fingerprint: str,
  fix: NavPvtFix,
  database_frames: list[bytes] | tuple[bytes, ...],
  saved_at_utc: datetime | None = None,
  rtc_counter_seconds: int | None = None,
  quality: NavigationQuality | None = None,
  receiver_cycle: int | None = None,
) -> GpsAssistanceCache:
  if type(receiver_fingerprint) is not str or not receiver_fingerprint.strip():
    raise CacheValidationError("Receiver fingerprint is empty")

  if not fix.reliable:
    raise CacheValidationError(
      "A reliable GPS fix is required before saving assistance data"
    )

  frames = tuple(database_frames)
  _validate_database_frames(frames)

  position_accuracy_cm = max(
    fix.horizontal_accuracy_cm,
    fix.vertical_accuracy_cm,
  )

  _validate_position(
    fix.latitude_e7,
    fix.longitude_e7,
    fix.altitude_cm,
    position_accuracy_cm,
  )

  saved_at = saved_at_utc or datetime.now(UTC)

  saved_at = _validated_utc(saved_at, "Cache timestamp")

  if (
    rtc_counter_seconds is not None
    and (type(rtc_counter_seconds) is not int or rtc_counter_seconds < 0)
  ):
    raise CacheValidationError(
      "RTC counter cannot be negative"
    )

  if receiver_cycle is not None and (
    type(receiver_cycle) is not int or receiver_cycle < 0
  ):
    raise CacheValidationError("Receiver cycle cannot be negative")

  return GpsAssistanceCache(
    saved_at_utc=saved_at,
    receiver_fingerprint=receiver_fingerprint.strip(),
    latitude_e7=fix.latitude_e7,
    longitude_e7=fix.longitude_e7,
    altitude_cm=fix.altitude_cm,
    position_accuracy_cm=position_accuracy_cm,
    database_frames=frames,
    rtc_counter_seconds=rtc_counter_seconds,
    quality=quality,
    receiver_cycle=receiver_cycle,
  )


def _quality_to_json(quality: NavigationQuality) -> dict:
  if not _quality_is_valid(quality):
    raise CacheValidationError("GPS assistance quality metadata is invalid")
  payload = {
    "version": quality.quality_version,
    "policy_version": quality.policy_version,
    "capture_context": quality.capture_context,
    "continuous_reliable_fix_seconds": quality.continuous_reliable_fix_seconds,
    "continuous_orbit_quality_seconds": quality.continuous_orbit_quality_seconds,
    "gps_satellites_known": quality.gps_satellites_known,
    "glonass_satellites_known": quality.glonass_satellites_known,
    "gps_ephemeris_available": quality.gps_ephemeris_available,
    "glonass_ephemeris_available": quality.glonass_ephemeris_available,
    "total_ephemeris_available": quality.total_ephemeris_available,
    "satellites_used": quality.satellites_used,
    "gps_almanac_available": quality.gps_almanac_available,
    "glonass_almanac_available": quality.glonass_almanac_available,
    "assistnow_offline_available": quality.assistnow_offline_available,
    "orbit_source_counts": quality.orbit_source_counts,
  }
  if quality.gps_almanac_satellite_ids is not None:
    payload["gps_almanac_satellite_ids"] = list(
      quality.gps_almanac_satellite_ids
    )
  return payload


def _quality_is_valid(quality: NavigationQuality) -> bool:
  integer_values = (
    quality.quality_version,
    quality.policy_version,
    quality.gps_satellites_known,
    quality.glonass_satellites_known,
    quality.gps_ephemeris_available,
    quality.glonass_ephemeris_available,
    quality.satellites_used,
    quality.gps_almanac_available,
    quality.glonass_almanac_available,
    quality.assistnow_offline_available,
  )
  durations = (
    quality.continuous_reliable_fix_seconds,
    quality.continuous_orbit_quality_seconds,
  )
  try:
    if (
      quality.quality_version != QUALITY_VERSION
      or quality.policy_version != QUALITY_POLICY_VERSION
      or type(quality.capture_context) is not str
      or quality.capture_context not in ("onroad", "post_drive")
      or any(type(value) is not int or value < 0 for value in integer_values)
      or any(type(value) not in (int, float) or isinstance(value, bool) or not isfinite(value) or value < 0 for value in durations)
      or type(quality.orbit_source_counts) is not dict
      or any(type(key) is not str or type(value) is not int or value < 0 for key, value in quality.orbit_source_counts.items())
      or (
        quality.gps_almanac_satellite_ids is not None
        and (
          type(quality.gps_almanac_satellite_ids) is not tuple
          or quality.gps_almanac_satellite_ids
          != tuple(sorted(set(quality.gps_almanac_satellite_ids)))
          or any(
            type(satellite_id) is not int
            or not 1 <= satellite_id <= 32
            for satellite_id in quality.gps_almanac_satellite_ids
          )
        )
      )
    ):
      return False
  except (OverflowError, TypeError, ValueError):
    return False

  total_known = quality.gps_satellites_known + quality.glonass_satellites_known
  # NAV-SAT supplies exactly one orbit source for every tracked GPS/GLONASS SV.
  return (
    quality.gps_ephemeris_available <= quality.gps_satellites_known
    and quality.gps_almanac_available <= quality.gps_satellites_known
    and quality.glonass_ephemeris_available <= quality.glonass_satellites_known
    and quality.glonass_almanac_available <= quality.glonass_satellites_known
    and quality.satellites_used <= total_known
    and quality.assistnow_offline_available <= total_known
    and (
      quality.gps_almanac_satellite_ids is None
      or len(quality.gps_almanac_satellite_ids)
      <= quality.gps_almanac_available
    )
    and sum(quality.orbit_source_counts.values()) == total_known
  )


def _quality_from_json(raw: object) -> NavigationQuality:
  if type(raw) is not dict:
    raise CacheValidationError("GPS assistance quality metadata is not an object")
  integer_names = (
    "version",
    "policy_version",
    "gps_satellites_known",
    "glonass_satellites_known",
    "gps_ephemeris_available",
    "glonass_ephemeris_available",
    "total_ephemeris_available",
    "satellites_used",
    "gps_almanac_available",
    "glonass_almanac_available",
    "assistnow_offline_available",
  )
  duration_names = (
    "continuous_reliable_fix_seconds",
    "continuous_orbit_quality_seconds",
  )
  try:
    if any(type(raw[name]) is not int for name in integer_names):
      raise CacheValidationError("GPS assistance quality integer field is invalid")
    if any(
      type(raw[name]) not in (int, float)
      or isinstance(raw[name], bool)
      or not isfinite(raw[name])
      for name in duration_names
    ):
      raise CacheValidationError("GPS assistance quality duration is invalid")
    if type(raw["capture_context"]) is not str:
      raise CacheValidationError("GPS assistance quality context is invalid")
    orbit_source_counts = raw["orbit_source_counts"]
    if type(orbit_source_counts) is not dict:
      raise CacheValidationError("GPS assistance orbit-source metadata is invalid")
    raw_almanac_ids = raw.get("gps_almanac_satellite_ids")
    if raw_almanac_ids is None:
      gps_almanac_satellite_ids = None
    elif type(raw_almanac_ids) is list:
      gps_almanac_satellite_ids = tuple(raw_almanac_ids)
    else:
      raise CacheValidationError(
        "GPS assistance almanac PRN metadata is invalid"
      )
    quality = NavigationQuality(
      quality_version=raw["version"],
      policy_version=raw["policy_version"],
      capture_context=raw["capture_context"],
      continuous_reliable_fix_seconds=raw["continuous_reliable_fix_seconds"],
      continuous_orbit_quality_seconds=raw["continuous_orbit_quality_seconds"],
      gps_satellites_known=raw["gps_satellites_known"],
      glonass_satellites_known=raw["glonass_satellites_known"],
      gps_ephemeris_available=raw["gps_ephemeris_available"],
      glonass_ephemeris_available=raw["glonass_ephemeris_available"],
      satellites_used=raw["satellites_used"],
      gps_almanac_available=raw["gps_almanac_available"],
      glonass_almanac_available=raw["glonass_almanac_available"],
      assistnow_offline_available=raw["assistnow_offline_available"],
      orbit_source_counts=orbit_source_counts,
      gps_almanac_satellite_ids=gps_almanac_satellite_ids,
    )
  except CacheValidationError:
    raise
  except (KeyError, OverflowError, TypeError, ValueError) as exc:
    raise CacheValidationError("GPS assistance quality metadata is malformed") from exc
  if not _quality_is_valid(quality):
    raise CacheValidationError("GPS assistance quality metadata is invalid")
  if raw["total_ephemeris_available"] != quality.total_ephemeris_available:
    raise CacheValidationError("GPS assistance quality total does not match")
  return quality


def _encode_cache(cache: GpsAssistanceCache) -> bytes:
  _validate_database_frames(cache.database_frames)
  _validate_position(
    cache.latitude_e7,
    cache.longitude_e7,
    cache.altitude_cm,
    cache.position_accuracy_cm,
  )

  database = b"".join(cache.database_frames)

  payload = {
    "version": CACHE_VERSION,
    "saved_at_utc": _validated_utc(cache.saved_at_utc, "Cache timestamp").isoformat(),
    "rtc_counter_seconds": cache.rtc_counter_seconds,
    "receiver_fingerprint": cache.receiver_fingerprint,
    "position": {
      "latitude_e7": cache.latitude_e7,
      "longitude_e7": cache.longitude_e7,
      "altitude_cm": cache.altitude_cm,
      "accuracy_cm": cache.position_accuracy_cm,
    },
    "database": {
      "complete": True,
      "message_count": len(cache.database_frames),
      "byte_count": len(database),
      "sha256": hashlib.sha256(database).hexdigest(),
      "ubx_base64": base64.b64encode(database).decode("ascii"),
    },
  }
  if cache.quality is not None:
    payload["quality"] = _quality_to_json(cache.quality)
  if cache.receiver_cycle is not None:
    if type(cache.receiver_cycle) is not int or cache.receiver_cycle < 0:
      raise CacheValidationError("Receiver cycle is invalid")
    payload["receiver_cycle"] = cache.receiver_cycle

  encoded = (
    json.dumps(
      payload,
      sort_keys=True,
      separators=(",", ":"),
    ).encode("utf-8")
    + b"\n"
  )

  if len(encoded) > MAX_CACHE_FILE_BYTES:
    raise CacheValidationError("Encoded cache exceeds the size limit")
  return encoded


def _require_nofollow() -> int:
  nofollow = getattr(os, "O_NOFOLLOW", None)
  if nofollow is None:
    raise CacheValidationError("Secure cache-file handling is unavailable")
  return nofollow


def _read_fixed_file(path: Path) -> bytes:
  flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | _require_nofollow()
  try:
    descriptor = os.open(path, flags)
  except FileNotFoundError as exc:
    raise CacheValidationError("GPS assistance cache does not exist") from exc
  except (NotImplementedError, OSError) as exc:
    raise CacheValidationError("GPS assistance cache cannot be opened safely") from exc
  contents: bytes | None = None
  operation_error: BaseException | None = None
  try:
    information = os.fstat(descriptor)
    if not stat.S_ISREG(information.st_mode):
      raise CacheValidationError("GPS assistance cache is not a regular file")
    if information.st_size <= 0 or information.st_size > MAX_CACHE_FILE_BYTES:
      raise CacheValidationError("GPS assistance cache has an invalid size")
    chunks = []
    remaining = information.st_size
    while remaining:
      chunk = os.read(descriptor, min(remaining, 64 * 1024))
      if not chunk:
        raise CacheValidationError("GPS assistance cache changed while reading")
      chunks.append(chunk)
      remaining -= len(chunk)
    if os.read(descriptor, 1):
      raise CacheValidationError("GPS assistance cache grew while reading")
    contents = b"".join(chunks)
  except BaseException as exc:
    operation_error = exc
  try:
    os.close(descriptor)
  except BaseException as close_exc:
    if operation_error is None:
      raise
    operation_error.add_note(
      f"Cache descriptor close also failed: {type(close_exc).__name__}"
    )
  if operation_error is not None:
    raise operation_error
  assert contents is not None
  return contents


def _fsync_cache_directory(directory: Path) -> None:
  directory_flag = getattr(os, "O_DIRECTORY", None)
  if directory_flag is None:
    raise CacheValidationError("Secure cache-directory handling is unavailable")
  flags = os.O_RDONLY | directory_flag | getattr(os, "O_CLOEXEC", 0)
  descriptor = os.open(directory, flags)
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
      f"Cache directory descriptor close also failed: {type(close_exc).__name__}"
    )
  if operation_error is not None:
    raise operation_error


def save_cache(path: Path, cache: GpsAssistanceCache) -> None:
  encoded = _encode_cache(cache)

  path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

  file_descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{path.name}.",
    suffix=".tmp",
    dir=path.parent,
  )
  temporary_path = Path(temporary_name)

  try:
    os.fchmod(file_descriptor, 0o600)

    try:
      temporary_file = os.fdopen(file_descriptor, "wb")
    except BaseException:
      os.close(file_descriptor)
      raise
    with temporary_file:
      temporary_file.write(encoded)
      temporary_file.flush()
      os.fsync(temporary_file.fileno())

    os.replace(temporary_path, path)

    _fsync_cache_directory(path.parent)

  finally:
    temporary_path.unlink(missing_ok=True)


def load_cache(
  path: Path,
  expected_receiver_fingerprint: str | None = None,
  now_utc: datetime | None = None,
  max_age_seconds: int = DEFAULT_MAX_CACHE_AGE_SECONDS,
  *,
  require_complete: bool = False,
  expected_receiver_cycle: int | None = None,
) -> GpsAssistanceCache:
  try:
    encoded = _read_fixed_file(path)

    def reject_nonfinite_constant(value: str) -> None:
      raise ValueError(f"Unsupported JSON constant: {value}")

    raw = json.loads(
      encoded.decode("utf-8"),
      parse_constant=reject_nonfinite_constant,
    )
    if type(raw) is not dict:
      raise CacheValidationError("GPS assistance cache root is not an object")
    if type(raw.get("version")) is not int or raw["version"] != CACHE_VERSION:
      raise CacheValidationError("Unsupported GPS assistance cache version")

    saved_at_raw = raw["saved_at_utc"]
    receiver_fingerprint = raw["receiver_fingerprint"]
    position = raw["position"]
    database_metadata = raw["database"]
    if type(saved_at_raw) is not str:
      raise CacheValidationError("GPS assistance cache timestamp is not a string")
    if type(receiver_fingerprint) is not str or not receiver_fingerprint.strip():
      raise CacheValidationError("Receiver fingerprint is invalid")
    receiver_fingerprint = receiver_fingerprint.strip()
    if type(position) is not dict:
      raise CacheValidationError("GPS assistance position metadata is not an object")
    if type(database_metadata) is not dict:
      raise CacheValidationError("GPS assistance database metadata is not an object")

    saved_at = _validated_utc(
      datetime.fromisoformat(saved_at_raw), "Cache timestamp",
    )
    position_names = (
      "latitude_e7", "longitude_e7", "altitude_cm", "accuracy_cm",
    )
    if any(type(position[name]) is not int for name in position_names):
      raise CacheValidationError("GPS assistance position metadata is invalid")
    latitude_e7 = position["latitude_e7"]
    longitude_e7 = position["longitude_e7"]
    altitude_cm = position["altitude_cm"]
    position_accuracy_cm = position["accuracy_cm"]

    count_names = ("message_count", "byte_count")
    if any(
      type(database_metadata[name]) is not int or database_metadata[name] < 0
      for name in count_names
    ):
      raise CacheValidationError("GPS assistance database count is invalid")
    expected_message_count = database_metadata["message_count"]
    expected_byte_count = database_metadata["byte_count"]
    expected_sha256 = database_metadata["sha256"]
    encoded_database = database_metadata["ubx_base64"]
    if (
      type(expected_sha256) is not str
      or len(expected_sha256) != 64
      or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
      raise CacheValidationError("GPS assistance database checksum metadata is invalid")
    if type(encoded_database) is not str:
      raise CacheValidationError("GPS assistance database base64 metadata is invalid")

    completion_present = "complete" in database_metadata
    if completion_present and database_metadata["complete"] is not True:
      raise CacheValidationError("Navigation database completion metadata is invalid")
    if require_complete and not completion_present:
      raise CacheValidationError("Navigation database completion metadata is missing")

    rtc_counter_raw = raw.get("rtc_counter_seconds")
    if rtc_counter_raw is None:
      rtc_counter_seconds = None
    elif type(rtc_counter_raw) is int and rtc_counter_raw >= 0:
      rtc_counter_seconds = rtc_counter_raw
    else:
      raise CacheValidationError("RTC counter is invalid")

    if "receiver_cycle" in raw:
      receiver_cycle = raw["receiver_cycle"]
      if type(receiver_cycle) is not int or receiver_cycle < 0:
        raise CacheValidationError("Receiver cycle is invalid")
    else:
      receiver_cycle = None
    if expected_receiver_cycle is not None and receiver_cycle != expected_receiver_cycle:
      raise CacheValidationError("GPS assistance cache belongs to a different receiver cycle")

    if "quality" in raw:
      quality = _quality_from_json(raw["quality"])
    else:
      quality = None

    if expected_receiver_fingerprint is not None:
      if type(expected_receiver_fingerprint) is not str:
        raise CacheValidationError("Expected receiver fingerprint is invalid")
      if not receiver_fingerprints_compatible(
        receiver_fingerprint,
        expected_receiver_fingerprint,
      ):
        raise CacheValidationError("GPS assistance cache belongs to a different receiver")

    if now_utc is not None:
      validated_now = _validated_utc(now_utc, "Current cache-validation time")
      age_seconds = (validated_now - saved_at).total_seconds()
      if age_seconds < -300:
        raise CacheValidationError("GPS assistance cache timestamp is in the future")
      if age_seconds > max_age_seconds:
        raise CacheValidationError("GPS assistance cache is too old")

    database = base64.b64decode(
      encoded_database, validate=True,
    )
    if len(database) != expected_byte_count:
      raise CacheValidationError("GPS assistance database byte count does not match")
    if hashlib.sha256(database).hexdigest() != expected_sha256:
      raise CacheValidationError("GPS assistance database checksum does not match")
    frames = split_ubx_frames(database)
    if len(frames) != expected_message_count:
      raise CacheValidationError("GPS assistance database message count does not match")
    _validate_database_frames(frames)
    _validate_position(
      latitude_e7, longitude_e7, altitude_cm, position_accuracy_cm,
    )
  except CacheValidationError:
    raise
  except (
    AttributeError,
    binascii.Error,
    json.JSONDecodeError,
    KeyError,
    OSError,
    OverflowError,
    RecursionError,
    TypeError,
    UnicodeError,
    ValueError,
  ) as exc:
    raise CacheValidationError("GPS assistance cache is malformed") from exc
  except Exception as exc:
    raise CacheValidationError("GPS assistance cache validation failed safely") from exc

  return GpsAssistanceCache(
    saved_at_utc=saved_at,
    receiver_fingerprint=receiver_fingerprint,
    latitude_e7=latitude_e7,
    longitude_e7=longitude_e7,
    altitude_cm=altitude_cm,
    position_accuracy_cm=position_accuracy_cm,
    database_frames=frames,
    rtc_counter_seconds=rtc_counter_seconds,
    quality=quality,
    receiver_cycle=receiver_cycle,
  )


def _validate_database_frames(
  frames: tuple[bytes, ...],
) -> None:
  if not frames:
    raise CacheValidationError("Navigation database is empty")

  if len(frames) > MAX_DATABASE_FRAMES:
    raise CacheValidationError("Navigation database has too many messages")

  total_bytes = sum(len(frame) for frame in frames)

  if total_bytes > MAX_DATABASE_BYTES:
    raise CacheValidationError(
      "Navigation database exceeds the size limit"
    )

  for frame in frames:
    if (
      not validate_ubx_frame(frame)
      or frame[2] != UBX_CLASS_MGA
      or frame[3] != UBX_ID_MGA_DBD
    ):
      raise CacheValidationError(
        "Navigation database contains an invalid message"
      )


def _validate_position(
  latitude_e7: int,
  longitude_e7: int,
  altitude_cm: int,
  position_accuracy_cm: int,
) -> None:
  if any(type(value) is not int for value in (
    latitude_e7, longitude_e7, altitude_cm, position_accuracy_cm,
  )):
    raise CacheValidationError("Position metadata must contain exact integers")
  if not -900_000_000 <= latitude_e7 <= 900_000_000:
    raise CacheValidationError("Latitude is outside the valid range")

  if not -1_800_000_000 <= longitude_e7 <= 1_800_000_000:
    raise CacheValidationError("Longitude is outside the valid range")

  if not -10_000_000 <= altitude_cm <= 10_000_000:
    raise CacheValidationError("Altitude is outside the valid range")

  if not 1 <= position_accuracy_cm <= MAX_RESTORE_POSITION_ACCURACY_CM:
    raise CacheValidationError(
      "Position accuracy is outside the valid range"
    )


class CacheFileState(Enum):
  ABSENT = auto()
  VALID = auto()
  INVALID = auto()


@dataclass(frozen=True)
class CacheFileInspection:
  generation: str
  path: Path
  state: CacheFileState
  cache: GpsAssistanceCache | None = None
  error: str | None = None


@dataclass(frozen=True)
class CacheSelection:
  generation: str
  cache: GpsAssistanceCache
  reason: str


@dataclass(frozen=True)
class CacheInventory:
  primary: CacheFileInspection
  previous: CacheFileInspection


@dataclass(frozen=True)
class RtcCacheEstimateSelection:
  generation: str
  cache: GpsAssistanceCache
  estimate: RtcEstimateSuccess


def select_rtc_estimate(
  inventory: CacheInventory,
  current_rtc_seconds: int | None,
) -> tuple[
  RtcCacheEstimateSelection | None,
  tuple[tuple[CacheFileInspection, RtcEstimateResult | None], ...],
]:
  evaluations: list[tuple[CacheFileInspection, RtcEstimateResult | None]] = []
  candidates: list[RtcCacheEstimateSelection] = []
  for inspection in (inventory.primary, inventory.previous):
    if inspection.cache is None:
      evaluations.append((inspection, None))
      continue
    result = evaluate_utc_from_rtc(
      inspection.cache, current_rtc_seconds=current_rtc_seconds,
    )
    evaluations.append((inspection, result))
    if isinstance(result, RtcEstimateSuccess):
      candidates.append(RtcCacheEstimateSelection(
        inspection.generation, inspection.cache, result,
      ))
  if not candidates:
    return None, tuple(evaluations)
  return max(
    candidates,
    key=lambda candidate: (
      candidate.estimate.estimated_utc,
      candidate.generation == "primary",
    ),
  ), tuple(evaluations)


class CachePromotionStatus(Enum):
  SAVED = auto()
  PRESERVED_EXISTING = auto()
  FAILED = auto()


class CachePromotionStage(StrEnum):
  CANDIDATE_WRITE = "candidate_write"
  CANDIDATE_FILE_FSYNC = "candidate_file_fsync"
  CANDIDATE_DIRECTORY_FSYNC = "candidate_directory_fsync"
  CANDIDATE_READBACK = "candidate_readback"
  CANDIDATE_VALIDATION = "candidate_validation"
  STORED_SELECTION = "stored_selection"
  PRIMARY_TO_PREVIOUS_REPLACE = "primary_to_previous_replace"
  FALLBACK_DIRECTORY_FSYNC = "fallback_directory_fsync"
  CANDIDATE_TO_PRIMARY_REPLACE = "candidate_to_primary_replace"
  FINAL_DIRECTORY_FSYNC = "final_directory_fsync"
  PRIMARY_READBACK = "primary_readback"
  PRIMARY_CANDIDATE_COMPARISON = "primary_candidate_comparison"
  PRESERVE_CANDIDATE_DELETE = "preserve_candidate_delete"
  PRESERVE_DIRECTORY_FSYNC = "preserve_directory_fsync"


@dataclass(frozen=True)
class CachePromotionResult:
  status: CachePromotionStatus
  reason: str
  selected: CacheSelection | None
  inventory: CacheInventory
  stage: CachePromotionStage
  fallback_generation: str | None = None
  selection_reason: str | None = None
  cleanup_failure: str | None = None


def _bounded_exception_detail(exc: BaseException) -> str:
  detail = f"{type(exc).__name__}:{exc}"
  notes = getattr(exc, "__notes__", ())
  if notes:
    detail += "; notes=" + " | ".join(str(note) for note in notes)
  return detail[:240]


class NavigationCacheStore:
  """Crash-tolerant fixed-file primary/previous navigation cache store."""

  def __init__(
    self,
    primary_path: Path,
    loader: Callable[..., GpsAssistanceCache] = load_cache,
  ) -> None:
    self.primary_path = primary_path
    self.previous_path = primary_path.with_name(
      f"{primary_path.stem}_previous{primary_path.suffix}"
    )
    self.candidate_path = primary_path.with_name(
      f"{primary_path.stem}_candidate.tmp"
    )
    self._load_cache = loader

  @staticmethod
  def _assert_safe_known_path(path: Path) -> None:
    try:
      information = path.lstat()
    except FileNotFoundError:
      return
    except OSError as exc:
      raise CacheValidationError("Cache file status is unavailable") from exc
    if stat.S_ISLNK(information.st_mode):
      raise CacheValidationError(f"Cache file is a symbolic link: {path.name}")
    if not stat.S_ISREG(information.st_mode):
      raise CacheValidationError(f"Cache file is not regular: {path.name}")

  def _inspect_one(
    self,
    generation: str,
    path: Path,
    receiver_fingerprint: str | None,
    now_utc: datetime | None,
  ) -> CacheFileInspection:
    try:
      cache = self._load_cache(
        path,
        expected_receiver_fingerprint=receiver_fingerprint,
        now_utc=now_utc,
      )
    except Exception as exc:
      try:
        path.lstat()
      except FileNotFoundError:
        absent = True
      except Exception:
        absent = False
      else:
        absent = False
      return CacheFileInspection(
        generation,
        path,
        CacheFileState.ABSENT if absent else CacheFileState.INVALID,
        error=str(exc),
      )
    return CacheFileInspection(generation, path, CacheFileState.VALID, cache)

  def inspect(
    self,
    receiver_fingerprint: str | None,
    now_utc: datetime | None,
  ) -> CacheInventory:
    return CacheInventory(
      self._inspect_one("primary", self.primary_path, receiver_fingerprint, now_utc),
      self._inspect_one("previous", self.previous_path, receiver_fingerprint, now_utc),
    )

  @staticmethod
  def select_inventory(
    inventory: CacheInventory,
    *,
    age_evidence: CacheAgeEvidence,
  ) -> CacheSelection | None:
    primary = inventory.primary.cache
    previous = inventory.previous.cache

    primary_tier = (
      None
      if primary is None
      else navigation_quality_tier(primary.quality)
    )
    previous_tier = (
      None
      if previous is None
      else navigation_quality_tier(previous.quality)
    )

    primary_ineligible = (
      primary is not None
      and primary_tier is None
    )
    previous_ineligible = (
      previous is not None
      and previous_tier is None
    )

    if primary_ineligible:
      primary = None
    if previous_ineligible:
      previous = None

    if primary is None and previous is None:
      return None

    if primary is None:
      reason = (
        "primary_ineligible_fallback"
        if primary_ineligible
        else "previous_only"
      )
      return CacheSelection(
        "previous",
        previous,
        reason,
      )

    if previous is None:
      reason = (
        "previous_ineligible"
        if previous_ineligible
        else "primary_only"
      )
      return CacheSelection(
        "primary",
        primary,
        reason,
      )

    assert primary_tier is not None
    assert previous_tier is not None

    freshness_delta = (
      previous.saved_at_utc
      - primary.saved_at_utc
    ).total_seconds()

    # Current-quality cache timestamps have trusted provenance at capture time,
    # so relative freshness remains meaningful even when current cache age
    # cannot be independently verified. Legacy caches retain tier-based
    # ordering because their timestamp provenance is not guaranteed.
    if (
      freshness_delta
      >= CACHE_TIER_FRESHNESS_WINDOW_SECONDS
      and previous_tier in (
        CacheQualityTier.USABLE,
        CacheQualityTier.QUALIFIED,
      )
    ):
      return CacheSelection(
        "previous",
        previous,
        "previous_materially_fresher",
      )

    if (
      freshness_delta
      <= -CACHE_TIER_FRESHNESS_WINDOW_SECONDS
      and primary_tier in (
        CacheQualityTier.USABLE,
        CacheQualityTier.QUALIFIED,
      )
    ):
      return CacheSelection(
        "primary",
        primary,
        "primary_materially_fresher",
      )

    tier_rank = {
      CacheQualityTier.LEGACY: 0,
      CacheQualityTier.USABLE: 1,
      CacheQualityTier.QUALIFIED: 2,
    }

    if tier_rank[previous_tier] > tier_rank[primary_tier]:
      return CacheSelection(
        "previous",
        previous,
        "previous_higher_quality_tier",
      )

    if tier_rank[primary_tier] > tier_rank[previous_tier]:
      return CacheSelection(
        "primary",
        primary,
        "primary_higher_quality_tier",
      )

    primary_quality = primary.quality
    previous_quality = previous.quality

    if (
      primary_quality is not None
      and previous_quality is not None
    ):
      if (
        previous_quality.gps_startup_ready
        and not primary_quality.gps_startup_ready
      ):
        return CacheSelection(
          "previous",
          previous,
          "previous_gps_startup_ready",
        )

      if (
        primary_quality.gps_startup_ready
        and not previous_quality.gps_startup_ready
      ):
        return CacheSelection(
          "primary",
          primary,
          "primary_gps_startup_ready",
        )

    if (
      primary_quality is not None
      and previous_quality is not None
      and navigation_quality_strictly_better(
        previous_quality,
        primary_quality,
      )
    ):
      return CacheSelection(
        "previous",
        previous,
        "previous_strictly_better",
      )

    return CacheSelection(
      "primary",
      primary,
      "primary_equal_or_incomparable",
    )

  def select_best(
    self,
    receiver_fingerprint: str | None,
    now_utc: datetime | None,
    *,
    age_evidence: CacheAgeEvidence | None = None,
  ) -> tuple[CacheSelection | None, CacheInventory]:
    if age_evidence is None:
      age_evidence = (
        CacheAgeEvidence.TRUSTED_UTC
        if now_utc is not None
        else CacheAgeEvidence.UNVERIFIED
      )

    if age_evidence.verified and now_utc is None:
      raise ValueError(
        "Verified cache-age evidence requires current UTC"
      )

    inventory = self.inspect(
      receiver_fingerprint,
      now_utc,
    )
    selection = self.select_inventory(
      inventory,
      age_evidence=age_evidence,
    )
    return selection, inventory

  def _remove_candidate(
    self,
    *,
    fsync_directory: bool,
    stage_callback: Callable[[CachePromotionStage], None] | None = None,
    delete_stage: CachePromotionStage = CachePromotionStage.PRESERVE_CANDIDATE_DELETE,
    fsync_stage: CachePromotionStage = CachePromotionStage.PRESERVE_DIRECTORY_FSYNC,
  ) -> bool:
    try:
      information = self.candidate_path.lstat()
    except FileNotFoundError:
      return False
    if stat.S_ISLNK(information.st_mode):
      raise CacheValidationError("Cache candidate is a symbolic link")
    if not stat.S_ISREG(information.st_mode):
      raise CacheValidationError("Cache candidate is not a regular file")
    if stage_callback is not None:
      stage_callback(delete_stage)
    self.candidate_path.unlink()
    if fsync_directory:
      if stage_callback is not None:
        stage_callback(fsync_stage)
      _fsync_cache_directory(self.primary_path.parent)
    return True

  def remove_stale_candidate(self) -> str | None:
    try:
      self._remove_candidate(fsync_directory=True)
    except Exception as exc:
      return f"{type(exc).__name__}:{exc}"
    return None

  def _write_candidate(
    self,
    candidate: GpsAssistanceCache,
    stage_callback: Callable[[CachePromotionStage], None] | None = None,
  ) -> GpsAssistanceCache:
    def set_stage(stage: CachePromotionStage) -> None:
      if stage_callback is not None:
        stage_callback(stage)

    set_stage(CachePromotionStage.CANDIDATE_WRITE)
    encoded = _encode_cache(candidate)
    self.primary_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    self._assert_safe_known_path(self.candidate_path)
    self._remove_candidate(fsync_directory=False)
    flags = (
      os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
      | _require_nofollow()
    )
    descriptor = os.open(self.candidate_path, flags, 0o600)
    candidate_file = None
    try:
      candidate_file = os.fdopen(descriptor, "wb")
    except BaseException as exc:
      try:
        os.close(descriptor)
      except OSError as close_exc:
        exc.add_note(f"Candidate descriptor close also failed: {type(close_exc).__name__}")
      raise
    operation_error: BaseException | None = None
    try:
      candidate_file.write(encoded)
      candidate_file.flush()
      set_stage(CachePromotionStage.CANDIDATE_FILE_FSYNC)
      os.fsync(candidate_file.fileno())
    except BaseException as exc:
      operation_error = exc
    try:
      if operation_error is None:
        set_stage(CachePromotionStage.CANDIDATE_WRITE)
      candidate_file.close()
    except BaseException as close_exc:
      if operation_error is None:
        raise
      operation_error.add_note(f"Candidate close also failed: {type(close_exc).__name__}")
    if operation_error is not None:
      raise operation_error
    set_stage(CachePromotionStage.CANDIDATE_DIRECTORY_FSYNC)
    _fsync_cache_directory(self.primary_path.parent)
    set_stage(CachePromotionStage.CANDIDATE_READBACK)
    return self._load_cache(
      self.candidate_path,
      expected_receiver_fingerprint=candidate.receiver_fingerprint,
      now_utc=candidate.saved_at_utc,
      require_complete=True,
      expected_receiver_cycle=candidate.receiver_cycle,
    )

  def promote(
    self,
    candidate: GpsAssistanceCache,
    receiver_fingerprint: str,
    trusted_now: datetime,
    active_receiver_cycle: int,
  ) -> CachePromotionResult:
    stage = CachePromotionStage.CANDIDATE_VALIDATION
    candidate_written = False
    fallback_generation = None
    selection_reason = None

    def set_stage(value: CachePromotionStage) -> None:
      nonlocal stage
      stage = value

    try:
      empty_inventory = self.inspect(receiver_fingerprint, trusted_now)
      validated_trusted_now = _validated_utc(
        trusted_now, "Cache promotion trusted time",
      )
      if (
        candidate.quality is None
        or not candidate.quality.usable_for_capture
        or candidate.receiver_fingerprint != receiver_fingerprint
        or candidate.receiver_cycle != active_receiver_cycle
        or candidate.saved_at_utc != validated_trusted_now
      ):
        return CachePromotionResult(
          CachePromotionStatus.FAILED,
          "candidate_not_usable_for_active_receiver_cycle",
          self.select_inventory(empty_inventory, age_evidence=CacheAgeEvidence.TRUSTED_UTC),
          empty_inventory,
          stage,
        )

      for path in (self.primary_path, self.previous_path, self.candidate_path):
        self._assert_safe_known_path(path)
      candidate_written = True
      validated_candidate = self._write_candidate(candidate, set_stage)
      set_stage(CachePromotionStage.CANDIDATE_VALIDATION)
      if validated_candidate != candidate:
        raise CacheValidationError("Candidate readback changed cache data")

      set_stage(CachePromotionStage.STORED_SELECTION)
      inventory = self.inspect(receiver_fingerprint, trusted_now)
      selected = self.select_inventory(inventory, age_evidence=CacheAgeEvidence.TRUSTED_UTC)
      fallback_generation = selected.generation if selected is not None else None
      selection_reason = selected.reason if selected is not None else "no_eligible_stored_cache"
      if selected is not None:
        replace, reason = compare_cache_quality(selected.cache, validated_candidate, trusted_now)
        if not replace:
          self._remove_candidate(
            fsync_directory=True,
            stage_callback=set_stage,
          )
          return CachePromotionResult(
            CachePromotionStatus.PRESERVED_EXISTING,
            reason,
            selected,
            inventory,
            stage,
            fallback_generation,
            selection_reason,
          )
      else:
        reason = "no_eligible_stored_cache"

      if selected is not None and selected.generation == "primary":
        set_stage(CachePromotionStage.PRIMARY_TO_PREVIOUS_REPLACE)
        os.replace(self.primary_path, self.previous_path)
        set_stage(CachePromotionStage.FALLBACK_DIRECTORY_FSYNC)
        _fsync_cache_directory(self.primary_path.parent)

      set_stage(CachePromotionStage.CANDIDATE_TO_PRIMARY_REPLACE)
      os.replace(self.candidate_path, self.primary_path)
      candidate_written = False
      set_stage(CachePromotionStage.FINAL_DIRECTORY_FSYNC)
      _fsync_cache_directory(self.primary_path.parent)
      set_stage(CachePromotionStage.PRIMARY_READBACK)
      primary = self._load_cache(
        self.primary_path,
        expected_receiver_fingerprint=receiver_fingerprint,
        now_utc=trusted_now,
        require_complete=True,
        expected_receiver_cycle=active_receiver_cycle,
      )
      set_stage(CachePromotionStage.PRIMARY_CANDIDATE_COMPARISON)
      if primary != validated_candidate:
        raise CacheValidationError("Promoted primary differs from validated candidate")
      final_inventory = self.inspect(receiver_fingerprint, trusted_now)
      final_selection = self.select_inventory(final_inventory, age_evidence=CacheAgeEvidence.TRUSTED_UTC)
      return CachePromotionResult(
        CachePromotionStatus.SAVED,
        reason,
        CacheSelection("primary", primary, "new_primary_saved")
        if final_selection is None else final_selection,
        final_inventory,
        stage,
        fallback_generation,
        selection_reason,
      )
    except Exception as exc:
      cleanup_failure = None
      if candidate_written:
        try:
          self._remove_candidate(fsync_directory=True)
        except Exception as cleanup_exc:
          cleanup_failure = _bounded_exception_detail(cleanup_exc)
      final_inventory = self.inspect(receiver_fingerprint, trusted_now)
      return CachePromotionResult(
        CachePromotionStatus.FAILED,
        f"{stage.value}_failed:{_bounded_exception_detail(exc)}",
        self.select_inventory(final_inventory, age_evidence=CacheAgeEvidence.TRUSTED_UTC),
        final_inventory,
        stage,
        fallback_generation,
        selection_reason,
        cleanup_failure,
      )


class ReliableFixTracker:
  def __init__(
    self,
    stability_seconds: float = MINIMUM_USABLE_RELIABLE_FIX_SECONDS,
    maximum_gap_seconds: float = 2.0,
  ) -> None:
    self.stability_seconds = stability_seconds
    self.maximum_gap_seconds = maximum_gap_seconds

    self._reliable_since: float | None = None
    self._last_update: float | None = None
    self._latest_fix: NavPvtFix | None = None

  def reset(self) -> None:
    self._reliable_since = None
    self._last_update = None
    self._latest_fix = None

  def update(
    self,
    fix: NavPvtFix,
    monotonic_time: float,
  ) -> None:
    if not fix.reliable:
      self.reset()
      return

    if (
      self._last_update is None
      or monotonic_time < self._last_update
      or monotonic_time - self._last_update
      > self.maximum_gap_seconds
    ):
      self._reliable_since = monotonic_time

    self._last_update = monotonic_time
    self._latest_fix = fix

  def stable_fix(
    self,
    monotonic_time: float,
  ) -> NavPvtFix | None:
    if (
      self._reliable_since is None
      or self._last_update is None
      or self._latest_fix is None
      or monotonic_time < self._last_update
      or monotonic_time - self._last_update
      > self.maximum_gap_seconds
      or monotonic_time - self._reliable_since
      < self.stability_seconds
    ):
      return None

    return self._latest_fix


class CaptureQualityTracker:
  def __init__(self) -> None:
    self.reset()

  def reset(self) -> None:
    self._reliable_since: float | None = None
    self._last_fix_time: float | None = None
    self._latest_fix: NavPvtFix | None = None
    self._orbit_since: float | None = None
    self._last_nav_sat_time: float | None = None
    self._latest_nav_sat: NavSatQuality | None = None

  def update_fix(self, fix: NavPvtFix, now: float) -> str | None:
    reset_reason = None
    if not fix.reliable:
      if self._reliable_since is not None:
        reset_reason = "unreliable_nav_pvt"
      self._reliable_since = None
      self._latest_fix = None
    elif self._last_fix_time is None:
      self._reliable_since = now
      self._latest_fix = fix
    elif now < self._last_fix_time or now - self._last_fix_time > MAXIMUM_NAV_PVT_GAP_SECONDS:
      reset_reason = "nav_pvt_monotonic_reversal" if self._last_fix_time is not None and now < self._last_fix_time else "nav_pvt_gap"
      self._reliable_since = now
      self._latest_fix = fix
    else:
      if self._reliable_since is None:
        self._reliable_since = now
      self._latest_fix = fix
    self._last_fix_time = now
    return reset_reason

  def update_nav_sat(self, report: NavSatQuality, now: float) -> str | None:
    reset_reason = None
    gap = self._last_nav_sat_time is not None and now - self._last_nav_sat_time > MAXIMUM_NAV_SAT_AGE_SECONDS
    reversal = self._last_nav_sat_time is not None and now < self._last_nav_sat_time
    if not report.passes_thresholds:
      if self._orbit_since is not None:
        reset_reason = "orbit_threshold_failure"
      self._orbit_since = None
    elif gap or reversal:
      reset_reason = "nav_sat_monotonic_reversal" if reversal else "nav_sat_gap"
      self._orbit_since = now
    elif self._orbit_since is None:
      self._orbit_since = now
    self._last_nav_sat_time = now
    self._latest_nav_sat = report
    return reset_reason

  def quality(self, now: float, context: str) -> NavigationQuality | None:
    if (
      self._reliable_since is None
      or self._last_fix_time is None
      or self._latest_fix is None
      or self._last_nav_sat_time is None
      or self._latest_nav_sat is None
      or now < self._last_fix_time
      or now < self._last_nav_sat_time
      or now - self._last_fix_time > MAXIMUM_NAV_PVT_GAP_SECONDS
      or now - self._last_nav_sat_time > MAXIMUM_NAV_SAT_AGE_SECONDS
    ):
      return None
    report = self._latest_nav_sat
    return NavigationQuality(
      quality_version=QUALITY_VERSION,
      policy_version=QUALITY_POLICY_VERSION,
      capture_context=context,
      continuous_reliable_fix_seconds=now - self._reliable_since,
      continuous_orbit_quality_seconds=(
        0.0 if self._orbit_since is None else now - self._orbit_since
      ),
      gps_satellites_known=report.gps_satellites_known,
      glonass_satellites_known=report.glonass_satellites_known,
      gps_ephemeris_available=report.gps_ephemeris_available,
      glonass_ephemeris_available=report.glonass_ephemeris_available,
      satellites_used=report.satellites_used,
      gps_almanac_available=report.gps_almanac_available,
      glonass_almanac_available=report.glonass_almanac_available,
      assistnow_offline_available=report.assistnow_offline_available,
      orbit_source_counts=dict(report.orbit_source_counts),
      gps_almanac_satellite_ids=tuple(
        sorted(report.gps_almanac_satellite_ids)
      ),
    )

  def eligible(self, now: float) -> bool:
    quality = self.quality(now, "onroad")
    return quality is not None and quality.passes_policy

  def orbit_eligible(self, now: float) -> bool:
    return (
      self._orbit_since is not None
      and self._last_nav_sat_time is not None
      and self._latest_nav_sat is not None
      and self._latest_nav_sat.passes_thresholds
      and now >= self._last_nav_sat_time
      and now - self._last_nav_sat_time <= MAXIMUM_NAV_SAT_AGE_SECONDS
      and now - self._orbit_since >= MINIMUM_ORBIT_QUALITY_SECONDS
    )

  @property
  def latest_fix(self) -> NavPvtFix | None:
    return self._latest_fix

  @property
  def latest_nav_sat(self) -> NavSatQuality | None:
    return self._latest_nav_sat

  @property
  def latest_nav_sat_time(self) -> float | None:
    return self._last_nav_sat_time


class NavigationDatabaseDumpCollector:
  def __init__(
    self,
    timeout_seconds: float = 5.0,
  ) -> None:
    self.timeout_seconds = timeout_seconds
    self._active = False
    self._deadline = 0.0
    self._frames: list[bytes] = []
    self._total_bytes = 0

  @property
  def active(self) -> bool:
    return self._active

  def start(self, monotonic_time: float) -> None:
    self._active = True
    self._deadline = monotonic_time + self.timeout_seconds
    self._frames = []
    self._total_bytes = 0

  def cancel(self) -> None:
    self._active = False
    self._deadline = 0.0
    self._frames = []
    self._total_bytes = 0

  def expired(self, monotonic_time: float) -> bool:
    return self._active and monotonic_time >= self._deadline

  def feed(
    self,
    frame: bytes,
  ) -> tuple[bytes, ...] | None:
    if not self._active:
      return None

    if (
      validate_ubx_frame(frame)
      and frame[2] == UBX_CLASS_MGA
      and frame[3] == UBX_ID_MGA_DBD
    ):
      self._total_bytes += len(frame)

      if self._total_bytes > MAX_DATABASE_BYTES:
        self.cancel()
        raise CacheValidationError(
          "Navigation database exceeds the size limit"
        )

      self._frames.append(frame)
      return None

    acknowledgment = parse_mga_ack(frame)

    if (
      acknowledgment is None
      or acknowledgment.message_id != UBX_ID_MGA_DBD
    ):
      return None

    if not acknowledgment.accepted:
      info_code = acknowledgment.info_code
      self.cancel()
      raise CacheValidationError(f"Navigation database poll was rejected with infoCode {info_code}")

    expected_count = int.from_bytes(
      acknowledgment.message_payload_start,
      "little",
    )
    database_frames = tuple(self._frames)

    if expected_count != len(database_frames):
      self.cancel()
      raise CacheValidationError(f"Incomplete navigation database dump: expected {expected_count}, received {len(database_frames)}")

    if not database_frames:
      self.cancel()
      raise CacheValidationError(
        "Receiver returned an empty navigation database"
      )

    _validate_database_frames(database_frames)
    self.cancel()

    return database_frames
