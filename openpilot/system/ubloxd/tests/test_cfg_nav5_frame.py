from __future__ import annotations

from collections.abc import Callable

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import (
  Nav5Config,
  add_ubx_checksum,
  build_cfg_nav5_set_message,
  parse_cfg_nav5,
  validate_ubx_frame,
)


# Historical malformed CFG-NAV5 from develop before this fix: declared length
# 0x24 (36) but carried 38 payload bytes before the trailing checksum.
MALFORMED_CFG_NAV5_MESSAGE = (
  b"\xb5\x62\x06\x24\x24\x00\x05\x00\x04\x03\x00\x00\x00\x00\x00\x00"
  + b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
  + b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x5a\x63"
)


def _independent_checksum(message_without_checksum: bytes) -> bytes:
  checksum_a = 0
  checksum_b = 0
  for value in message_without_checksum[2:]:
    checksum_a = (checksum_a + value) & 0xFF
    checksum_b = (checksum_b + checksum_a) & 0xFF
  return bytes((checksum_a, checksum_b))


def test_malformed_historical_cfg_nav5_fails_validation():
  declared = int.from_bytes(MALFORMED_CFG_NAV5_MESSAGE[4:6], "little")
  actual = len(MALFORMED_CFG_NAV5_MESSAGE) - 8
  assert declared == 36
  assert actual == 38
  assert not validate_ubx_frame(MALFORMED_CFG_NAV5_MESSAGE)


def test_cfg_nav5_set_message_payload_length_and_checksum():
  frame = build_cfg_nav5_set_message(dynamic_model=4, fix_mode=3)
  declared = int.from_bytes(frame[4:6], "little")
  actual = len(frame) - 8

  assert frame[:4] == b"\xb5\x62\x06\x24"
  assert declared == 36
  assert actual == 36
  assert len(frame) == 44
  assert validate_ubx_frame(frame)

  recomputed = _independent_checksum(frame[:-2])
  assert frame[-2:] == recomputed
  assert add_ubx_checksum(frame[:-2]) == frame


def test_cfg_nav5_set_message_semantic_fields():
  frame = build_cfg_nav5_set_message(dynamic_model=4, fix_mode=3)
  payload = frame[6:-2]
  mask = int.from_bytes(payload[0:2], "little")
  dynamic_model = payload[2]
  fix_mode = payload[3]

  assert mask == 0x0005
  assert dynamic_model == 4
  assert fix_mode == 3
  assert parse_cfg_nav5(frame) == Nav5Config(dynamic_model=4, fix_mode=3)


def test_cfg_nav5_uart_boundary_rejects_malformed_and_sends_valid(monkeypatch):
  class RecordingPigeon(pigeond.TTYPigeon):
    def __init__(self) -> None:
      self.sent: list[bytes] = []
      self._serial = None

    def begin_response_transaction(
      self,
      data: bytes,
      operation: str = "response_transaction",
      before_send: Callable[[], None] | None = None,
      deadline: float | None = None,
    ) -> pigeond.ResponseTransaction:
      if before_send is not None:
        before_send()
      self.sent.append(data)
      return pigeond.ResponseTransaction(
        parser=pigeond.UbxStreamParser(),
        request=data,
        operation=operation,
        sent_at=0.0,
      )

  pigeon = RecordingPigeon()

  with pytest.raises(
    pigeond.ReceiverConfigurationError,
    match="invalid UBX CFG frame",
  ):
    pigeon.send_with_ack(MALFORMED_CFG_NAV5_MESSAGE)
  assert pigeon.sent == []

  valid = build_cfg_nav5_set_message(dynamic_model=4, fix_mode=3)
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", lambda *_args, **_kwargs: True)
  pigeon.send_with_ack(valid)

  assert pigeon.sent == [valid]
  assert validate_ubx_frame(valid)


def test_cfg_nav5_configuration_item_writes_until_dynamic_model_4():
  observed = [
    Nav5Config(dynamic_model=0, fix_mode=3),
    Nav5Config(dynamic_model=4, fix_mode=3),
  ]
  writes: list[bytes] = []
  valid = build_cfg_nav5_set_message(dynamic_model=4, fix_mode=3)

  def verify(value: object) -> None:
    assert isinstance(value, Nav5Config)
    if value.dynamic_model != 4 or value.fix_mode != 3:
      raise pigeond.ReceiverConfigurationError(f"CFG-NAV5 mismatch: {value}")

  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-NAV5",
    mandatory=True,
    expected_value="dynamic=4, fix=3",
    poll=lambda: observed.pop(0),
    verify=verify,
    write=lambda: writes.append(valid),
  )

  assert result.verified
  assert result.write_attempt_count == 1
  assert result.poll_attempt_count == 2
  assert writes == [valid]
  assert parse_cfg_nav5(valid) == Nav5Config(dynamic_model=4, fix_mode=3)


def test_pigeond_exports_cfg_nav5_builder():
  assert callable(pigeond.build_cfg_nav5_set_message)
  frame = pigeond.build_cfg_nav5_set_message(dynamic_model=4, fix_mode=3)
  assert validate_ubx_frame(frame)
  assert parse_cfg_nav5(frame) == Nav5Config(4, 3)
