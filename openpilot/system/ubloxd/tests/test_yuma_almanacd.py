import pytest

import openpilot.cereal.messaging as messaging
from openpilot.cereal import log

from openpilot.system.ubloxd.yuma_almanac_download import (
  YumaAlmanacRefreshStatus,
)
from openpilot.system.ubloxd.yuma_almanacd import (
  YUMA_FAILURE_RETRY_SECONDS,
  YUMA_NETWORK_STABILITY_SECONDS,
  YUMA_SUCCESS_RECHECK_SECONDS,
  YumaRefreshScheduler,
  unmetered_yuma_network_available,
)


@pytest.mark.parametrize(
  ("network_type", "metered", "expected"),
  (
    (log.DeviceState.NetworkType.wifi, False, True),
    (log.DeviceState.NetworkType.ethernet, False, True),
    (log.DeviceState.NetworkType.wifi, True, False),
    (log.DeviceState.NetworkType.cell4G, False, False),
    (log.DeviceState.NetworkType.none, False, False),
  ),
)
def test_unmetered_yuma_network_available(
  network_type,
  metered: bool,
  expected: bool,
):
  assert unmetered_yuma_network_available(
    network_type,
    metered,
  ) is expected


def test_unmetered_yuma_network_available_accepts_runtime_enum():
  message = messaging.new_message("deviceState")
  message.deviceState.networkType = (
    log.DeviceState.NetworkType.wifi
  )

  runtime_network_type = message.deviceState.networkType

  assert runtime_network_type == (
    log.DeviceState.NetworkType.wifi
  )
  assert unmetered_yuma_network_available(
    runtime_network_type,
    False,
  )


def test_scheduler_waits_for_stable_network():
  scheduler = YumaRefreshScheduler()

  assert not scheduler.should_attempt(
    100.0,
    network_eligible=True,
    time_trusted=True,
  )
  assert not scheduler.should_attempt(
    100.0 + YUMA_NETWORK_STABILITY_SECONDS - 0.001,
    network_eligible=True,
    time_trusted=True,
  )
  assert scheduler.should_attempt(
    100.0 + YUMA_NETWORK_STABILITY_SECONDS,
    network_eligible=True,
    time_trusted=True,
  )


def test_scheduler_waits_for_trusted_time():
  scheduler = YumaRefreshScheduler()

  assert not scheduler.should_attempt(
    100.0,
    network_eligible=True,
    time_trusted=False,
  )
  assert not scheduler.should_attempt(
    100.0 + YUMA_NETWORK_STABILITY_SECONDS,
    network_eligible=True,
    time_trusted=False,
  )
  assert scheduler.should_attempt(
    100.0 + YUMA_NETWORK_STABILITY_SECONDS + 1.0,
    network_eligible=True,
    time_trusted=True,
  )


@pytest.mark.parametrize(
  "status",
  (
    YumaAlmanacRefreshStatus.UPDATED,
    YumaAlmanacRefreshStatus.UNCHANGED,
    YumaAlmanacRefreshStatus.PRESERVED_NEWER,
  ),
)
def test_successful_result_uses_six_hour_recheck(status):
  scheduler = YumaRefreshScheduler(
    eligible_since=0.0,
  )
  scheduler.record_result(10.0, status)

  assert scheduler.consecutive_failures == 0
  assert not scheduler.should_attempt(
    10.0 + YUMA_SUCCESS_RECHECK_SECONDS - 0.001,
    network_eligible=True,
    time_trusted=True,
  )
  assert scheduler.should_attempt(
    10.0 + YUMA_SUCCESS_RECHECK_SECONDS,
    network_eligible=True,
    time_trusted=True,
  )


def test_failure_backoff_progresses_and_caps():
  scheduler = YumaRefreshScheduler(
    eligible_since=0.0,
  )
  now = 10.0

  for attempt_index, expected_delay in enumerate(
    YUMA_FAILURE_RETRY_SECONDS,
    start=1,
  ):
    scheduler.record_result(
      now,
      YumaAlmanacRefreshStatus.FAILED,
    )
    assert scheduler.consecutive_failures == attempt_index
    assert scheduler.next_attempt_at == now + expected_delay
    now = scheduler.next_attempt_at

  scheduler.record_result(
    now,
    YumaAlmanacRefreshStatus.FAILED,
  )
  assert scheduler.next_attempt_at == (
    now + YUMA_FAILURE_RETRY_SECONDS[-1]
  )


def test_success_resets_failure_backoff():
  scheduler = YumaRefreshScheduler(
    eligible_since=0.0,
  )
  scheduler.record_result(
    10.0,
    YumaAlmanacRefreshStatus.FAILED,
  )
  scheduler.record_result(
    20.0,
    YumaAlmanacRefreshStatus.FAILED,
  )
  scheduler.record_result(
    30.0,
    YumaAlmanacRefreshStatus.UNCHANGED,
  )

  assert scheduler.consecutive_failures == 0
  assert scheduler.next_attempt_at == (
    30.0 + YUMA_SUCCESS_RECHECK_SECONDS
  )


def test_network_flapping_does_not_bypass_cooldown():
  scheduler = YumaRefreshScheduler(
    eligible_since=0.0,
  )
  scheduler.record_result(
    10.0,
    YumaAlmanacRefreshStatus.UNCHANGED,
  )
  next_attempt = scheduler.next_attempt_at

  assert not scheduler.should_attempt(
    20.0,
    network_eligible=False,
    time_trusted=True,
  )
  assert not scheduler.should_attempt(
    next_attempt - 1.0,
    network_eligible=True,
    time_trusted=True,
  )
  assert not scheduler.should_attempt(
    next_attempt + YUMA_NETWORK_STABILITY_SECONDS - 1.001,
    network_eligible=True,
    time_trusted=True,
  )
  assert scheduler.should_attempt(
    next_attempt + YUMA_NETWORK_STABILITY_SECONDS,
    network_eligible=True,
    time_trusted=True,
  )


@pytest.mark.parametrize(
  "now",
  (
    -1.0,
    float("inf"),
    float("nan"),
    True,
  ),
)
def test_scheduler_rejects_invalid_monotonic_time(now):
  scheduler = YumaRefreshScheduler()

  with pytest.raises(
    ValueError,
    match="non-negative finite",
  ):
    scheduler.should_attempt(
      now,
      network_eligible=True,
      time_trusted=True,
    )
