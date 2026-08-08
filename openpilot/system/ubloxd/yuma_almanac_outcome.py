import json
import os
import tempfile
from datetime import UTC, datetime
from enum import Enum
from math import isfinite
from pathlib import Path
from typing import Any


YUMA_LAST_OUTCOME_VERSION = 3
YUMA_LAST_OUTCOME_PATH = Path(
  "/data/gps_assistance/public_yuma_last_outcome.json"
)
YUMA_LAST_OUTCOME_MAX_BYTES = 64 * 1024


class YumaOutcomeStoreError(ValueError):
  pass


def _enum_value(value: object | None) -> str | None:
  if value is None:
    return None
  if isinstance(value, Enum):
    return str(value.value)
  return str(value)


def _timestamp(value: datetime | None, field: str) -> str | None:
  if value is None:
    return None
  if not isinstance(value, datetime):
    raise YumaOutcomeStoreError(f"{field} must be a datetime or None")
  if value.tzinfo is None or value.utcoffset() is None:
    raise YumaOutcomeStoreError(f"{field} must be timezone-aware")
  return value.astimezone(UTC).isoformat()


def _optional_text(
  value: object | None,
  field: str,
  *,
  maximum_length: int = 240,
) -> str | None:
  if value is None:
    return None
  if not isinstance(value, str):
    raise YumaOutcomeStoreError(f"{field} must be a string or None")
  normalized = value.strip()
  if not normalized:
    return None
  return normalized[:maximum_length]


def _optional_number(
  value: object | None,
  field: str,
) -> int | float | None:
  if value is None:
    return None
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    raise YumaOutcomeStoreError(f"{field} must be numeric or None")
  if not isfinite(value) or value < 0:
    raise YumaOutcomeStoreError(
      f"{field} must be non-negative and finite"
    )
  return value


def _optional_count(
  value: object | None,
  field: str,
) -> int | None:
  if value is None:
    return None
  if isinstance(value, bool) or not isinstance(value, int) or value < 0:
    raise YumaOutcomeStoreError(
      f"{field} must be a non-negative integer or None"
    )
  return value


def _optional_bool(
  value: object | None,
  field: str,
) -> bool | None:
  if value is None:
    return None
  if not isinstance(value, bool):
    raise YumaOutcomeStoreError(f"{field} must be a bool or None")
  return value


def _text_sequence(
  value: object,
  field: str,
) -> list[str]:
  try:
    values = tuple(value)
  except TypeError as exc:
    raise YumaOutcomeStoreError(
      f"{field} must be an iterable of strings"
    ) from exc
  normalized = []
  for item in values:
    text = _optional_text(item, field)
    if text is None:
      raise YumaOutcomeStoreError(
        f"{field} contains an empty value"
      )
    normalized.append(text)
  return normalized


def _satellite_ids(
  value: object,
  field: str,
) -> list[int]:
  try:
    satellite_ids = tuple(value)
  except TypeError as exc:
    raise YumaOutcomeStoreError(
      f"{field} must be an iterable of satellite IDs"
    ) from exc
  if any(
    isinstance(satellite_id, bool)
    or not isinstance(satellite_id, int)
    or not 1 <= satellite_id <= 32
    for satellite_id in satellite_ids
  ):
    raise YumaOutcomeStoreError(
      f"{field} contains an invalid satellite ID"
    )
  return sorted(set(satellite_ids))


def _satellite_sequence(
  value: object,
  field: str,
) -> list[int]:
  try:
    satellite_ids = tuple(value)
  except TypeError as exc:
    raise YumaOutcomeStoreError(
      f"{field} must be an iterable of satellite IDs"
    ) from exc
  if any(
    isinstance(satellite_id, bool)
    or not isinstance(satellite_id, int)
    or not 1 <= satellite_id <= 32
    for satellite_id in satellite_ids
  ):
    raise YumaOutcomeStoreError(
      f"{field} contains an invalid satellite ID"
    )
  return list(satellite_ids)


def _attempt_counts(attempts: tuple[object, ...]) -> dict[str, int]:
  counts: dict[str, int] = {}
  for attempt in attempts:
    result = getattr(attempt, "transmit_result", None)
    if result is None:
      continue
    for satellite_id in _satellite_sequence(
      getattr(result, "attempted_satellite_ids", ()),
      "attempted_satellite_ids",
    ):
      key = str(satellite_id)
      counts[key] = counts.get(key, 0) + 1
  return counts


def _aggregate_satellite_ids(
  attempts: tuple[object, ...],
  field: str,
) -> list[int]:
  values: list[int] = []
  for attempt in attempts:
    result = getattr(attempt, "transmit_result", None)
    if result is None:
      continue
    values.extend(
      _satellite_sequence(
        getattr(result, field, ()),
        field,
      )
    )
  return sorted(set(values))


def _transmit_result_payload(result: object | None) -> dict[str, Any] | None:
  if result is None:
    return None
  return {
    "status": _enum_value(getattr(result, "status", None)),
    "requested_satellite_ids": _satellite_ids(
      getattr(result, "requested_satellite_ids", ()),
      "requested_satellite_ids",
    ),
    "attempted_satellite_ids": _satellite_sequence(
      getattr(result, "attempted_satellite_ids", ()),
      "attempted_satellite_ids",
    ),
    "accepted_satellite_ids": _satellite_ids(
      getattr(result, "accepted_satellite_ids", ()),
      "accepted_satellite_ids",
    ),
    "failed_satellite_ids": _satellite_ids(
      getattr(result, "failed_satellite_ids", ()),
      "failed_satellite_ids",
    ),
    "rejected_satellite_ids": _satellite_ids(
      getattr(result, "rejected_satellite_ids", ()),
      "rejected_satellite_ids",
    ),
    "timed_out_satellite_ids": _satellite_ids(
      getattr(result, "timed_out_satellite_ids", ()),
      "timed_out_satellite_ids",
    ),
    "deferred_satellite_ids": _satellite_ids(
      getattr(result, "deferred_satellite_ids", ()),
      "deferred_satellite_ids",
    ),
    "unavailable_satellite_ids": _satellite_ids(
      getattr(result, "unavailable_satellite_ids", ()),
      "unavailable_satellite_ids",
    ),
    "reference_time_utc": _timestamp(
      getattr(result, "reference_time_utc", None),
      "reference_time_utc",
    ),
    "downloaded_at_utc": _timestamp(
      getattr(result, "downloaded_at_utc", None),
      "downloaded_at_utc",
    ),
  }


def _attempt_payload(attempt: object) -> dict[str, Any]:
  return {
    "attempt": _optional_count(
      getattr(attempt, "attempt", None),
      "attempt",
    ),
    "elapsed_ms": _optional_number(
      getattr(attempt, "elapsed_ms", None),
      "elapsed_ms",
    ),
    "error": _optional_text(
      getattr(attempt, "error", None),
      "attempt_error",
    ),
    "transmit_result": _transmit_result_payload(
      getattr(attempt, "transmit_result", None)
    ),
  }


def serialize_yuma_supplementation_outcome(
  outcome: object,
  *,
  commit: str | None,
  receiver_cycle: int,
  recorded_at_utc: datetime | None,
) -> bytes:
  if isinstance(receiver_cycle, bool) or not isinstance(receiver_cycle, int) or receiver_cycle < 0:
    raise YumaOutcomeStoreError(
      "receiver_cycle must be a non-negative integer"
    )

  plan = getattr(outcome, "plan", None)
  if plan is None:
    raise YumaOutcomeStoreError("outcome plan is missing")

  attempts = tuple(getattr(outcome, "attempt_history", ()))
  restored_almanac_ids = getattr(
    outcome,
    "restored_gps_almanac_satellite_ids",
    None,
  )
  payload = {
    "version": YUMA_LAST_OUTCOME_VERSION,
    "recorded_at_utc": _timestamp(
      recorded_at_utc,
      "recorded_at_utc",
    ),
    "completion_utc": _timestamp(
      getattr(outcome, "completion_utc", None),
      "completion_utc",
    ),
    "commit": _optional_text(commit, "commit", maximum_length=128),
    "receiver_cycle": receiver_cycle,
    "feature_enabled": _optional_bool(
      getattr(outcome, "feature_enabled", None),
      "feature_enabled",
    ),
    "terminal": _optional_bool(
      getattr(outcome, "terminal", None),
      "terminal",
    ),
    "retry_pending": _optional_bool(
      getattr(outcome, "retry_pending", None),
      "retry_pending",
    ),
    "cancellation_reason": _enum_value(
      getattr(outcome, "cancellation_reason", None)
    ),
    "time": {
      "anchor_source": _optional_text(
        getattr(outcome, "time_anchor_source", None),
        "time_anchor_source",
      ),
      "anchor_utc": _timestamp(
        getattr(outcome, "time_anchor_utc", None),
        "time_anchor_utc",
      ),
      "trusted_now_utc": _timestamp(
        getattr(outcome, "trusted_now_utc", None),
        "trusted_now_utc",
      ),
      "trusted_time_wait_expired": _optional_bool(
        getattr(outcome, "trusted_time_wait_expired", None),
        "trusted_time_wait_expired",
      ),
      "cache_wait_expired": _optional_bool(
        getattr(outcome, "cache_wait_expired", None),
        "cache_wait_expired",
      ),
      "nav_sat_observation_expired": _optional_bool(
        getattr(outcome, "nav_sat_observation_expired", None),
        "nav_sat_observation_expired",
      ),
      "runtime_elapsed_seconds": _optional_number(
        getattr(outcome, "runtime_elapsed_seconds", None),
        "runtime_elapsed_seconds",
      ),
      "anchor_elapsed_seconds": _optional_number(
        getattr(outcome, "time_anchor_elapsed_seconds", None),
        "time_anchor_elapsed_seconds",
      ),
      "decision_ready_elapsed_seconds": _optional_number(
        getattr(outcome, "decision_ready_elapsed_seconds", None),
        "decision_ready_elapsed_seconds",
      ),
      "nav_sat_observed_elapsed_seconds": _optional_number(
        getattr(outcome, "nav_sat_observed_elapsed_seconds", None),
        "nav_sat_observed_elapsed_seconds",
      ),
      "nav_sat_wait_seconds": _optional_number(
        getattr(outcome, "nav_sat_wait_seconds", None),
        "nav_sat_wait_seconds",
      ),
      "completion_elapsed_seconds": _optional_number(
        getattr(outcome, "completion_elapsed_seconds", None),
        "completion_elapsed_seconds",
      ),
    },
    "database": {
      "state": _enum_value(
        getattr(outcome, "database_state", None)
      ),
      "age_seconds": _optional_number(
        getattr(outcome, "database_age_seconds", None),
        "database_age_seconds",
      ),
      "age_evidence": _optional_text(
        getattr(outcome, "restored_cache_age_evidence", None),
        "restored_cache_age_evidence",
      ),
      "age_verified": _optional_bool(
        getattr(outcome, "restored_cache_age_verified", None),
        "restored_cache_age_verified",
      ),
      "captured_gps_ephemeris_available": _optional_count(
        getattr(outcome, "captured_gps_ephemeris_available", None),
        "captured_gps_ephemeris_available",
      ),
      "captured_glonass_ephemeris_available": _optional_count(
        getattr(outcome, "captured_glonass_ephemeris_available", None),
        "captured_glonass_ephemeris_available",
      ),
      "captured_gps_startup_ready": _optional_bool(
        getattr(outcome, "captured_gps_startup_ready", None),
        "captured_gps_startup_ready",
      ),
      "gps_ephemeris_fresh": _optional_bool(
        getattr(outcome, "restored_gps_ephemeris_fresh", None),
        "restored_gps_ephemeris_fresh",
      ),
      "glonass_ephemeris_fresh": _optional_bool(
        getattr(outcome, "restored_glonass_ephemeris_fresh", None),
        "restored_glonass_ephemeris_fresh",
      ),
      "quality_expiration_reasons": _text_sequence(
        getattr(
          outcome,
          "restored_quality_expiration_reasons",
          (),
        ),
        "restored_quality_expiration_reasons",
      ),
      "restored_cache_generation": _optional_text(
        getattr(outcome, "restored_cache_generation", None),
        "restored_cache_generation",
      ),
      "restored_cache_selection_reason": _optional_text(
        getattr(outcome, "restored_cache_selection_reason", None),
        "restored_cache_selection_reason",
      ),
      "restored_gps_almanac_available": _optional_count(
        getattr(outcome, "restored_gps_almanac_available", None),
        "restored_gps_almanac_available",
      ),
      "restored_glonass_almanac_available": _optional_count(
        getattr(outcome, "restored_glonass_almanac_available", None),
        "restored_glonass_almanac_available",
      ),
      "restored_gps_ephemeris_available": _optional_count(
        getattr(outcome, "restored_gps_ephemeris_available", None),
        "restored_gps_ephemeris_available",
      ),
      "restored_glonass_ephemeris_available": _optional_count(
        getattr(outcome, "restored_glonass_ephemeris_available", None),
        "restored_glonass_ephemeris_available",
      ),
      "restored_satellites_used": _optional_count(
        getattr(outcome, "restored_satellites_used", None),
        "restored_satellites_used",
      ),
      "restored_gps_startup_ready": _optional_bool(
        getattr(outcome, "restored_gps_startup_ready", None),
        "restored_gps_startup_ready",
      ),
      "restored_gps_almanac_satellite_ids": (
        None
        if restored_almanac_ids is None
        else _satellite_ids(
          restored_almanac_ids,
          "restored_gps_almanac_satellite_ids",
        )
      ),
    },
    "plan": {
      "action": _enum_value(getattr(plan, "action", None)),
      "reason": _enum_value(getattr(plan, "reason", None)),
      "satellite_ids": _satellite_ids(
        getattr(plan, "satellite_ids", ()),
        "plan_satellite_ids",
      ),
      "unavailable_satellite_ids": _satellite_ids(
        getattr(plan, "unavailable_satellite_ids", ()),
        "plan_unavailable_satellite_ids",
      ),
    },
    "yuma": {
      "reference_utc": _timestamp(
        getattr(outcome, "yuma_reference_utc", None),
        "yuma_reference_utc",
      ),
      "reference_age_seconds": _optional_number(
        getattr(outcome, "yuma_reference_age_seconds", None),
        "yuma_reference_age_seconds",
      ),
      "downloaded_at_utc": _timestamp(
        getattr(outcome, "downloaded_at_utc", None),
        "downloaded_at_utc",
      ),
      "cache_error": _optional_text(
        getattr(outcome, "cache_error", None),
        "cache_error",
      ),
      "snapshot_sha256": _optional_text(
        getattr(outcome, "yuma_snapshot_sha256", None),
        "yuma_snapshot_sha256",
        maximum_length=64,
      ),
    },
    "transmission_summary": {
      "requested_satellite_ids": _satellite_ids(
        getattr(plan, "satellite_ids", ()),
        "plan_satellite_ids",
      ),
      "attempted_satellite_ids": [
        satellite_id
        for attempt in attempts
        for satellite_id in _satellite_sequence(
          getattr(
            getattr(attempt, "transmit_result", None),
            "attempted_satellite_ids",
            (),
          ),
          "attempted_satellite_ids",
        )
      ],
      "per_prn_attempt_counts": _attempt_counts(attempts),
      "accepted_satellite_ids": _aggregate_satellite_ids(
        attempts,
        "accepted_satellite_ids",
      ),
      "rejected_satellite_ids": _aggregate_satellite_ids(
        attempts,
        "rejected_satellite_ids",
      ),
      "timed_out_satellite_ids": _aggregate_satellite_ids(
        attempts,
        "timed_out_satellite_ids",
      ),
      "failed_satellite_ids": _aggregate_satellite_ids(
        attempts,
        "failed_satellite_ids",
      ),
      "deferred_satellite_ids": _aggregate_satellite_ids(
        attempts,
        "deferred_satellite_ids",
      ),
    },
    "attempts": [
      _attempt_payload(attempt)
      for attempt in attempts
    ],
    "latest": {
      "transmission_attempt": _optional_count(
        getattr(outcome, "transmission_attempt", None),
        "transmission_attempt",
      ),
      "transmission_elapsed_ms": _optional_number(
        getattr(outcome, "transmission_elapsed_ms", None),
        "transmission_elapsed_ms",
      ),
      "error": _optional_text(
        getattr(outcome, "error", None),
        "error",
      ),
      "transmit_result": _transmit_result_payload(
        getattr(outcome, "transmit_result", None)
      ),
    },
  }

  encoded = (
    json.dumps(
      payload,
      sort_keys=True,
      separators=(",", ":"),
    ).encode("utf-8")
    + b"\n"
  )
  if len(encoded) > YUMA_LAST_OUTCOME_MAX_BYTES:
    raise YumaOutcomeStoreError(
      f"Serialized YUMA outcome is too large: {len(encoded)} bytes"
    )
  return encoded


def save_yuma_supplementation_outcome(
  path: Path,
  outcome: object,
  *,
  commit: str | None,
  receiver_cycle: int,
  recorded_at_utc: datetime | None,
) -> None:
  encoded = serialize_yuma_supplementation_outcome(
    outcome,
    commit=commit,
    receiver_cycle=receiver_cycle,
    recorded_at_utc=recorded_at_utc,
  )
  path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)

  descriptor, temporary_name = tempfile.mkstemp(
    dir=path.parent,
    prefix=f".{path.name}.",
    suffix=".tmp",
  )
  temporary_path = Path(temporary_name)

  try:
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "wb") as output:
      descriptor = -1
      output.write(encoded)
      output.flush()
      os.fsync(output.fileno())

    os.replace(temporary_path, path)

    directory_descriptor = os.open(
      path.parent,
      os.O_RDONLY | os.O_DIRECTORY,
    )
    try:
      os.fsync(directory_descriptor)
    finally:
      os.close(directory_descriptor)

  finally:
    if descriptor >= 0:
      os.close(descriptor)
    temporary_path.unlink(missing_ok=True)
