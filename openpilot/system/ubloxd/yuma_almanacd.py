#!/usr/bin/env python3

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from math import isfinite
from typing import NoReturn

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log

from openpilot.common.swaglog import cloudlog
from openpilot.common.time_helpers import trusted_time_synced
from openpilot.system.ubloxd.yuma_almanac_download import (
  YumaAlmanacRefreshStatus,
  refresh_public_yuma_almanac,
)


YUMA_NETWORK_STABILITY_SECONDS = 5.0
YUMA_SUCCESS_RECHECK_SECONDS = 6 * 60 * 60
YUMA_FAILURE_RETRY_SECONDS = (
  30.0,
  2 * 60.0,
  5 * 60.0,
  15 * 60.0,
  30 * 60.0,
)
UNMETERED_YUMA_NETWORK_TYPES = (
  log.DeviceState.NetworkType.wifi,
  log.DeviceState.NetworkType.ethernet,
)


def unmetered_yuma_network_available(
  network_type: log.DeviceState.NetworkType,
  network_metered: bool,
) -> bool:
  return (
    not network_metered
    and network_type in UNMETERED_YUMA_NETWORK_TYPES
  )


@dataclass
class YumaRefreshScheduler:
  eligible_since: float | None = None
  next_attempt_at: float = 0.0
  consecutive_failures: int = 0

  @staticmethod
  def _validate_monotonic(now: float) -> None:
    if (
      isinstance(now, bool)
      or not isinstance(now, (int, float))
      or not isfinite(now)
      or now < 0
    ):
      raise ValueError(
        "monotonic time must be a non-negative finite number"
      )

  def should_attempt(
    self,
    now: float,
    *,
    network_eligible: bool,
    time_trusted: bool,
  ) -> bool:
    self._validate_monotonic(now)

    if not network_eligible:
      self.eligible_since = None
      return False

    if self.eligible_since is None:
      self.eligible_since = now

    return (
      time_trusted
      and now >= self.next_attempt_at
      and (
        now - self.eligible_since
        >= YUMA_NETWORK_STABILITY_SECONDS
      )
    )

  def record_result(
    self,
    now: float,
    status: YumaAlmanacRefreshStatus,
  ) -> None:
    self._validate_monotonic(now)

    if status is YumaAlmanacRefreshStatus.FAILED:
      retry_index = min(
        self.consecutive_failures,
        len(YUMA_FAILURE_RETRY_SECONDS) - 1,
      )
      delay = YUMA_FAILURE_RETRY_SECONDS[retry_index]
      self.consecutive_failures += 1
    else:
      delay = YUMA_SUCCESS_RECHECK_SECONDS
      self.consecutive_failures = 0

    self.next_attempt_at = now + delay


def _log_refresh_result(result) -> None:
  stored_downloaded_at = (
    result.stored.downloaded_at_utc.isoformat()
    if result.stored is not None
    else "none"
  )
  candidate_reference = (
    result.candidate_reference_time_utc.isoformat()
    if result.candidate_reference_time_utc is not None
    else "none"
  )
  message = ", ".join((
    "Public YUMA almanac refresh result",
    f"status={result.status.value}",
    f"reason={result.reason}",
    f"candidate_reference_utc={candidate_reference}",
    f"stored_downloaded_at_utc={stored_downloaded_at}",
  ))

  if result.status is YumaAlmanacRefreshStatus.FAILED:
    cloudlog.warning(message)
  else:
    cloudlog.info(message)


def main() -> NoReturn:
  sm = messaging.SubMaster(["deviceState"])
  scheduler = YumaRefreshScheduler()

  while True:
    sm.update(1000)
    now = time.monotonic()
    device_state = sm["deviceState"]
    network_eligible = (
      sm.alive["deviceState"]
      and sm.valid["deviceState"]
      and unmetered_yuma_network_available(
        device_state.networkType,
        device_state.networkMetered,
      )
    )

    if not scheduler.should_attempt(
      now,
      network_eligible=network_eligible,
      time_trusted=trusted_time_synced(),
    ):
      continue

    try:
      result = refresh_public_yuma_almanac(
        trusted_now=datetime.now(UTC),
      )
    except Exception:
      cloudlog.exception(
        "Unexpected public YUMA almanac refresh failure"
      )
      scheduler.record_result(
        time.monotonic(),
        YumaAlmanacRefreshStatus.FAILED,
      )
      continue

    _log_refresh_result(result)
    scheduler.record_result(
      time.monotonic(),
      result.status,
    )


if __name__ == "__main__":
  main()
