from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import Enum, StrEnum
from math import isfinite
from pathlib import Path
from typing import Any, TypeGuard

from openpilot.common.swaglog import cloudlog
from openpilot.system.ubloxd.receiver_time_provenance import is_mga_time_assistance_message
from openpilot.system.ubloxd.rtc_time_observation import CrossBootRtcObservation, RtcObservationState
from openpilot.system.ubloxd.trusted_time_anchor import TimeProvenance, TrustedTimeSource
from openpilot.system.ubloxd.trusted_time_authority import TimeAuthorityEvaluation, TimeAuthorityRejectionReason
from openpilot.system.ubloxd.yuma_almanac import (
  YumaAlmanac,
  YumaAlmanacError,
  validate_yuma_reference_time,
)
from openpilot.system.ubloxd.yuma_almanac_store import YUMA_ALMANAC_CACHE_PATH, StoredYumaAlmanac, load_yuma_almanac
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  YumaAlmanacTransmitResult,
  YumaAssistanceStateUnavailableError,
  transmit_public_yuma_almanac,
)


PROVISIONAL_YUMA_MAX_RTC_ELAPSED_SECONDS = 24 * 60 * 60
PROVISIONAL_YUMA_MAX_UNCERTAINTY_SECONDS = 30.0
PROVISIONAL_YUMA_TRANSMIT_BUDGET_SECONDS = 8.0
PROVISIONAL_YUMA_TRANSMIT_MARGIN_SECONDS = 1.0
PROVISIONAL_YUMA_DISABLE_STATE_VERSION = 1
PROVISIONAL_YUMA_DISABLE_STATE_PATH = Path(
  "/data/gps_assistance/provisional_yuma_disabled.json"
)
PROVISIONAL_YUMA_DISABLE_REASON_VALIDATION_DISAGREES = (
  "independent_validation_disagrees"
)

PROVISIONAL_YUMA_DECISION_STATE_VERSION = 1
PROVISIONAL_YUMA_DECISION_STATE_PATH = Path(
  "/data/gps_assistance/provisional_yuma_last_decision.json"
)


def _telemetry_json_value(value: Any) -> Any:
  if value is None or isinstance(value, (bool, int, float, str)):
    return value
  if isinstance(value, datetime):
    if value.tzinfo is None or value.utcoffset() is None:
      return value.isoformat()
    return value.astimezone(UTC).isoformat()
  if isinstance(value, Enum):
    return value.value
  if isinstance(value, Path):
    return str(value)
  if is_dataclass(value):
    return {
      field.name: _telemetry_json_value(getattr(value, field.name))
      for field in fields(value)
    }
  if isinstance(value, dict):
    return {
      str(key): _telemetry_json_value(item)
      for key, item in value.items()
    }
  if isinstance(value, (tuple, list, set, frozenset)):
    return [_telemetry_json_value(item) for item in value]
  return str(value)


def _atomic_write_private_json(
  path: Path,
  payload: dict[str, Any],
) -> None:
  encoded = (
    json.dumps(payload, sort_keys=True, separators=(",", ":"))
    + "\n"
  ).encode("utf-8")
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{path.name}.",
    suffix=".tmp",
    dir=path.parent,
  )
  temporary = Path(temporary_name)
  try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
      descriptor = -1
      stream.write(encoded)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
      os.fsync(directory_descriptor)
    finally:
      os.close(directory_descriptor)
  finally:
    if descriptor >= 0:
      os.close(descriptor)
    temporary.unlink(missing_ok=True)


def store_provisional_yuma_decision_event(
  event: str,
  *,
  current_boot_id: str,
  receiver_cycle: int,
  observed_at: float,
  observation: object | None = None,
  authority: object | None = None,
  decision: object | None = None,
  accepted: bool | None = None,
  outcome: object | None = None,
  validation: object | None = None,
  path: Path = PROVISIONAL_YUMA_DECISION_STATE_PATH,
) -> None:
  if not isinstance(event, str) or not event.strip():
    raise ValueError("event must be a non-empty string")
  if not _valid_boot_id(current_boot_id):
    raise ValueError(
      "current_boot_id must be a non-empty single-line string"
    )
  if (
    isinstance(receiver_cycle, bool)
    or not isinstance(receiver_cycle, int)
    or receiver_cycle < 0
  ):
    raise ValueError(
      "receiver_cycle must be a non-negative integer"
    )
  if not _valid_nonnegative_finite(observed_at):
    raise ValueError(
      "observed_at must be finite and non-negative"
    )

  normalized_boot_id = current_boot_id.strip()
  normalized_event = event.strip()
  payload: dict[str, Any] = {}
  previous_error: str | None = None
  try:
    previous = json.loads(path.read_text(encoding="utf-8"))
    if (
      isinstance(previous, dict)
      and previous.get("version")
      == PROVISIONAL_YUMA_DECISION_STATE_VERSION
      and previous.get("boot_id") == normalized_boot_id
      and previous.get("receiver_cycle") == receiver_cycle
    ):
      payload = previous
  except FileNotFoundError:
    pass
  except (OSError, json.JSONDecodeError) as exc:
    previous_error = f"{type(exc).__name__}: {exc}"

  events = payload.get("events")
  if not isinstance(events, dict):
    events = {}

  event_payload: dict[str, Any] = {
    "observed_at_monotonic": float(observed_at),
  }
  for key, value in (
    ("observation", observation),
    ("authority", authority),
    ("decision", decision),
    ("accepted", accepted),
    ("outcome", outcome),
    ("validation", validation),
  ):
    if value is not None:
      event_payload[key] = _telemetry_json_value(value)

  events[normalized_event] = event_payload
  payload = {
    "version": PROVISIONAL_YUMA_DECISION_STATE_VERSION,
    "boot_id": normalized_boot_id,
    "receiver_cycle": receiver_cycle,
    "updated_event": normalized_event,
    "updated_at_monotonic": float(observed_at),
    "events": events,
  }
  if previous_error is not None:
    payload["previous_state_error"] = previous_error
  _atomic_write_private_json(path, payload)


YumaMessageSender = Callable[[bytes], None]
YumaCacheLoader = Callable[[Path], StoredYumaAlmanac]
YumaReferenceValidator = Callable[[YumaAlmanac, datetime], datetime]
MonotonicClock = Callable[[], float]


class ProvisionalYumaRejection(StrEnum):
  AUTHORIZED_TIME_AVAILABLE = "authorized_time_available"
  AUTHORITY_REJECTION_NOT_CROSS_BOOT = "authority_rejection_not_cross_boot"
  OBSERVATION_NOT_READY = "observation_not_ready"
  CANDIDATE_UNAVAILABLE = "candidate_unavailable"
  TICK_NOT_CONSISTENT = "tick_not_consistent"
  ANCHOR_SELECTION_MISMATCH = "anchor_selection_mismatch"
  ANCHOR_NOT_AUTHORIZED = "anchor_not_authorized"
  ANCHOR_NOT_INDEPENDENT = "anchor_not_independent"
  ANCHOR_SOURCE_NOT_INDEPENDENT = "anchor_source_not_independent"
  CROSS_BOOT_ID_INVALID = "cross_boot_id_invalid"
  RTC_ELAPSED_ABOVE_MAXIMUM = "rtc_elapsed_above_maximum"
  UNCERTAINTY_ABOVE_MAXIMUM = "uncertainty_above_maximum"
  RTC_VOLTAGE_FAULT = "rtc_voltage_fault"
  RECEIVER_CYCLE_INVALID = "receiver_cycle_invalid"


@dataclass(frozen=True)
class ProvisionalYumaReferenceTime:
  utc: datetime
  observed_at: float
  uncertainty_seconds: float
  receiver_cycle: int
  anchor_generation: str
  anchor_sequence: int
  anchor_source: TrustedTimeSource
  anchor_provenance: TimeProvenance
  anchor_boot_id: str
  current_boot_id: str
  rtc_elapsed_seconds: int


@dataclass(frozen=True)
class ProvisionalYumaReferenceDecision:
  reference: ProvisionalYumaReferenceTime | None
  rejection: ProvisionalYumaRejection | None

  @property
  def eligible(self) -> bool:
    return self.reference is not None and self.rejection is None


@dataclass(frozen=True)
class ProvisionalYumaBootDisableState:
  disabled: bool
  current_boot_id: str | None
  stored_boot_id: str | None
  reason: str | None
  error: str | None


@dataclass(frozen=True)
class ProvisionalYumaTransmissionOutcome:
  reference: ProvisionalYumaReferenceTime
  attempted_at: float
  elapsed_ms: float
  satellite_ids: tuple[int, ...]
  snapshot_sha256: str | None
  validated_reference_utc: datetime | None
  receiver_write_attempted: bool
  transmit_result: YumaAlmanacTransmitResult | None
  error: str | None
  time_assistance_written: bool = False
  cache_quality_changed: bool = False
  anchor_written: bool = False
  system_clock_changed: bool = False
  receiver_reset: bool = False


def _valid_nonnegative_finite(value: object) -> bool:
  return type(value) in (int, float) and not isinstance(value, bool) and isfinite(value) and value >= 0.0


def _valid_boot_id(value: object) -> TypeGuard[str]:
  return isinstance(value, str) and bool(value.strip()) and "\n" not in value and "\r" not in value


def load_provisional_yuma_boot_disable_state(
  current_boot_id: str | None,
  *,
  path: Path = PROVISIONAL_YUMA_DISABLE_STATE_PATH,
) -> ProvisionalYumaBootDisableState:
  if not _valid_boot_id(current_boot_id):
    return ProvisionalYumaBootDisableState(
      True, current_boot_id, None, None, "invalid_current_boot_id"
    )
  normalized_current_boot_id = current_boot_id.strip()
  try:
    payload = json.loads(path.read_text(encoding="utf-8"))
  except FileNotFoundError:
    return ProvisionalYumaBootDisableState(
      False, normalized_current_boot_id, None, None, None
    )
  except (OSError, json.JSONDecodeError) as exc:
    return ProvisionalYumaBootDisableState(
      True, normalized_current_boot_id, None, None,
      f"{type(exc).__name__}: {exc}",
    )

  if not isinstance(payload, dict):
    return ProvisionalYumaBootDisableState(
      True, normalized_current_boot_id, None, None, "state_not_object"
    )
  stored_boot_id = payload.get("boot_id")
  reason = payload.get("reason")
  if payload.get("version") != PROVISIONAL_YUMA_DISABLE_STATE_VERSION:
    return ProvisionalYumaBootDisableState(
      True, normalized_current_boot_id, None, None, "unsupported_version"
    )
  if not _valid_boot_id(stored_boot_id):
    return ProvisionalYumaBootDisableState(
      True, normalized_current_boot_id, None, None, "invalid_stored_boot_id"
    )
  if not isinstance(reason, str) or not reason.strip():
    return ProvisionalYumaBootDisableState(
      True, normalized_current_boot_id, stored_boot_id.strip(), None,
      "invalid_reason",
    )
  normalized_stored_boot_id = stored_boot_id.strip()
  normalized_reason = reason.strip()
  return ProvisionalYumaBootDisableState(
    normalized_stored_boot_id == normalized_current_boot_id,
    normalized_current_boot_id,
    normalized_stored_boot_id,
    normalized_reason,
    None,
  )


def store_provisional_yuma_boot_disable_state(
  current_boot_id: str,
  reason: str,
  *,
  path: Path = PROVISIONAL_YUMA_DISABLE_STATE_PATH,
) -> None:
  if not _valid_boot_id(current_boot_id):
    raise ValueError("current_boot_id must be a non-empty single-line string")
  if not isinstance(reason, str) or not reason.strip():
    raise ValueError("reason must be a non-empty string")
  payload = (
    json.dumps(
      {
        "version": PROVISIONAL_YUMA_DISABLE_STATE_VERSION,
        "boot_id": current_boot_id.strip(),
        "reason": reason.strip(),
      },
      sort_keys=True,
      separators=(",", ":"),
    )
    + "\n"
  ).encode("utf-8")
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{path.name}.",
    suffix=".tmp",
    dir=path.parent,
  )
  temporary = Path(temporary_name)
  try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb", closefd=True) as stream:
      descriptor = -1
      stream.write(payload)
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
      os.fsync(directory_descriptor)
    finally:
      os.close(directory_descriptor)
  finally:
    if descriptor >= 0:
      os.close(descriptor)
    try:
      temporary.unlink()
    except FileNotFoundError:
      pass


def _independent_anchor_pair(source: object, provenance: object) -> bool:
  return (
    source is TrustedTimeSource.SYSTEM_SYNCHRONIZED
    and provenance is TimeProvenance.NETWORK_INDEPENDENT
  ) or (
    source is TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
    and provenance is TimeProvenance.GNSS_INDEPENDENT
  )


def evaluate_provisional_yuma_reference(
  observation: CrossBootRtcObservation,
  authority: TimeAuthorityEvaluation,
  *,
  receiver_cycle: int,
) -> ProvisionalYumaReferenceDecision:
  if isinstance(receiver_cycle, bool) or not isinstance(receiver_cycle, int) or receiver_cycle < 0:
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.RECEIVER_CYCLE_INVALID)
  if authority.authorized_time is not None:
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.AUTHORIZED_TIME_AVAILABLE)
  if authority.rejection_reason is not TimeAuthorityRejectionReason.CROSS_BOOT_CONTINUITY_UNPROVABLE:
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.AUTHORITY_REJECTION_NOT_CROSS_BOOT)
  if observation.state is not RtcObservationState.OBSERVED:
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.OBSERVATION_NOT_READY)
  candidate = observation.candidate
  if candidate is None:
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.CANDIDATE_UNAVAILABLE)
  if observation.tick_consistent is not True:
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.TICK_NOT_CONSISTENT)
  if (
    candidate.anchor_generation != authority.selected_anchor_generation
    or candidate.anchor_sequence != authority.selected_anchor_sequence
  ):
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.ANCHOR_SELECTION_MISMATCH)
  if candidate.anchor_authorized is not True:
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.ANCHOR_NOT_AUTHORIZED)
  if candidate.anchor_independent is not True:
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.ANCHOR_NOT_INDEPENDENT)
  if not _independent_anchor_pair(candidate.anchor_source, candidate.anchor_provenance):
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.ANCHOR_SOURCE_NOT_INDEPENDENT)
  if (
    not candidate.anchor_boot_id
    or not candidate.current_boot_id
    or candidate.anchor_boot_id == candidate.current_boot_id
  ):
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.CROSS_BOOT_ID_INVALID)
  if (
    candidate.rtc_voltage_status_supported
    and candidate.rtc_voltage_status_flags != 0
  ):
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.RTC_VOLTAGE_FAULT)
  if candidate.rtc_elapsed_seconds > PROVISIONAL_YUMA_MAX_RTC_ELAPSED_SECONDS:
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.RTC_ELAPSED_ABOVE_MAXIMUM)
  if (
    not _valid_nonnegative_finite(candidate.uncertainty_seconds)
    or candidate.uncertainty_seconds > PROVISIONAL_YUMA_MAX_UNCERTAINTY_SECONDS
  ):
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.UNCERTAINTY_ABOVE_MAXIMUM)
  observed_at = observation.second_observed_at
  if not _valid_nonnegative_finite(observed_at):
    return ProvisionalYumaReferenceDecision(None, ProvisionalYumaRejection.OBSERVATION_NOT_READY)

  return ProvisionalYumaReferenceDecision(
    ProvisionalYumaReferenceTime(
      utc=candidate.candidate_utc.astimezone(UTC),
      observed_at=float(observed_at),
      uncertainty_seconds=float(candidate.uncertainty_seconds),
      receiver_cycle=receiver_cycle,
      anchor_generation=candidate.anchor_generation,
      anchor_sequence=candidate.anchor_sequence,
      anchor_source=candidate.anchor_source,
      anchor_provenance=candidate.anchor_provenance,
      anchor_boot_id=candidate.anchor_boot_id,
      current_boot_id=candidate.current_boot_id,
      rtc_elapsed_seconds=candidate.rtc_elapsed_seconds,
    ),
    None,
  )


def transmit_provisional_yuma_reference(
  reference: ProvisionalYumaReferenceTime,
  send_message: YumaMessageSender,
  *,
  path: Path = YUMA_ALMANAC_CACHE_PATH,
  cache_loader: YumaCacheLoader = load_yuma_almanac,
  reference_validator: YumaReferenceValidator = validate_yuma_reference_time,
  monotonic: MonotonicClock = time.monotonic,
) -> ProvisionalYumaTransmissionOutcome:
  started_at = monotonic()
  satellite_ids: tuple[int, ...] = ()
  snapshot_sha256: str | None = None
  validated_reference_utc: datetime | None = None
  receiver_write_attempted = False

  def tracked_send_message(message: bytes) -> None:
    nonlocal receiver_write_attempted
    try:
      send_message(message)
    except YumaAssistanceStateUnavailableError:
      raise
    except Exception:
      receiver_write_attempted = True
      raise
    receiver_write_attempted = True

  try:
    stored = cache_loader(path)
    validated_reference_utc = reference_validator(stored.almanac, reference.utc)
    frames = tuple(stored.almanac.frames)
    if any(is_mga_time_assistance_message(frame) for frame in frames):
      raise YumaAlmanacError("YUMA snapshot unexpectedly contains an MGA time message")
    satellite_ids = tuple(sorted({frame[8] for frame in frames}))
    if not satellite_ids:
      raise YumaAlmanacError("YUMA snapshot contains no GPS almanac PRNs")
    snapshot_sha256 = hashlib.sha256(stored.almanac.ubx_data).hexdigest()
    transmit_result = transmit_public_yuma_almanac(
      tracked_send_message,
      trusted_now=reference.utc,
      satellite_ids=frozenset(satellite_ids),
      path=path,
      stored_almanac=stored,
      max_duration_seconds=PROVISIONAL_YUMA_TRANSMIT_BUDGET_SECONDS,
      minimum_remaining_seconds=PROVISIONAL_YUMA_TRANSMIT_MARGIN_SECONDS,
      monotonic=monotonic,
    )
    error = None
  except (OSError, YumaAlmanacError, ValueError) as exc:
    transmit_result = None
    error = f"{type(exc).__name__}: {exc}"
  except Exception as exc:
    cloudlog.exception("Unexpected provisional public YUMA transmission failure")
    transmit_result = None
    error = f"{type(exc).__name__}: {exc}"

  completed_at = monotonic()
  return ProvisionalYumaTransmissionOutcome(
    reference=reference,
    attempted_at=started_at,
    elapsed_ms=max(0.0, completed_at - started_at) * 1000.0,
    satellite_ids=satellite_ids,
    snapshot_sha256=snapshot_sha256,
    validated_reference_utc=validated_reference_utc,
    receiver_write_attempted=receiver_write_attempted,
    transmit_result=transmit_result,
    error=error,
  )
