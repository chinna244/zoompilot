from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from math import isfinite
from pathlib import Path
import time

from openpilot.common.swaglog import cloudlog
from openpilot.system.ubloxd.gps_assistance import CacheValidationError
from openpilot.system.ubloxd.yuma_almanac import (
  YumaAlmanacError,
  validate_yuma_reference_time,
)
from openpilot.system.ubloxd.yuma_almanac_store import (
  YUMA_ALMANAC_CACHE_PATH,
  StoredYumaAlmanac,
  load_yuma_almanac,
)


YUMA_ALMANAC_MAX_TRANSMIT_SECONDS = 12.0
YUMA_ALMANAC_RETRY_DELAY_SECONDS = 0.25
YumaMessageSender = Callable[[bytes], None]
MonotonicClock = Callable[[], float]
Sleeper = Callable[[float], None]


class MgaReceiverNackError(CacheValidationError):
  def __init__(
    self,
    message: str,
    *,
    message_id: int | None = None,
    message_type: int | None = None,
    ack_type: int | None = None,
    ack_version: int | None = None,
    info_code: int | None = None,
    rejected_message_id: int | None = None,
  ) -> None:
    super().__init__(message)
    self.message_id = message_id
    self.message_type = message_type
    self.ack_type = ack_type
    self.ack_version = ack_version
    self.info_code = info_code
    self.rejected_message_id = rejected_message_id


class MgaWriteError(OSError):
  def __init__(
    self,
    message: str,
    *,
    message_id: int | None = None,
    message_type: int | None = None,
  ) -> None:
    super().__init__(message)
    self.message_id = message_id
    self.message_type = message_type


class MgaTransactionError(Exception):
  def __init__(
    self,
    message: str,
    *,
    message_id: int | None = None,
    message_type: int | None = None,
    write_succeeded: bool | None = None,
  ) -> None:
    super().__init__(message)
    self.message_id = message_id
    self.message_type = message_type
    self.write_succeeded = write_succeeded


class YumaAssistanceStateUnavailableError(RuntimeError):
  """Durable assistance ownership is unavailable before receiver I/O."""


class YumaAlmanacTransmitStatus(StrEnum):
  COMPLETE = "complete"
  PARTIAL = "partial"
  FAILED = "failed"
  BUDGET_EXPIRED = "budget_expired"
  NO_SATELLITES = "no_satellites"
  MISSING = "missing"
  UNAVAILABLE = "unavailable"
  TIME_UNAVAILABLE = "time_unavailable"


@dataclass(frozen=True)
class YumaAlmanacTransmitResult:
  status: YumaAlmanacTransmitStatus
  requested_satellite_ids: tuple[int, ...] = ()
  attempted_satellite_ids: tuple[int, ...] = ()
  accepted_satellite_ids: tuple[int, ...] = ()
  failed_satellite_ids: tuple[int, ...] = ()
  rejected_satellite_ids: tuple[int, ...] = ()
  timed_out_satellite_ids: tuple[int, ...] = ()
  deferred_satellite_ids: tuple[int, ...] = ()
  unavailable_satellite_ids: tuple[int, ...] = ()
  reference_time_utc: datetime | None = None
  downloaded_at_utc: datetime | None = None
  assistance_state_unavailable: bool = False

  @property
  def receiver_write_attempted(self) -> bool:
    return bool(self.attempted_satellite_ids)


def _validated_satellite_ids(
  satellite_ids: frozenset[int],
) -> tuple[int, ...]:
  if not isinstance(satellite_ids, frozenset):
    raise ValueError("satellite_ids must be a frozenset")
  if any(
    isinstance(satellite_id, bool)
    or not isinstance(satellite_id, int)
    or not 1 <= satellite_id <= 32
    for satellite_id in satellite_ids
  ):
    raise ValueError(
      "satellite_ids must contain integers from 1 through 32"
    )
  return tuple(sorted(satellite_ids))


def _validated_duration(value: float) -> float:
  if (
    isinstance(value, bool)
    or not isinstance(value, (int, float))
    or not isfinite(value)
    or value <= 0
  ):
    raise ValueError(
      "max_duration_seconds must be a positive finite number"
    )
  return float(value)


def _validated_nonnegative_duration(
  value: float,
  field: str,
) -> float:
  if (
    isinstance(value, bool)
    or not isinstance(value, (int, float))
    or not isfinite(value)
    or value < 0
  ):
    raise ValueError(
      f"{field} must be a non-negative finite number"
    )
  return float(value)


def transmit_public_yuma_almanac(
  send_message: YumaMessageSender,
  *,
  trusted_now: datetime | None,
  satellite_ids: frozenset[int],
  path: Path = YUMA_ALMANAC_CACHE_PATH,
  stored_almanac: StoredYumaAlmanac | None = None,
  max_duration_seconds: float = YUMA_ALMANAC_MAX_TRANSMIT_SECONDS,
  minimum_remaining_seconds: float = 0.0,
  monotonic: MonotonicClock = time.monotonic,
  sleep: Sleeper = time.sleep,
) -> YumaAlmanacTransmitResult:
  requested = _validated_satellite_ids(satellite_ids)
  maximum_duration = _validated_duration(max_duration_seconds)
  minimum_remaining = _validated_nonnegative_duration(
    minimum_remaining_seconds,
    "minimum_remaining_seconds",
  )

  if trusted_now is None:
    return YumaAlmanacTransmitResult(
      YumaAlmanacTransmitStatus.TIME_UNAVAILABLE,
      requested_satellite_ids=requested,
    )

  if not requested:
    return YumaAlmanacTransmitResult(
      YumaAlmanacTransmitStatus.NO_SATELLITES,
    )

  if stored_almanac is None and not path.exists():
    return YumaAlmanacTransmitResult(
      YumaAlmanacTransmitStatus.MISSING,
      requested_satellite_ids=requested,
    )

  try:
    stored = (
      load_yuma_almanac(path)
      if stored_almanac is None
      else stored_almanac
    )
    reference_time = validate_yuma_reference_time(
      stored.almanac,
      trusted_now,
    )
  except (OSError, YumaAlmanacError):
    return YumaAlmanacTransmitResult(
      YumaAlmanacTransmitStatus.UNAVAILABLE,
      requested_satellite_ids=requested,
    )
  except Exception:
    cloudlog.exception(
      "Unexpected public YUMA cache load failure"
    )
    return YumaAlmanacTransmitResult(
      YumaAlmanacTransmitStatus.UNAVAILABLE,
      requested_satellite_ids=requested,
    )

  frames_by_satellite = {
    frame[8]: frame
    for frame in stored.almanac.frames
  }
  unavailable = tuple(
    satellite_id
    for satellite_id in requested
    if satellite_id not in frames_by_satellite
  )
  pending = [
    (satellite_id, frames_by_satellite[satellite_id])
    for satellite_id in requested
    if satellite_id in frames_by_satellite
  ]

  if not pending:
    return YumaAlmanacTransmitResult(
      YumaAlmanacTransmitStatus.NO_SATELLITES,
      requested_satellite_ids=requested,
      unavailable_satellite_ids=unavailable,
      reference_time_utc=reference_time,
      downloaded_at_utc=stored.downloaded_at_utc,
    )

  started_at = monotonic()
  attempted: list[int] = []
  accepted: set[int] = set()
  failed: list[tuple[int, bytes]] = []
  rejected: set[int] = set()
  timed_out: set[int] = set()
  deferred: set[int] = set()
  budget_expired = False

  for attempt_index in range(2):
    current = pending
    pending = []

    for frame_index, (satellite_id, frame) in enumerate(current):
      elapsed = monotonic() - started_at
      if (
        elapsed >= maximum_duration
        or maximum_duration - elapsed < minimum_remaining
      ):
        deferred.update(
          item[0]
          for item in current[frame_index:]
        )
        deferred.update(
          item[0]
          for item in pending
        )
        budget_expired = True
        break

      attempted.append(satellite_id)
      try:
        send_message(frame)
      except YumaAssistanceStateUnavailableError:
        attempted.pop()
        accepted_ids = tuple(sorted(accepted))
        unavailable_ids = tuple(
          value
          for value in requested
          if value not in accepted
        )
        return YumaAlmanacTransmitResult(
          status=(
            YumaAlmanacTransmitStatus.PARTIAL
            if accepted_ids
            else YumaAlmanacTransmitStatus.UNAVAILABLE
          ),
          requested_satellite_ids=requested,
          attempted_satellite_ids=tuple(attempted),
          accepted_satellite_ids=accepted_ids,
          failed_satellite_ids=tuple(
            sorted(item[0] for item in failed)
          ),
          rejected_satellite_ids=tuple(sorted(rejected)),
          timed_out_satellite_ids=tuple(sorted(timed_out)),
          deferred_satellite_ids=tuple(sorted(deferred)),
          unavailable_satellite_ids=unavailable_ids,
          reference_time_utc=reference_time,
          downloaded_at_utc=stored.downloaded_at_utc,
          assistance_state_unavailable=True,
        )
      except TimeoutError:
        timed_out.add(satellite_id)
        pending.append((satellite_id, frame))
      except MgaReceiverNackError:
        rejected.add(satellite_id)
        pending.append((satellite_id, frame))
      except (MgaWriteError, MgaTransactionError):
        pending.append((satellite_id, frame))
      except Exception:
        cloudlog.exception(
          f"Unexpected public YUMA receiver write failure, satellite_id={satellite_id}"
        )
        raise
      else:
        accepted.add(satellite_id)

    if budget_expired or not pending:
      break

    if attempt_index == 0:
      sleep(YUMA_ALMANAC_RETRY_DELAY_SECONDS)

  if pending and not budget_expired:
    failed = pending

  failed_ids = tuple(sorted(item[0] for item in failed))
  accepted_ids = tuple(sorted(accepted))
  rejected_ids = tuple(sorted(rejected))
  timed_out_ids = tuple(sorted(timed_out))
  deferred_ids = tuple(sorted(deferred))

  if budget_expired:
    status = (
      YumaAlmanacTransmitStatus.PARTIAL
      if accepted_ids
      else YumaAlmanacTransmitStatus.BUDGET_EXPIRED
    )
  elif not failed_ids and not unavailable:
    status = YumaAlmanacTransmitStatus.COMPLETE
  elif accepted_ids:
    status = YumaAlmanacTransmitStatus.PARTIAL
  else:
    status = YumaAlmanacTransmitStatus.FAILED

  return YumaAlmanacTransmitResult(
    status=status,
    requested_satellite_ids=requested,
    attempted_satellite_ids=tuple(attempted),
    accepted_satellite_ids=accepted_ids,
    failed_satellite_ids=failed_ids,
    rejected_satellite_ids=rejected_ids,
    timed_out_satellite_ids=timed_out_ids,
    deferred_satellite_ids=deferred_ids,
    unavailable_satellite_ids=unavailable,
    reference_time_utc=reference_time,
    downloaded_at_utc=stored.downloaded_at_utc,
  )
