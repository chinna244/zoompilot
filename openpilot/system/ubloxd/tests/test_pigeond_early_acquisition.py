from __future__ import annotations

import pytest

from openpilot.system.ubloxd import pigeond


def test_process_start_deadline_remains_45_seconds_without_legacy_wait() -> None:
  assert not hasattr(pigeond, "NAVIGATION_DATABASE_TRUSTED_TIME_WAIT_SECONDS")
  assert not hasattr(pigeond, "NAVIGATION_DATABASE_TRUSTED_TIME_POLL_SECONDS")
  assert not hasattr(pigeond, "should_wait_for_navigation_database_trusted_time")
  assert not hasattr(pigeond, "navigation_database_trusted_time_wait_expired")
  assert not hasattr(pigeond, "wait_for_current_independent_network_time")
  assert pigeond.NAVIGATION_DATABASE_PROCESS_START_TIME_DEADLINE_SECONDS == 45.0
  assert (
    pigeond.navigation_database_process_start_wait_seconds(
      cycle_started_at=10.0,
      now=13.0,
    )
    == 42.0
  )
  assert (
    pigeond.navigation_database_process_start_wait_seconds(
      cycle_started_at=10.0,
      now=60.0,
    )
    == 0.0
  )


def test_initialize_receiver_cycle_rejects_legacy_trusted_time_wait_switch() -> None:
  import inspect

  signature = inspect.signature(pigeond.initialize_receiver_cycle)
  assert "allow_database_trusted_time_wait" not in signature.parameters
  assert "network_available_reader" not in signature.parameters


def test_device_network_availability_distinguishes_unready_from_offline() -> None:
  class DeviceStateSubMaster:
    def __init__(self) -> None:
      self.alive = {"deviceState": False}
      self.valid = {"deviceState": False}
      self.device_state = type("DeviceState", (), {"networkType": pigeond.log.DeviceState.NetworkType.none})()

    def update(self, _timeout: int) -> None:
      pass

    def __getitem__(self, _service: str):
      return self.device_state

  sm = DeviceStateSubMaster()
  assert pigeond.device_network_available(sm) is None  # type: ignore[arg-type, ty:invalid-argument-type]

  sm.alive["deviceState"] = True
  sm.valid["deviceState"] = True
  assert not pigeond.device_network_available(sm)  # type: ignore[arg-type, ty:invalid-argument-type]

  sm.device_state.networkType = pigeond.log.DeviceState.NetworkType.wifi
  assert pigeond.device_network_available(sm)  # type: ignore[arg-type, ty:invalid-argument-type]


def test_receiver_acquisition_guard_records_early_dbd_outcome() -> None:
  events: list[str] = []

  class Runtime:
    acquisition_started = False
    database_restore_pending = True

    def note_early_acquisition_started(self) -> bool:
      events.append("early")
      self.acquisition_started = True
      self.database_restore_pending = False
      return True

    def note_acquisition_started(self) -> bool:
      events.append("normal")
      return True

  runtime = Runtime()
  guard = pigeond.ReceiverAcquisitionStateGuard()

  assert guard.note_once(runtime)  # type: ignore[arg-type, ty:invalid-argument-type]
  assert guard.note_once(runtime) is None  # type: ignore[arg-type, ty:invalid-argument-type]
  assert events == ["early"]


def test_transition_telemetry_uses_post_send_timestamps(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []
  logs: list[str] = []
  clock = iter((10.0, 13.5))

  class Pigeon:
    def send(self, message: bytes) -> None:
      if message == pigeond.CONTROLLED_GNSS_STOP_MESSAGE:
        events.append("stop_send")
      elif message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        events.append("start_send")

  def monotonic() -> float:
    value = next(clock)
    events.append(f"monotonic:{value:.1f}")
    return value

  def log_info(message: str) -> None:
    events.append("log")
    logs.append(message)

  monkeypatch.setattr(pigeond.time, "monotonic", monotonic)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond.cloudlog, "info", log_info)

  with pigeond.paused_gnss_acquisition(
    Pigeon(),  # type: ignore[arg-type, ty:invalid-argument-type]
  ):
    events.append("body")

  assert events == [
    "stop_send",
    "monotonic:10.0",
    "log",
    "body",
    "start_send",
    "monotonic:13.5",
    "log",
  ]
  assert logs == [
    "GPS acquisition transition: phase=stop_sent monotonic=10.000000",
    ("GPS acquisition transition: phase=start_sent monotonic=13.500000 prestart_elapsed_seconds=3.500000"),
  ]
