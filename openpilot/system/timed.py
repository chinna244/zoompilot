#!/usr/bin/env python3
import datetime
import subprocess
import time
from typing import NoReturn

from openpilot.cereal import log
import openpilot.cereal.messaging as messaging
from openpilot.common.time_helpers import (
  HostTimeSource,
  MAX_DATE,
  mark_time_synced,
  min_date,
  set_system_time,
  system_time_valid,
)
from openpilot.common.gps_time import ublox_gps_time_valid
from openpilot.common.swaglog import cloudlog
from openpilot.common.params import Params
from openpilot.common.gps import (
  GPS_SOURCE_STATE_FRESH_SECONDS,
  GPS_SOURCE_STATE_SERVICE,
  accept_gps_source_epoch,
  gps_source_state_is_fresh,
  selected_source_to_service,
)


def set_time(new_time: datetime.datetime) -> bool:
  cloudlog.debug(f"Setting time from trusted source: {new_time}")

  try:
    return set_system_time(new_time)
  except (OSError, subprocess.CalledProcessError):
    cloudlog.exception("timed.failed_setting_time")
    return False


def main() -> NoReturn:
  """
  timed has two responsibilities:
  - getting the current time from GPS
  - publishing the time in the logs

  AGNOS will also use NTP to update the time.

  PR80 coordination:
  - Follow fresh, epoch-monotonic gpsSourceState only.
  - Only synchronize system clock from u-blox when selected is ubloxPrimary.
  - QCOM fallback does NOT drive system time (coordinated no-GPS-time).
  """

  params = Params()
  _ = params

  pm = messaging.PubMaster(['clocks'])
  sm = messaging.SubMaster([GPS_SOURCE_STATE_SERVICE, 'gpsLocationExternal', 'gpsLocation'])

  selected: str | None = None
  transition_mono_ns = 0
  last_generation: int | None = None
  last_state_recv_mono: float | None = None
  accepted_transition_mono_ns: int | None = None

  while True:
    sm.update(1000)
    now_mono = time.monotonic()

    msg = messaging.new_message('clocks')
    msg.valid = system_time_valid()
    msg.clocks.wallTimeNanos = time.time_ns()
    pm.send('clocks', msg)

    if sm.updated[GPS_SOURCE_STATE_SERVICE] and sm.valid[GPS_SOURCE_STATE_SERVICE]:
      st = sm[GPS_SOURCE_STATE_SERVICE]
      recv_ns = int(sm.logMonoTime[GPS_SOURCE_STATE_SERVICE])
      cand_selected = str(st.selected)
      cand_gen = int(st.generation)
      cand_epoch = int(st.transitionMonoNs)
      if accept_gps_source_epoch(
        transition_mono_ns=cand_epoch,
        generation=cand_gen,
        selected=cand_selected,
        recv_mono_ns=recv_ns,
        last_transition_mono_ns=accepted_transition_mono_ns,
        last_generation=last_generation,
        last_selected=selected,
      ):
        selected = cand_selected
        transition_mono_ns = cand_epoch
        last_generation = cand_gen
        accepted_transition_mono_ns = cand_epoch
        last_state_recv_mono = recv_ns / 1e9
      # else: reject regressing/inconsistent/future epoch; keep prior authority

    if not gps_source_state_is_fresh(
      now_mono=now_mono,
      last_state_recv_mono=last_state_recv_mono,
      fresh_seconds=GPS_SOURCE_STATE_FRESH_SECONDS,
    ):
      continue

    if selected is None:
      continue

    service = selected_source_to_service(selected)
    if service is None:
      continue

    if service != "gpsLocationExternal":
      continue

    if not sm.updated[service]:
      continue
    if (now_mono - sm.logMonoTime[service] / 1e9) > 2.0:
      continue

    if sm.logMonoTime[service] <= transition_mono_ns:
      continue

    gps = sm[service]
    if gps.source != log.GpsLocationData.SensorSource.ublox:
      continue
    if not ublox_gps_time_valid(gps.flags):
      continue

    gps_time = datetime.datetime.fromtimestamp(
      gps.unixTimestampMillis / 1000.0,
      tz=datetime.UTC,
    )

    minimum_time = min_date().replace(tzinfo=datetime.UTC)
    maximum_time = MAX_DATE.replace(tzinfo=datetime.UTC)
    if gps_time < minimum_time or gps_time > maximum_time:
      continue

    if set_time(gps_time):
      if not mark_time_synced(HostTimeSource.RECEIVER_DERIVED):
        cloudlog.warning("Failed to write trusted GPS time marker")
      time.sleep(10)


if __name__ == "__main__":
  main()
