"""Durable one-shot post-START MGA-INI-POS retry state."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
import json
from math import isfinite
import os
from pathlib import Path
import tempfile
from typing import Any, cast

from openpilot.system.ubloxd.gps_assistance import receiver_fingerprints_compatible
from openpilot.system.ubloxd.navigation_database_restore_runtime import (
  NavigationDatabaseRestoreExecution,
  PositionAssistanceAckStatus,
  PositionAssistanceWriteStatus,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  read_boot_id,
  read_boottime_seconds,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  MgaReceiverNackError,
  MgaTransactionError,
  MgaWriteError,
)


POSITION_ASSISTANCE_RETRY_STATE_VERSION = 1
POSITION_ASSISTANCE_RETRY_STATE_PATH = Path("/data/gps_assistance/position_assistance_retry_state.json")
POSITION_ASSISTANCE_NOT_READY_INFO_CODE = 5


class PositionAssistanceRetryStateError(ValueError):
  """Persisted position-assistance retry state is invalid."""


class PositionAssistanceRetryResult(StrEnum):
  NOT_ARMED = "not_armed"
  ARMED = "armed"
  ACCEPTED = "accepted"
  REJECTED = "rejected"
  TIMED_OUT = "timed_out"
  ACK_OBSERVATION_FAILED = "ack_observation_failed"
  WRITE_FAILED = "write_failed"
  CANCELLED_EXISTING_FIX = "cancelled_existing_fix"
  CANCELLED_RECEIVER_CYCLE_CHANGED = "cancelled_receiver_cycle_changed"
  CANCELLED_PROCESS_RESTART = "cancelled_process_restart"
  INTERRUPTED_AFTER_CLAIM = "interrupted_after_claim"
  CLAIM_PERSIST_FAILED = "claim_persist_failed"


@dataclass(frozen=True)
class PositionAssistanceRetryState:
  version: int
  boot_id: str
  receiver_fingerprint: str
  initial_attempted: bool = False
  initial_write_status: PositionAssistanceWriteStatus = PositionAssistanceWriteStatus.NOT_ATTEMPTED
  initial_ack_status: PositionAssistanceAckStatus = PositionAssistanceAckStatus.NOT_ATTEMPTED
  initial_ack_info_code: int | None = None
  retry_armed: bool = False
  retry_triggered_at: float | None = None
  retry_claimed: bool = False
  retry_completed: bool = False
  retry_result: PositionAssistanceRetryResult = PositionAssistanceRetryResult.NOT_ARMED
  retry_write_status: PositionAssistanceWriteStatus = PositionAssistanceWriteStatus.NOT_ATTEMPTED
  retry_ack_status: PositionAssistanceAckStatus = PositionAssistanceAckStatus.NOT_ATTEMPTED
  retry_ack_info_code: int | None = None
  retry_error_type: str | None = None
  retry_error: str | None = None

  def __post_init__(self) -> None:
    if self.version != POSITION_ASSISTANCE_RETRY_STATE_VERSION:
      raise PositionAssistanceRetryStateError("unsupported state version")
    if not isinstance(self.boot_id, str) or not self.boot_id.strip():
      raise PositionAssistanceRetryStateError("boot_id is invalid")
    if not isinstance(self.receiver_fingerprint, str):
      raise PositionAssistanceRetryStateError("receiver_fingerprint is invalid")
    for name, value in (
      ("initial_attempted", self.initial_attempted),
      ("retry_armed", self.retry_armed),
      ("retry_claimed", self.retry_claimed),
      ("retry_completed", self.retry_completed),
    ):
      if not isinstance(value, bool):
        raise PositionAssistanceRetryStateError(f"{name} is invalid")
    if not isinstance(
      self.initial_write_status,
      PositionAssistanceWriteStatus,
    ):
      raise PositionAssistanceRetryStateError("initial_write_status is invalid")
    if not isinstance(
      self.initial_ack_status,
      PositionAssistanceAckStatus,
    ):
      raise PositionAssistanceRetryStateError("initial_ack_status is invalid")
    if not isinstance(self.retry_result, PositionAssistanceRetryResult):
      raise PositionAssistanceRetryStateError("retry_result is invalid")
    if not isinstance(
      self.retry_write_status,
      PositionAssistanceWriteStatus,
    ):
      raise PositionAssistanceRetryStateError("retry_write_status is invalid")
    if not isinstance(
      self.retry_ack_status,
      PositionAssistanceAckStatus,
    ):
      raise PositionAssistanceRetryStateError("retry_ack_status is invalid")
    for name, value in (
      ("initial_ack_info_code", self.initial_ack_info_code),
      ("retry_ack_info_code", self.retry_ack_info_code),
    ):
      if value is not None and (isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 255):
        raise PositionAssistanceRetryStateError(f"{name} is invalid")
    if self.retry_triggered_at is not None and (
      isinstance(self.retry_triggered_at, bool)
      or not isinstance(self.retry_triggered_at, (int, float))
      or not isfinite(float(self.retry_triggered_at))
      or float(self.retry_triggered_at) < 0.0
    ):
      raise PositionAssistanceRetryStateError("retry_triggered_at is invalid")
    for name, value in (
      ("retry_error_type", self.retry_error_type),
      ("retry_error", self.retry_error),
    ):
      if value is not None and not isinstance(value, str):
        raise PositionAssistanceRetryStateError(f"{name} is invalid")
    if self.retry_claimed and not self.retry_armed:
      raise PositionAssistanceRetryStateError("claimed retry must have been armed")
    if self.retry_completed and not self.retry_claimed:
      raise PositionAssistanceRetryStateError("completed retry must have been claimed")
    if self.retry_claimed and self.retry_triggered_at is None:
      raise PositionAssistanceRetryStateError("claimed retry requires a trigger timestamp")
    if self.retry_armed and (
      not self.initial_attempted
      or self.initial_write_status is not PositionAssistanceWriteStatus.SUCCEEDED
      or self.initial_ack_status is not PositionAssistanceAckStatus.REJECTED
      or self.initial_ack_info_code != POSITION_ASSISTANCE_NOT_READY_INFO_CODE
    ):
      raise PositionAssistanceRetryStateError("retry arm condition is invalid")

  @property
  def pending(self) -> bool:
    return self.retry_armed and not self.retry_claimed and not self.retry_completed

  def to_json_dict(self) -> dict[str, Any]:
    value = asdict(self)
    value["initial_write_status"] = self.initial_write_status.value
    value["initial_ack_status"] = self.initial_ack_status.value
    value["retry_result"] = self.retry_result.value
    value["retry_write_status"] = self.retry_write_status.value
    value["retry_ack_status"] = self.retry_ack_status.value
    return value

  @classmethod
  def from_json_dict(
    cls,
    value: object,
  ) -> PositionAssistanceRetryState:
    if not isinstance(value, dict):
      raise PositionAssistanceRetryStateError("state root is invalid")
    mapping = cast(dict[str, object], value)
    expected_keys = set(
      cls(
        version=POSITION_ASSISTANCE_RETRY_STATE_VERSION,
        boot_id="probe",
        receiver_fingerprint="probe",
      ).to_json_dict()
    )
    if set(mapping) != expected_keys:
      raise PositionAssistanceRetryStateError("state keys are invalid")
    try:
      return cls(
        version=cast(int, mapping["version"]),
        boot_id=cast(str, mapping["boot_id"]),
        receiver_fingerprint=cast(
          str,
          mapping["receiver_fingerprint"],
        ),
        initial_attempted=cast(
          bool,
          mapping["initial_attempted"],
        ),
        initial_write_status=PositionAssistanceWriteStatus(cast(str, mapping["initial_write_status"])),
        initial_ack_status=PositionAssistanceAckStatus(cast(str, mapping["initial_ack_status"])),
        initial_ack_info_code=cast(
          int | None,
          mapping["initial_ack_info_code"],
        ),
        retry_armed=cast(bool, mapping["retry_armed"]),
        retry_triggered_at=cast(
          float | None,
          mapping["retry_triggered_at"],
        ),
        retry_claimed=cast(bool, mapping["retry_claimed"]),
        retry_completed=cast(
          bool,
          mapping["retry_completed"],
        ),
        retry_result=PositionAssistanceRetryResult(cast(str, mapping["retry_result"])),
        retry_write_status=PositionAssistanceWriteStatus(cast(str, mapping["retry_write_status"])),
        retry_ack_status=PositionAssistanceAckStatus(cast(str, mapping["retry_ack_status"])),
        retry_ack_info_code=cast(
          int | None,
          mapping["retry_ack_info_code"],
        ),
        retry_error_type=cast(
          str | None,
          mapping["retry_error_type"],
        ),
        retry_error=cast(str | None, mapping["retry_error"]),
      )
    except (TypeError, ValueError) as exc:
      raise PositionAssistanceRetryStateError("state value is invalid") from exc


def load_position_assistance_retry_state(
  path: Path = POSITION_ASSISTANCE_RETRY_STATE_PATH,
) -> PositionAssistanceRetryState | None:
  try:
    raw = path.read_text(encoding="utf-8")
  except FileNotFoundError:
    return None
  except (OSError, UnicodeDecodeError) as exc:
    raise PositionAssistanceRetryStateError("state read failed") from exc
  try:
    value = json.loads(raw)
  except json.JSONDecodeError as exc:
    raise PositionAssistanceRetryStateError("state JSON is invalid") from exc
  return PositionAssistanceRetryState.from_json_dict(value)


def store_position_assistance_retry_state(
  state: PositionAssistanceRetryState,
  path: Path = POSITION_ASSISTANCE_RETRY_STATE_PATH,
) -> None:
  if not isinstance(state, PositionAssistanceRetryState):
    raise PositionAssistanceRetryStateError("state is invalid")
  path.parent.mkdir(parents=True, exist_ok=True)
  descriptor, temporary_name = tempfile.mkstemp(
    prefix=f".{path.name}.",
    suffix=".tmp",
    dir=path.parent,
  )
  temporary_path = Path(temporary_name)
  try:
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
      json.dump(
        state.to_json_dict(),
        stream,
        separators=(",", ":"),
        sort_keys=True,
      )
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temporary_path, path)
    directory_descriptor = os.open(path.parent, os.O_DIRECTORY)
    try:
      os.fsync(directory_descriptor)
    finally:
      os.close(directory_descriptor)
  except Exception:
    try:
      temporary_path.unlink()
    except FileNotFoundError:
      pass
    raise


def _bounded_error(exc: BaseException, maximum: int = 512) -> str:
  detail = f"{type(exc).__name__}:{exc}"
  return detail if len(detail) <= maximum else detail[:maximum]


class PositionAssistanceRetryRuntime:
  def __init__(
    self,
    receiver_fingerprint: str,
    *,
    state_path: Path = POSITION_ASSISTANCE_RETRY_STATE_PATH,
    boot_id_reader: Callable[[], str | None] = read_boot_id,
    boottime_reader: Callable[[], float | None] = read_boottime_seconds,
    state_loader: Callable[[Path], PositionAssistanceRetryState | None] = load_position_assistance_retry_state,
    state_storer: Callable[[PositionAssistanceRetryState, Path], None] = store_position_assistance_retry_state,
    new_receiver_cycle: bool = False,
  ) -> None:
    if not isinstance(receiver_fingerprint, str):
      raise ValueError("receiver_fingerprint must be a string")
    if not isinstance(state_path, Path):
      raise ValueError("state_path must be a Path")
    if not isinstance(new_receiver_cycle, bool):
      raise ValueError("new_receiver_cycle must be a bool")
    self._receiver_fingerprint = receiver_fingerprint
    self._state_path = state_path
    self._state_storer = state_storer
    self._message: bytes | None = None
    self._persistence_error: str | None = None

    boot_id = boot_id_reader()
    if not isinstance(boot_id, str) or not boot_id.strip():
      raise PositionAssistanceRetryStateError("boot_id is unavailable")
    self._boot_id = boot_id
    baseline = PositionAssistanceRetryState(
      version=POSITION_ASSISTANCE_RETRY_STATE_VERSION,
      boot_id=boot_id,
      receiver_fingerprint=receiver_fingerprint,
    )
    try:
      persisted = state_loader(state_path)
    except Exception as exc:
      raise PositionAssistanceRetryStateError(f"state load failed: {_bounded_error(exc)}") from exc
    if new_receiver_cycle:
      self._state = baseline
      if not self._persist():
        raise PositionAssistanceRetryStateError("receiver cycle baseline persist failed: " + (self._persistence_error or "unknown"))
      return
    if (
      persisted is None
      or persisted.boot_id != boot_id
      or not receiver_fingerprints_compatible(
        persisted.receiver_fingerprint,
        receiver_fingerprint,
      )
    ):
      self._state = baseline
      self._persist()
      return

    self._state = persisted
    restart_time = boottime_reader()
    if isinstance(restart_time, bool) or not isinstance(restart_time, (int, float)) or not isfinite(float(restart_time)) or float(restart_time) < 0.0:
      restart_time = 0.0
    if persisted.pending:
      self._state = replace(
        persisted,
        retry_triggered_at=float(restart_time),
        retry_claimed=True,
        retry_completed=True,
        retry_result=(PositionAssistanceRetryResult.CANCELLED_PROCESS_RESTART),
      )
      self._persist()
    elif persisted.retry_claimed and not persisted.retry_completed:
      self._state = replace(
        persisted,
        retry_completed=True,
        retry_result=(PositionAssistanceRetryResult.INTERRUPTED_AFTER_CLAIM),
      )
      self._persist()

  @property
  def state(self) -> PositionAssistanceRetryState:
    return self._state

  @property
  def persistence_error(self) -> str | None:
    return self._persistence_error

  def _persist(self) -> bool:
    try:
      self._state_storer(self._state, self._state_path)
    except Exception as exc:
      self._persistence_error = _bounded_error(exc)
      return False
    self._persistence_error = None
    return True

  def arm_from_initial(
    self,
    execution: NavigationDatabaseRestoreExecution,
    message: bytes | None,
  ) -> bool:
    if not isinstance(execution, NavigationDatabaseRestoreExecution):
      raise ValueError("execution must be a navigation restore execution")
    if self._state.retry_armed or self._state.retry_completed:
      return self._state.pending

    exact_not_ready_rejection = (
      execution.position_assistance_attempted
      and execution.position_assistance_write_status is PositionAssistanceWriteStatus.SUCCEEDED
      and execution.position_assistance_ack_status is PositionAssistanceAckStatus.REJECTED
      and execution.position_assistance_ack_info_code == POSITION_ASSISTANCE_NOT_READY_INFO_CODE
      and isinstance(message, bytes)
      and len(message) >= 8
    )
    self._message = message if exact_not_ready_rejection else None
    self._state = replace(
      self._state,
      initial_attempted=execution.position_assistance_attempted,
      initial_write_status=(execution.position_assistance_write_status),
      initial_ack_status=execution.position_assistance_ack_status,
      initial_ack_info_code=(execution.position_assistance_ack_info_code),
      retry_armed=exact_not_ready_rejection,
      retry_result=(PositionAssistanceRetryResult.ARMED if exact_not_ready_rejection else PositionAssistanceRetryResult.NOT_ARMED),
    )
    if not self._persist():
      if exact_not_ready_rejection:
        self._state = replace(
          self._state,
          retry_claimed=True,
          retry_completed=True,
          retry_result=(PositionAssistanceRetryResult.CLAIM_PERSIST_FAILED),
          retry_triggered_at=0.0,
          retry_error_type="StatePersistenceError",
          retry_error=self._persistence_error,
        )
      else:
        self._state = replace(
          self._state,
          retry_result=(PositionAssistanceRetryResult.CLAIM_PERSIST_FAILED),
          retry_error_type="StatePersistenceError",
          retry_error=self._persistence_error,
        )
      self._message = None
      return False
    return self._state.pending

  def cancel(
    self,
    result: PositionAssistanceRetryResult,
    triggered_at: float,
  ) -> PositionAssistanceRetryState:
    if result not in (
      PositionAssistanceRetryResult.CANCELLED_EXISTING_FIX,
      PositionAssistanceRetryResult.CANCELLED_RECEIVER_CYCLE_CHANGED,
    ):
      raise ValueError("cancellation result is invalid")
    if not self._state.pending:
      return self._state
    self._state = replace(
      self._state,
      retry_triggered_at=float(triggered_at),
      retry_claimed=True,
      retry_completed=True,
      retry_result=result,
    )
    self._persist()
    return self._state

  def retry_once(
    self,
    send_message: Callable[[bytes], object],
    triggered_at: float,
  ) -> PositionAssistanceRetryState:
    if not callable(send_message):
      raise ValueError("send_message must be callable")
    message = self._message
    if not self._state.pending or message is None:
      return self._state

    self._state = replace(
      self._state,
      retry_triggered_at=float(triggered_at),
      retry_claimed=True,
    )
    if not self._persist():
      self._state = replace(
        self._state,
        retry_completed=True,
        retry_result=(PositionAssistanceRetryResult.CLAIM_PERSIST_FAILED),
        retry_error_type="StatePersistenceError",
        retry_error=self._persistence_error,
      )
      return self._state

    try:
      send_message(message)
    except MgaReceiverNackError as exc:
      state = replace(
        self._state,
        retry_completed=True,
        retry_result=PositionAssistanceRetryResult.REJECTED,
        retry_write_status=PositionAssistanceWriteStatus.SUCCEEDED,
        retry_ack_status=PositionAssistanceAckStatus.REJECTED,
        retry_ack_info_code=exc.info_code,
        retry_error_type=type(exc).__name__,
        retry_error=_bounded_error(exc),
      )
    except TimeoutError as exc:
      state = replace(
        self._state,
        retry_completed=True,
        retry_result=PositionAssistanceRetryResult.TIMED_OUT,
        retry_write_status=PositionAssistanceWriteStatus.SUCCEEDED,
        retry_ack_status=PositionAssistanceAckStatus.TIMED_OUT,
        retry_error_type=type(exc).__name__,
        retry_error=_bounded_error(exc),
      )
    except MgaWriteError as exc:
      state = replace(
        self._state,
        retry_completed=True,
        retry_result=PositionAssistanceRetryResult.WRITE_FAILED,
        retry_write_status=PositionAssistanceWriteStatus.FAILED,
        retry_ack_status=(PositionAssistanceAckStatus.NOT_ATTEMPTED),
        retry_error_type=type(exc).__name__,
        retry_error=_bounded_error(exc),
      )
    except MgaTransactionError as exc:
      write_succeeded = exc.write_succeeded is True
      state = replace(
        self._state,
        retry_completed=True,
        retry_result=(PositionAssistanceRetryResult.ACK_OBSERVATION_FAILED if write_succeeded else PositionAssistanceRetryResult.WRITE_FAILED),
        retry_write_status=(PositionAssistanceWriteStatus.SUCCEEDED if write_succeeded else PositionAssistanceWriteStatus.FAILED),
        retry_ack_status=(PositionAssistanceAckStatus.OBSERVATION_FAILED if write_succeeded else PositionAssistanceAckStatus.NOT_ATTEMPTED),
        retry_error_type=type(exc).__name__,
        retry_error=_bounded_error(exc),
      )
    except Exception as exc:
      state = replace(
        self._state,
        retry_completed=True,
        retry_result=PositionAssistanceRetryResult.WRITE_FAILED,
        retry_write_status=PositionAssistanceWriteStatus.FAILED,
        retry_ack_status=(PositionAssistanceAckStatus.NOT_ATTEMPTED),
        retry_error_type=type(exc).__name__,
        retry_error=_bounded_error(exc),
      )
    else:
      state = replace(
        self._state,
        retry_completed=True,
        retry_result=PositionAssistanceRetryResult.ACCEPTED,
        retry_write_status=PositionAssistanceWriteStatus.SUCCEEDED,
        retry_ack_status=PositionAssistanceAckStatus.ACCEPTED,
        retry_ack_info_code=0,
      )

    self._state = state
    self._persist()
    self._message = None
    return self._state
