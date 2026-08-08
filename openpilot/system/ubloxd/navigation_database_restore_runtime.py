"""Linux-boot-scoped execution of the trusted-age MGA-DBD restore policy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import tempfile
import time
from typing import Any, cast

from openpilot.common.swaglog import cloudlog
from openpilot.system.ubloxd.gps_assistance import (
  CacheAgeEvidence,
  CacheFileInspection,
  CacheFileState,
  CacheInventory,
  CacheValidationError,
  GpsAssistanceCache,
  GPS_ASSISTANCE_CACHE_PATH,
  NavigationCacheStore,
  NavigationQuality,
  RestoredNavigationQuality,
  age_safe_restore_position_accuracy_cm,
  build_position_assistance_message,
  effective_restored_navigation_quality,
  load_cache,
  receiver_fingerprints_compatible,
)
from openpilot.system.ubloxd.navigation_database_restore import (
  DEFAULT_NAVIGATION_DATABASE_RESTORE_AGE_POLICY,
  NavigationDatabaseRestoreAgePolicy,
  NavigationDatabaseRestoreBootController,
  NavigationDatabaseRestoreDisposition,
  evaluate_navigation_database_restore,
  is_current_independent_network_time,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  read_boot_id,
  read_boottime_seconds,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AuthorizedTime,
  TimeAuthorizationEvidence,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  MgaReceiverNackError,
  MgaTransactionError,
  MgaWriteError,
)


NAVIGATION_DATABASE_RESTORE_STATE_VERSION = 4
POLICY_NAVIGATION_DATABASE_RESTORE_STATE_VERSION = 3
LEGACY_NAVIGATION_DATABASE_RESTORE_STATE_VERSION = 2
NAVIGATION_DATABASE_RESTORE_STATE_PATH = Path("/data/gps_assistance/navigation_database_restore_state.json")
NAVIGATION_DATABASE_RESTORE_TRANSFER_BUDGET_SECONDS = 15.0


class NavigationDatabaseRestoreStateError(ValueError):
  pass


class NavigationDatabaseRestoreStateContentError(
  NavigationDatabaseRestoreStateError
):
  """Persistent state was readable, but its contents are invalid."""


class NavigationDatabaseRestoreInitializationError(RuntimeError):
  """Receiver startup cannot safely establish boot-scoped DBD ownership."""


class PositionAssistanceWriteStatus(StrEnum):
  NOT_ATTEMPTED = "not_attempted"
  SUCCEEDED = "succeeded"
  FAILED = "failed"


class PositionAssistanceAckStatus(StrEnum):
  NOT_ATTEMPTED = "not_attempted"
  ACCEPTED = "accepted"
  REJECTED = "rejected"
  TIMED_OUT = "timed_out"
  OBSERVATION_FAILED = "observation_failed"


class PositionAssistanceFailureKind(StrEnum):
  BUILD = "build"
  WRITE = "write"
  ACK_REJECTED = "ack_rejected"
  ACK_TIMEOUT = "ack_timeout"
  ACK_OBSERVATION_FAILED = "ack_observation_failed"
  AGE_UNVERIFIED = "position_age_unverified"
  UNCERTAINTY_UNREPRESENTABLE = "position_uncertainty_unrepresentable"


@dataclass(frozen=True)
class NavigationDatabaseRestoreSnapshot:
  saved_at_utc: datetime
  database_frames: tuple[bytes, ...]
  latitude_e7: int
  longitude_e7: int
  altitude_cm: int
  position_accuracy_cm: int
  quality: NavigationQuality | None
  generation: str
  selection_reason: str

  @classmethod
  def from_cache(
    cls,
    cache: GpsAssistanceCache,
    *,
    generation: str,
    selection_reason: str,
  ) -> NavigationDatabaseRestoreSnapshot:
    return cls(
      saved_at_utc=cache.saved_at_utc,
      database_frames=cache.database_frames,
      latitude_e7=cache.latitude_e7,
      longitude_e7=cache.longitude_e7,
      altitude_cm=cache.altitude_cm,
      position_accuracy_cm=cache.position_accuracy_cm,
      quality=getattr(cache, "quality", None),
      generation=generation,
      selection_reason=selection_reason,
    )

  @property
  def database_digest(self) -> str:
    digest = sha256()
    for frame in self.database_frames:
      digest.update(len(frame).to_bytes(4, "big"))
      digest.update(frame)
    return digest.hexdigest()


@dataclass(frozen=True)
class NavigationDatabaseRestoreFrozenCaches:
  position_snapshot: NavigationDatabaseRestoreSnapshot | None
  primary_snapshot: NavigationDatabaseRestoreSnapshot | None
  previous_snapshot: NavigationDatabaseRestoreSnapshot | None
  inventory: CacheInventory | None = None

  @property
  def database_candidates(self) -> tuple[NavigationDatabaseRestoreSnapshot, ...]:
    return tuple(snapshot for snapshot in (self.primary_snapshot, self.previous_snapshot) if snapshot is not None and snapshot.database_frames)


class NavigationDatabaseRestoreTerminalBoundaryError(
  CacheValidationError
):
  """The active DBD restore can no longer safely send any frame."""


class NavigationDatabaseRestoreTransferDeadlineError(
  NavigationDatabaseRestoreTerminalBoundaryError
):
  """The whole-DBD transfer budget expired."""


class NavigationDatabaseRestoreFrameFailureKind(StrEnum):
  REJECTED = "rejected"
  TIMED_OUT = "timed_out"
  WRITE_ERROR = "write_error"
  TRANSACTION_ERROR = "transaction_error"
  VALIDATION_ERROR = "validation_error"
  TRANSFER_DEADLINE = "transfer_deadline"
  UNEXPECTED_ERROR = "unexpected_error"

  @property
  def retryable(self) -> bool:
    return self in (
      NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT,
      NavigationDatabaseRestoreFrameFailureKind.WRITE_ERROR,
      NavigationDatabaseRestoreFrameFailureKind.TRANSACTION_ERROR,
    )


@dataclass(frozen=True)
class NavigationDatabaseRestoreFrameFailure:
  frame_index: int
  attempt: int
  kind: NavigationDatabaseRestoreFrameFailureKind
  error: str

  def __post_init__(self) -> None:
    if isinstance(self.frame_index, bool) or not isinstance(self.frame_index, int) or self.frame_index < 0:
      raise NavigationDatabaseRestoreStateError("failure frame index is invalid")
    if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt not in (1, 2):
      raise NavigationDatabaseRestoreStateError("failure attempt is invalid")
    if not isinstance(self.kind, NavigationDatabaseRestoreFrameFailureKind):
      raise NavigationDatabaseRestoreStateError("failure kind is invalid")
    if not isinstance(self.error, str) or not self.error:
      raise NavigationDatabaseRestoreStateError("failure error is invalid")

  def to_json_dict(self) -> dict[str, object]:
    return {
      "frame_index": self.frame_index,
      "attempt": self.attempt,
      "kind": self.kind.value,
      "error": self.error,
    }

  @classmethod
  def from_json_dict(
    cls,
    value: object,
  ) -> NavigationDatabaseRestoreFrameFailure:
    if not isinstance(value, dict) or set(value) != {
      "frame_index",
      "attempt",
      "kind",
      "error",
    }:
      raise NavigationDatabaseRestoreStateError("frame failure is invalid")
    mapping = cast(dict[str, object], value)
    try:
      kind = NavigationDatabaseRestoreFrameFailureKind(
        cast(str, mapping["kind"])
      )
    except (TypeError, ValueError) as exc:
      raise NavigationDatabaseRestoreStateError("failure kind is invalid") from exc
    return cls(
      frame_index=cast(int, mapping["frame_index"]),
      attempt=cast(int, mapping["attempt"]),
      kind=kind,
      error=cast(str, mapping["error"]),
    )


@dataclass(frozen=True)
class NavigationDatabaseRestoreCandidateIdentity:
  generation: str
  saved_at_utc: datetime
  database_digest: str

  def __post_init__(self) -> None:
    if not isinstance(self.generation, str) or self.generation not in ("primary", "previous", "legacy"):
      raise NavigationDatabaseRestoreStateError("candidate generation is invalid")
    if not isinstance(self.saved_at_utc, datetime):
      raise NavigationDatabaseRestoreStateError("candidate timestamp is invalid")
    if self.saved_at_utc.tzinfo is None or self.saved_at_utc.utcoffset() is None:
      raise NavigationDatabaseRestoreStateError("candidate timestamp must be aware")
    if not isinstance(self.database_digest, str):
      raise NavigationDatabaseRestoreStateError("candidate digest is invalid")
    if len(self.database_digest) != 64 or any(character not in "0123456789abcdef" for character in self.database_digest):
      raise NavigationDatabaseRestoreStateError("candidate digest is invalid")

  @classmethod
  def from_snapshot(
    cls,
    snapshot: NavigationDatabaseRestoreSnapshot,
  ) -> NavigationDatabaseRestoreCandidateIdentity:
    return cls(
      generation=snapshot.generation,
      saved_at_utc=snapshot.saved_at_utc,
      database_digest=snapshot.database_digest,
    )

  def matches(self, snapshot: NavigationDatabaseRestoreSnapshot) -> bool:
    return self == NavigationDatabaseRestoreCandidateIdentity.from_snapshot(snapshot)

  def to_json_dict(self) -> dict[str, str]:
    return {
      "generation": self.generation,
      "saved_at_utc": self.saved_at_utc.astimezone(UTC).isoformat(),
      "database_digest": self.database_digest,
    }

  @classmethod
  def from_json_dict(
    cls,
    value: object,
  ) -> NavigationDatabaseRestoreCandidateIdentity:
    if not isinstance(value, dict) or set(value) != {
      "generation",
      "saved_at_utc",
      "database_digest",
    }:
      raise NavigationDatabaseRestoreStateError("candidate identity is invalid")
    mapping = cast(dict[str, object], value)
    try:
      saved_at = datetime.fromisoformat(
        cast(str, mapping["saved_at_utc"])
      )
    except (TypeError, ValueError) as exc:
      raise NavigationDatabaseRestoreStateError("candidate timestamp is invalid") from exc
    return cls(
      generation=cast(str, mapping["generation"]),
      saved_at_utc=saved_at,
      database_digest=cast(str, mapping["database_digest"]),
    )


@dataclass(frozen=True)
class NavigationDatabaseRestoreCandidatePolicy:
  snapshot: NavigationDatabaseRestoreSnapshot
  age_policy: NavigationDatabaseRestoreAgePolicy = (
    DEFAULT_NAVIGATION_DATABASE_RESTORE_AGE_POLICY
  )

  def __post_init__(self) -> None:
    if not isinstance(self.snapshot, NavigationDatabaseRestoreSnapshot):
      raise ValueError("candidate snapshot is invalid")
    if not isinstance(self.age_policy, NavigationDatabaseRestoreAgePolicy):
      raise ValueError("candidate age policy is invalid")

  @property
  def identity(self) -> NavigationDatabaseRestoreCandidateIdentity:
    return NavigationDatabaseRestoreCandidateIdentity.from_snapshot(
      self.snapshot
    )

  @property
  def maximum_age_seconds(self) -> float:
    return self.age_policy.maximum_age_seconds

  @property
  def expires_at_utc(self) -> datetime:
    return self.snapshot.saved_at_utc + timedelta(
      seconds=self.maximum_age_seconds
    )

  @property
  def database_digest(self) -> str:
    return self.identity.database_digest

  def accepts_age(self, cache_age_seconds: float | None) -> bool:
    return self.age_policy.accepts(cache_age_seconds)


@dataclass(frozen=True)
class NavigationDatabaseRestoreExecution:
  disposition: NavigationDatabaseRestoreDisposition
  total_frame_count: int
  accepted_frame_count: int
  database_write_attempt_count: int
  initial_failures: tuple[NavigationDatabaseRestoreFrameFailure, ...] = ()
  retry_accepted_indexes: tuple[int, ...] = ()
  permanent_failures: tuple[NavigationDatabaseRestoreFrameFailure, ...] = ()
  execution_error: str | None = None
  failure_phase: str | None = None
  position_assistance_attempted: bool = False
  position_assistance_succeeded: bool = False
  position_assistance_message_id: int | None = None
  position_assistance_message_type: int | None = None
  position_assistance_write_status: PositionAssistanceWriteStatus = PositionAssistanceWriteStatus.NOT_ATTEMPTED
  position_assistance_ack_status: PositionAssistanceAckStatus = PositionAssistanceAckStatus.NOT_ATTEMPTED
  position_assistance_ack_info_code: int | None = None
  position_assistance_failure_kind: PositionAssistanceFailureKind | None = None
  position_assistance_error_type: str | None = None
  position_assistance_error: str | None = None
  cache_saved_at_utc: datetime | None = None
  cache_generation: str | None = None
  cache_selection_reason: str | None = None
  cache_database_digest: str | None = None
  cache_age_seconds: float | None = None
  cache_maximum_age_seconds: float | None = None
  cache_expires_at_utc: datetime | None = None
  candidate_identities: tuple[
    NavigationDatabaseRestoreCandidateIdentity, ...
  ] = ()
  effective_quality: RestoredNavigationQuality | None = None
  captured_quality: NavigationQuality | None = None
  boot_id: str | None = None
  state_persistence_error: str | None = None
  recovered_interrupted_attempt: bool = False
  transfer_budget_seconds: float | None = None
  transfer_started_at: float | None = None
  transfer_completed_at: float | None = None
  transfer_deadline: float | None = None

  @property
  def database_available(self) -> bool:
    return self.disposition.database_available

  @property
  def initially_failed_indexes(self) -> tuple[int, ...]:
    return tuple(failure.frame_index for failure in self.initial_failures)

  @property
  def permanently_failed_indexes(self) -> tuple[int, ...]:
    return tuple(failure.frame_index for failure in self.permanent_failures)

  def initial_indexes(
    self,
    *kinds: NavigationDatabaseRestoreFrameFailureKind,
  ) -> tuple[int, ...]:
    return tuple(failure.frame_index for failure in self.initial_failures if failure.kind in kinds)

  def permanent_indexes(
    self,
    *kinds: NavigationDatabaseRestoreFrameFailureKind,
  ) -> tuple[int, ...]:
    return tuple(failure.frame_index for failure in self.permanent_failures if failure.kind in kinds)

  @property
  def first_failure(self) -> NavigationDatabaseRestoreFrameFailure | None:
    if self.initial_failures:
      return self.initial_failures[0]
    return self.permanent_failures[0] if self.permanent_failures else None

  @property
  def transfer_elapsed_seconds(self) -> float | None:
    if (
      self.transfer_started_at is None
      or self.transfer_completed_at is None
    ):
      return None
    return max(
      0.0,
      self.transfer_completed_at - self.transfer_started_at,
    )


@dataclass(frozen=True)
class NavigationDatabaseRestorePersistedExecution:
  disposition: NavigationDatabaseRestoreDisposition
  total_frame_count: int
  accepted_frame_count: int
  database_write_attempt_count: int
  initial_failures: tuple[NavigationDatabaseRestoreFrameFailure, ...] = ()
  retry_accepted_indexes: tuple[int, ...] = ()
  permanent_failures: tuple[NavigationDatabaseRestoreFrameFailure, ...] = ()
  execution_error: str | None = None
  failure_phase: str | None = None
  cache_selection_reason: str | None = None
  cache_age_seconds: float | None = None
  transfer_budget_seconds: float | None = None
  transfer_started_at: float | None = None
  transfer_completed_at: float | None = None
  transfer_deadline: float | None = None

  def __post_init__(self) -> None:
    if not isinstance(self.disposition, NavigationDatabaseRestoreDisposition) or not self.disposition.terminal or self.disposition.intentionally_skipped:
      raise NavigationDatabaseRestoreStateError("persisted restore disposition is invalid")
    for name, value in (
      ("total_frame_count", self.total_frame_count),
      ("accepted_frame_count", self.accepted_frame_count),
      ("database_write_attempt_count", self.database_write_attempt_count),
    ):
      if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise NavigationDatabaseRestoreStateError(f"{name} is invalid")
    if self.accepted_frame_count > self.total_frame_count:
      raise NavigationDatabaseRestoreStateError("accepted frame count exceeds total")
    if self.database_write_attempt_count < self.accepted_frame_count:
      raise NavigationDatabaseRestoreStateError(
        "write attempt count is below accepted frame count"
      )
    for name, failures in (
      ("initial_failures", self.initial_failures),
      ("permanent_failures", self.permanent_failures),
    ):
      if not isinstance(failures, tuple) or not all(isinstance(failure, NavigationDatabaseRestoreFrameFailure) for failure in failures):
        raise NavigationDatabaseRestoreStateError(f"{name} is invalid")
      if any(failure.frame_index >= self.total_frame_count for failure in failures):
        raise NavigationDatabaseRestoreStateError(f"{name} frame index is invalid")
      indexes = tuple(failure.frame_index for failure in failures)
      if len(set(indexes)) != len(indexes):
        raise NavigationDatabaseRestoreStateError(
          f"{name} frame indexes are duplicated"
        )
    if any(failure.attempt != 1 for failure in self.initial_failures):
      raise NavigationDatabaseRestoreStateError(
        "initial failure attempt is invalid"
      )
    if not isinstance(self.retry_accepted_indexes, tuple) or not all(
      not isinstance(index, bool) and isinstance(index, int) and 0 <= index < self.total_frame_count
      for index in self.retry_accepted_indexes
    ):
      raise NavigationDatabaseRestoreStateError("retry accepted indexes are invalid")
    if len(set(self.retry_accepted_indexes)) != len(self.retry_accepted_indexes):
      raise NavigationDatabaseRestoreStateError("retry accepted indexes are duplicated")
    initial_indexes = {
      failure.frame_index for failure in self.initial_failures
    }
    permanent_indexes = {
      failure.frame_index for failure in self.permanent_failures
    }
    retry_accepted_indexes = set(self.retry_accepted_indexes)
    if not retry_accepted_indexes <= initial_indexes:
      raise NavigationDatabaseRestoreStateError(
        "retry accepted index lacks an initial failure"
      )
    if retry_accepted_indexes & permanent_indexes:
      raise NavigationDatabaseRestoreStateError(
        "retry accepted and permanent failure indexes overlap"
      )
    if len(retry_accepted_indexes) > self.accepted_frame_count:
      raise NavigationDatabaseRestoreStateError(
        "retry accepted count exceeds accepted frame count"
      )
    if any(
      failure.attempt == 2
      and failure.frame_index not in initial_indexes
      for failure in self.permanent_failures
    ):
      raise NavigationDatabaseRestoreStateError(
        "retry failure lacks an initial failure"
      )
    for name, value in (
      ("execution_error", self.execution_error),
      ("failure_phase", self.failure_phase),
      ("cache_selection_reason", self.cache_selection_reason),
    ):
      if value is not None and (not isinstance(value, str) or not value):
        raise NavigationDatabaseRestoreStateError(f"{name} is invalid")
    for name, value in (
      ("cache_age_seconds", self.cache_age_seconds),
      ("transfer_budget_seconds", self.transfer_budget_seconds),
      ("transfer_started_at", self.transfer_started_at),
      ("transfer_completed_at", self.transfer_completed_at),
      ("transfer_deadline", self.transfer_deadline),
    ):
      if value is not None and (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(float(value))
      ):
        raise NavigationDatabaseRestoreStateError(f"{name} is invalid")
    if self.cache_age_seconds is not None and self.cache_age_seconds < 0.0:
      raise NavigationDatabaseRestoreStateError("cache age is invalid")
    if self.transfer_budget_seconds is not None and self.transfer_budget_seconds <= 0.0:
      raise NavigationDatabaseRestoreStateError("transfer budget is invalid")
    transfer_boundary_values = (
      self.transfer_budget_seconds,
      self.transfer_started_at,
      self.transfer_deadline,
    )
    if any(value is None for value in transfer_boundary_values) and any(value is not None for value in transfer_boundary_values):
      raise NavigationDatabaseRestoreStateError("transfer boundary fields must be all present or all absent")
    if self.transfer_started_at is None and self.transfer_completed_at is not None:
      raise NavigationDatabaseRestoreStateError("transfer completion requires a start")
    if self.transfer_started_at is not None:
      assert self.transfer_budget_seconds is not None
      assert self.transfer_deadline is not None
      if self.transfer_completed_at is not None and self.transfer_completed_at < self.transfer_started_at:
        raise NavigationDatabaseRestoreStateError("transfer completion precedes start")
      if self.transfer_deadline != self.transfer_started_at + self.transfer_budget_seconds:
        raise NavigationDatabaseRestoreStateError("transfer deadline does not match budget")
    if self.disposition is NavigationDatabaseRestoreDisposition.RESTORED and (
      self.total_frame_count <= 0
      or self.accepted_frame_count != self.total_frame_count
      or self.permanent_failures
      or self.execution_error is not None
    ):
      raise NavigationDatabaseRestoreStateError("completed restore result is inconsistent")
    if (
      self.disposition
      is NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL
      and self.accepted_frame_count == 0
    ):
      raise NavigationDatabaseRestoreStateError(
        "partial restore has no accepted frames"
      )
    if (
      self.disposition
      is not NavigationDatabaseRestoreDisposition.RESTORED
      and self.disposition
      is not NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL
      and self.accepted_frame_count > 0
    ):
      raise NavigationDatabaseRestoreStateError(
        "unsuccessful accepted frames require a partial disposition"
      )

  @property
  def first_failure(self) -> NavigationDatabaseRestoreFrameFailure | None:
    if self.initial_failures:
      return self.initial_failures[0]
    return self.permanent_failures[0] if self.permanent_failures else None

  def to_json_dict(self) -> dict[str, object]:
    return {
      "disposition": self.disposition.value,
      "total_frame_count": self.total_frame_count,
      "accepted_frame_count": self.accepted_frame_count,
      "database_write_attempt_count": self.database_write_attempt_count,
      "initial_failures": [failure.to_json_dict() for failure in self.initial_failures],
      "retry_accepted_indexes": list(self.retry_accepted_indexes),
      "permanent_failures": [failure.to_json_dict() for failure in self.permanent_failures],
      "execution_error": self.execution_error,
      "failure_phase": self.failure_phase,
      "cache_selection_reason": self.cache_selection_reason,
      "cache_age_seconds": self.cache_age_seconds,
      "transfer_budget_seconds": self.transfer_budget_seconds,
      "transfer_started_at": self.transfer_started_at,
      "transfer_completed_at": self.transfer_completed_at,
      "transfer_deadline": self.transfer_deadline,
    }

  @classmethod
  def from_json_dict(
    cls,
    value: object,
  ) -> NavigationDatabaseRestorePersistedExecution:
    keys = {
      "disposition",
      "total_frame_count",
      "accepted_frame_count",
      "database_write_attempt_count",
      "initial_failures",
      "retry_accepted_indexes",
      "permanent_failures",
      "execution_error",
      "failure_phase",
      "cache_selection_reason",
      "cache_age_seconds",
      "transfer_budget_seconds",
      "transfer_started_at",
      "transfer_completed_at",
      "transfer_deadline",
    }
    if not isinstance(value, dict) or set(value) != keys:
      raise NavigationDatabaseRestoreStateError("persisted restore result is invalid")
    mapping = cast(dict[str, object], value)
    try:
      disposition = NavigationDatabaseRestoreDisposition(
        cast(str, mapping["disposition"])
      )
    except (TypeError, ValueError) as exc:
      raise NavigationDatabaseRestoreStateError("persisted restore disposition is invalid") from exc
    initial_failures = mapping["initial_failures"]
    retry_accepted_indexes = mapping["retry_accepted_indexes"]
    permanent_failures = mapping["permanent_failures"]
    if not isinstance(initial_failures, list) or not isinstance(retry_accepted_indexes, list) or not isinstance(permanent_failures, list):
      raise NavigationDatabaseRestoreStateError("persisted restore collections are invalid")
    return cls(
      disposition=disposition,
      total_frame_count=cast(int, mapping["total_frame_count"]),
      accepted_frame_count=cast(int, mapping["accepted_frame_count"]),
      database_write_attempt_count=cast(int, mapping["database_write_attempt_count"]),
      initial_failures=tuple(NavigationDatabaseRestoreFrameFailure.from_json_dict(failure) for failure in initial_failures),
      retry_accepted_indexes=tuple(cast(int, index) for index in retry_accepted_indexes),
      permanent_failures=tuple(NavigationDatabaseRestoreFrameFailure.from_json_dict(failure) for failure in permanent_failures),
      execution_error=cast(str | None, mapping["execution_error"]),
      failure_phase=cast(str | None, mapping["failure_phase"]),
      cache_selection_reason=cast(str | None, mapping["cache_selection_reason"]),
      cache_age_seconds=cast(float | None, mapping["cache_age_seconds"]),
      transfer_budget_seconds=cast(float | None, mapping["transfer_budget_seconds"]),
      transfer_started_at=cast(float | None, mapping["transfer_started_at"]),
      transfer_completed_at=cast(float | None, mapping["transfer_completed_at"]),
      transfer_deadline=cast(float | None, mapping["transfer_deadline"]),
    )


@dataclass(frozen=True)
class NavigationDatabaseRestoreBootState:
  version: int
  boot_id: str
  receiver_fingerprint: str
  disposition: NavigationDatabaseRestoreDisposition
  restore_attempted: bool
  position_assistance_claimed: bool
  acquisition_started: bool
  yuma_sent: bool
  candidate_identities: tuple[NavigationDatabaseRestoreCandidateIdentity, ...] = ()
  cache_generation: str | None = None
  cache_saved_at_utc: datetime | None = None
  cache_database_digest: str | None = None
  cache_maximum_age_seconds: float | None = None
  cache_expires_at_utc: datetime | None = None
  restore_result: NavigationDatabaseRestorePersistedExecution | None = None

  def __post_init__(self) -> None:
    if self.version != NAVIGATION_DATABASE_RESTORE_STATE_VERSION:
      raise NavigationDatabaseRestoreStateError("unsupported state version")
    if not isinstance(self.boot_id, str) or not self.boot_id.strip():
      raise NavigationDatabaseRestoreStateError("boot_id is invalid")
    if not isinstance(self.receiver_fingerprint, str):
      raise NavigationDatabaseRestoreStateError("receiver_fingerprint is invalid")
    if not isinstance(self.disposition, NavigationDatabaseRestoreDisposition):
      raise NavigationDatabaseRestoreStateError("disposition is invalid")
    for name, value in (
      ("restore_attempted", self.restore_attempted),
      ("position_assistance_claimed", self.position_assistance_claimed),
      ("acquisition_started", self.acquisition_started),
      ("yuma_sent", self.yuma_sent),
    ):
      if not isinstance(value, bool):
        raise NavigationDatabaseRestoreStateError(f"{name} is invalid")
    if self.restore_attempted and self.disposition.intentionally_skipped:
      raise NavigationDatabaseRestoreStateError("attempted restore cannot have an intentional-skip disposition")
    if (
      self.disposition
      in (
        NavigationDatabaseRestoreDisposition.RESTORED,
        NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
        NavigationDatabaseRestoreDisposition.RESTORE_REJECTED,
        NavigationDatabaseRestoreDisposition.RESTORE_RESPONSE_TIMEOUT,
        NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE,
        NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR,
        NavigationDatabaseRestoreDisposition.RESTORE_CACHE_EXPIRED,
        NavigationDatabaseRestoreDisposition.WRITE_FAILED,
      )
      and not self.restore_attempted
    ):
      raise NavigationDatabaseRestoreStateError("restore completion requires restore_attempted")
    if not isinstance(self.candidate_identities, tuple) or not all(
      isinstance(identity, NavigationDatabaseRestoreCandidateIdentity) for identity in self.candidate_identities
    ):
      raise NavigationDatabaseRestoreStateError("candidate identities are invalid")
    generations = tuple(identity.generation for identity in self.candidate_identities)
    if len(set(generations)) != len(generations):
      raise NavigationDatabaseRestoreStateError("candidate generations are duplicated")
    if self.cache_generation is not None and not isinstance(self.cache_generation, str):
      raise NavigationDatabaseRestoreStateError("cache_generation is invalid")
    if self.cache_saved_at_utc is not None:
      if not isinstance(self.cache_saved_at_utc, datetime):
        raise NavigationDatabaseRestoreStateError("cache_saved_at_utc is invalid")
      if self.cache_saved_at_utc.tzinfo is None or self.cache_saved_at_utc.utcoffset() is None:
        raise NavigationDatabaseRestoreStateError("cache_saved_at_utc must be timezone-aware")
    selected_policy_values = (
      self.cache_generation,
      self.cache_saved_at_utc,
      self.cache_database_digest,
      self.cache_maximum_age_seconds,
      self.cache_expires_at_utc,
    )
    if any(value is None for value in selected_policy_values) and any(
      value is not None for value in selected_policy_values
    ):
      raise NavigationDatabaseRestoreStateError(
        "selected cache policy fields must be all present or all absent"
      )
    if self.restore_result is not None:
      if not isinstance(self.restore_result, NavigationDatabaseRestorePersistedExecution):
        raise NavigationDatabaseRestoreStateError("restore_result is invalid")
      if not self.restore_attempted:
        raise NavigationDatabaseRestoreStateError("restore_result requires restore_attempted")
      if self.restore_result.disposition is not self.disposition:
        raise NavigationDatabaseRestoreStateError("restore_result disposition does not match state")
    if self.cache_database_digest is not None:
      if (
        len(self.cache_database_digest) != 64
        or any(
          character not in "0123456789abcdef"
          for character in self.cache_database_digest
        )
      ):
        raise NavigationDatabaseRestoreStateError(
          "selected cache digest is invalid"
        )
      maximum_age = self.cache_maximum_age_seconds
      if (
        isinstance(maximum_age, bool)
        or not isinstance(maximum_age, (int, float))
        or not isfinite(float(maximum_age))
        or float(maximum_age) < 0.0
      ):
        raise NavigationDatabaseRestoreStateError(
          "selected cache maximum age is invalid"
        )
      expires_at = self.cache_expires_at_utc
      if not isinstance(expires_at, datetime):
        raise NavigationDatabaseRestoreStateError(
          "selected cache expiration is invalid"
        )
      if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise NavigationDatabaseRestoreStateError(
          "selected cache expiration must be timezone-aware"
        )
      assert self.cache_saved_at_utc is not None
      expected_expiration = self.cache_saved_at_utc + timedelta(
        seconds=float(maximum_age)
      )
      if expires_at != expected_expiration:
        raise NavigationDatabaseRestoreStateError(
          "selected cache expiration does not match policy"
        )

  def to_json_dict(self) -> dict[str, Any]:
    return {
      "version": self.version,
      "boot_id": self.boot_id,
      "receiver_fingerprint": self.receiver_fingerprint,
      "disposition": self.disposition.value,
      "restore_attempted": self.restore_attempted,
      "position_assistance_claimed": self.position_assistance_claimed,
      "acquisition_started": self.acquisition_started,
      "yuma_sent": self.yuma_sent,
      "candidate_identities": [identity.to_json_dict() for identity in self.candidate_identities],
      "cache_generation": self.cache_generation,
      "cache_saved_at_utc": (self.cache_saved_at_utc.astimezone(UTC).isoformat() if self.cache_saved_at_utc is not None else None),
      "cache_database_digest": self.cache_database_digest,
      "cache_maximum_age_seconds": self.cache_maximum_age_seconds,
      "cache_expires_at_utc": (self.cache_expires_at_utc.astimezone(UTC).isoformat() if self.cache_expires_at_utc is not None else None),
      "restore_result": (self.restore_result.to_json_dict() if self.restore_result is not None else None),
    }

  @classmethod
  def from_json_dict(cls, value: object) -> NavigationDatabaseRestoreBootState:
    if not isinstance(value, dict):
      raise NavigationDatabaseRestoreStateError("state root is invalid")
    mapping = cast(dict[str, object], value)
    common_keys = {
      "version",
      "boot_id",
      "receiver_fingerprint",
      "disposition",
      "restore_attempted",
      "position_assistance_claimed",
      "acquisition_started",
      "yuma_sent",
      "candidate_identities",
      "cache_generation",
      "cache_saved_at_utc",
    }
    policy_keys = {
      "cache_database_digest",
      "cache_maximum_age_seconds",
      "cache_expires_at_utc",
    }
    result_keys = {"restore_result"}
    version = mapping.get("version")
    if version == NAVIGATION_DATABASE_RESTORE_STATE_VERSION:
      if set(mapping) != common_keys | policy_keys | result_keys:
        raise NavigationDatabaseRestoreStateError("state keys are invalid")
    elif version == POLICY_NAVIGATION_DATABASE_RESTORE_STATE_VERSION:
      if set(mapping) != common_keys | policy_keys:
        raise NavigationDatabaseRestoreStateError("policy state keys are invalid")
    elif version == LEGACY_NAVIGATION_DATABASE_RESTORE_STATE_VERSION:
      if set(mapping) != common_keys:
        raise NavigationDatabaseRestoreStateError("legacy state keys are invalid")
    else:
      raise NavigationDatabaseRestoreStateError("unsupported state version")

    def parse_optional_datetime(field: str) -> datetime | None:
      raw = mapping[field]
      if raw is None:
        return None
      if not isinstance(raw, str):
        raise NavigationDatabaseRestoreStateError(f"{field} is invalid")
      try:
        return datetime.fromisoformat(raw)
      except ValueError as exc:
        raise NavigationDatabaseRestoreStateError(
          f"{field} is invalid"
        ) from exc

    saved_at = parse_optional_datetime("cache_saved_at_utc")
    try:
      disposition = NavigationDatabaseRestoreDisposition(
        cast(str, mapping["disposition"])
      )
    except (TypeError, ValueError) as exc:
      raise NavigationDatabaseRestoreStateError(
        "disposition is invalid"
      ) from exc
    identities_raw = mapping["candidate_identities"]
    if not isinstance(identities_raw, list):
      raise NavigationDatabaseRestoreStateError(
        "candidate identities are invalid"
      )

    legacy = version == LEGACY_NAVIGATION_DATABASE_RESTORE_STATE_VERSION
    policy_state = version in (
      POLICY_NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
      NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
    )
    restore_result_raw = (
      mapping["restore_result"]
      if version == NAVIGATION_DATABASE_RESTORE_STATE_VERSION
      else None
    )
    return cls(
      version=NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
      boot_id=cast(str, mapping["boot_id"]),
      receiver_fingerprint=cast(str, mapping["receiver_fingerprint"]),
      disposition=disposition,
      restore_attempted=cast(bool, mapping["restore_attempted"]),
      position_assistance_claimed=cast(
        bool,
        mapping["position_assistance_claimed"],
      ),
      acquisition_started=cast(bool, mapping["acquisition_started"]),
      yuma_sent=cast(bool, mapping["yuma_sent"]),
      candidate_identities=tuple(
        NavigationDatabaseRestoreCandidateIdentity.from_json_dict(identity)
        for identity in identities_raw
      ),
      cache_generation=(
        None if legacy else cast(str | None, mapping["cache_generation"])
      ),
      cache_saved_at_utc=(None if legacy else saved_at),
      cache_database_digest=(
        cast(str | None, mapping["cache_database_digest"])
        if policy_state
        else None
      ),
      cache_maximum_age_seconds=(
        cast(float | None, mapping["cache_maximum_age_seconds"])
        if policy_state
        else None
      ),
      cache_expires_at_utc=(
        parse_optional_datetime("cache_expires_at_utc")
        if policy_state
        else None
      ),
      restore_result=(
        None
        if restore_result_raw is None
        else NavigationDatabaseRestorePersistedExecution.from_json_dict(
          restore_result_raw
        )
      ),
    )



def load_navigation_database_restore_boot_state(
  path: Path = NAVIGATION_DATABASE_RESTORE_STATE_PATH,
) -> NavigationDatabaseRestoreBootState | None:
  try:
    raw = path.read_text(encoding="utf-8")
  except FileNotFoundError:
    return None
  except UnicodeDecodeError as exc:
    raise NavigationDatabaseRestoreStateContentError(
      "state encoding is invalid"
    ) from exc
  except OSError as exc:
    raise NavigationDatabaseRestoreStateError("state read failed") from exc

  try:
    value = json.loads(raw)
  except (ValueError, RecursionError) as exc:
    raise NavigationDatabaseRestoreStateContentError(
      "state JSON is invalid"
    ) from exc

  try:
    return NavigationDatabaseRestoreBootState.from_json_dict(value)
  except NavigationDatabaseRestoreStateError as exc:
    raise NavigationDatabaseRestoreStateContentError(
      str(exc)
    ) from exc


def _navigation_database_restore_quarantine_prefix(
  path: Path,
  boot_id: str,
) -> str:
  if not isinstance(path, Path):
    raise NavigationDatabaseRestoreStateError("state path is invalid")
  if not isinstance(boot_id, str) or not boot_id.strip():
    raise NavigationDatabaseRestoreStateError("boot_id is invalid")

  boot_digest = sha256(boot_id.encode("utf-8")).hexdigest()[:16]
  return f"{path.name}.invalid-{boot_digest}-"


def navigation_database_restore_state_quarantine_exists(
  path: Path,
  boot_id: str,
) -> bool:
  prefix = _navigation_database_restore_quarantine_prefix(
    path,
    boot_id,
  )

  try:
    with os.scandir(path.parent) as entries:
      return any(entry.name.startswith(prefix) for entry in entries)
  except FileNotFoundError:
    return False
  except OSError as exc:
    raise NavigationDatabaseRestoreStateError(
      "state quarantine probe failed"
    ) from exc


def quarantine_navigation_database_restore_boot_state(
  path: Path,
  boot_id: str,
) -> Path:
  prefix = _navigation_database_restore_quarantine_prefix(
    path,
    boot_id,
  )
  quarantine_path = path.with_name(
    f"{prefix}{time.time_ns()}-{os.getpid()}"
  )

  try:
    os.replace(path, quarantine_path)

    directory_descriptor = os.open(path.parent, os.O_RDONLY)
    try:
      os.fsync(directory_descriptor)
    finally:
      os.close(directory_descriptor)
  except OSError as exc:
    raise NavigationDatabaseRestoreStateError(
      "state quarantine failed"
    ) from exc

  return quarantine_path


def store_navigation_database_restore_boot_state(
  state: NavigationDatabaseRestoreBootState,
  path: Path = NAVIGATION_DATABASE_RESTORE_STATE_PATH,
) -> None:
  if not isinstance(state, NavigationDatabaseRestoreBootState):
    raise NavigationDatabaseRestoreStateError("state is invalid")
  parent = path.parent
  parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{path.name}.",
    suffix=".tmp",
    dir=parent,
  )
  temporary = Path(temporary_name)
  try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
      json.dump(
        state.to_json_dict(),
        stream,
        sort_keys=True,
        separators=(",", ":"),
      )
      stream.write("\n")
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temporary, path)
    directory_descriptor = os.open(parent, os.O_RDONLY)
    try:
      os.fsync(directory_descriptor)
    finally:
      os.close(directory_descriptor)
  finally:
    temporary.unlink(missing_ok=True)


def _snapshot_from_inspection(
  inspection: CacheFileInspection,
  *,
  reason: str,
) -> NavigationDatabaseRestoreSnapshot | None:
  if inspection.cache is None:
    return None
  return NavigationDatabaseRestoreSnapshot.from_cache(
    inspection.cache,
    generation=inspection.generation,
    selection_reason=reason,
  )


def load_navigation_database_restore_frozen_caches(
  receiver_fingerprint: str,
) -> NavigationDatabaseRestoreFrozenCaches:
  store = NavigationCacheStore(GPS_ASSISTANCE_CACHE_PATH, loader=load_cache)
  store.remove_stale_candidate()
  inventory = store.inspect(receiver_fingerprint, None)
  position_selection = store.select_inventory(
    inventory,
    age_evidence=CacheAgeEvidence.UNVERIFIED,
  )
  position_snapshot = (
    None
    if position_selection is None
    else NavigationDatabaseRestoreSnapshot.from_cache(
      position_selection.cache,
      generation=position_selection.generation,
      selection_reason=f"position:{position_selection.reason}",
    )
  )
  return NavigationDatabaseRestoreFrozenCaches(
    position_snapshot=position_snapshot,
    primary_snapshot=_snapshot_from_inspection(
      inventory.primary,
      reason="frozen_primary",
    ),
    previous_snapshot=_snapshot_from_inspection(
      inventory.previous,
      reason="frozen_previous",
    ),
    inventory=inventory,
  )


def _bounded_error(exc: BaseException) -> str:
  return f"{type(exc).__name__}:{exc}"[:240]


def _classify_failure(
  exc: BaseException,
) -> NavigationDatabaseRestoreFrameFailureKind:
  if isinstance(exc, NavigationDatabaseRestoreTransferDeadlineError):
    return NavigationDatabaseRestoreFrameFailureKind.TRANSFER_DEADLINE
  if isinstance(exc, MgaReceiverNackError):
    return NavigationDatabaseRestoreFrameFailureKind.REJECTED
  if isinstance(exc, TimeoutError):
    return NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT
  if isinstance(exc, MgaWriteError):
    return NavigationDatabaseRestoreFrameFailureKind.WRITE_ERROR
  if isinstance(exc, MgaTransactionError):
    return NavigationDatabaseRestoreFrameFailureKind.TRANSACTION_ERROR
  if isinstance(exc, CacheValidationError):
    return NavigationDatabaseRestoreFrameFailureKind.VALIDATION_ERROR
  return NavigationDatabaseRestoreFrameFailureKind.UNEXPECTED_ERROR


class NavigationDatabaseRestoreRuntime:
  """Persists one DBD decision and receiver-write claims per receiver cycle."""

  def __init__(
    self,
    receiver_fingerprint: str,
    *,
    snapshot_loader: Callable[
      [str],
      NavigationDatabaseRestoreFrozenCaches | NavigationDatabaseRestoreSnapshot | None,
    ] = load_navigation_database_restore_frozen_caches,
    retry_delay_seconds: float = 0.25,
    transfer_budget_seconds: float = (
      NAVIGATION_DATABASE_RESTORE_TRANSFER_BUDGET_SECONDS
    ),
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    state_path: Path = NAVIGATION_DATABASE_RESTORE_STATE_PATH,
    boot_id_reader: Callable[[], str | None] = read_boot_id,
    boottime_reader: Callable[[], float | None] = read_boottime_seconds,
    state_loader: Callable[[Path], NavigationDatabaseRestoreBootState | None] = load_navigation_database_restore_boot_state,
    state_quarantiner: Callable[[Path, str], Path] = quarantine_navigation_database_restore_boot_state,
    state_storer: Callable[[NavigationDatabaseRestoreBootState, Path], None] = store_navigation_database_restore_boot_state,
    new_receiver_cycle: bool = False,
  ) -> None:
    if not isinstance(receiver_fingerprint, str):
      raise ValueError("receiver_fingerprint must be a string")
    for name, dependency in (
      ("snapshot_loader", snapshot_loader),
      ("monotonic", monotonic),
      ("sleeper", sleeper),
      ("boot_id_reader", boot_id_reader),
      ("boottime_reader", boottime_reader),
      ("state_loader", state_loader),
      ("state_quarantiner", state_quarantiner),
      ("state_storer", state_storer),
    ):
      if not callable(dependency):
        raise ValueError(f"{name} must be callable")
    if (
      isinstance(retry_delay_seconds, bool)
      or not isinstance(retry_delay_seconds, (int, float))
      or not isfinite(float(retry_delay_seconds))
      or float(retry_delay_seconds) < 0.0
    ):
      raise ValueError("retry_delay_seconds must be finite and non-negative")
    if (
      isinstance(transfer_budget_seconds, bool)
      or not isinstance(transfer_budget_seconds, (int, float))
      or not isfinite(float(transfer_budget_seconds))
      or float(transfer_budget_seconds) <= 0.0
    ):
      raise ValueError(
        "transfer_budget_seconds must be finite and positive"
      )
    if not isinstance(state_path, Path):
      raise ValueError("state_path must be a Path")
    if not isinstance(new_receiver_cycle, bool):
      raise ValueError("new_receiver_cycle must be a bool")

    self._receiver_fingerprint = receiver_fingerprint
    self._snapshot_loader = snapshot_loader
    self._retry_delay_seconds = float(retry_delay_seconds)
    self._transfer_budget_seconds = float(transfer_budget_seconds)
    self._monotonic = monotonic
    self._sleeper = sleeper
    self._state_path = state_path
    self._boottime_reader = boottime_reader
    self._state_storer = state_storer
    self._controller = NavigationDatabaseRestoreBootController()
    self._caches_loaded = False
    self._snapshot_load_error: str | None = None
    self._frozen_caches: NavigationDatabaseRestoreFrozenCaches | None = None
    self._position_snapshot: NavigationDatabaseRestoreSnapshot | None = None
    self._database_snapshot: NavigationDatabaseRestoreSnapshot | None = None
    self._database_policy: NavigationDatabaseRestoreCandidatePolicy | None = None
    self._candidate_identities: tuple[NavigationDatabaseRestoreCandidateIdentity, ...] = ()
    self._position_claimed = False
    self._position_attempted = False
    self._position_succeeded = False
    self._position_message: bytes | None = None
    self._position_message_id: int | None = None
    self._position_message_type: int | None = None
    self._position_write_status = PositionAssistanceWriteStatus.NOT_ATTEMPTED
    self._position_ack_status = PositionAssistanceAckStatus.NOT_ATTEMPTED
    self._position_ack_info_code: int | None = None
    self._position_failure_kind: PositionAssistanceFailureKind | None = None
    self._position_error_type: str | None = None
    self._position_error: str | None = None
    self._yuma_sent = False
    self._state_persistence_error: str | None = None
    self._assistance_state_disabled = False
    self._assistance_state_disabled_reason: str | None = None
    self._recovered_interrupted_attempt = False
    self._persisted_candidate_identities: tuple[NavigationDatabaseRestoreCandidateIdentity, ...] = ()
    self._persisted_cache_generation: str | None = None
    self._persisted_cache_saved_at_utc: datetime | None = None
    self._persisted_cache_database_digest: str | None = None
    self._persisted_cache_maximum_age_seconds: float | None = None
    self._persisted_cache_expires_at_utc: datetime | None = None
    self._last_authorized_time: AuthorizedTime | None = None
    self._last_initial_failures: tuple[NavigationDatabaseRestoreFrameFailure, ...] = ()
    self._last_retry_accepted_indexes: tuple[int, ...] = ()
    self._last_permanent_failures: tuple[NavigationDatabaseRestoreFrameFailure, ...] = ()
    self._last_execution_error: str | None = None
    self._last_failure_phase: str | None = None
    self._restore_result_details_available = False
    self._last_total_frame_count = 0
    self._last_accepted_frame_count = 0
    self._last_write_attempt_count = 0
    self._last_cache_selection_reason: str | None = None
    self._last_cache_age_seconds: float | None = None
    self._transfer_started_at: float | None = None
    self._transfer_completed_at: float | None = None
    self._transfer_deadline: float | None = None

    try:
      boot_id = boot_id_reader()
    except Exception as exc:
      raise NavigationDatabaseRestoreInitializationError(
        f"boot_state:boot_id_read_failed:{_bounded_error(exc)}"
      ) from exc
    self._boot_id = (
      boot_id if isinstance(boot_id, str) and boot_id.strip() else None
    )
    if self._boot_id is None:
      raise NavigationDatabaseRestoreInitializationError(
        "boot_state:boot_id_unavailable"
      )

    quarantine_closed = False

    try:
      quarantine_exists = (
        navigation_database_restore_state_quarantine_exists(
          self._state_path,
          self._boot_id,
        )
      )
    except Exception as exc:
      raise NavigationDatabaseRestoreInitializationError(
        f"boot_state:state_quarantine_probe_failed:{_bounded_error(exc)}"
      ) from exc

    if quarantine_exists:
      self._fail_closed("boot_state:invalid_state_quarantined")
      if not self._persist_state():
        detail = self._state_persistence_error or "unknown"
        raise NavigationDatabaseRestoreInitializationError(
          f"boot_state:terminal_state_persist_failed:{detail}"
        )
      persisted = None
      quarantine_closed = True
    else:
      try:
        persisted = state_loader(self._state_path)
      except NavigationDatabaseRestoreStateContentError as exc:
        try:
          state_quarantiner(
            self._state_path,
            self._boot_id,
          )
        except Exception as quarantine_exc:
          raise NavigationDatabaseRestoreInitializationError(
            f"boot_state:state_quarantine_failed:{_bounded_error(quarantine_exc)}"
          ) from quarantine_exc

        self._fail_closed(
          f"boot_state:invalid_state_quarantined:{_bounded_error(exc)}"
        )
        if not self._persist_state():
          detail = self._state_persistence_error or "unknown"
          raise NavigationDatabaseRestoreInitializationError(
            f"boot_state:terminal_state_persist_failed:{detail}"
          ) from exc

        persisted = None
        quarantine_closed = True
      except Exception as exc:
        raise NavigationDatabaseRestoreInitializationError(
          f"boot_state:state_load_failed:{_bounded_error(exc)}"
        ) from exc

    if persisted is not None and not isinstance(
      persisted,
      NavigationDatabaseRestoreBootState,
    ):
      raise NavigationDatabaseRestoreInitializationError(
        "boot_state:state_load_returned_invalid_type"
      )

    valid_same_boot_state = (
      persisted is not None
      and persisted.boot_id == self._boot_id
      and receiver_fingerprints_compatible(
        persisted.receiver_fingerprint,
        self._receiver_fingerprint,
      )
    )

    receiver_fingerprint_mismatch = (
      persisted is not None
      and persisted.boot_id == self._boot_id
      and not receiver_fingerprints_compatible(
        persisted.receiver_fingerprint,
        self._receiver_fingerprint,
      )
    )

    if quarantine_closed:
      pass
    elif receiver_fingerprint_mismatch:
      self._fail_closed("boot_state:receiver_fingerprint_mismatch")
      if not self._persist_state():
        detail = self._state_persistence_error or "unknown"
        raise NavigationDatabaseRestoreInitializationError(
          f"boot_state:current_boot_baseline_persist_failed:{detail}"
        )
    elif new_receiver_cycle:
      if not self._persist_state():
        detail = self._state_persistence_error or "unknown"
        raise NavigationDatabaseRestoreInitializationError(
          "receiver_cycle:baseline_persist_failed:"
          + detail
        )
    elif valid_same_boot_state:
      self._restore_persisted_state(persisted)
    else:
      if not self._persist_state():
        detail = self._state_persistence_error or "unknown"
        raise NavigationDatabaseRestoreInitializationError(
          f"boot_state:current_boot_baseline_persist_failed:{detail}"
        )

    self._execution = self._build_execution()

  @property
  def controller(self) -> NavigationDatabaseRestoreBootController:
    return self._controller

  @property
  def snapshot(self) -> NavigationDatabaseRestoreSnapshot | None:
    return self._database_snapshot or self._position_snapshot

  @property
  def acquisition_started(self) -> bool:
    return self._controller.acquisition_started

  @property
  def yuma_sent(self) -> bool:
    return self._yuma_sent

  @property
  def execution(self) -> NavigationDatabaseRestoreExecution:
    return self._execution

  @property
  def position_assistance_message(self) -> bytes | None:
    return self._position_message

  @property
  def state_available(self) -> bool:
    return not self._assistance_state_disabled

  @staticmethod
  def _candidate_restorable(
    snapshot: NavigationDatabaseRestoreSnapshot,
  ) -> bool:
    quality = snapshot.quality
    return bool(
      snapshot.database_frames
      and quality is not None
      and quality.gps_startup_ready
    )

  @staticmethod
  def _candidate_wait_qualified(
    snapshot: NavigationDatabaseRestoreSnapshot,
  ) -> bool:
    quality = snapshot.quality
    return bool(snapshot.database_frames) and (
      quality is not None and quality.gps_startup_ready
    )

  @staticmethod
  def _candidate_age_policy(
    _snapshot: NavigationDatabaseRestoreSnapshot,
  ) -> NavigationDatabaseRestoreAgePolicy:
    return DEFAULT_NAVIGATION_DATABASE_RESTORE_AGE_POLICY

  @property
  def has_prequalified_database_candidate(self) -> bool:
    self.prepare()
    return bool(
      self._frozen_caches is not None
      and any(
        self._candidate_wait_qualified(candidate)
        for candidate in self._frozen_caches.database_candidates
      )
    )

  @property
  def assistance_state_disabled_reason(self) -> str | None:
    return self._assistance_state_disabled_reason

  def _disable_assistance_state(self, reason: str) -> None:
    bounded_reason = reason if isinstance(reason, str) and reason else "unknown"
    if not self._assistance_state_disabled:
      self._assistance_state_disabled = True
      self._assistance_state_disabled_reason = bounded_reason
    self._state_persistence_error = self._assistance_state_disabled_reason
    self._position_claimed = True
    if self._position_error is None:
      self._position_error = (
        "boot_state:assistance_state_disabled:"
        + (self._assistance_state_disabled_reason or "unknown")
      )
    if self._controller.pending and not self._controller.restore_attempted:
      self._controller.skip(
        NavigationDatabaseRestoreDisposition.SKIPPED_STATE_UNAVAILABLE
      )

  def _fail_closed(self, reason: str) -> None:
    self._position_claimed = True
    self._position_error = reason
    if self._controller.pending and not self._controller.restore_attempted:
      self._controller.skip(NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED)

  def _load_persisted_execution(
    self,
    result: NavigationDatabaseRestorePersistedExecution,
  ) -> None:
    self._restore_result_details_available = True
    self._last_total_frame_count = result.total_frame_count
    self._last_accepted_frame_count = result.accepted_frame_count
    self._last_write_attempt_count = result.database_write_attempt_count
    self._last_initial_failures = result.initial_failures
    self._last_retry_accepted_indexes = result.retry_accepted_indexes
    self._last_permanent_failures = result.permanent_failures
    self._last_execution_error = result.execution_error
    self._last_failure_phase = result.failure_phase
    self._last_cache_selection_reason = result.cache_selection_reason
    self._last_cache_age_seconds = result.cache_age_seconds
    if result.transfer_budget_seconds is not None:
      self._transfer_budget_seconds = result.transfer_budget_seconds
    self._transfer_started_at = result.transfer_started_at
    self._transfer_completed_at = result.transfer_completed_at
    self._transfer_deadline = result.transfer_deadline

  def _persisted_execution_result(
    self,
  ) -> NavigationDatabaseRestorePersistedExecution | None:
    disposition = self._controller.disposition
    if (
      not self._restore_result_details_available
      or not self._controller.restore_attempted
      or not disposition.terminal
    ):
      return None
    return NavigationDatabaseRestorePersistedExecution(
      disposition=disposition,
      total_frame_count=self._last_total_frame_count,
      accepted_frame_count=self._last_accepted_frame_count,
      database_write_attempt_count=self._last_write_attempt_count,
      initial_failures=self._last_initial_failures,
      retry_accepted_indexes=self._last_retry_accepted_indexes,
      permanent_failures=self._last_permanent_failures,
      execution_error=self._last_execution_error,
      failure_phase=self._last_failure_phase,
      cache_selection_reason=self._last_cache_selection_reason,
      cache_age_seconds=self._last_cache_age_seconds,
      transfer_budget_seconds=(
        self._transfer_budget_seconds
        if self._transfer_started_at is not None
        else None
      ),
      transfer_started_at=self._transfer_started_at,
      transfer_completed_at=self._transfer_completed_at,
      transfer_deadline=self._transfer_deadline,
    )

  def _restore_persisted_state(
    self,
    state: NavigationDatabaseRestoreBootState | None,
  ) -> None:
    if state is None:
      return
    assert self._boot_id is not None
    if state.boot_id != self._boot_id:
      return
    if not receiver_fingerprints_compatible(
      state.receiver_fingerprint,
      self._receiver_fingerprint,
    ):
      self._fail_closed("boot_state:receiver_fingerprint_mismatch")
      self._persist_state()
      return

    self._position_claimed = state.position_assistance_claimed
    self._yuma_sent = state.yuma_sent
    self._persisted_candidate_identities = state.candidate_identities
    self._persisted_cache_generation = state.cache_generation
    self._persisted_cache_saved_at_utc = state.cache_saved_at_utc
    self._persisted_cache_database_digest = state.cache_database_digest
    self._persisted_cache_maximum_age_seconds = (
      state.cache_maximum_age_seconds
    )
    self._persisted_cache_expires_at_utc = state.cache_expires_at_utc
    if state.restore_result is not None:
      self._load_persisted_execution(state.restore_result)
    if state.acquisition_started:
      self._controller.note_acquisition_started()

    if state.restore_attempted:
      self._controller.begin_restore_attempt()
      if state.disposition is NavigationDatabaseRestoreDisposition.PENDING:
        self._last_execution_error = "interrupted_restore_result_unavailable"
        self._last_failure_phase = "state_recovery"
        self._restore_result_details_available = True
        self._controller.finish_restore(NavigationDatabaseRestoreDisposition.WRITE_FAILED)
        self._recovered_interrupted_attempt = True
        self._persist_state()
      else:
        self._controller.finish_restore(state.disposition)
    elif state.disposition.intentionally_skipped:
      self._controller.skip(state.disposition)
    elif state.disposition is NavigationDatabaseRestoreDisposition.PENDING:
      # A same-boot PENDING state belongs to an earlier process. The prior
      # process may have observed acquisition but failed before durably
      # recording the latch, so never reopen the DBD window after restart.
      self._controller.skip(
        NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
      )
      self._persist_state()
    else:
      self._fail_closed("boot_state:invalid_terminal_state")
      self._persist_state()

  def _state(self) -> NavigationDatabaseRestoreBootState:
    if self._boot_id is None:
      raise NavigationDatabaseRestoreStateError("boot_id is unavailable")
    snapshot = self._database_snapshot
    generation = snapshot.generation if snapshot is not None else self._persisted_cache_generation
    saved_at = snapshot.saved_at_utc if snapshot is not None else self._persisted_cache_saved_at_utc
    identities = self._candidate_identities or self._persisted_candidate_identities
    return NavigationDatabaseRestoreBootState(
      version=NAVIGATION_DATABASE_RESTORE_STATE_VERSION,
      boot_id=self._boot_id,
      receiver_fingerprint=self._receiver_fingerprint,
      disposition=self._controller.disposition,
      restore_attempted=self._controller.restore_attempted,
      position_assistance_claimed=self._position_claimed,
      acquisition_started=self._controller.acquisition_started,
      yuma_sent=self._yuma_sent,
      candidate_identities=identities,
      cache_generation=generation,
      cache_saved_at_utc=saved_at,
      cache_database_digest=(
        self._database_policy.database_digest
        if self._database_policy is not None
        else self._persisted_cache_database_digest
      ),
      cache_maximum_age_seconds=(
        self._database_policy.maximum_age_seconds
        if self._database_policy is not None
        else self._persisted_cache_maximum_age_seconds
      ),
      cache_expires_at_utc=(
        self._database_policy.expires_at_utc
        if self._database_policy is not None
        else self._persisted_cache_expires_at_utc
      ),
      restore_result=self._persisted_execution_result(),
    )

  def _persist_state(self) -> bool:
    if self._assistance_state_disabled:
      return False
    if self._boot_id is None:
      self._disable_assistance_state("boot_id_unavailable")
      return False
    try:
      self._state_storer(self._state(), self._state_path)
    except Exception as exc:
      self._disable_assistance_state(_bounded_error(exc))
      return False
    self._state_persistence_error = None
    return True

  @staticmethod
  def _normalize_frozen_caches(
    loaded: NavigationDatabaseRestoreFrozenCaches | NavigationDatabaseRestoreSnapshot | None,
  ) -> NavigationDatabaseRestoreFrozenCaches:
    if loaded is None:
      return NavigationDatabaseRestoreFrozenCaches(None, None, None)
    if isinstance(loaded, NavigationDatabaseRestoreSnapshot):
      return NavigationDatabaseRestoreFrozenCaches(
        position_snapshot=loaded,
        primary_snapshot=(loaded if loaded.generation != "previous" else None),
        previous_snapshot=(loaded if loaded.generation == "previous" else None),
      )
    if not isinstance(loaded, NavigationDatabaseRestoreFrozenCaches):
      raise TypeError("snapshot loader returned an invalid type")
    return loaded

  @staticmethod
  def _frozen_cache_file_present(
    frozen: NavigationDatabaseRestoreFrozenCaches,
  ) -> bool:
    inventory = frozen.inventory
    if inventory is not None:
      return any(
        inspection.state is not CacheFileState.ABSENT
        for inspection in (inventory.primary, inventory.previous)
      )
    return any((
      frozen.position_snapshot is not None,
      frozen.primary_snapshot is not None,
      frozen.previous_snapshot is not None,
    ))

  def prepare(self) -> NavigationDatabaseRestoreExecution:
    if not self.state_available:
      self._execution = self._build_execution(self._last_authorized_time)
      return self._execution
    if self._caches_loaded:
      return self._execution
    self._caches_loaded = True
    try:
      loaded = self._snapshot_loader(self._receiver_fingerprint)
      frozen = self._normalize_frozen_caches(loaded)
    except Exception as exc:
      self._snapshot_load_error = _bounded_error(exc)
      frozen = NavigationDatabaseRestoreFrozenCaches(None, None, None)
      self._position_error = f"snapshot_load:{self._snapshot_load_error}"
    self._frozen_caches = frozen
    self._position_snapshot = frozen.position_snapshot
    self._candidate_identities = tuple(NavigationDatabaseRestoreCandidateIdentity.from_snapshot(candidate) for candidate in frozen.database_candidates)

    if self._persisted_candidate_identities:
      if self._candidate_identities != self._persisted_candidate_identities:
        self._fail_closed("snapshot_identity_changed_within_boot")
        self._persist_state()
        self._execution = self._build_execution()
        return self._execution
    elif self._candidate_identities:
      if not self._persist_state():
        self._fail_closed("boot_state:candidate_identity_persist_failed")

    if self._controller.pending:
      cache_file_present = self._frozen_cache_file_present(frozen)
      if not frozen.database_candidates:
        self._controller.skip(
          NavigationDatabaseRestoreDisposition.SKIPPED_CACHE_UNQUALIFIED
          if self._snapshot_load_error is not None or cache_file_present
          else NavigationDatabaseRestoreDisposition.SKIPPED_NO_CACHE
        )
        self._persist_state()
      elif not any(
        self._candidate_restorable(candidate)
        for candidate in frozen.database_candidates
      ):
        self._controller.skip(
          NavigationDatabaseRestoreDisposition.SKIPPED_CACHE_UNQUALIFIED
        )
        self._persist_state()

    self._execution = self._build_execution()
    return self._execution

  def send_position_once(
    self,
    send_message: Callable[[bytes], object],
  ) -> NavigationDatabaseRestoreExecution:
    if not callable(send_message):
      raise ValueError("send_message must be callable")
    self.prepare()
    if not self.state_available:
      self._execution = self._build_execution(self._last_authorized_time)
      return self._execution
    snapshot = self._position_snapshot
    if snapshot is None or self._position_claimed:
      return self._execution
    self._position_claimed = True
    if not self._persist_state():
      self._position_error = "boot_state:position_claim_persist_failed"
      self._execution = self._build_execution()
      return self._execution

    self._position_attempted = True
    try:
      age_seconds: float | None = None
      age_verified = False
      authorized = self._last_authorized_time
      if authorized is not None and (
        is_current_independent_network_time(authorized)
        or authorized.evidence is TimeAuthorizationEvidence.SAME_BOOT_BOOTTIME
      ):
        age_seconds = (
          authorized.utc - snapshot.saved_at_utc
        ).total_seconds()
        age_verified = True
      position_accuracy_cm, accuracy_reason = age_safe_restore_position_accuracy_cm(
        snapshot.position_accuracy_cm,
        age_seconds=age_seconds,
        age_verified=age_verified,
      )
      try:
        cloudlog.info(
          ", ".join((
            "GPS position assistance uncertainty",
            f"reason={accuracy_reason}",
            (
              f"accuracy_cm={position_accuracy_cm}"
              if position_accuracy_cm is not None
              else "accuracy_cm=skipped"
            ),
            f"age_verified={str(age_verified).lower()}",
            (
              f"age_seconds={age_seconds:.1f}"
              if age_seconds is not None
              else "age_seconds=unknown"
            ),
          ))
        )
      except Exception:
        # Observability must never interfere with position assistance send.
        pass
      if position_accuracy_cm is None:
        if accuracy_reason == "position_uncertainty_unrepresentable":
          self._position_failure_kind = (
            PositionAssistanceFailureKind.UNCERTAINTY_UNREPRESENTABLE
          )
        else:
          self._position_failure_kind = PositionAssistanceFailureKind.AGE_UNVERIFIED
        self._position_error_type = "PositionAssistanceAgePolicy"
        self._position_error = accuracy_reason
        self._position_succeeded = False
        self._execution = self._build_execution(self._last_authorized_time)
        return self._execution
      message = build_position_assistance_message(
        latitude_e7=snapshot.latitude_e7,
        longitude_e7=snapshot.longitude_e7,
        altitude_cm=snapshot.altitude_cm,
        position_accuracy_cm=position_accuracy_cm,
      )
    except Exception as exc:
      self._position_failure_kind = PositionAssistanceFailureKind.BUILD
      self._position_error_type = type(exc).__name__
      self._position_error = _bounded_error(exc)
      self._position_succeeded = False
    else:
      self._position_message = message
      self._position_message_id = message[3] if len(message) > 3 else None
      payload_length = int.from_bytes(message[4:6], "little") if len(message) >= 6 else 0
      self._position_message_type = (
        message[6]
        if payload_length > 0 and len(message) > 6
        else None
      )
      try:
        send_message(message)
      except MgaReceiverNackError as exc:
        self._position_message_id = (
          exc.message_id
          if exc.message_id is not None
          else self._position_message_id
        )
        self._position_message_type = (
          exc.message_type
          if exc.message_type is not None
          else self._position_message_type
        )
        self._position_write_status = PositionAssistanceWriteStatus.SUCCEEDED
        self._position_ack_status = PositionAssistanceAckStatus.REJECTED
        self._position_ack_info_code = exc.info_code
        self._position_failure_kind = PositionAssistanceFailureKind.ACK_REJECTED
        self._position_error_type = type(exc).__name__
        self._position_error = _bounded_error(exc)
        self._position_succeeded = False
      except TimeoutError as exc:
        self._position_write_status = PositionAssistanceWriteStatus.SUCCEEDED
        self._position_ack_status = PositionAssistanceAckStatus.TIMED_OUT
        self._position_failure_kind = PositionAssistanceFailureKind.ACK_TIMEOUT
        self._position_error_type = type(exc).__name__
        self._position_error = _bounded_error(exc)
        self._position_succeeded = False
      except MgaWriteError as exc:
        self._position_message_id = (
          exc.message_id
          if exc.message_id is not None
          else self._position_message_id
        )
        self._position_message_type = (
          exc.message_type
          if exc.message_type is not None
          else self._position_message_type
        )
        self._position_write_status = PositionAssistanceWriteStatus.FAILED
        self._position_ack_status = PositionAssistanceAckStatus.NOT_ATTEMPTED
        self._position_failure_kind = PositionAssistanceFailureKind.WRITE
        self._position_error_type = type(exc).__name__
        self._position_error = _bounded_error(exc)
        self._position_succeeded = False
      except MgaTransactionError as exc:
        self._position_message_id = (
          exc.message_id
          if exc.message_id is not None
          else self._position_message_id
        )
        self._position_message_type = (
          exc.message_type
          if exc.message_type is not None
          else self._position_message_type
        )
        write_succeeded = exc.write_succeeded is True
        self._position_write_status = (
          PositionAssistanceWriteStatus.SUCCEEDED
          if write_succeeded
          else PositionAssistanceWriteStatus.FAILED
        )
        self._position_ack_status = (
          PositionAssistanceAckStatus.OBSERVATION_FAILED
          if write_succeeded
          else PositionAssistanceAckStatus.NOT_ATTEMPTED
        )
        self._position_failure_kind = (
          PositionAssistanceFailureKind.ACK_OBSERVATION_FAILED
          if write_succeeded
          else PositionAssistanceFailureKind.WRITE
        )
        self._position_error_type = type(exc).__name__
        self._position_error = _bounded_error(exc)
        self._position_succeeded = False
      except Exception as exc:
        self._position_write_status = PositionAssistanceWriteStatus.FAILED
        self._position_ack_status = PositionAssistanceAckStatus.NOT_ATTEMPTED
        self._position_failure_kind = PositionAssistanceFailureKind.WRITE
        self._position_error_type = type(exc).__name__
        self._position_error = _bounded_error(exc)
        self._position_succeeded = False
      else:
        self._position_write_status = PositionAssistanceWriteStatus.SUCCEEDED
        self._position_ack_status = PositionAssistanceAckStatus.ACCEPTED
        self._position_ack_info_code = 0
        self._position_succeeded = True
    self._execution = self._build_execution()
    return self._execution

  def _close_restore_window(
    self,
    disposition: NavigationDatabaseRestoreDisposition,
  ) -> bool:
    if not self.state_available:
      return False
    if self._controller.pending and not self._controller.restore_attempted:
      self._controller.skip(disposition)
    persisted = self._persist_state()
    self._execution = self._build_execution(self._last_authorized_time)
    return persisted

  def close_restore_window_unverified(self) -> bool:
    """Compatibility terminal skip for invalid or unclassified evidence."""
    return self._close_restore_window(
      NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED
    )

  def close_restore_window_no_trusted_time(self) -> bool:
    return self._close_restore_window(
      NavigationDatabaseRestoreDisposition.SKIPPED_NO_TRUSTED_TIME
    )

  def close_restore_window_wait_timeout(self) -> bool:
    return self._close_restore_window(
      NavigationDatabaseRestoreDisposition.SKIPPED_WAIT_TIMEOUT
    )

  def close_restore_window_wait_error(self) -> bool:
    return self._close_restore_window(
      NavigationDatabaseRestoreDisposition.SKIPPED_WAIT_ERROR
    )

  def close_restore_window_for_early_acquisition(self) -> bool:
    # Persist a terminal DBD skip while preserving pre-START assistance.
    if not self.state_available:
      return False
    if self._controller.pending and not self._controller.restore_attempted:
      self._controller.skip(
        NavigationDatabaseRestoreDisposition.SKIPPED_EARLY_ACQUISITION
      )
    persisted = self._persist_state()
    self._execution = self._build_execution(self._last_authorized_time)
    return persisted

  def claim_acquisition_start(self) -> bool:
    """Durably close the DBD window before sending GNSS START."""
    if self._controller.acquisition_started:
      return self.state_available
    if not self.state_available:
      self._controller.note_acquisition_started()
      self._execution = self._build_execution(self._last_authorized_time)
      return False
    if self._controller.pending and not self._controller.restore_attempted:
      self._controller.skip(
        NavigationDatabaseRestoreDisposition.SKIPPED_ACQUISITION_ALREADY_STARTED
      )
    self._controller.note_acquisition_started()
    persisted = self._persist_state()
    self._execution = self._build_execution(self._last_authorized_time)
    return persisted

  def note_acquisition_started(self) -> bool:
    return self.claim_acquisition_start()

  def note_early_acquisition_started(self) -> bool:
    """Durably close only DBD when bootstrap frames show acquisition."""
    if self._controller.acquisition_started:
      return self.state_available
    if not self.state_available:
      self._controller.note_acquisition_started()
      self._execution = self._build_execution(self._last_authorized_time)
      return False
    if self._controller.pending and not self._controller.restore_attempted:
      self._controller.skip(
        NavigationDatabaseRestoreDisposition.SKIPPED_EARLY_ACQUISITION
      )
    self._controller.note_acquisition_started()
    persisted = self._persist_state()
    self._execution = self._build_execution(self._last_authorized_time)
    return persisted

  @property
  def database_restore_pending(self) -> bool:
    return self._controller.pending

  def claim_yuma_transmission(self) -> bool:
    """Durably consume YUMA only after the DBD decision is terminal."""
    if not self.state_available or self.database_restore_pending:
      return False
    if self._yuma_sent:
      return True
    self._yuma_sent = True
    if self._controller.pending and not self._controller.restore_attempted:
      self._controller.skip(
        NavigationDatabaseRestoreDisposition.SKIPPED_YUMA_ALREADY_SENT
      )
    persisted = self._persist_state()
    self._execution = self._build_execution(self._last_authorized_time)
    return persisted

  def note_yuma_sent(self) -> bool:
    return self.claim_yuma_transmission()

  @staticmethod
  def _cache_age(
    snapshot: NavigationDatabaseRestoreSnapshot,
    now_utc: datetime,
  ) -> float | None:
    try:
      age = (now_utc - snapshot.saved_at_utc).total_seconds()
    except (OverflowError, TypeError, ValueError):
      return None
    return float(age) if isfinite(age) else None

  def _effective_cache_age(
    self,
    snapshot: NavigationDatabaseRestoreSnapshot,
    authorized_time: AuthorizedTime,
  ) -> float | None:
    nominal_age = self._cache_age(snapshot, authorized_time.utc)
    uncertainty = authorized_time.uncertainty_seconds
    observed_boottime = authorized_time.observed_boottime_seconds
    if (
      nominal_age is None
      or isinstance(uncertainty, bool)
      or not isinstance(uncertainty, (int, float))
      or not isfinite(float(uncertainty))
      or float(uncertainty) < 0.0
      or isinstance(observed_boottime, bool)
      or not isinstance(observed_boottime, (int, float))
      or not isfinite(float(observed_boottime))
      or float(observed_boottime) < 0.0
    ):
      return None
    try:
      current_boottime = self._boottime_reader()
    except Exception:
      return None
    if (
      isinstance(current_boottime, bool)
      or not isinstance(current_boottime, (int, float))
      or not isfinite(float(current_boottime))
      or float(current_boottime) < float(observed_boottime)
    ):
      return None
    elapsed = float(current_boottime) - float(observed_boottime)
    effective_age = nominal_age + float(uncertainty) + elapsed
    return effective_age if isfinite(effective_age) else None

  def _select_database_policy(
    self,
    authorized_time: AuthorizedTime,
  ) -> tuple[NavigationDatabaseRestoreCandidatePolicy | None, float | None]:
    assert is_current_independent_network_time(authorized_time)
    assert self._frozen_caches is not None
    candidates = tuple(
      candidate
      for candidate in self._frozen_caches.database_candidates
      if self._candidate_restorable(candidate)
    )
    ages = {
      candidate.generation: self._effective_cache_age(candidate, authorized_time)
      for candidate in candidates
    }

    if self._persisted_cache_generation is not None:
      matching = [
        candidate
        for candidate in candidates
        if (
          candidate.generation == self._persisted_cache_generation
          and candidate.saved_at_utc == self._persisted_cache_saved_at_utc
          and candidate.database_digest
          == self._persisted_cache_database_digest
        )
      ]
      if (
        len(matching) != 1
        or self._persisted_cache_maximum_age_seconds is None
        or self._persisted_cache_expires_at_utc is None
      ):
        return None, None
      selected = matching[0]
      policy = NavigationDatabaseRestoreCandidatePolicy(
        selected,
        NavigationDatabaseRestoreAgePolicy(
          self._persisted_cache_maximum_age_seconds
        ),
      )
      if policy.expires_at_utc != self._persisted_cache_expires_at_utc:
        return None, None
      return policy, ages[selected.generation]

    eligible: list[NavigationDatabaseRestoreCandidatePolicy] = []
    for candidate in candidates:
      age = ages[candidate.generation]
      policy = NavigationDatabaseRestoreCandidatePolicy(
        candidate,
        self._candidate_age_policy(candidate),
      )
      if age is not None and policy.accepts_age(age):
        eligible.append(policy)
    if not eligible:
      valid_ages = [age for age in ages.values() if age is not None and age >= 0.0]
      if len(valid_ages) == len(candidates) and valid_ages:
        return None, min(valid_ages)
      return None, None

    if len(eligible) == 1:
      selected_policy = eligible[0]
      selected = replace(
        selected_policy.snapshot,
        selection_reason=(
          "trusted_age_only_eligible:"
          + selected_policy.snapshot.generation
        ),
      )
      return (
        NavigationDatabaseRestoreCandidatePolicy(
          selected,
          selected_policy.age_policy,
        ),
        ages[selected.generation],
      )

    inventory = self._frozen_caches.inventory
    if inventory is None:
      selected_policy = next(
        (
          policy
          for policy in eligible
          if policy.snapshot.generation == "primary"
        ),
        eligible[0],
      )
      selected = replace(
        selected_policy.snapshot,
        selection_reason="trusted_age_primary_tiebreak",
      )
      return (
        NavigationDatabaseRestoreCandidatePolicy(
          selected,
          selected_policy.age_policy,
        ),
        ages[selected.generation],
      )

    eligible_generations = {
      policy.snapshot.generation for policy in eligible
    }

    def filtered(inspection: CacheFileInspection) -> CacheFileInspection:
      if inspection.generation in eligible_generations:
        return inspection
      return CacheFileInspection(
        generation=inspection.generation,
        path=inspection.path,
        state=CacheFileState.ABSENT,
      )

    selection = NavigationCacheStore.select_inventory(
      CacheInventory(
        primary=filtered(inventory.primary),
        previous=filtered(inventory.previous),
      ),
      age_evidence=CacheAgeEvidence.TRUSTED_UTC,
    )
    if selection is None:
      return None, None
    selected_policy = next(
      policy
      for policy in eligible
      if policy.snapshot.generation == selection.generation
    )
    selected = replace(
      selected_policy.snapshot,
      selection_reason=f"trusted_age:{selection.reason}",
    )
    return (
      NavigationDatabaseRestoreCandidatePolicy(
        selected,
        selected_policy.age_policy,
      ),
      ages[selected.generation],
    )

  def _activate_selected_database_policy(
    self,
    authorized_time: AuthorizedTime,
  ) -> tuple[NavigationDatabaseRestoreCandidatePolicy | None, float | None]:
    selected_policy, cache_age_seconds = (
      self._select_database_policy(authorized_time)
    )
    if selected_policy is not None:
      selected = selected_policy.snapshot
      self._database_policy = selected_policy
      self._database_snapshot = selected
      self._last_total_frame_count = len(selected.database_frames)
      self._last_cache_selection_reason = selected.selection_reason
      self._persisted_cache_generation = selected.generation
      self._persisted_cache_saved_at_utc = selected.saved_at_utc
      self._persisted_cache_database_digest = (
        selected_policy.database_digest
      )
      self._persisted_cache_maximum_age_seconds = (
        selected_policy.maximum_age_seconds
      )
      self._persisted_cache_expires_at_utc = (
        selected_policy.expires_at_utc
      )
    self._last_cache_age_seconds = cache_age_seconds
    return selected_policy, cache_age_seconds

  def record_pre_restore_transport_error(
    self,
    *,
    authorized_time: AuthorizedTime,
    error: BaseException,
    phase: str,
  ) -> NavigationDatabaseRestoreExecution:
    if not isinstance(authorized_time, AuthorizedTime):
      raise ValueError("authorized_time is invalid")
    if not isinstance(error, BaseException):
      raise ValueError("error is invalid")
    if not isinstance(phase, str) or not phase:
      raise ValueError("phase is invalid")
    self.prepare()
    self._last_authorized_time = authorized_time
    if not self.state_available or self._controller.terminal:
      self._execution = self._build_execution(authorized_time)
      return self._execution

    _selected_policy, cache_age_seconds = (
      self._activate_selected_database_policy(authorized_time)
    )
    decision = evaluate_navigation_database_restore(
      reliable_fix_available=False,
      yuma_already_sent=self._yuma_sent,
      authorized_time=authorized_time,
      cache_age_seconds=cache_age_seconds,
      gnss_acquisition_started=self._controller.acquisition_started,
      age_policy=(
        self._database_policy.age_policy
        if self._database_policy is not None
        else DEFAULT_NAVIGATION_DATABASE_RESTORE_AGE_POLICY
      ),
    )
    if not self._controller.apply_decision(decision):
      self._execution = self._build_execution(authorized_time)
      return self._execution
    if not decision.should_restore:
      self._persist_state()
      self._execution = self._build_execution(authorized_time)
      return self._execution

    self._last_accepted_frame_count = 0
    self._last_write_attempt_count = 0
    self._last_initial_failures = ()
    self._last_retry_accepted_indexes = ()
    self._last_permanent_failures = ()
    self._last_execution_error = _bounded_error(error)
    self._last_failure_phase = phase
    self._restore_result_details_available = True
    self._controller.finish_restore(
      NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR
    )
    self._persist_state()
    self._execution = self._build_execution(authorized_time)
    return self._execution

  def remaining_transfer_seconds(
    self,
    frame_index: int,
    *,
    phase: str = "ack_wait_budget",
  ) -> float:
    if (
      isinstance(frame_index, bool)
      or not isinstance(frame_index, int)
      or frame_index < 0
    ):
      raise ValueError("database frame index must be a non-negative int")
    deadline = self._transfer_deadline
    if deadline is None:
      raise NavigationDatabaseRestoreTerminalBoundaryError(
        "DBD receiver write blocked: transfer deadline unavailable"
      )
    now = self._monotonic()
    if (
      isinstance(now, bool)
      or not isinstance(now, (int, float))
      or not isfinite(float(now))
    ):
      raise NavigationDatabaseRestoreTransferDeadlineError(
        "DBD transfer monotonic time is invalid: "
        + f"phase={phase},frame_index={frame_index}"
      )
    remaining = deadline - float(now)
    if remaining <= 0.0:
      raise NavigationDatabaseRestoreTransferDeadlineError(
        "DBD transfer deadline expired: "
        + f"phase={phase},frame_index={frame_index}"
      )
    return remaining

  def _check_transfer_deadline(
    self,
    phase: str,
    frame_index: int,
  ) -> None:
    self.remaining_transfer_seconds(frame_index, phase=phase)

  def _sleep_before_retry(self, frame_index: int) -> None:
    if not self._retry_delay_seconds:
      return
    self._check_transfer_deadline("retry_delay_before", frame_index)
    assert self._transfer_deadline is not None
    now = self._monotonic()
    if (
      isinstance(now, bool)
      or not isinstance(now, (int, float))
      or not isfinite(float(now))
    ):
      raise NavigationDatabaseRestoreTransferDeadlineError(
        "DBD transfer monotonic time is invalid before retry delay"
      )
    remaining = self._transfer_deadline - float(now)
    if remaining <= self._retry_delay_seconds:
      raise NavigationDatabaseRestoreTransferDeadlineError(
        "DBD transfer deadline cannot cover retry delay: "
        + f"frame_index={frame_index}"
      )
    self._sleeper(self._retry_delay_seconds)
    self._check_transfer_deadline("retry_delay_after", frame_index)

  def _completion_disposition(
    self,
    *,
    total_frames: int,
    accepted_frames: int,
    permanent_failures: tuple[NavigationDatabaseRestoreFrameFailure, ...],
    execution_error: str | None,
  ) -> NavigationDatabaseRestoreDisposition:
    if (
      execution_error is None
      and total_frames > 0
      and accepted_frames == total_frames
      and not permanent_failures
    ):
      return NavigationDatabaseRestoreDisposition.RESTORED
    # Once any DBD frame reached the receiver, every unsuccessful outcome is
    # partial regardless of the later failure kind.  This is the safety-
    # relevant fact for subsequent assistance arbitration.
    if accepted_frames > 0:
      return NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL
    kinds = {failure.kind for failure in permanent_failures}
    errors = tuple(failure.error.casefold() for failure in permanent_failures)
    if NavigationDatabaseRestoreFrameFailureKind.TRANSFER_DEADLINE in kinds:
      return (
        NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE
      )
    if any("trusted cache age expired" in error for error in errors):
      return NavigationDatabaseRestoreDisposition.RESTORE_CACHE_EXPIRED
    if (
      NavigationDatabaseRestoreFrameFailureKind.WRITE_ERROR in kinds
      or NavigationDatabaseRestoreFrameFailureKind.TRANSACTION_ERROR in kinds
    ):
      return NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR
    if NavigationDatabaseRestoreFrameFailureKind.REJECTED in kinds:
      return NavigationDatabaseRestoreDisposition.RESTORE_REJECTED
    if NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT in kinds:
      return NavigationDatabaseRestoreDisposition.RESTORE_RESPONSE_TIMEOUT
    return NavigationDatabaseRestoreDisposition.WRITE_FAILED

  def validate_database_write_boundary(self, frame_index: int) -> None:
    """Revalidate trusted age and acquisition at each receiver-write boundary."""
    if isinstance(frame_index, bool) or not isinstance(frame_index, int) or frame_index < 0:
      raise ValueError("database frame index must be a non-negative int")
    if not self.state_available:
      raise NavigationDatabaseRestoreTerminalBoundaryError(
        "DBD receiver write blocked: assistance state unavailable"
      )
    if not self._controller.restore_attempted or self._controller.terminal:
      raise NavigationDatabaseRestoreTerminalBoundaryError(
        "DBD receiver write is outside an active restore attempt"
      )
    if self._controller.acquisition_started:
      raise NavigationDatabaseRestoreTerminalBoundaryError(
        "DBD receiver write blocked: GNSS acquisition already started"
      )
    self._check_transfer_deadline("write_boundary", frame_index)
    policy = self._database_policy
    snapshot = self._database_snapshot
    authorized_time = self._last_authorized_time
    if (
      policy is None
      or snapshot is None
      or authorized_time is None
      or not is_current_independent_network_time(authorized_time)
    ):
      raise NavigationDatabaseRestoreTerminalBoundaryError(
        "DBD receiver write blocked: trusted-age evidence is unavailable"
      )
    cache_age_seconds = self._effective_cache_age(snapshot, authorized_time)
    self._last_cache_age_seconds = cache_age_seconds
    if cache_age_seconds is None or cache_age_seconds < 0.0:
      raise NavigationDatabaseRestoreTerminalBoundaryError(
        "DBD receiver write blocked: trusted cache age is unverified"
      )
    if not policy.accepts_age(cache_age_seconds):
      raise NavigationDatabaseRestoreTerminalBoundaryError(
        "DBD receiver write blocked: trusted cache age expired"
      )

  def evaluate(
    self,
    *,
    authorized_time: AuthorizedTime | None,
    reliable_fix_available: bool,
    yuma_already_sent: bool,
    send_database_message: Callable[
      [bytes, int, Callable[[], None]], object
    ],
    pre_start_deadline: float | None = None,
  ) -> NavigationDatabaseRestoreExecution:
    if not callable(send_database_message):
      raise ValueError("send_database_message must be callable")
    if pre_start_deadline is not None and (
      isinstance(pre_start_deadline, bool)
      or not isinstance(pre_start_deadline, (int, float))
      or not isfinite(float(pre_start_deadline))
    ):
      raise ValueError("pre_start_deadline must be finite or None")
    if not self.state_available:
      self._execution = self._build_execution(authorized_time)
      return self._execution
    self.prepare()
    self._last_authorized_time = authorized_time
    if not self.state_available:
      self._execution = self._build_execution(authorized_time)
      return self._execution
    if self._controller.terminal:
      self._execution = self._build_execution(authorized_time)
      return self._execution

    cache_age_seconds = None
    if authorized_time is not None and is_current_independent_network_time(authorized_time):
      _selected_policy, cache_age_seconds = (
        self._activate_selected_database_policy(authorized_time)
      )

    decision = evaluate_navigation_database_restore(
      reliable_fix_available=reliable_fix_available,
      yuma_already_sent=(yuma_already_sent or self._yuma_sent),
      authorized_time=authorized_time,
      cache_age_seconds=cache_age_seconds,
      gnss_acquisition_started=self._controller.acquisition_started,
      age_policy=(
        self._database_policy.age_policy
        if self._database_policy is not None
        else DEFAULT_NAVIGATION_DATABASE_RESTORE_AGE_POLICY
      ),
    )
    if not self._controller.apply_decision(decision):
      self._execution = self._build_execution(authorized_time)
      return self._execution
    if not decision.should_restore:
      self._persist_state()
      self._execution = self._build_execution(authorized_time)
      return self._execution

    snapshot = self._database_snapshot
    if snapshot is None:
      self._last_total_frame_count = 0
      self._last_execution_error = "eligible_database_snapshot_missing"
      self._last_failure_phase = "selection"
      self._restore_result_details_available = True
      self._controller.finish_restore(NavigationDatabaseRestoreDisposition.WRITE_FAILED)
      self._persist_state()
      self._execution = self._build_execution(authorized_time)
      return self._execution

    if not self._persist_state():
      self._controller.finish_restore(NavigationDatabaseRestoreDisposition.WRITE_FAILED)
      self._last_execution_error = "restore_claim_persist_failed"
      self._last_failure_phase = "state_persistence"
      self._execution = self._build_execution(authorized_time)
      return self._execution

    try:
      transfer_started_at = self._monotonic()
    except Exception as exc:
      self._last_total_frame_count = len(snapshot.database_frames)
      self._last_execution_error = _bounded_error(exc)
      self._last_failure_phase = "transfer_start_clock"
      self._restore_result_details_available = True
      self._controller.finish_restore(
        NavigationDatabaseRestoreDisposition.WRITE_FAILED
      )
      self._persist_state()
      self._execution = self._build_execution(authorized_time)
      return self._execution
    if (
      isinstance(transfer_started_at, bool)
      or not isinstance(transfer_started_at, (int, float))
      or not isfinite(float(transfer_started_at))
    ):
      self._last_total_frame_count = len(snapshot.database_frames)
      self._last_execution_error = "transfer_monotonic_time_invalid"
      self._last_failure_phase = "transfer_start"
      self._restore_result_details_available = True
      self._controller.finish_restore(
        NavigationDatabaseRestoreDisposition.WRITE_FAILED
      )
      self._persist_state()
      self._execution = self._build_execution(authorized_time)
      return self._execution
    started_at = float(transfer_started_at)
    transfer_deadline = min(
      started_at + self._transfer_budget_seconds,
      float(pre_start_deadline)
      if pre_start_deadline is not None
      else float("inf"),
    )

    if transfer_deadline <= started_at:
      self._last_total_frame_count = len(snapshot.database_frames)
      self._last_execution_error = "pre_start_deadline_expired"
      self._last_failure_phase = "transfer_start"
      self._restore_result_details_available = True
      self._controller.finish_restore(
        NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE
      )
      self._persist_state()
      self._execution = self._build_execution(authorized_time)
      return self._execution
    self._transfer_started_at = started_at
    self._transfer_budget_seconds = transfer_deadline - started_at
    self._transfer_deadline = transfer_deadline
    self._transfer_completed_at = None

    accepted: set[int] = set()
    initial_failures: list[NavigationDatabaseRestoreFrameFailure] = []
    retry_accepted: list[int] = []
    permanent_failures: list[NavigationDatabaseRestoreFrameFailure] = []
    retry_frames: list[tuple[int, bytes]] = []
    write_attempts = 0
    execution_error = None
    failure_phase = None

    def write_attempt_marker() -> Callable[[], None]:
      marked = False

      def mark_write_attempt() -> None:
        nonlocal marked
        nonlocal write_attempts
        if marked:
          return
        marked = True
        write_attempts += 1

      return mark_write_attempt

    try:
      failure_phase = "initial_pass"
      terminal_boundary_failed = False
      for index, frame in enumerate(snapshot.database_frames):
        try:
          # Cheap pre-drain guard. The sender retains the post-drain guard
          # immediately before the UART write to close the acquisition race.
          self.validate_database_write_boundary(index)
          send_database_message(frame, index, write_attempt_marker())
          self._check_transfer_deadline(
            "acknowledgment_complete",
            index,
          )
          accepted.add(index)
        except Exception as exc:
          kind = _classify_failure(exc)
          failure = NavigationDatabaseRestoreFrameFailure(
            frame_index=index,
            attempt=1,
            kind=kind,
            error=_bounded_error(exc),
          )
          initial_failures.append(failure)
          if isinstance(
            exc,
            NavigationDatabaseRestoreTerminalBoundaryError,
          ):
            permanent_failures.append(failure)
            retry_frames.clear()
            terminal_boundary_failed = True
            break
          if kind.retryable:
            retry_frames.append((index, frame))
          else:
            permanent_failures.append(failure)

      if retry_frames and not terminal_boundary_failed:
        failure_phase = "retry_delay"
        try:
          self._sleep_before_retry(retry_frames[0][0])
        except NavigationDatabaseRestoreTransferDeadlineError as exc:
          permanent_failures.append(
            NavigationDatabaseRestoreFrameFailure(
              frame_index=retry_frames[0][0],
              attempt=2,
              kind=(
                NavigationDatabaseRestoreFrameFailureKind.TRANSFER_DEADLINE
              ),
              error=_bounded_error(exc),
            )
          )
          terminal_boundary_failed = True

        if not terminal_boundary_failed:
          failure_phase = "retry_pass"
        for index, frame in (() if terminal_boundary_failed else retry_frames):
          try:
            self.validate_database_write_boundary(index)
            send_database_message(frame, index, write_attempt_marker())
            self._check_transfer_deadline(
              "acknowledgment_complete",
              index,
            )
            accepted.add(index)
            retry_accepted.append(index)
          except Exception as exc:
            permanent_failures.append(
              NavigationDatabaseRestoreFrameFailure(
                frame_index=index,
                attempt=2,
                kind=_classify_failure(exc),
                error=_bounded_error(exc),
              )
            )
            if isinstance(
              exc,
              NavigationDatabaseRestoreTerminalBoundaryError,
            ):
              break
    except Exception as exc:
      execution_error = _bounded_error(exc)
    try:
      transfer_completed_at = self._monotonic()
    except Exception as exc:
      execution_error = _bounded_error(exc)
      failure_phase = "transfer_completion_clock"
      self._transfer_completed_at = None
    else:
      self._transfer_completed_at = (
        float(transfer_completed_at)
        if (
          not isinstance(transfer_completed_at, bool)
          and isinstance(transfer_completed_at, (int, float))
          and isfinite(float(transfer_completed_at))
        )
        else None
      )
      if self._transfer_completed_at is None:
        execution_error = "transfer_monotonic_time_invalid"
        failure_phase = "transfer_completion_clock"
    final_failures = tuple(permanent_failures)
    disposition = self._completion_disposition(
      total_frames=len(snapshot.database_frames),
      accepted_frames=len(accepted),
      permanent_failures=final_failures,
      execution_error=execution_error,
    )
    succeeded = disposition is NavigationDatabaseRestoreDisposition.RESTORED
    self._last_total_frame_count = len(snapshot.database_frames)
    self._last_initial_failures = tuple(initial_failures)
    self._last_retry_accepted_indexes = tuple(retry_accepted)
    self._last_permanent_failures = final_failures
    self._last_execution_error = execution_error
    self._last_failure_phase = None if succeeded else failure_phase
    self._last_accepted_frame_count = len(accepted)
    self._last_write_attempt_count = write_attempts
    self._restore_result_details_available = True
    self._controller.finish_restore(disposition)
    self._persist_state()
    self._execution = self._build_execution(authorized_time)
    return self._execution

  def _build_execution(
    self,
    authorized_time: AuthorizedTime | None = None,
  ) -> NavigationDatabaseRestoreExecution:
    snapshot = self._database_snapshot
    cache_age_seconds = self._last_cache_age_seconds
    effective_quality = None
    if snapshot is not None:
      current_network_time = authorized_time if (authorized_time is not None and is_current_independent_network_time(authorized_time)) else None
      age_evidence = CacheAgeEvidence.TRUSTED_UTC if current_network_time is not None else CacheAgeEvidence.UNVERIFIED
      if current_network_time is not None:
        cache_age_seconds = self._effective_cache_age(snapshot, current_network_time)
      effective_quality = effective_restored_navigation_quality(
        snapshot.quality,
        snapshot.saved_at_utc,
        (current_network_time.utc if current_network_time is not None else None),
        age_evidence,
      )

    return NavigationDatabaseRestoreExecution(
      disposition=self._controller.disposition,
      total_frame_count=(
        len(snapshot.database_frames)
        if snapshot is not None
        else self._last_total_frame_count
      ),
      accepted_frame_count=self._last_accepted_frame_count,
      database_write_attempt_count=self._last_write_attempt_count,
      initial_failures=self._last_initial_failures,
      retry_accepted_indexes=self._last_retry_accepted_indexes,
      permanent_failures=self._last_permanent_failures,
      execution_error=self._last_execution_error,
      failure_phase=self._last_failure_phase,
      position_assistance_attempted=self._position_attempted,
      position_assistance_succeeded=self._position_succeeded,
      position_assistance_message_id=self._position_message_id,
      position_assistance_message_type=self._position_message_type,
      position_assistance_write_status=self._position_write_status,
      position_assistance_ack_status=self._position_ack_status,
      position_assistance_ack_info_code=self._position_ack_info_code,
      position_assistance_failure_kind=self._position_failure_kind,
      position_assistance_error_type=self._position_error_type,
      position_assistance_error=self._position_error,
      cache_saved_at_utc=(
        snapshot.saved_at_utc
        if snapshot is not None
        else self._persisted_cache_saved_at_utc
      ),
      cache_generation=(
        snapshot.generation
        if snapshot is not None
        else self._persisted_cache_generation
      ),
      cache_selection_reason=(
        snapshot.selection_reason
        if snapshot is not None
        else self._last_cache_selection_reason
      ),
      cache_database_digest=(
        self._database_policy.database_digest
        if self._database_policy is not None
        else self._persisted_cache_database_digest
      ),
      cache_age_seconds=cache_age_seconds,
      cache_maximum_age_seconds=(
        self._database_policy.maximum_age_seconds
        if self._database_policy is not None
        else self._persisted_cache_maximum_age_seconds
      ),
      cache_expires_at_utc=(
        self._database_policy.expires_at_utc
        if self._database_policy is not None
        else self._persisted_cache_expires_at_utc
      ),
      candidate_identities=(
        self._candidate_identities
        or self._persisted_candidate_identities
      ),
      effective_quality=effective_quality,
      captured_quality=(snapshot.quality if snapshot else None),
      boot_id=self._boot_id,
      state_persistence_error=self._state_persistence_error,
      recovered_interrupted_attempt=self._recovered_interrupted_attempt,
      transfer_budget_seconds=self._transfer_budget_seconds,
      transfer_started_at=self._transfer_started_at,
      transfer_completed_at=self._transfer_completed_at,
      transfer_deadline=self._transfer_deadline,
    )


class NavigationDatabaseRestoreUnavailableRuntime(NavigationDatabaseRestoreRuntime):
  # Disables assistance writes when durable state cannot be established.

  def __init__(self, receiver_fingerprint: str, error: str) -> None:
    if not isinstance(receiver_fingerprint, str):
      raise ValueError("receiver_fingerprint must be a string")
    if not isinstance(error, str) or not error:
      raise ValueError("error must be a non-empty string")
    self._controller = NavigationDatabaseRestoreBootController()
    self._controller.skip(
      NavigationDatabaseRestoreDisposition.SKIPPED_STATE_UNAVAILABLE
    )
    self._database_snapshot = None
    self._database_policy = None
    self._transfer_budget_seconds = (
      NAVIGATION_DATABASE_RESTORE_TRANSFER_BUDGET_SECONDS
    )
    self._transfer_started_at = None
    self._transfer_completed_at = None
    self._transfer_deadline = None
    self._position_snapshot = None
    self._position_message = None
    self._yuma_sent = False
    self._state_persistence_error = error
    self._assistance_state_disabled = True
    self._assistance_state_disabled_reason = error
    self._execution = NavigationDatabaseRestoreExecution(
      disposition=self._controller.disposition,
      total_frame_count=0,
      accepted_frame_count=0,
      database_write_attempt_count=0,
      execution_error=error,
      failure_phase="state_initialization",
      position_assistance_error_type=(
        NavigationDatabaseRestoreInitializationError.__name__
      ),
      position_assistance_error=error,
      state_persistence_error=error,
    )

  def prepare(self) -> NavigationDatabaseRestoreExecution:
    return self._execution

  @property
  def has_prequalified_database_candidate(self) -> bool:
    return False

  def send_position_once(
    self,
    send_message: Callable[[bytes], object],
  ) -> NavigationDatabaseRestoreExecution:
    if not callable(send_message):
      raise ValueError("send_message must be callable")
    return self._execution

  def close_restore_window_unverified(self) -> bool:
    return False

  def close_restore_window_no_trusted_time(self) -> bool:
    return False

  def close_restore_window_wait_timeout(self) -> bool:
    return False

  def close_restore_window_wait_error(self) -> bool:
    return False

  def close_restore_window_for_early_acquisition(self) -> bool:
    return False

  def claim_acquisition_start(self) -> bool:
    self._controller.note_acquisition_started()
    return False

  def note_acquisition_started(self) -> bool:
    return self.claim_acquisition_start()

  @property
  def database_restore_pending(self) -> bool:
    return False

  def claim_yuma_transmission(self) -> bool:
    return False

  def note_yuma_sent(self) -> bool:
    return False

  def validate_database_write_boundary(self, frame_index: int) -> None:
    if (
      isinstance(frame_index, bool)
      or not isinstance(frame_index, int)
      or frame_index < 0
    ):
      raise ValueError(
        "database frame index must be a non-negative int"
      )
    raise NavigationDatabaseRestoreTerminalBoundaryError(
      "DBD receiver write blocked: assistance state unavailable"
    )

  def evaluate(
    self,
    *,
    authorized_time: AuthorizedTime | None,
    reliable_fix_available: bool,
    yuma_already_sent: bool,
    send_database_message: Callable[
      [bytes, int, Callable[[], None]], object
    ],
    pre_start_deadline: float | None = None,
  ) -> NavigationDatabaseRestoreExecution:
    del authorized_time, reliable_fix_available, yuma_already_sent, pre_start_deadline
    if not callable(send_database_message):
      raise ValueError("send_database_message must be callable")
    return self._execution
