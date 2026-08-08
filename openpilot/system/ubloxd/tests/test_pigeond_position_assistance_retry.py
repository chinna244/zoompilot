from __future__ import annotations

import inspect
import json
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import (
  NavPvtFix,
  build_position_assistance_message,
)
from openpilot.system.ubloxd.navigation_database_restore import (
  NavigationDatabaseRestoreDisposition,
)
from openpilot.system.ubloxd.navigation_database_restore_runtime import (
  NavigationDatabaseRestoreExecution,
  PositionAssistanceAckStatus,
  PositionAssistanceWriteStatus,
)
from openpilot.system.ubloxd.position_assistance_retry import (
  PositionAssistanceRetryResult,
  PositionAssistanceRetryRuntime,
  PositionAssistanceRetryState,
  PositionAssistanceRetryStateError,
  load_position_assistance_retry_state,
  store_position_assistance_retry_state,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  MgaReceiverNackError,
)


BOOT_ID = "12345678-1234-5678-9234-567812345678"
POSITION_MESSAGE = build_position_assistance_message(
  latitude_e7=320_000_000,
  longitude_e7=-960_000_000,
  altitude_cm=20_000,
  position_accuracy_cm=5_000_000,
)


def execution(
  *,
  write_status: PositionAssistanceWriteStatus,
  ack_status: PositionAssistanceAckStatus,
  info_code: int | None,
) -> NavigationDatabaseRestoreExecution:
  return NavigationDatabaseRestoreExecution(
    disposition=(NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED),
    total_frame_count=0,
    accepted_frame_count=0,
    database_write_attempt_count=0,
    position_assistance_attempted=True,
    position_assistance_succeeded=False,
    position_assistance_message_id=0x40,
    position_assistance_message_type=0x01,
    position_assistance_write_status=write_status,
    position_assistance_ack_status=ack_status,
    position_assistance_ack_info_code=info_code,
  )


def runtime(tmp_path: Path) -> PositionAssistanceRetryRuntime:
  return PositionAssistanceRetryRuntime(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    state_path=tmp_path / "position_retry.json",
    boot_id_reader=lambda: BOOT_ID,
  )


def fix(*, fix_ok: bool) -> NavPvtFix:
  return NavPvtFix(
    fix_ok=fix_ok,
    satellites=0,
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    horizontal_accuracy_cm=4_000_000,
    vertical_accuracy_cm=4_000_000,
  )


def build_no_fix_nav_pvt_frame() -> bytes:
  payload = bytearray(92)
  payload[20] = 3
  payload[21] = 0
  return pigeond.add_ubx_checksum(b"\xb5\x62\x01\x07" + len(payload).to_bytes(2, "little") + payload)


def test_exact_initial_info_code_5_arms_retry(
  tmp_path: Path,
) -> None:
  retry = runtime(tmp_path)

  armed = retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )

  assert armed
  assert retry.state.pending
  assert retry.state.retry_result is PositionAssistanceRetryResult.ARMED


@pytest.mark.parametrize(
  ("write_status", "ack_status", "info_code"),
  (
    (
      PositionAssistanceWriteStatus.SUCCEEDED,
      PositionAssistanceAckStatus.ACCEPTED,
      0,
    ),
    (
      PositionAssistanceWriteStatus.SUCCEEDED,
      PositionAssistanceAckStatus.REJECTED,
      4,
    ),
    (
      PositionAssistanceWriteStatus.SUCCEEDED,
      PositionAssistanceAckStatus.TIMED_OUT,
      None,
    ),
    (
      PositionAssistanceWriteStatus.FAILED,
      PositionAssistanceAckStatus.NOT_ATTEMPTED,
      None,
    ),
  ),
)
def test_only_exact_initial_not_ready_rejection_arms(
  tmp_path: Path,
  write_status: PositionAssistanceWriteStatus,
  ack_status: PositionAssistanceAckStatus,
  info_code: int | None,
) -> None:
  retry = runtime(tmp_path)

  assert not retry.arm_from_initial(
    execution(
      write_status=write_status,
      ack_status=ack_status,
      info_code=info_code,
    ),
    POSITION_MESSAGE,
  )
  assert not retry.state.pending
  assert retry.state.retry_result is PositionAssistanceRetryResult.NOT_ARMED


def test_retry_claim_is_persisted_before_receiver_write(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "position_retry.json"
  retry = PositionAssistanceRetryRuntime(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
  )
  assert retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )
  writes: list[bytes] = []

  def send(message: bytes) -> None:
    persisted = load_position_assistance_retry_state(state_path)
    assert persisted is not None
    assert persisted.retry_claimed
    assert not persisted.retry_completed
    writes.append(message)

  state = retry.retry_once(send, 101.0)

  assert writes == [POSITION_MESSAGE]
  assert state.retry_completed
  assert state.retry_result is PositionAssistanceRetryResult.ACCEPTED
  assert state.retry_ack_status is PositionAssistanceAckStatus.ACCEPTED


def test_retry_rejection_is_structured_and_not_repeated(
  tmp_path: Path,
) -> None:
  retry = runtime(tmp_path)
  assert retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )
  calls = 0

  def reject(_message: bytes) -> None:
    nonlocal calls
    calls += 1
    raise MgaReceiverNackError(
      "still not ready",
      message_id=0x40,
      message_type=0x01,
      info_code=5,
    )

  first = retry.retry_once(reject, 101.0)
  second = retry.retry_once(reject, 102.0)

  assert calls == 1
  assert first == second
  assert first.retry_result is PositionAssistanceRetryResult.REJECTED
  assert first.retry_ack_status is PositionAssistanceAckStatus.REJECTED
  assert first.retry_ack_info_code == 5


def test_same_boot_process_restart_cancels_pending_retry(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "position_retry.json"
  first = PositionAssistanceRetryRuntime(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
  )
  assert first.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )

  second = PositionAssistanceRetryRuntime(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
  )
  writes: list[bytes] = []
  state = second.retry_once(writes.append, 101.0)

  assert writes == []
  assert state.retry_completed
  assert state.retry_result is PositionAssistanceRetryResult.CANCELLED_PROCESS_RESTART


def test_controller_uses_first_same_cycle_post_start_nav_pvt(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  retry = runtime(tmp_path)
  assert retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )
  controller = pigeond.PositionAssistancePostStartRetryController(retry)
  controller.begin_receiver_cycle(7, 100.0)
  writes: list[bytes] = []
  monkeypatch.setattr(
    pigeond,
    "parse_nav_pvt",
    lambda frame: fix(fix_ok=False) if frame == b"nav" else None,
  )
  monkeypatch.setattr(
    pigeond,
    "log_position_assistance_retry_state",
    lambda *_args, **_kwargs: None,
  )

  controller.observe_frames([b"nav"], 99.0, 7)
  controller.observe_frames([b"nav"], 101.0, 8)
  controller.observe_frames([], 1000.0, 7)
  assert writes == []
  assert retry.state.pending
  assert not controller.retry_ready

  controller.observe_frames([b"nav"], 101.0, 7)
  controller.observe_frames([b"nav"], 102.0, 7)

  assert writes == []
  assert controller.retry_ready
  controller.execute_ready(writes.append)
  controller.execute_ready(writes.append)

  assert writes == [POSITION_MESSAGE]
  assert retry.state.retry_result is PositionAssistanceRetryResult.ACCEPTED


def test_first_post_start_nav_pvt_with_fix_cancels_retry(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  retry = runtime(tmp_path)
  assert retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )
  controller = pigeond.PositionAssistancePostStartRetryController(retry)
  controller.begin_receiver_cycle(3, 50.0)
  writes: list[bytes] = []
  monkeypatch.setattr(
    pigeond,
    "parse_nav_pvt",
    lambda _frame: fix(fix_ok=True),
  )
  monkeypatch.setattr(
    pigeond,
    "log_position_assistance_retry_state",
    lambda *_args, **_kwargs: None,
  )

  controller.observe_frames([b"nav"], 51.0, 3)
  controller.execute_ready(writes.append)

  assert writes == []
  assert not controller.retry_ready
  assert retry.state.retry_result is PositionAssistanceRetryResult.CANCELLED_EXISTING_FIX


def test_receiver_cycle_change_cancels_pending_retry(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  retry = runtime(tmp_path)
  assert retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )
  controller = pigeond.PositionAssistancePostStartRetryController(retry)
  controller.begin_receiver_cycle(1, 20.0)
  monkeypatch.setattr(
    pigeond,
    "log_position_assistance_retry_state",
    lambda *_args, **_kwargs: None,
  )

  controller.cancel_receiver_cycle(21.0)

  assert retry.state.retry_result is PositionAssistanceRetryResult.CANCELLED_RECEIVER_CYCLE_CHANGED


def test_pre_start_input_is_drained_only_when_retry_is_armed(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []

  class Pigeon:
    def drain_before_transaction(self, operation: str) -> None:
      events.append(operation)

    def send(self, message: bytes) -> None:
      if message == pigeond.CONTROLLED_GNSS_STOP_MESSAGE:
        events.append("stop")
      elif message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        events.append("start")

  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)

  with pigeond.install_pre_acquisition_initialization(lambda: None) as initialization:
    initialization.require_pre_gnss_start_drain()
    with pigeond.paused_gnss_acquisition(Pigeon()):  # ty: ignore[invalid-argument-type]
      initialization.run()

  assert events == [
    "stop",
    "position_assistance_pre_gnss_start_boundary",
    "start",
  ]


def test_pre_start_drain_failure_still_attempts_gnss_start(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []

  class Pigeon:
    def drain_before_transaction(self, operation: str) -> None:
      events.append(operation)
      raise OSError("drain failed")

    def send(self, message: bytes) -> None:
      if message == pigeond.CONTROLLED_GNSS_STOP_MESSAGE:
        events.append("stop")
      elif message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        events.append("start")

  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)

  with pytest.raises(OSError, match="drain failed"):
    with pigeond.install_pre_acquisition_initialization(lambda: None) as initialization:
      initialization.require_pre_gnss_start_drain()
      with pigeond.paused_gnss_acquisition(Pigeon()):  # ty: ignore[invalid-argument-type]
        initialization.run()

  assert events == [
    "stop",
    "position_assistance_pre_gnss_start_boundary",
    "start",
  ]


def test_normal_gnss_start_does_not_run_position_retry_drain(
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  events: list[str] = []

  class Pigeon:
    def drain_before_transaction(self, operation: str) -> None:
      events.append(operation)

    def send(self, message: bytes) -> None:
      if message == pigeond.CONTROLLED_GNSS_STOP_MESSAGE:
        events.append("stop")
      elif message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        events.append("start")

  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)

  with pigeond.paused_gnss_acquisition(Pigeon()):  # ty: ignore[invalid-argument-type]
    pass

  assert events == ["stop", "start"]


def test_retry_log_contains_structured_result(
  tmp_path: Path,
) -> None:
  retry = runtime(tmp_path)
  assert retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )
  state = retry.cancel(
    PositionAssistanceRetryResult.CANCELLED_EXISTING_FIX,
    101.0,
  )

  message = pigeond.format_position_assistance_retry_state(
    state,
    trigger="first_post_start_nav_pvt",
    receiver_cycle=2,
    gnss_start_sent_at=100.0,
    nav_pvt_observed_at=101.0,
    persistence_error=None,
  )

  assert "position_assistance_initial_ack_info_code=5" in message
  assert "position_assistance_retry_trigger=first_post_start_nav_pvt" in message
  assert "position_assistance_retry_result=cancelled_existing_fix" in message
  assert "position_assistance_retry_receiver_cycle=2" in message


def test_retry_controller_is_active_immediately_after_gnss_start(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  retry = runtime(tmp_path)
  controller = pigeond.PositionAssistancePostStartRetryController(retry)
  events: list[str] = []

  class Pigeon:
    receiver_cycle = 4

    def drain_before_transaction(self, operation: str) -> None:
      events.append(operation)

    def send(self, message: bytes) -> None:
      if message == pigeond.CONTROLLED_GNSS_STOP_MESSAGE:
        events.append("stop")
      elif message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        events.append("start")

  def pre_acquisition() -> None:
    events.append("arm")
    assert retry.arm_from_initial(
      execution(
        write_status=PositionAssistanceWriteStatus.SUCCEEDED,
        ack_status=PositionAssistanceAckStatus.REJECTED,
        info_code=5,
      ),
      POSITION_MESSAGE,
    )
    initialization.require_pre_gnss_start_drain()

  def gnss_started(now: float) -> None:
    events.append("begin")
    controller.begin_receiver_cycle(4, now)

  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(
    pigeond,
    "parse_nav_pvt",
    lambda frame: fix(fix_ok=False) if frame == b"nav" else None,
  )
  monkeypatch.setattr(
    pigeond,
    "log_position_assistance_retry_state",
    lambda *_args, **_kwargs: None,
  )

  with pigeond.install_pre_acquisition_initialization(
    pre_acquisition,
    gnss_started,
  ) as initialization:
    with pigeond.paused_gnss_acquisition(Pigeon()):  # ty: ignore[invalid-argument-type]
      initialization.run()

  assert events == [
    "stop",
    "arm",
    "position_assistance_pre_gnss_start_boundary",
    "start",
    "begin",
  ]
  assert initialization.gnss_start_sent_at is not None
  writes: list[bytes] = []
  controller.observe_frames(
    [b"nav"],
    initialization.gnss_start_sent_at + 0.001,
    4,
  )
  assert writes == []
  assert controller.retry_ready
  controller.execute_ready(writes.append)
  assert writes == [POSITION_MESSAGE]


def test_live_integration_uses_safe_retry_boundaries() -> None:
  initialize_source = inspect.getsource(pigeond.initialize_receiver_cycle)
  run_source = inspect.getsource(pigeond.run_receiving)

  assert initialize_source.index("retry_runtime.arm_from_initial") < initialize_source.index("with install_pre_acquisition_initialization")
  assert "initialization.require_pre_gnss_start_drain()" in initialize_source
  assert "note_gnss_start_sent" in initialize_source
  assert "position_assistance_retry.observe_frames(" in run_source
  assert "position_assistance_retry.process_frames(" not in run_source
  assert run_source.count("execute_position_assistance_retry()") == 5
  assert run_source.count("position_assistance_retry=position_assistance_retry") == 2
  assert run_source.count("recover_receiver(") == 3


@pytest.mark.parametrize("info_code", [1, 2, 3, 6])
def test_other_initial_nack_codes_do_not_arm(
  tmp_path: Path,
  info_code: int,
) -> None:
  retry = runtime(tmp_path)

  assert not retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=info_code,
    ),
    POSITION_MESSAGE,
  )
  assert not retry.state.pending
  assert retry.state.retry_result is PositionAssistanceRetryResult.NOT_ARMED


def test_initial_ack_observation_failure_does_not_arm(
  tmp_path: Path,
) -> None:
  retry = runtime(tmp_path)

  assert not retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.OBSERVATION_FAILED,
      info_code=None,
    ),
    POSITION_MESSAGE,
  )
  assert not retry.state.pending
  assert retry.state.retry_result is PositionAssistanceRetryResult.NOT_ARMED


def test_restart_after_persisted_claim_is_interrupted_without_write(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "position_retry.json"
  first = PositionAssistanceRetryRuntime(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
  )
  assert first.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )
  store_position_assistance_retry_state(
    replace(
      first.state,
      retry_triggered_at=100.0,
      retry_claimed=True,
      retry_completed=False,
    ),
    state_path,
  )

  second = PositionAssistanceRetryRuntime(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    state_path=state_path,
    boot_id_reader=lambda: BOOT_ID,
  )
  writes: list[bytes] = []
  state = second.retry_once(writes.append, 101.0)

  assert writes == []
  assert state.retry_completed
  assert state.retry_result is PositionAssistanceRetryResult.INTERRUPTED_AFTER_CLAIM


def test_arm_persistence_failure_is_structured_and_logged_by_integration(
  tmp_path: Path,
) -> None:
  def fail_store(_state: object, _path: Path) -> None:
    raise OSError("storage unavailable")

  retry = PositionAssistanceRetryRuntime(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    state_path=tmp_path / "position_retry.json",
    boot_id_reader=lambda: BOOT_ID,
    state_storer=fail_store,
  )

  assert not retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )
  assert retry.state.retry_result is PositionAssistanceRetryResult.CLAIM_PERSIST_FAILED
  assert retry.persistence_error is not None

  initialize_source = inspect.getsource(pigeond.initialize_receiver_cycle)
  assert "PositionAssistanceRetryResult.CLAIM_PERSIST_FAILED" in initialize_source
  assert "retry_runtime.persistence_error is not None" in initialize_source
  assert "log_position_assistance_retry_state(" in initialize_source


def test_nav_pvt_arriving_during_active_cfg_transaction_defers_retry(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  retry = runtime(tmp_path)
  assert retry.arm_from_initial(
    execution(
      write_status=PositionAssistanceWriteStatus.SUCCEEDED,
      ack_status=PositionAssistanceAckStatus.REJECTED,
      info_code=5,
    ),
    POSITION_MESSAGE,
  )
  controller = pigeond.PositionAssistancePostStartRetryController(retry)
  controller.begin_receiver_cycle(4, 100.0)
  events: list[str] = []
  monkeypatch.setattr(
    pigeond,
    "log_position_assistance_retry_state",
    lambda *_args, **_kwargs: None,
  )

  cfg_request = pigeond.add_ubx_checksum(b"\xb5\x62\x06\x08\x00\x00")
  cfg_ack = pigeond.add_ubx_checksum(b"\xb5\x62\x05\x01\x02\x00" + cfg_request[2:4])
  nav_pvt = build_no_fix_nav_pvt_frame()
  assert pigeond.parse_nav_pvt(nav_pvt) is not None

  class TransactionPigeon(pigeond.TTYPigeon):
    def __init__(
      self,
      raw_publisher: Callable[[bytes], None] | None = None,
      frame_dispatcher: Callable[[list[bytes]], None] | None = None,
    ) -> None:
      del raw_publisher, frame_dispatcher
      self._stream_parser = pigeond.UbxStreamParser()
      self._pending_frames = deque()
      self._pending_frame_bytes = 0
      self._pending_unpublished = None
      self._raw_publisher = None
      self._frame_dispatcher = self.dispatch
      self._receiver_cycle = 4
      self.sent: list[bytes] = []
      self.reads: deque[bytes] = deque([nav_pvt, cfg_ack])

    def dispatch(self, frames: list[bytes]) -> None:
      events.append("dispatch")
      assert self.sent == [cfg_request]
      controller.observe_frames(frames, 101.0, self.receiver_cycle)
      assert self.sent == [cfg_request]

    def send(self, dat: bytes) -> None:
      events.append("cfg" if dat == cfg_request else "unexpected")
      self.sent.append(dat)

    def _read_stream(self) -> tuple[bytes, list[bytes]]:
      return b"", []

    def receive_transaction_data(
      self,
      transaction: pigeond.ResponseTransaction,
    ) -> tuple[bytes, list[bytes], list[bytes]]:
      data = self.reads.popleft() if self.reads else b""
      return data, self._stream_parser.feed(data), transaction.parser.feed(data)

  pigeon = TransactionPigeon()
  transaction = pigeon.begin_response_transaction(cfg_request, "outer_cfg")

  assert events == ["cfg"]
  assert pigeon.sent == [cfg_request]
  assert not controller.retry_ready

  assert pigeon.wait_for_ack(transaction)

  assert events == ["cfg", "dispatch"]
  assert pigeon.sent == [cfg_request]
  assert controller.retry_ready
  assert retry.state.pending

  def send_retry(message: bytes) -> None:
    events.append("mga")
    pigeon.sent.append(message)

  controller.execute_ready(send_retry)
  controller.execute_ready(send_retry)

  assert events == ["cfg", "dispatch", "mga"]
  assert pigeon.sent == [cfg_request, POSITION_MESSAGE]
  assert retry.state.retry_result is PositionAssistanceRetryResult.ACCEPTED


@pytest.mark.parametrize("contents", ["{broken", "{}"])
def test_invalid_existing_retry_state_fails_closed_without_overwrite(
  tmp_path: Path,
  contents: str,
) -> None:
  state_path = tmp_path / "position_retry.json"
  state_path.write_text(contents, encoding="utf-8")
  before = state_path.read_bytes()

  with pytest.raises(
    PositionAssistanceRetryStateError,
    match="state load failed",
  ):
    PositionAssistanceRetryRuntime(
      "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
      state_path=state_path,
      boot_id_reader=lambda: BOOT_ID,
    )

  assert state_path.read_bytes() == before


def test_invalid_retry_state_value_fails_closed_without_overwrite(
  tmp_path: Path,
) -> None:
  state_path = tmp_path / "position_retry.json"
  first = runtime(tmp_path)
  value = first.state.to_json_dict()
  value["retry_armed"] = "invalid"
  state_path.write_text(
    json.dumps(value, separators=(",", ":"), sort_keys=True),
    encoding="utf-8",
  )
  before = state_path.read_bytes()

  with pytest.raises(
    PositionAssistanceRetryStateError,
    match="state load failed",
  ):
    PositionAssistanceRetryRuntime(
      "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
      state_path=state_path,
      boot_id_reader=lambda: BOOT_ID,
    )

  assert state_path.read_bytes() == before


def test_retry_state_read_error_disables_runtime_without_store_or_write(
  tmp_path: Path,
) -> None:
  stores: list[tuple[PositionAssistanceRetryState, Path]] = []

  def fail_load(_path: Path) -> PositionAssistanceRetryState | None:
    raise OSError("read unavailable")

  def record_store(
    state: PositionAssistanceRetryState,
    path: Path,
  ) -> None:
    stores.append((state, path))

  with pytest.raises(
    PositionAssistanceRetryStateError,
    match="OSError:read unavailable",
  ):
    PositionAssistanceRetryRuntime(
      "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
      state_path=tmp_path / "position_retry.json",
      boot_id_reader=lambda: BOOT_ID,
      state_loader=fail_load,
      state_storer=record_store,
    )

  writes: list[bytes] = []
  controller = pigeond.PositionAssistancePostStartRetryController(None)
  controller.begin_receiver_cycle(1, 100.0)
  controller.observe_frames([build_no_fix_nav_pvt_frame()], 101.0, 1)
  controller.execute_ready(writes.append)

  assert stores == []
  assert writes == []
  run_source = inspect.getsource(pigeond.run_receiving)
  assert "GPS position assistance retry state unavailable" in run_source
  assert "position_assistance_retry_runtime = None" in run_source
