import pytest

from openpilot.system.ubloxd import pigeond


def test_watchdog_waits_for_timeout():
  watchdog = pigeond.UbloxDataWatchdog(
    timeout=10.0,
    start_time=100.0,
  )

  assert not watchdog.check(109.999)
  assert watchdog.check(110.0)
  assert watchdog.recoveries == 1
  assert watchdog.last_recovery_reason is pigeond.ReceiverRecoveryReason.NO_DATA


def test_watchdog_raises_after_failed_recovery():
  watchdog = pigeond.UbloxDataWatchdog(
    timeout=10.0,
    max_recoveries=1,
    recovery_cooldown_seconds=0.0,
    start_time=100.0,
  )

  assert watchdog.check(110.0)
  watchdog.recovery_completed(110.0)

  with pytest.raises(
    RuntimeError,
    match="GPS receiver recovery budget exhausted",
  ):
    watchdog.check(120.0)


def test_single_data_block_does_not_reset_recovery_budget():
  watchdog = pigeond.UbloxDataWatchdog(
    timeout=10.0,
    max_recoveries=1,
    recovery_cooldown_seconds=0.0,
    healthy_rearm_seconds=10.0,
    start_time=100.0,
  )

  assert watchdog.check(110.0)
  watchdog.recovery_completed(110.0)
  assert not watchdog.note_data(111.0)

  with pytest.raises(
    RuntimeError,
    match="reason=all_zero_data",
  ):
    watchdog.request_recovery(
      pigeond.ReceiverRecoveryReason.ALL_ZERO_DATA,
      111.0,
    )


def test_sustained_healthy_data_rearms_recovery_budget():
  watchdog = pigeond.UbloxDataWatchdog(
    timeout=10.0,
    max_recoveries=1,
    recovery_cooldown_seconds=0.0,
    healthy_rearm_seconds=10.0,
    start_time=100.0,
  )

  assert watchdog.check(110.0)
  watchdog.recovery_completed(110.0)
  assert not watchdog.note_data(111.0)
  assert not watchdog.note_data(120.999)
  assert watchdog.note_data(121.0)
  assert watchdog.recoveries == 0

  assert watchdog.request_recovery(
    pigeond.ReceiverRecoveryReason.ALL_ZERO_DATA,
    121.0,
  )


def test_unhealthy_data_breaks_healthy_rearm_window():
  watchdog = pigeond.UbloxDataWatchdog(
    max_recoveries=1,
    recovery_cooldown_seconds=0.0,
    healthy_rearm_seconds=10.0,
    start_time=100.0,
  )

  assert watchdog.request_recovery(
    pigeond.ReceiverRecoveryReason.ALL_ZERO_DATA,
    100.0,
  )
  watchdog.recovery_completed(100.0)
  assert not watchdog.note_data(101.0)
  assert not watchdog.note_data(109.0, healthy=False)
  assert not watchdog.note_data(110.0)
  assert not watchdog.note_data(119.999)
  assert watchdog.note_data(120.0)


def test_recovery_cooldown_blocks_an_immediate_second_reset():
  watchdog = pigeond.UbloxDataWatchdog(
    max_recoveries=2,
    recovery_cooldown_seconds=5.0,
    start_time=100.0,
  )

  assert watchdog.request_recovery(
    pigeond.ReceiverRecoveryReason.ALL_ZERO_DATA,
    100.0,
  )
  watchdog.recovery_completed(100.0)

  assert not watchdog.request_recovery(
    pigeond.ReceiverRecoveryReason.NO_DATA,
    104.999,
  )
  assert watchdog.request_recovery(
    pigeond.ReceiverRecoveryReason.NO_DATA,
    105.0,
  )
  assert watchdog.recoveries == 2


def test_no_data_and_all_zero_share_one_recovery_budget():
  watchdog = pigeond.UbloxDataWatchdog(
    timeout=10.0,
    max_recoveries=1,
    recovery_cooldown_seconds=0.0,
    start_time=100.0,
  )

  assert watchdog.check(110.0)
  watchdog.recovery_completed(110.0)

  with pytest.raises(
    RuntimeError,
    match="reason=all_zero_data",
  ):
    watchdog.request_recovery(
      pigeond.ReceiverRecoveryReason.ALL_ZERO_DATA,
      111.0,
    )


def test_repeated_all_zero_requests_respect_configured_limit():
  watchdog = pigeond.UbloxDataWatchdog(
    max_recoveries=2,
    recovery_cooldown_seconds=0.0,
    start_time=100.0,
  )

  assert watchdog.request_recovery(
    pigeond.ReceiverRecoveryReason.ALL_ZERO_DATA,
    100.0,
  )
  watchdog.recovery_completed(100.0)
  assert watchdog.request_recovery(
    pigeond.ReceiverRecoveryReason.ALL_ZERO_DATA,
    101.0,
  )
  watchdog.recovery_completed(101.0)

  with pytest.raises(
    RuntimeError,
    match="attempts=2, max_attempts=2",
  ):
    watchdog.request_recovery(
      pigeond.ReceiverRecoveryReason.ALL_ZERO_DATA,
      102.0,
    )


def test_long_reinitialization_restarts_no_data_timeout_at_completion():
  watchdog = pigeond.UbloxDataWatchdog(
    timeout=10.0,
    max_recoveries=2,
    recovery_cooldown_seconds=0.0,
    start_time=100.0,
  )

  assert watchdog.request_recovery(
    pigeond.ReceiverRecoveryReason.ALL_ZERO_DATA,
    100.0,
  )
  watchdog.recovery_completed(150.0)

  assert not watchdog.check(159.999)
  assert watchdog.check(160.0)


def test_init_continues_when_receiver_configuration_fails(
  monkeypatch,
):
  post_start_calls = []
  monkeypatch.setattr(
    pigeond.signal,
    "signal",
    lambda *_args, **_kwargs: None,
  )
  monkeypatch.setattr(
    pigeond,
    "set_power",
    lambda _enabled: None,
  )
  monkeypatch.setattr(
    pigeond.time,
    "sleep",
    lambda _seconds: None,
  )
  monkeypatch.setattr(
    pigeond,
    "init_baudrate",
    lambda _pigeon: None,
  )
  monkeypatch.setattr(
    pigeond,
    "poll_mon_ver",
    lambda _pigeon, _timeout: object(),
  )
  monkeypatch.setattr(
    pigeond,
    "init_pigeon",
    lambda _pigeon: False,
  )
  monkeypatch.setattr(
    pigeond,
    "run_post_start_legacy_assistance",
    lambda _pigeon: post_start_calls.append(True),
  )

  class Pigeon:
    def send(self, _message: bytes) -> None:
      pass

  pigeond.init(Pigeon())

  assert post_start_calls == [True]


def test_zero_prefixed_ublox_payload_is_not_all_zero():
  payload = bytearray(4096)
  payload[1024] = 0x01

  assert not pigeond.is_all_zero_ublox_data(bytes(payload))


def test_all_zero_ublox_payload_is_detected():
  assert pigeond.is_all_zero_ublox_data(bytes(4096))


def test_empty_ublox_payload_is_not_all_zero():
  assert not pigeond.is_all_zero_ublox_data(b"")
