from collections import deque
from datetime import UTC, datetime
import json
from types import SimpleNamespace
from typing import cast

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import (
  GnssConfig,
  GnssConfigBlock,
  ItfmConfig,
  MessageRateConfig,
  Nav5Config,
  OdoConfig,
  RateConfig,
  RxmConfig,
  Pm2Config,
  UbxStreamParser,
  add_ubx_checksum,
  build_cfg_itfm_poll_message,
  build_cfg_msg_poll_message,
  build_cfg_nav5_poll_message,
  build_cfg_odo_poll_message,
  build_cfg_rate_poll_message,
  parse_cfg_itfm,
  parse_cfg_msg,
  parse_cfg_nav5,
  parse_cfg_odo,
  parse_cfg_rate,
)


def network_host_observation():
  return pigeond.HostTimeObservation(
    utc=datetime(2026, 7, 23, 12, tzinfo=UTC),
    observed_boottime_seconds=100.0,
    uncertainty_seconds=30.0,
    source=pigeond.HostTimeSource.NETWORK_SYNCHRONIZED,
    independent=True,
    generation="network:test",
  )


def ubx_frame(message_class: int, message_id: int, payload: bytes) -> bytes:
  return add_ubx_checksum(b"\xb5\x62" + bytes((message_class, message_id)) + len(payload).to_bytes(2, "little") + payload)


def cfg_ack(message_id: int, *, accepted: bool = True) -> bytes:
  return ubx_frame(0x05, 0x01 if accepted else 0x00, bytes((0x06, message_id)))


def cfg_write(message_id: int, payload: bytes = b"\x01") -> bytes:
  return ubx_frame(0x06, message_id, payload)


def expected_port_config(port_id: int) -> pigeond.PortConfig:
  return {
    0: pigeond.PortConfig(0, 0, 0, 0, 0, 0, 0),
    1: pigeond.PortConfig(1, 0, 0x08C0, 460800, 1, 1, 0),
    3: pigeond.PortConfig(3, 0, 0, 0, 1, 1, 0),
    4: pigeond.PortConfig(4, 0, 0, 0, 0, 0, 0),
  }[port_id]


def cfg_rate_frame(
  measurement_period_ms: int = 100,
  navigation_rate: int = 1,
  time_reference: int = 0,
) -> bytes:
  payload = measurement_period_ms.to_bytes(2, "little") + navigation_rate.to_bytes(2, "little") + time_reference.to_bytes(2, "little")
  return ubx_frame(0x06, 0x08, payload)


def cfg_prt_frame(config: pigeond.PortConfig) -> bytes:
  payload = bytearray(20)
  payload[0] = config.port_id
  payload[2:4] = config.tx_ready.to_bytes(2, "little")
  payload[4:8] = config.mode.to_bytes(4, "little")
  payload[8:12] = config.baud_rate.to_bytes(4, "little")
  payload[12:14] = config.input_protocol_mask.to_bytes(2, "little")
  payload[14:16] = config.output_protocol_mask.to_bytes(2, "little")
  payload[16:18] = config.flags.to_bytes(2, "little")
  return ubx_frame(0x06, 0x00, bytes(payload))


def cfg_nav5_frame(dynamic_model: int = 4, fix_mode: int = 3) -> bytes:
  payload = bytearray(36)
  payload[2] = dynamic_model
  payload[3] = fix_mode
  return ubx_frame(0x06, 0x24, bytes(payload))


def cfg_odo_frame(flags: int = 1, profile: int = 3) -> bytes:
  payload = bytearray(20)
  payload[4] = flags
  payload[5] = profile
  return ubx_frame(0x06, 0x1E, bytes(payload))


def cfg_itfm_frame(config: int = 0xAD62ADFF, config2: int = 0x0000631E) -> bytes:
  return ubx_frame(
    0x06,
    0x39,
    config.to_bytes(4, "little") + config2.to_bytes(4, "little"),
  )


def cfg_msg_frame(message_class: int, message_id: int, uart1_rate: int = 1) -> bytes:
  return ubx_frame(
    0x06,
    0x01,
    bytes((message_class, message_id, 0, uart1_rate, 0, 0, 0, 0)),
  )


def sos_frame(command: int, response: int) -> bytes:
  return ubx_frame(
    0x09,
    0x14,
    bytes((command, 0, 0, 0, response, 0, 0, 0)),
  )


def nav_pvt_frame() -> bytes:
  payload = bytearray(92)
  payload[20] = 3
  payload[21] = 1
  payload[23] = 7
  return ubx_frame(0x01, 0x07, bytes(payload))


def verified_configuration_items(
  *,
  expected_value: str = "expected",
) -> tuple[pigeond.ReceiverConfigurationItemResult, ...]:
  return tuple(
    pigeond.ReceiverConfigurationItemResult(
      item_name=item_name,
      mandatory=mandatory,
      attempted=False,
      write_attempt_count=0,
      ack_status=pigeond.ReceiverConfigurationAckStatus.NOT_REQUIRED,
      poll_attempt_count=1,
      readback_status=pigeond.ReceiverConfigurationReadbackStatus.VERIFIED,
      verified=True,
      expected_value=expected_value,
      observed_value="verified",
      failure_kind=None,
      failure_phase=None,
      error_type=None,
      error=None,
    )
    for item_name, mandatory in pigeond.RECEIVER_CONFIGURATION_ITEM_INVENTORY
  )


def complete_configuration_summary(
  *,
  receiver_cycle: int = 3,
  receiver_fingerprint: str = "unidentified",
  started_at: float = 1.0,
  completed_at: float = 2.0,
  items: tuple[pigeond.ReceiverConfigurationItemResult, ...] | None = None,
  gnss_start_attempted: bool = True,
  gnss_start_sent: bool = True,
  navx5_ack_aiding_result: pigeond.Navx5AckAidingConfigurationResult | None = (pigeond.Navx5AckAidingConfigurationResult.ALREADY_ENABLED),
) -> pigeond.ReceiverConfigurationSummary:
  return pigeond.ReceiverConfigurationSummary(
    receiver_cycle=receiver_cycle,
    transport_verified=True,
    configuration_started_at=started_at,
    configuration_completed_at=completed_at,
    items=(verified_configuration_items() if items is None else items),
    gnss_start_attempted=gnss_start_attempted,
    gnss_start_sent=gnss_start_sent,
    receiver_fingerprint=receiver_fingerprint,
    navx5_ack_aiding_result=navx5_ack_aiding_result,
  )


def persist_current_configuration_summary(
  summary: pigeond.ReceiverConfigurationSummary,
) -> bool:
  pigeond.set_receiver_configuration_context(
    summary.receiver_cycle,
    summary.receiver_fingerprint,
  )
  return pigeond.persist_receiver_configuration_summary(summary)


def rawx_frame() -> bytes:
  # Version 1 RXM-RAWX header with no measurements.
  payload = bytearray(16)
  payload[8:10] = (2300).to_bytes(2, "little")
  payload[10] = 18
  payload[13] = 1
  return ubx_frame(0x02, 0x15, bytes(payload))


def mga_ack(message: bytes, *, accepted: bool) -> bytes:
  payload = bytes((1 if accepted else 0, 0, 0 if accepted else 1, message[3]))
  payload += message[6:10].ljust(4, b"\x00")
  return ubx_frame(0x13, 0x60, payload)


class ScriptedPigeon(pigeond.TTYPigeon):
  def __init__(self, responses=(), pre_transaction=()):
    self.responses = deque(responses)
    self.available = deque(pre_transaction)
    self.sent: list[bytes] = []
    self.published: list[bytes] = []
    self._stream_parser = UbxStreamParser()
    self._pending_frames: deque[bytes] = deque()
    self._pending_frame_bytes = 0
    self._pending_unpublished = None
    self._raw_publisher = self.published.append
    self._frame_dispatcher = None
    self._receiver_cycle = 0

  def _receive_tty(self) -> bytes:
    return self.available.popleft() if self.available else b""

  def send(self, data: bytes) -> None:
    self.sent.append(data)
    if self.responses:
      response = self.responses.popleft()
      if isinstance(response, bytes):
        self.available.append(response)
      else:
        self.available.extend(response)


def test_send_with_ack_accepts_only_matching_cfg_ack():
  message = cfg_write(0x08)
  pigeon = ScriptedPigeon((cfg_ack(0x08),))
  pigeon.send_with_ack(message)
  assert pigeon.sent == [message]


def test_send_with_ack_raises_for_matching_cfg_nak():
  pigeon = ScriptedPigeon((cfg_ack(0x08, accepted=False),))
  with pytest.raises(pigeond.CfgNakError, match="0x06 0x08"):
    pigeon.send_with_ack(cfg_write(0x08))


def test_invalid_cfg_frame_is_not_sent():
  invalid = bytearray(cfg_write(0x08))
  invalid[-1] ^= 0xFF
  pigeon = ScriptedPigeon()
  with pytest.raises(pigeond.ReceiverConfigurationError, match="invalid UBX CFG"):
    pigeon.send_with_ack(bytes(invalid))
  assert pigeon.sent == []


def test_cfg_ack_ignores_unrelated_ack_before_matching_ack():
  received = cfg_ack(0x24) + cfg_ack(0x08)
  pigeon = ScriptedPigeon((received,))
  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)
  assert pigeon.published == [received]
  assert pigeon.receive_normal() == (b"", [cfg_ack(0x24)])


def test_cfg_ack_ignores_corrupt_checksum():
  corrupt = bytearray(cfg_ack(0x08))
  corrupt[-1] ^= 0xFF
  pigeon = ScriptedPigeon((bytes(corrupt),))
  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08, timeout=0.005) is None


def test_cfg_ack_like_bytes_inside_another_frame_do_not_match():
  outer = ubx_frame(0x01, 0x07, b"prefix" + cfg_ack(0x08) + b"suffix")
  pigeon = ScriptedPigeon((outer,))
  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08, timeout=0.005) is None
  assert pigeon.published == [outer]
  assert pigeon.receive_normal() == (b"", [outer])


@pytest.mark.parametrize(
  ("responses", "expected"),
  [
    (lambda: cfg_ack(0x08) + cfg_ack(0x08, accepted=False), True),
    (lambda: cfg_ack(0x08, accepted=False) + cfg_ack(0x08), False),
  ],
)
def test_cfg_ack_and_nak_in_one_read_follow_frame_order(responses, expected):
  pigeon = ScriptedPigeon((responses(),))
  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08) is expected


def test_fragmented_cfg_ack_is_reassembled():
  acknowledgment = cfg_ack(0x08)
  pigeon = ScriptedPigeon(((acknowledgment[:3], acknowledgment[3:8], acknowledgment[8:]),))
  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)


def test_partial_ack_from_timed_out_attempt_cannot_complete_in_next_transaction():
  acknowledgment = cfg_ack(0x08)
  pigeon = ScriptedPigeon(((acknowledgment[:6],), (acknowledgment[6:],)))

  first = pigeon.begin_response_transaction(cfg_write(0x08, b"first"))
  assert pigeond.wait_for_cfg_ack(pigeon, first, 0x06, 0x08, timeout=0.005) is None

  second = pigeon.begin_response_transaction(cfg_write(0x08, b"second"))
  assert pigeond.wait_for_cfg_ack(pigeon, second, 0x06, 0x08, timeout=0.005) is None
  assert pigeon.sent == [cfg_write(0x08, b"first"), cfg_write(0x08, b"second")]


def test_complete_delayed_ack_is_drained_before_retry_write():
  acknowledgment = cfg_ack(0x08)
  pigeon = ScriptedPigeon((b"",), pre_transaction=(acknowledgment,))

  transaction = pigeon.begin_response_transaction(cfg_write(0x08, b"retry"))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08, timeout=0.005) is None
  assert pigeon.published == [acknowledgment]
  assert pigeon.receive_normal() == (b"", [acknowledgment])


def test_delayed_same_key_ack_cannot_satisfy_next_receiver_cycle():
  acknowledgment = cfg_ack(0x08)
  pigeon = ScriptedPigeon((b"", b""))

  first = pigeon.begin_response_transaction(cfg_write(0x08, b"first"))
  assert (
    pigeond.wait_for_cfg_ack(
      pigeon,
      first,
      0x06,
      0x08,
      timeout=0.005,
    )
    is None
  )

  pigeon.available.append(acknowledgment)
  pigeon.reset_response_state()
  second = pigeon.begin_response_transaction(cfg_write(0x08, b"second"))
  assert (
    pigeond.wait_for_cfg_ack(
      pigeon,
      second,
      0x06,
      0x08,
      timeout=0.005,
    )
    is None
  )
  assert pigeon.published == [acknowledgment]
  assert pigeon.receive_normal() == (b"", [acknowledgment])


def test_same_key_ack_after_official_response_window_is_not_reused(monkeypatch):
  clock = SimpleNamespace(now=0.0)
  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock.now)
  monkeypatch.setattr(
    pigeond.time,
    "sleep",
    lambda duration: setattr(clock, "now", clock.now + duration),
  )
  pigeon = ScriptedPigeon((b"", b""))

  first = pigeon.begin_response_transaction(cfg_write(0x08, b"first"))
  assert (
    pigeond.wait_for_cfg_ack(
      pigeon,
      first,
      0x06,
      0x08,
      timeout=pigeond.CFG_ACK_TIMEOUT,
    )
    is None
  )
  assert clock.now == pytest.approx(1.1)

  pigeon.available.append(cfg_ack(0x08))
  pigeon.reset_response_state()
  second = pigeon.begin_response_transaction(cfg_write(0x08, b"second"))
  assert (
    pigeond.wait_for_cfg_ack(
      pigeon,
      second,
      0x06,
      0x08,
      timeout=0.005,
    )
    is None
  )


def test_consecutive_cfg_prt_polls_correlate_same_message_id_by_payload():
  port_one = bytearray(20)
  port_one[0] = 1
  port_three = bytearray(20)
  port_three[0] = 3
  pigeon = ScriptedPigeon((ubx_frame(0x06, 0x00, port_one), ubx_frame(0x06, 0x00, port_three)))

  assert pigeond.poll_cfg_prt(pigeon, 1).port_id == 1
  assert pigeond.poll_cfg_prt(pigeon, 3).port_id == 3
  assert [message[3] for message in pigeon.sent] == [0x00, 0x00]


@pytest.mark.parametrize("port_id", [0, 1, 3, 4])
def test_cfg_prt_poll_uses_documented_one_byte_port_id(port_id):
  config = expected_port_config(port_id)
  pigeon = ScriptedPigeon((cfg_prt_frame(config),))
  assert pigeond.poll_cfg_prt(pigeon, port_id) == config
  assert pigeon.sent == [pigeond.build_cfg_prt_poll_message(port_id)]
  assert pigeon.sent[0][4:6] == b"\x01\x00"
  assert pigeon.sent[0][6] == port_id


def test_cfg_prt_zero_length_poll_is_never_transmitted():
  responses = tuple(cfg_prt_frame(expected_port_config(port)) for port in (0, 1, 3, 4))
  pigeon = ScriptedPigeon(responses)
  for port_id in (0, 1, 3, 4):
    pigeond.poll_cfg_prt(pigeon, port_id)
  assert all(message[4:6] == b"\x01\x00" for message in pigeon.sent)


def test_cfg_prt_wrong_port_id_fails():
  pigeon = ScriptedPigeon((cfg_prt_frame(expected_port_config(3)),))
  with pytest.raises(pigeond.CfgPollTimeoutError):
    pigeond.poll_cfg_prt(pigeon, 1, timeout=0.005)


@pytest.mark.parametrize(
  ("port_id", "field", "fails"),
  [
    (1, "baud_rate", True),
    (1, "mode", True),
    (1, "input_protocol_mask", True),
    (1, "output_protocol_mask", True),
    (1, "flags", True),
    (0, "baud_rate", False),
    (0, "flags", True),
    (3, "baud_rate", False),
    (3, "mode", False),
    (3, "input_protocol_mask", True),
    (3, "flags", False),
    (4, "baud_rate", False),
    (4, "flags", True),
  ],
)
def test_cfg_prt_verifies_only_fields_explicit_for_port(port_id, field, fails):
  expected = expected_port_config(port_id)
  actual = expected.__class__(
    **{
      **expected.__dict__,
      field: getattr(expected, field) ^ 1,
    }
  )
  if fails:
    with pytest.raises(pigeond.ReceiverConfigurationError, match="CFG-PRT"):
      pigeond.verify_cfg_prt_config(actual, expected)
  else:
    pigeond.verify_cfg_prt_config(actual, expected)


def test_consecutive_cfg_msg_polls_correlate_same_message_id_by_payload():
  pigeon = ScriptedPigeon((cfg_msg_frame(0x01, 0x07), cfg_msg_frame(0x02, 0x15)))

  assert pigeond.poll_cfg_msg(pigeon, 0x01, 0x07).message_id == 0x07
  assert pigeond.poll_cfg_msg(pigeon, 0x02, 0x15).message_id == 0x15
  assert [message[3] for message in pigeon.sent] == [0x01, 0x01]


def test_pre_transaction_drain_publishes_but_does_not_correlate_stale_bytes():
  nav = nav_pvt_frame()
  stale_ack = cfg_ack(0x08)
  current_ack = cfg_ack(0x08)
  pigeon = ScriptedPigeon((current_ack,), pre_transaction=(nav + stale_ack,))

  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)
  assert pigeon.published == [nav + stale_ack, current_ack]
  assert pigeon.receive_normal() == (b"", [nav, stale_ack])


def test_multiple_frames_in_one_read_preserve_unrelated_order_once():
  nav = ubx_frame(0x01, 0x07, b"nav")
  rawx = ubx_frame(0x02, 0x15, b"rawx")
  received = nav + cfg_ack(0x08) + rawx
  pigeon = ScriptedPigeon((received,))
  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)
  assert pigeon.published == [received]
  assert pigeon.receive_normal() == (b"", [nav, rawx])
  assert pigeon.receive() == b""


def test_cfg_poll_response_and_unrelated_frame_are_preserved():
  nav = ubx_frame(0x01, 0x07, b"nav")
  received = nav + cfg_rate_frame()
  pigeon = ScriptedPigeon((received,))
  assert pigeond.poll_cfg_rate(pigeon) == RateConfig(100, 1, 0)
  assert pigeon.sent == [build_cfg_rate_poll_message()]
  assert pigeon.published == [received]
  assert pigeon.receive_normal() == (b"", [nav])


def test_corrupt_cfg_poll_response_is_ignored_until_timeout():
  corrupt = bytearray(cfg_rate_frame())
  corrupt[-1] ^= 0xFF
  pigeon = ScriptedPigeon((bytes(corrupt),))
  with pytest.raises(TimeoutError, match="No valid CFG response"):
    pigeond.poll_cfg_rate(pigeon, timeout=0.005)


@pytest.mark.parametrize(
  ("message_id", "poll"),
  (
    (0x00, lambda pigeon: pigeond.poll_cfg_prt(pigeon, 1, timeout=0.01)),
    (0x08, lambda pigeon: pigeond.poll_cfg_rate(pigeon, timeout=0.01)),
    (0x24, lambda pigeon: pigeond.poll_cfg_nav5(pigeon, timeout=0.01)),
    (0x1E, lambda pigeon: pigeond.poll_cfg_odo(pigeon, timeout=0.01)),
    (0x39, lambda pigeon: pigeond.poll_cfg_itfm(pigeon, timeout=0.01)),
    (0x01, lambda pigeon: pigeond.poll_cfg_msg(pigeon, 0x01, 0x07, timeout=0.01)),
  ),
)
def test_real_malformed_mandatory_cfg_response_is_parser_error(message_id, poll):
  malformed_response = ubx_frame(0x06, message_id, b"\x00")
  pigeon = ScriptedPigeon((malformed_response,))

  result = pigeond.run_receiver_configuration_item(
    item_name=f"CFG-{message_id:02X}",
    mandatory=True,
    expected_value="expected",
    poll=lambda: poll(pigeon),
    verify=lambda _value: None,
    write=lambda: None,
    max_write_attempts=0,
  )

  assert not result.verified
  assert result.poll_attempt_count == 1
  assert result.failure_kind is pigeond.ReceiverConfigurationFailureKind.PARSER_ERROR
  assert result.readback_status is pigeond.ReceiverConfigurationReadbackStatus.PARSER_ERROR
  assert result.failure_phase == "initial_readback"
  assert result.error_type == "ReceiverConfigurationParserError"


def test_cfg_poll_timeout():
  pigeon = ScriptedPigeon()
  with pytest.raises(TimeoutError, match="No valid CFG response"):
    pigeond.poll_cfg_rate(pigeon, timeout=0.005)


def test_cfg_rate_readback_success_and_mismatch():
  assert parse_cfg_rate(cfg_rate_frame()) == RateConfig(100, 1, 0)
  valid = (
    RateConfig(100, 1, 0),
    Nav5Config(4, 3),
    OdoConfig(0, 1, 3),
    ItfmConfig(0xAD62ADFF, 0x0000631E),
    MessageRateConfig(0x01, 0x07, (0, 1, 0, 0, 0, 0)),
    MessageRateConfig(0x02, 0x15, (0, 1, 0, 0, 0, 0)),
  )
  pigeond.verify_startup_configuration(*valid)
  with pytest.raises(pigeond.ReceiverConfigurationError, match="CFG-RATE"):
    pigeond.verify_startup_configuration(RateConfig(1000, 1, 0), *valid[1:])


@pytest.mark.parametrize(("message_class", "message_id"), [(0x01, 0x07), (0x02, 0x15)])
def test_cfg_msg_nav_pvt_and_rawx_readback(message_class, message_id):
  frame = cfg_msg_frame(message_class, message_id)
  expected = MessageRateConfig(message_class, message_id, (0, 1, 0, 0, 0, 0))
  assert parse_cfg_msg(frame) == expected
  pigeon = ScriptedPigeon((frame,))
  assert pigeond.poll_cfg_msg(pigeon, message_class, message_id) == expected
  assert pigeon.sent == [build_cfg_msg_poll_message(message_class, message_id)]


def test_cfg_msg_poll_preserves_other_message_configuration():
  unrelated = cfg_msg_frame(0x01, 0x35)
  expected_frame = cfg_msg_frame(0x02, 0x15)
  received = unrelated + expected_frame
  pigeon = ScriptedPigeon((received,))
  assert pigeond.poll_cfg_msg(pigeon, 0x02, 0x15).message_id == 0x15
  assert pigeon.published == [received]
  assert pigeon.receive_normal() == (b"", [unrelated])


def test_cfg_nav5_odo_and_itfm_parsing_and_polls():
  nav5 = cfg_nav5_frame()
  odo = cfg_odo_frame()
  itfm = cfg_itfm_frame()
  assert parse_cfg_nav5(nav5) == Nav5Config(4, 3)
  assert parse_cfg_odo(odo) == OdoConfig(0, 1, 3)
  assert parse_cfg_itfm(itfm) == ItfmConfig(0xAD62ADFF, 0x0000631E)

  pigeon = ScriptedPigeon((nav5, odo, itfm))
  assert pigeond.poll_cfg_nav5(pigeon) == Nav5Config(4, 3)
  assert pigeond.poll_cfg_odo(pigeon) == OdoConfig(0, 1, 3)
  assert pigeond.poll_cfg_itfm(pigeon) == ItfmConfig(0xAD62ADFF, 0x0000631E)
  assert pigeon.sent == [
    build_cfg_nav5_poll_message(),
    build_cfg_odo_poll_message(),
    build_cfg_itfm_poll_message(),
  ]


def test_fragmented_unrelated_rawx_is_preserved_for_later_publication():
  rawx = ubx_frame(0x02, 0x15, b"rawx")
  acknowledgment = cfg_ack(0x08)
  chunks = (rawx[:5], rawx[5:] + acknowledgment)
  pigeon = ScriptedPigeon((chunks,))
  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)
  assert b"".join(pigeon.published) == rawx + acknowledgment
  assert pigeon.receive_normal() == (b"", [rawx])
  assert pigeon.receive_normal() == (b"", [])


def test_rawx_fragment_crossing_normal_and_response_wait_is_preserved_once():
  rawx = ubx_frame(0x02, 0x15, b"rawx")
  acknowledgment = cfg_ack(0x08)
  pigeon = ScriptedPigeon((rawx[5:] + acknowledgment,), pre_transaction=(rawx[:5],))

  data, frames = pigeon.receive_normal()
  assert data == rawx[:5]
  assert frames == []
  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)

  data, frames = pigeon.receive_normal()
  assert data == b""
  assert frames == [rawx]
  assert b"".join(pigeon.published) == rawx + acknowledgment
  assert pigeon.receive_normal() == (b"", [])


def test_nav_pvt_fragment_crossing_normal_and_response_wait_is_preserved_once():
  nav = nav_pvt_frame()
  acknowledgment = cfg_ack(0x08)
  pigeon = ScriptedPigeon(((nav[9:] + acknowledgment,),), pre_transaction=(nav[:9],))

  assert pigeon.receive_normal() == (nav[:9], [])
  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)
  assert pigeon.receive_normal() == (b"", [nav])
  assert b"".join(pigeon.published) == nav + acknowledgment


def test_rawx_fragment_starts_during_wait_and_completes_afterward():
  rawx = rawx_frame()
  acknowledgment = cfg_ack(0x08)
  pigeon = ScriptedPigeon(((acknowledgment + rawx[:7],),))

  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)
  pigeon.available.append(rawx[7:])
  assert pigeon.receive_normal() == (rawx[7:], [rawx])
  assert b"".join(pigeon.published) == acknowledgment + rawx


def test_pubmaster_callback_publishes_each_uart_chunk_exactly_once(monkeypatch):
  sent = []

  class PubMaster:
    def send(self, service, message):
      sent.append((service, bytes(message.ubloxRaw)))

  monkeypatch.setattr(
    pigeond.messaging,
    "new_message",
    lambda _service, _size, valid: SimpleNamespace(ubloxRaw=b"", valid=valid),
  )
  pm = PubMaster()
  pigeon = ScriptedPigeon((cfg_ack(0x08),), pre_transaction=(nav_pvt_frame(),))
  pigeon._raw_publisher = lambda data: pigeond.publish_ublox_raw(pm, data)

  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)
  assert sent == [
    ("ubloxRaw", nav_pvt_frame()),
    ("ubloxRaw", cfg_ack(0x08)),
  ]


def test_valid_nav_pvt_and_rawx_reach_higher_level_processing_once():
  nav = nav_pvt_frame()
  rawx = rawx_frame()
  acknowledgment = cfg_ack(0x08)
  pigeon = ScriptedPigeon((nav + rawx + acknowledgment,))

  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)
  _, frames = pigeon.receive_normal()
  assert frames == [nav, rawx]
  assert sum(pigeond.parse_nav_pvt(frame) is not None for frame in frames) == 1
  diagnostics = pigeond.GpsStartupDiagnostics(0.0)
  for frame in frames:
    diagnostics.note_rawx(frame, 1.0)
  assert diagnostics.first_rawx_after_initialization_logged
  assert pigeon.receive_normal() == (b"", [])


def test_matching_response_is_not_queued_for_internal_processing_twice():
  acknowledgment = cfg_ack(0x08)
  pigeon = ScriptedPigeon((acknowledgment,))
  transaction = pigeon.begin_response_transaction(cfg_write(0x08))
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x08)
  assert pigeon.receive_normal() == (b"", [])


def test_pending_frames_dispatch_between_cfg_transactions():
  dispatched = []
  nav = nav_pvt_frame()
  pigeon = ScriptedPigeon((nav + cfg_ack(0x08), cfg_ack(0x24)))
  pigeon._frame_dispatcher = lambda frames: dispatched.extend(frames)
  pigeon.send_with_ack(cfg_write(0x08))
  pigeon.send_with_ack(cfg_write(0x24))
  assert dispatched == [nav]
  assert pigeon.receive_normal() == (b"", [])


def test_pending_frames_dispatch_between_mga_transactions():
  dispatched = []
  nav = nav_pvt_frame()
  first = pigeond.build_time_assistance_message(datetime(2026, 7, 10, tzinfo=UTC))
  second = pigeond.build_position_assistance_message(0, 0, 0, 1000)
  pigeon = ScriptedPigeon((nav + mga_ack(first, accepted=True), mga_ack(second, accepted=True)))
  pigeon._frame_dispatcher = lambda frames: dispatched.extend(frames)
  pigeond.send_mga_with_strict_ack(pigeon, first)
  pigeond.send_mga_with_strict_ack(pigeon, second)
  assert dispatched == [nav]


def test_pending_frames_dispatch_between_mga_dbd_restore_frames(monkeypatch):
  position = pigeond.build_position_assistance_message(0, 0, 0, 1000)
  database_frames = (
    ubx_frame(0x13, 0x80, b"database-0"),
    ubx_frame(0x13, 0x80, b"database-1"),
  )
  nav = nav_pvt_frame()
  rawx = rawx_frame()
  pigeon = ScriptedPigeon(
    (
      mga_ack(position, accepted=True),
      nav + mga_ack(database_frames[0], accepted=True),
      rawx + mga_ack(database_frames[1], accepted=True),
    )
  )
  dispatched = []
  pigeon._frame_dispatcher = lambda frames: dispatched.extend(frames)
  cache = SimpleNamespace(
    saved_at_utc=datetime(2026, 7, 10, tzinfo=UTC),
    rtc_counter_seconds=100,
    quality=None,
    database_frames=database_frames,
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    position_accuracy_cm=1000,
  )
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(pigeond, "load_cache", lambda *args, **kwargs: cache)

  result = pigeond.restore_navigation_assistance(
    pigeon,
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    trusted_now=datetime(2026, 7, 10, 0, 5, tzinfo=UTC),
    time_assistance_source="system_synchronized",
    allow_legacy_direct_restore=True,
  )
  assert result.status is pigeond.NavigationAssistanceRestoreStatus.COMPLETE
  assert result.accepted_frame_count == 2
  assert dispatched == [nav, rawx]


def test_raw_publisher_failure_retains_then_publishes_chunk_once():
  frame = nav_pvt_frame()
  attempts = []
  processed = []
  pigeon = ScriptedPigeon(pre_transaction=(frame,))

  def publish(data):
    attempts.append(data)
    if len(attempts) == 1:
      raise RuntimeError("temporary messaging failure")

  pigeon._raw_publisher = publish
  with pytest.raises(pigeond.RawPublicationError):
    pigeon._read_stream()
  assert pigeon._pending_unpublished == frame
  assert pigeon._stream_parser._buffer == b""

  data, frames = pigeon._read_stream()
  processed.extend(frames)
  assert data == frame
  assert attempts == [frame, frame]
  assert processed == [frame]
  assert pigeon._pending_unpublished is None


def test_generic_ubx_ack_for_wrong_class_id_is_rejected(monkeypatch):
  monkeypatch.setattr(pigeond, "CFG_ACK_TIMEOUT", 0.005)
  command = ubx_frame(0x09, 0x14, b"\x01\x00\x00\x00")
  pigeon = ScriptedPigeon((cfg_ack(0x08),))
  with pytest.raises(TimeoutError):
    pigeon.send_with_ack(command)


def test_sos_clear_rejects_ack_for_another_command(monkeypatch):
  monkeypatch.setattr(pigeond, "CFG_ACK_TIMEOUT", 0.005)
  clear = ubx_frame(0x09, 0x14, b"\x01\x00\x00\x00")
  pigeon = ScriptedPigeon((cfg_ack(0x24),))
  with pytest.raises(TimeoutError):
    pigeon.send_with_ack(clear)


def test_legacy_mga_path_rejects_unrelated_mga_ack(monkeypatch):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_ACK_TIMEOUT", 0.005)
  message = pigeond.build_time_assistance_message(datetime(2026, 7, 10, tzinfo=UTC))
  unrelated = pigeond.build_position_assistance_message(0, 0, 0, 1000)
  pigeon = ScriptedPigeon((mga_ack(unrelated, accepted=True),))
  with pytest.raises(TimeoutError):
    pigeon.send_with_ack(message, ack=pigeond.UBLOX_ASSIST_ACK)


def test_mixed_corrupt_and_valid_frames_in_one_read():
  corrupt = bytearray(cfg_ack(0x08))
  corrupt[-1] ^= 0xFF
  nav = nav_pvt_frame()
  pigeon = ScriptedPigeon((bytes(corrupt) + nav + cfg_ack(0x08),))
  pigeon.send_with_ack(cfg_write(0x08))
  assert pigeon.receive_normal() == (b"", [nav])


def test_response_state_reset_across_power_cycle_discards_parser_state_only():
  nav = nav_pvt_frame()
  pigeon = ScriptedPigeon(pre_transaction=(nav[:8],))
  assert pigeon.receive_normal() == (nav[:8], [])
  pigeon.reset_response_state()
  pigeon.available.append(nav[8:])
  assert pigeon.receive_normal() == (nav[8:], [])
  assert b"".join(pigeon.published) == nav


def test_init_resets_response_state_before_receiver_power_cycle(monkeypatch):
  events = []

  class ResetTrackingPigeon(ScriptedPigeon):
    def reset_response_state(self):
      events.append("reset")
      super().reset_response_state()

  nav = nav_pvt_frame()
  pigeon = ResetTrackingPigeon(pre_transaction=(nav[:8],))
  assert pigeon.receive_normal() == (nav[:8], [])
  monkeypatch.setattr(pigeond.signal, "signal", lambda *_args: None)
  monkeypatch.setattr(pigeond, "set_power", lambda enabled: events.append(f"power={enabled}"))
  monkeypatch.setattr(pigeond, "init_baudrate", lambda _pigeon: events.append("baud"))
  monkeypatch.setattr(
    pigeond,
    "poll_mon_ver",
    lambda _pigeon, _timeout: SimpleNamespace(),
  )
  monkeypatch.setattr(pigeond, "init_pigeon", lambda _pigeon: True)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _duration: None)

  pigeond.init(pigeon)

  assert events[:3] == ["reset", "power=False", "power=True"]
  assert pigeon.published == [nav[:8]]


def test_legacy_backup_and_assistnow_run_only_after_gnss_start(monkeypatch):
  events: list[str] = []

  class Pigeon:
    def send(self, message):
      if message == pigeond.CONTROLLED_GNSS_STOP_MESSAGE:
        events.append("gnss_stop")
      elif message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        events.append("gnss_start")

  initialization = pigeond.PreAcquisitionInitialization(
    callback=lambda: events.append("assistance"),
    transport_already_started=True,
  )
  monkeypatch.setattr(pigeond, "_ACTIVE_PRE_ACQUISITION_INITIALIZATION", initialization)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _duration: None)
  monkeypatch.setattr(pigeond, "finish_pigeon_initialization", lambda _pigeon: events.append("strict_configuration"))
  monkeypatch.setattr(
    pigeond,
    "finish_post_start_receiver_configuration",
    lambda _pigeon: events.append("optional_configuration"),
  )
  monkeypatch.setattr(pigeond, "run_post_start_legacy_assistance", lambda _pigeon: events.append("legacy_assistance"))

  pigeond.init(cast(pigeond.TTYPigeon, Pigeon()))

  assert events == [
    "gnss_stop",
    "strict_configuration",
    "assistance",
    "gnss_start",
    "optional_configuration",
    "legacy_assistance",
  ]


def test_mandatory_configuration_failure_is_degraded_but_not_fatal(monkeypatch):
  errors: list[str] = []
  monkeypatch.setattr(pigeond, "init_pigeon", lambda *_args, **_kwargs: False)
  monkeypatch.setattr(pigeond.cloudlog, "error", errors.append)

  assert not pigeond.finish_pigeon_initialization(cast(pigeond.TTYPigeon, object()))
  assert len(errors) == 1
  assert "degraded" in errors[0]
  assert "continues" in errors[0]


def test_pre_start_configuration_runs_only_mandatory_inventory(monkeypatch):
  calls: list[tuple[str, bool, float | None]] = []

  def run_item(**kwargs):
    calls.append(
      (
        kwargs["item_name"],
        kwargs["mandatory"],
        kwargs.get("pre_start_deadline"),
      )
    )
    return pigeond.ReceiverConfigurationItemResult(
      item_name=kwargs["item_name"],
      mandatory=kwargs["mandatory"],
      attempted=False,
      write_attempt_count=0,
      ack_status=pigeond.ReceiverConfigurationAckStatus.NOT_REQUIRED,
      poll_attempt_count=1,
      readback_status=pigeond.ReceiverConfigurationReadbackStatus.VERIFIED,
      verified=True,
      expected_value=kwargs["expected_value"],
      observed_value="verified",
      failure_kind=None,
      failure_phase=None,
      error_type=None,
      error=None,
    )

  monkeypatch.setattr(pigeond, "run_receiver_configuration_item", run_item)
  monkeypatch.setattr(
    pigeond,
    "run_optional_receiver_configuration_items",
    lambda _pigeon: (_ for _ in ()).throw(AssertionError("optional configuration ran before START")),
  )
  pigeon = SimpleNamespace(
    _receiver_cycle=4,
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    _transport_verified_for_receiver_cycle=True,
  )

  assert pigeond.init_pigeon(
    cast(pigeond.TTYPigeon, pigeon),
    pre_start_deadline=45.0,
    include_optional=False,
  )

  mandatory_inventory = tuple(item for item in pigeond.RECEIVER_CONFIGURATION_ITEM_INVENTORY if item[1])
  assert tuple((name, mandatory) for name, mandatory, _ in calls) == mandatory_inventory
  assert all(deadline == 45.0 for _, _, deadline in calls)
  summary = pigeond.last_receiver_configuration_summary()
  assert summary is not None
  assert tuple((item.item_name, item.mandatory) for item in summary.items) == pigeond.RECEIVER_CONFIGURATION_ITEM_INVENTORY
  assert all(item.verified for item in summary.items if item.mandatory)
  assert all(item.failure_kind is pigeond.ReceiverConfigurationFailureKind.DEFERRED_POST_START for item in summary.items if not item.mandatory)


def test_optional_inventory_replaces_deferred_results_after_start(monkeypatch):
  summary = complete_configuration_summary(
    receiver_cycle=4,
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    items=tuple(
      item
      if item.mandatory
      else pigeond.deferred_post_start_configuration_result(
        item.item_name,
        item.expected_value,
      )
      for item in verified_configuration_items()
    ),
  )
  optional_results = tuple(item for item in verified_configuration_items() if not item.mandatory)
  monkeypatch.setattr(
    pigeond,
    "_current_receiver_configuration_cycle",
    summary.receiver_cycle,
  )
  monkeypatch.setattr(
    pigeond,
    "_current_receiver_configuration_fingerprint",
    summary.receiver_fingerprint,
  )
  monkeypatch.setattr(
    pigeond,
    "_last_receiver_configuration_summary",
    summary,
  )
  monkeypatch.setattr(
    pigeond,
    "run_optional_receiver_configuration_items",
    lambda _pigeon: optional_results,
  )

  pigeond.finish_post_start_receiver_configuration(cast(pigeond.TTYPigeon, object()))

  completed = pigeond.last_receiver_configuration_summary()
  assert completed is not None
  assert all(item.verified for item in completed.items)


def test_mandatory_configuration_failure_still_starts_and_continues(monkeypatch):
  events: list[str] = []

  class Pigeon:
    def send(self, message):
      if message == pigeond.CONTROLLED_GNSS_STOP_MESSAGE:
        events.append("gnss_stop")
      elif message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        events.append("gnss_start")

  initialization = pigeond.PreAcquisitionInitialization(
    callback=lambda: events.append("assistance"),
    transport_already_started=True,
  )
  monkeypatch.setattr(pigeond, "_ACTIVE_PRE_ACQUISITION_INITIALIZATION", initialization)
  monkeypatch.setattr(pigeond, "init_pigeon", lambda *_args, **_kwargs: False)
  monkeypatch.setattr(pigeond, "run_post_start_legacy_assistance", lambda _pigeon: events.append("post_start"))
  monkeypatch.setattr(pigeond.time, "sleep", lambda _duration: None)

  pigeond.init(cast(pigeond.TTYPigeon, Pigeon()))

  assert events == ["gnss_stop", "assistance", "gnss_start", "post_start"]


def test_pending_frames_were_already_published_before_reset():
  nav = nav_pvt_frame()
  pigeon = ScriptedPigeon(pre_transaction=(nav,))
  pigeon.drain_before_transaction()
  assert pigeon.published == [nav]
  pigeon.reset_response_state()
  assert pigeon.receive_normal() == (b"", [])
  assert pigeon.published == [nav]


def test_pending_frame_queue_bound_and_drain(monkeypatch):
  frames = [ubx_frame(0x01, 0x07, bytes((index,))) for index in range(2)]
  pigeon = ScriptedPigeon()
  monkeypatch.setattr(pigeond, "PENDING_FRAME_MAX_COUNT", 2)
  monkeypatch.setattr(pigeond, "PENDING_FRAME_MAX_BYTES", sum(map(len, frames)))
  pigeon.queue_pending_frames(frames)
  with pytest.raises(pigeond.PendingFrameOverflowError):
    pigeon.queue_pending_frames([frames[0]])
  assert pigeon.receive_normal() == (b"", [*frames, frames[0]])
  pigeon.queue_pending_frames([frames[0]])
  assert pigeon.receive_normal() == (b"", [frames[0]])


def test_queue_overflow_during_initialization_is_controlled(monkeypatch):
  logs = []
  frames = nav_pvt_frame() + rawx_frame()
  pigeon = ScriptedPigeon(pre_transaction=(frames,))
  monkeypatch.setattr(pigeond, "PENDING_FRAME_MAX_COUNT", 1)
  monkeypatch.setattr(pigeond.cloudlog, "error", logs.append)

  assert not pigeond.init_pigeon(pigeon)
  assert pigeon.sent == []
  assert len(pigeon._pending_frames) == 2
  assert "frame_count=2" in logs[0]
  assert "operation=ubx_06_00" in logs[0]
  assert "receiver_cycle=0" in logs[0]
  assert "frame_limit" in logs[0]


def test_queue_overflow_during_mga_restore_returns_failed_result(monkeypatch):
  frames = nav_pvt_frame() + rawx_frame()
  pigeon = ScriptedPigeon(pre_transaction=(frames,))
  cache = SimpleNamespace(
    saved_at_utc=datetime(2026, 7, 10, tzinfo=UTC),
    rtc_counter_seconds=100,
    quality=None,
    database_frames=(ubx_frame(0x13, 0x80, b"dbd"),),
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    position_accuracy_cm=1000,
  )
  monkeypatch.setattr(pigeond, "PENDING_FRAME_MAX_COUNT", 1)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(pigeond, "load_cache", lambda *args, **kwargs: cache)

  result = pigeond.restore_navigation_assistance(
    pigeon,
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    trusted_now=datetime(2026, 7, 10, 0, 5, tzinfo=UTC),
    time_assistance_source="system_synchronized",
    allow_legacy_direct_restore=True,
  )
  assert result.status is pigeond.NavigationAssistanceRestoreStatus.FAILED
  assert result.accepted_frame_count == 0
  assert result.failure_phase is pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_WRITE
  assert len(pigeon._pending_frames) == 2


def test_cfg_ack_between_half_and_one_second_is_accepted(monkeypatch):
  clock = SimpleNamespace(now=0.0)

  class DelayedPigeon(ScriptedPigeon):
    def __init__(self):
      super().__init__()
      self.armed = False
      self.delivered = False

    def send(self, data):
      self.sent.append(data)
      self.armed = True

    def _receive_tty(self):
      if self.armed and not self.delivered and clock.now >= 0.75:
        self.delivered = True
        return cfg_ack(0x08)
      return b""

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock.now)
  monkeypatch.setattr(pigeond.time, "sleep", lambda duration: setattr(clock, "now", clock.now + duration))
  pigeon = DelayedPigeon()
  pigeon.send_with_ack(cfg_write(0x08))
  assert 0.75 <= clock.now < pigeond.CFG_ACK_TIMEOUT


def test_cfg_ack_times_out_after_new_bounded_timeout(monkeypatch):
  clock = SimpleNamespace(now=0.0)
  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock.now)
  monkeypatch.setattr(pigeond.time, "sleep", lambda duration: setattr(clock, "now", clock.now + duration))
  pigeon = ScriptedPigeon()
  with pytest.raises(TimeoutError):
    pigeon.send_with_ack(cfg_write(0x08))
  assert clock.now >= pigeond.CFG_ACK_TIMEOUT


def test_effective_startup_configuration_logging(monkeypatch):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  pigeond.log_startup_configuration(
    RateConfig(100, 1, 0),
    Nav5Config(4, 3),
    OdoConfig(0, 1, 3),
    ItfmConfig(0xAD62ADFF, 0x0000631E),
    MessageRateConfig(0x01, 0x07, (0, 1, 0, 0, 0, 0)),
    MessageRateConfig(0x02, 0x15, (0, 1, 0, 0, 0, 0)),
  )
  assert "measurement_period_ms=100" in logs[0]
  assert "navigation_rate=1" in logs[0]
  assert "time_reference=0" in logs[0]
  assert any("CFG-MSG NAV-PVT effective" in message and "uart1_rate=1" in message for message in logs)
  assert any("CFG-MSG RXM-RAWX effective" in message and "uart1_rate=1" in message for message in logs)


def test_configuration_item_retries_only_its_own_write_after_mismatch():
  observed = [0, 1]
  writes: list[str] = []

  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-MSG-NAV-PVT",
    mandatory=True,
    expected_value="uart1_rate=1",
    poll=lambda: observed.pop(0),
    verify=lambda value: (_ for _ in ()).throw(pigeond.ReceiverConfigurationError("rate mismatch")) if value != 1 else None,
    write=lambda: writes.append("uart_write"),
  )

  assert result.verified
  assert result.write_attempt_count == 1
  assert result.poll_attempt_count == 2
  assert writes == ["uart_write"]


@pytest.mark.parametrize(
  "stream_name",
  ("NAV-PVT", "RXM-RAWX", "RXM-SFRBX", "NAV-SAT", "MON-HW", "MON-HW2"),
)
def test_each_required_stream_is_verified_on_uart1(stream_name):
  result = pigeond.run_receiver_configuration_item(
    item_name=f"CFG-MSG-{stream_name}",
    mandatory=stream_name in ("NAV-PVT", "RXM-RAWX", "RXM-SFRBX"),
    expected_value="uart1_rate=1",
    poll=lambda: MessageRateConfig(1, 7, (0, 1, 0, 0, 0, 0)),
    verify=lambda value: (_ for _ in ()).throw(pigeond.ReceiverConfigurationError("wrong UART1 rate")) if value.rates[1] != 1 else None,
    write=lambda: pytest.fail("already-correct stream must not be rewritten"),
  )

  assert result.verified
  assert result.write_attempt_count == 0


def test_cfg_gnss_conservative_validation_requires_enabled_gps_and_valid_channels():
  config = GnssConfig(
    version=0,
    hardware_tracking_channels=16,
    configured_tracking_channels=12,
    blocks=(GnssConfigBlock(0, 8, 8, True, 0, 1), GnssConfigBlock(6, 4, 8, True, 0, 1)),
  )
  pigeond.verify_cfg_gnss_conservatively(config)
  with pytest.raises(pigeond.ReceiverConfigurationError, match="GPS is disabled"):
    pigeond.verify_cfg_gnss_conservatively(
      GnssConfig(0, 16, 8, (GnssConfigBlock(0, 8, 8, False, 0, 0),)),
    )


def test_cfg_rxm_and_pm2_conservative_validation_reject_unsupported_values():
  pigeond.verify_cfg_rxm_conservatively(RxmConfig(0))
  pigeond.verify_cfg_rxm_conservatively(RxmConfig(4))
  with pytest.raises(pigeond.ReceiverConfigurationError, match="continuous acquisition"):
    pigeond.verify_cfg_rxm_conservatively(RxmConfig(1))
  with pytest.raises(pigeond.ReceiverConfigurationError, match="unsupported"):
    pigeond.verify_cfg_rxm_conservatively(RxmConfig(2))
  pigeond.verify_cfg_pm2_conservatively(Pm2Config(1, 0, 0, 0, 0, 0, 0, 0, None))
  with pytest.raises(pigeond.ReceiverConfigurationError, match="inactive power-management"):
    pigeond.verify_cfg_pm2_conservatively(Pm2Config(1, 0, 1 << 17, 10_000, 10_000, 0, 1, 5, None))
  with pytest.raises(pigeond.ReceiverConfigurationError, match="unsupported"):
    pigeond.verify_cfg_pm2_conservatively(Pm2Config(3, 0, 0, 0, 0, 0, 0, 0, None))


@pytest.mark.parametrize(
  ("item_name", "observed", "verifier"),
  (
    ("CFG-RXM", RxmConfig(1), pigeond.verify_cfg_rxm_conservatively),
    (
      "CFG-PM2",
      Pm2Config(1, 0, 1 << 17, 10_000, 10_000, 0, 1, 5, None),
      pigeond.verify_cfg_pm2_conservatively,
    ),
  ),
)
def test_supported_but_unwanted_power_state_is_diagnostic_mismatch(item_name, observed, verifier):
  result = pigeond.run_receiver_configuration_item(
    item_name=item_name,
    mandatory=False,
    expected_value="continuous_inactive_power_management",
    poll=lambda: observed,
    verify=verifier,
    write=lambda: None,
    max_write_attempts=0,
  )

  assert not result.verified
  assert result.failure_kind is pigeond.ReceiverConfigurationFailureKind.READBACK_MISMATCH
  assert result.readback_status is pigeond.ReceiverConfigurationReadbackStatus.MISMATCHED
  assert result.ack_status is pigeond.ReceiverConfigurationAckStatus.NOT_REQUIRED


def test_configuration_deadline_prevents_another_uart_write(monkeypatch):
  monkeypatch.setattr(pigeond.time, "monotonic", lambda: 45.0)
  writes: list[str] = []
  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-MSG-NAV-PVT",
    mandatory=True,
    expected_value="uart1_rate=1",
    poll=lambda: 0,
    verify=lambda value: (_ for _ in ()).throw(pigeond.ReceiverConfigurationError("mismatch")) if value != 1 else None,
    write=lambda: writes.append("unexpected"),
    pre_start_deadline=45.0,
  )

  assert not result.verified
  assert result.failure_kind is pigeond.ReceiverConfigurationFailureKind.DEADLINE_EXHAUSTED
  assert writes == []


def test_configuration_deadline_is_rechecked_at_uart_write_boundary(monkeypatch):
  clock = SimpleNamespace(now=44.0)

  class DeadlineDrainPigeon(ScriptedPigeon):
    def drain_before_transaction(
      self,
      operation="pre_transaction_drain",
      deadline=None,
    ):
      clock.now = 45.0

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock.now)
  pigeon = DeadlineDrainPigeon()

  with pytest.raises(TimeoutError, match="before UART write") as exc_info:
    pigeond.send_configuration_with_ack(
      pigeon,
      cfg_write(0x08),
      pre_start_deadline=45.0,
    )

  assert not exc_info.value.receiver_write_attempted
  assert pigeon.sent == []


def test_configuration_ack_wait_uses_remaining_absolute_deadline(monkeypatch):
  clock = SimpleNamespace(now=44.0)

  class DeadlineDrainPigeon(ScriptedPigeon):
    def drain_before_transaction(
      self,
      operation="pre_transaction_drain",
      deadline=None,
    ):
      clock.now = 44.8

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock.now)
  monkeypatch.setattr(pigeond.time, "sleep", lambda duration: setattr(clock, "now", clock.now + duration))
  pigeon = DeadlineDrainPigeon()

  with pytest.raises(TimeoutError):
    pigeond.send_configuration_with_ack(
      pigeon,
      cfg_write(0x08),
      pre_start_deadline=45.0,
    )

  assert pigeon.sent == [cfg_write(0x08)]
  assert 45.0 <= clock.now < 45.01


def test_slow_transaction_drain_starts_gnss_at_absolute_deadline(
  monkeypatch,
):
  clock = SimpleNamespace(now=44.9)
  start_times: list[float] = []

  class SlowDrainPigeon(ScriptedPigeon):
    def _read_stream(self):
      clock.now = 45.0
      return b"stale", []

    def send(self, data):
      self.sent.append(data)
      if data == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        start_times.append(clock.now)

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock.now)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _duration: None)
  pigeon = SlowDrainPigeon()

  def configure() -> None:
    pigeond.send_configuration_with_ack(
      pigeon,
      cfg_write(0x08),
      pre_start_deadline=45.0,
    )

  with pytest.raises(TimeoutError, match="during receiver read"):
    with pigeond.install_pre_acquisition_initialization(
      configure,
      pre_start_deadline=45.0,
    ) as initialization:
      with pigeond.paused_gnss_acquisition(pigeon):
        initialization.run()

  assert start_times == [45.0]
  assert cfg_write(0x08) not in pigeon.sent


def test_slow_configuration_readback_starts_gnss_at_absolute_deadline(
  monkeypatch,
):
  clock = SimpleNamespace(now=44.9)
  start_times: list[float] = []

  class SlowReadbackPigeon(ScriptedPigeon):
    def send(self, data):
      self.sent.append(data)
      if data == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        start_times.append(clock.now)

    def receive_transaction_data(self, transaction):
      clock.now = 45.0
      response = cfg_rate_frame()
      return response, [response], [response]

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock.now)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _duration: None)
  pigeon = SlowReadbackPigeon()

  def poll() -> None:
    pigeond.poll_cfg_rate(
      pigeon,
      timeout=0.5,
      deadline=45.0,
    )

  with pytest.raises(TimeoutError, match="during readback"):
    with pigeond.install_pre_acquisition_initialization(
      poll,
      pre_start_deadline=45.0,
    ) as initialization:
      with pigeond.paused_gnss_acquisition(pigeon):
        initialization.run()

  assert start_times == [45.0]
  assert pigeond.build_cfg_rate_poll_message() in pigeon.sent


def test_configuration_item_retries_matching_nak_without_replaying_prior_items():
  writes: list[str] = []

  def write() -> None:
    writes.append("CFG-MSG-RXM-RAWX")
    if len(writes) == 1:
      raise pigeond.CfgNakError("matching CFG NAK")

  values = [0, 0, 1]
  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-MSG-RXM-RAWX",
    mandatory=True,
    expected_value="uart1_rate=1",
    poll=lambda: values.pop(0),
    verify=lambda value: (_ for _ in ()).throw(pigeond.ReceiverConfigurationError("rate mismatch")) if value != 1 else None,
    write=write,
  )

  assert result.verified
  assert result.write_attempt_count == 2
  assert writes == ["CFG-MSG-RXM-RAWX", "CFG-MSG-RXM-RAWX"]


def test_configuration_item_ack_timeout_then_success():
  writes = 0
  values = [0, 0, 1]

  def write() -> None:
    nonlocal writes
    writes += 1
    if writes == 1:
      raise TimeoutError("matching CFG ACK timed out")

  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-RATE",
    mandatory=True,
    expected_value="100ms/1",
    poll=lambda: values.pop(0),
    verify=lambda value: (_ for _ in ()).throw(pigeond.ReceiverConfigurationError("mismatch")) if value != 1 else None,
    write=write,
  )

  assert result.verified
  assert result.write_attempt_count == 2


def test_ack_timeout_then_verified_readback_preserves_exact_ack_status():
  observed = iter((0, 1))

  def write() -> None:
    raise TimeoutError("matching CFG ACK timed out")

  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-RATE",
    mandatory=True,
    expected_value="100ms/1",
    poll=observed.__next__,
    verify=lambda value: (_ for _ in ()).throw(pigeond.ReceiverConfigurationError("mismatch")) if value != 1 else None,
    write=write,
  )

  assert result.verified
  assert result.write_attempt_count == 1
  assert result.poll_attempt_count == 2
  assert result.ack_status is pigeond.ReceiverConfigurationAckStatus.TIMED_OUT
  assert result.readback_status is pigeond.ReceiverConfigurationReadbackStatus.VERIFIED


def test_successful_retry_clears_stale_write_error_for_terminal_result():
  writes = 0

  def write() -> None:
    nonlocal writes
    writes += 1
    if writes == 1:
      raise TimeoutError("first matching CFG ACK timed out")

  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-RATE",
    mandatory=True,
    expected_value="100ms/1",
    poll=lambda: 0,
    verify=lambda _value: (_ for _ in ()).throw(pigeond.ReceiverConfigurationError("final mismatch")),
    write=write,
  )

  assert not result.verified
  assert result.write_attempt_count == 2
  assert result.poll_attempt_count == 3
  assert result.failure_kind is pigeond.ReceiverConfigurationFailureKind.READBACK_MISMATCH
  assert result.failure_phase == "readback"
  assert result.ack_status is pigeond.ReceiverConfigurationAckStatus.ACKNOWLEDGED
  assert result.error == "final mismatch"


@pytest.mark.parametrize(
  ("readback_error", "expected_kind", "expected_status"),
  (
    (
      pigeond.CfgPollTimeoutError("terminal CFG readback timed out"),
      pigeond.ReceiverConfigurationFailureKind.POLL_TIMEOUT,
      pigeond.ReceiverConfigurationReadbackStatus.TIMED_OUT,
    ),
    (
      pigeond.ReceiverConfigurationParserError("terminal CFG readback malformed"),
      pigeond.ReceiverConfigurationFailureKind.PARSER_ERROR,
      pigeond.ReceiverConfigurationReadbackStatus.PARSER_ERROR,
    ),
  ),
)
def test_ack_timeout_and_terminal_readback_failure_preserve_both_outcomes(
  readback_error,
  expected_kind,
  expected_status,
):
  polls = 0

  def poll():
    nonlocal polls
    polls += 1
    if polls == 1:
      return 0
    raise readback_error

  def write() -> None:
    raise TimeoutError("matching CFG ACK timed out")

  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-PRT-3",
    mandatory=True,
    expected_value="port3",
    poll=poll,
    verify=lambda value: (_ for _ in ()).throw(pigeond.ReceiverConfigurationError("initial mismatch")) if value != 1 else None,
    write=write,
    max_write_attempts=1,
  )

  assert not result.verified
  assert result.write_attempt_count == 1
  assert result.poll_attempt_count == 2
  assert result.ack_status is pigeond.ReceiverConfigurationAckStatus.TIMED_OUT
  assert result.readback_status is expected_status
  assert result.failure_kind is expected_kind
  assert result.failure_phase == "readback"
  assert result.error_type == type(readback_error).__name__
  assert result.error == str(readback_error)


def test_cfg_nak_retry_waits_for_complete_official_response_window(monkeypatch):
  clock = SimpleNamespace(now=0.0)
  write_times: list[float] = []

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock.now)
  monkeypatch.setattr(
    pigeond.time,
    "sleep",
    lambda duration: setattr(clock, "now", clock.now + duration),
  )

  def write() -> None:
    write_times.append(clock.now)
    if len(write_times) == 1:
      raise pigeond.CfgNakError("matching CFG NAK", clock.now + pigeond.CFG_ACK_TIMEOUT)

  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-MSG-NAV-PVT",
    mandatory=True,
    expected_value="uart1_rate=1",
    poll=iter((0, 0, 1)).__next__,
    verify=lambda value: (_ for _ in ()).throw(pigeond.ReceiverConfigurationError("mismatch")) if value != 1 else None,
    write=write,
  )

  assert result.verified
  assert write_times == [0.0, pigeond.CFG_ACK_TIMEOUT]


def test_cfg_nak_retry_sleep_is_clipped_to_pre_start_deadline(monkeypatch):
  clock = SimpleNamespace(now=0.0)
  writes: list[float] = []
  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock.now)
  monkeypatch.setattr(pigeond.time, "sleep", lambda duration: setattr(clock, "now", clock.now + duration))

  def write() -> None:
    writes.append(clock.now)
    raise pigeond.CfgNakError("matching CFG NAK", clock.now + pigeond.CFG_ACK_TIMEOUT)

  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-MSG-NAV-PVT",
    mandatory=True,
    expected_value="uart1_rate=1",
    poll=lambda: 0,
    verify=lambda _value: (_ for _ in ()).throw(pigeond.ReceiverConfigurationError("mismatch")),
    write=write,
    pre_start_deadline=0.25,
  )

  assert writes == [0.0]
  assert clock.now == pytest.approx(0.25)
  assert result.failure_kind is pigeond.ReceiverConfigurationFailureKind.DEADLINE_EXHAUSTED
  assert result.write_attempt_count == 1


def test_cfg_nak_retry_boundary_uses_post_send_timestamp(monkeypatch):
  clock = SimpleNamespace(now=10.0)

  class DelayedWritePigeon(ScriptedPigeon):
    def send(self, data):
      self.sent.append(data)
      clock.now += 0.4
      self.available.append(cfg_ack(data[3], accepted=False))

  monkeypatch.setattr(pigeond.time, "monotonic", lambda: clock.now)
  pigeon = DelayedWritePigeon()

  with pytest.raises(pigeond.CfgNakError) as exc_info:
    pigeon.send_with_ack(cfg_write(0x08))

  assert clock.now == pytest.approx(10.4)
  assert exc_info.value.retry_not_before == pytest.approx(11.5)
  assert exc_info.value.retry_not_before != pytest.approx(11.1)


def test_cfg_poll_timeout_is_bounded_to_the_failing_item():
  writes: list[str] = []

  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-PRT-1",
    mandatory=True,
    expected_value="uart1",
    poll=lambda: (_ for _ in ()).throw(pigeond.CfgPollTimeoutError("CFG-PRT response timed out")),
    verify=lambda _value: None,
    write=lambda: writes.append("CFG-PRT-1"),
  )

  assert not result.verified
  assert result.failure_kind is pigeond.ReceiverConfigurationFailureKind.POLL_TIMEOUT
  assert result.write_attempt_count == pigeond.RECEIVER_CONFIGURATION_ITEM_MAX_WRITE_ATTEMPTS
  assert result.poll_attempt_count == pigeond.RECEIVER_CONFIGURATION_ITEM_MAX_WRITE_ATTEMPTS + 1
  assert writes == ["CFG-PRT-1"] * pigeond.RECEIVER_CONFIGURATION_ITEM_MAX_WRITE_ATTEMPTS


def test_permanent_item_rejection_is_bounded():
  writes: list[str] = []
  result = pigeond.run_receiver_configuration_item(
    item_name="CFG-RATE",
    mandatory=True,
    expected_value="100ms/1",
    poll=lambda: (_ for _ in ()).throw(pigeond.CfgPollTimeoutError("no readback")),
    verify=lambda _value: None,
    write=lambda: (writes.append("CFG-RATE"), (_ for _ in ()).throw(pigeond.CfgNakError("matching CFG NAK")))[1],
  )

  assert not result.verified
  assert result.failure_kind is pigeond.ReceiverConfigurationFailureKind.POLL_TIMEOUT
  assert result.failure_phase == "readback"
  assert result.ack_status is pigeond.ReceiverConfigurationAckStatus.REJECTED
  assert result.readback_status is pigeond.ReceiverConfigurationReadbackStatus.TIMED_OUT
  assert result.poll_attempt_count == pigeond.RECEIVER_CONFIGURATION_ITEM_MAX_WRITE_ATTEMPTS + 1
  assert len(writes) == pigeond.RECEIVER_CONFIGURATION_ITEM_MAX_WRITE_ATTEMPTS


def test_receiver_configuration_summary_round_trips_as_complete_json(monkeypatch, tmp_path):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  summary = complete_configuration_summary(
    receiver_cycle=7,
    started_at=10.0,
    completed_at=11.0,
    items=verified_configuration_items(
      expected_value="expected" * 1000,
    ),
  )

  assert persist_current_configuration_summary(summary)
  path = pigeond.receiver_configuration_summary_path()
  raw_record = json.loads(path.read_text())

  assert pigeond.load_receiver_configuration_summary_record() == raw_record
  assert raw_record["schema_version"] == pigeond.RECEIVER_CONFIGURATION_SUMMARY_SCHEMA_VERSION
  assert raw_record["boot_id"] == pigeond.RECEIVER_CONFIGURATION_BOOT_ID
  assert raw_record["process_start_id"] == pigeond.RECEIVER_CONFIGURATION_PROCESS_START_ID
  assert raw_record["receiver_fingerprint"] == "unidentified"
  assert raw_record["receiver_cycle"] == 7
  assert raw_record["total_items"] == len(pigeond.RECEIVER_CONFIGURATION_ITEM_INVENTORY)
  assert raw_record["verified_items"] == len(pigeond.RECEIVER_CONFIGURATION_ITEM_INVENTORY)
  assert raw_record["failed_items"] == 0
  assert raw_record["mandatory_failures"] == []
  assert raw_record["all_mandatory_items_verified"]
  assert len(path.read_bytes()) <= 16384
  assert raw_record["items"][0]["item_name"] == "CFG-PRT-3"
  assert len(raw_record["items"][0]["expected_value"]) == 128


def test_combined_ack_and_readback_failures_round_trip_durably(monkeypatch, tmp_path):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  failed_item = pigeond.ReceiverConfigurationItemResult(
    item_name="CFG-PRT-3",
    mandatory=True,
    attempted=True,
    write_attempt_count=1,
    ack_status=pigeond.ReceiverConfigurationAckStatus.TIMED_OUT,
    poll_attempt_count=2,
    readback_status=pigeond.ReceiverConfigurationReadbackStatus.PARSER_ERROR,
    verified=False,
    expected_value="port3",
    observed_value="initial mismatch",
    failure_kind=pigeond.ReceiverConfigurationFailureKind.PARSER_ERROR,
    failure_phase="readback",
    error_type="ReceiverConfigurationParserError",
    error="terminal CFG readback malformed",
  )
  summary = complete_configuration_summary(
    receiver_cycle=8,
    items=(failed_item, *verified_configuration_items()[1:]),
  )

  assert persist_current_configuration_summary(summary)
  record = pigeond.load_receiver_configuration_summary_record()
  assert record is not None
  persisted_item = cast(list[dict[str, object]], record["items"])[0]
  assert persisted_item["ack_status"] == "timed_out"
  assert persisted_item["readback_status"] == "parser_error"
  assert persisted_item["failure_kind"] == "parser_error"
  assert persisted_item["failure_phase"] == "readback"


@pytest.mark.parametrize(
  ("identity_field", "stale_value"),
  (
    ("boot_id", "previous-boot"),
    ("process_start_id", "previous-process"),
    ("receiver_fingerprint", "different-receiver"),
  ),
)
def test_receiver_configuration_loader_rejects_stale_identity(
  monkeypatch,
  tmp_path,
  identity_field,
  stale_value,
):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  summary = complete_configuration_summary(
    receiver_fingerprint="current-receiver",
  )
  assert persist_current_configuration_summary(summary)
  path = pigeond.receiver_configuration_summary_path()
  record = json.loads(path.read_text())
  record[identity_field] = stale_value
  path.write_text(json.dumps(record))

  assert (
    pigeond.load_receiver_configuration_summary_record(
      "current-receiver",
      3,
    )
    is None
  )


def test_receiver_configuration_loader_rejects_stale_cycle_and_missing_context(
  monkeypatch,
  tmp_path,
):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  summary = complete_configuration_summary(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
  )
  assert persist_current_configuration_summary(summary)

  assert pigeond.load_receiver_configuration_summary_record("v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov", 4) is None
  monkeypatch.setattr(
    pigeond,
    "_current_receiver_configuration_fingerprint",
    None,
  )
  monkeypatch.setattr(
    pigeond,
    "_current_receiver_configuration_cycle",
    None,
  )
  assert pigeond.load_receiver_configuration_summary_record() is None


def test_new_receiver_cycle_clears_cycle_scoped_configuration_state(
  monkeypatch,
):
  previous_summary = complete_configuration_summary(
    receiver_cycle=3,
    receiver_fingerprint="previous-receiver",
  )
  monkeypatch.setattr(
    pigeond,
    "_last_receiver_configuration_summary",
    previous_summary,
  )
  monkeypatch.setattr(
    pigeond,
    "_last_receiver_configuration_persistence_status",
    pigeond.ReceiverConfigurationPersistenceStatus("old", True),
  )
  pigeon = SimpleNamespace(
    _receiver_cycle=4,
    _transport_verified_for_receiver_cycle=True,
  )

  pigeond.begin_receiver_configuration_cycle(
    pigeon,
    "current-receiver",
    transport_verified=False,
  )

  assert pigeond.last_receiver_configuration_summary() is None
  assert pigeond.last_receiver_configuration_persistence_status() is None
  assert not pigeond._current_receiver_configuration_record_ready
  assert pigeond._current_receiver_configuration_cycle == 4
  assert pigeond._current_receiver_configuration_fingerprint == "current-receiver"
  assert not pigeon._transport_verified_for_receiver_cycle


def test_new_cycle_rejects_old_record_even_when_numeric_identity_repeats(
  monkeypatch,
  tmp_path,
):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  old_summary = complete_configuration_summary(
    receiver_cycle=4,
    receiver_fingerprint="same-receiver",
  )
  assert persist_current_configuration_summary(old_summary)
  assert pigeond.load_receiver_configuration_summary_record() is not None

  pigeond.begin_receiver_configuration_cycle(
    SimpleNamespace(_receiver_cycle=4),
    "same-receiver",
    transport_verified=False,
  )

  assert pigeond.load_receiver_configuration_summary_record() is None


def test_previous_cycle_summary_cannot_mutate_or_persist_in_current_cycle(
  monkeypatch,
  tmp_path,
):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  previous_summary = complete_configuration_summary(
    receiver_cycle=3,
    receiver_fingerprint="previous-receiver",
    gnss_start_attempted=False,
    gnss_start_sent=False,
  )
  pigeond.set_receiver_configuration_context(3, "previous-receiver")
  monkeypatch.setattr(
    pigeond,
    "_last_receiver_configuration_summary",
    previous_summary,
  )
  current_pigeon = SimpleNamespace(_receiver_cycle=4)
  pigeond.begin_receiver_configuration_cycle(
    current_pigeon,
    "current-receiver",
    transport_verified=False,
  )

  monkeypatch.setattr(
    pigeond,
    "_last_receiver_configuration_summary",
    previous_summary,
  )
  initialization = pigeond.PreAcquisitionInitialization(lambda: None)
  initialization.note_gnss_start_attempted()
  initialization.mark_gnss_start_sent()

  assert pigeond.last_receiver_configuration_summary() is previous_summary
  assert not previous_summary.gnss_start_attempted
  assert not previous_summary.gnss_start_sent
  assert not pigeond.persist_receiver_configuration_summary(previous_summary)
  assert pigeond._current_receiver_configuration_cycle == 4
  assert pigeond._current_receiver_configuration_fingerprint == "current-receiver"
  assert pigeond.load_receiver_configuration_summary_record() is None


def test_start_cleanup_never_persists_previous_cycle_summary(monkeypatch):
  persisted: list[pigeond.ReceiverConfigurationSummary] = []
  previous_summary = complete_configuration_summary(
    receiver_cycle=3,
    receiver_fingerprint="previous-receiver",
    gnss_start_attempted=False,
    gnss_start_sent=False,
  )
  pigeond.set_receiver_configuration_context(4, "current-receiver")
  monkeypatch.setattr(
    pigeond,
    "_last_receiver_configuration_summary",
    previous_summary,
  )
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(
    pigeond,
    "persist_receiver_configuration_summary",
    lambda summary: persisted.append(summary) or True,
  )

  class Pigeon:
    def send(self, _message):
      pass

  with pigeond.install_pre_acquisition_initialization(lambda: None):
    with pigeond.paused_gnss_acquisition(cast(pigeond.TTYPigeon, Pigeon())):
      pass

  assert persisted == []
  assert pigeond.last_receiver_configuration_summary() is previous_summary
  assert not previous_summary.gnss_start_attempted
  assert not previous_summary.gnss_start_sent


def test_loader_rejects_incomplete_but_internally_consistent_inventory(
  monkeypatch,
  tmp_path,
):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  summary = complete_configuration_summary()
  assert persist_current_configuration_summary(summary)
  path = pigeond.receiver_configuration_summary_path()
  record = json.loads(path.read_text())
  record["items"] = record["items"][:-1]
  record["total_items"] -= 1
  record["verified_items"] -= 1
  path.write_text(json.dumps(record))

  assert pigeond.load_receiver_configuration_summary_record() is None


def test_loader_rejects_wrong_inventory_classification_and_order(
  monkeypatch,
  tmp_path,
):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  summary = complete_configuration_summary()
  assert persist_current_configuration_summary(summary)
  path = pigeond.receiver_configuration_summary_path()

  wrong_classification = json.loads(path.read_text())
  wrong_classification["items"][-1]["mandatory"] = True
  path.write_text(json.dumps(wrong_classification))
  assert pigeond.load_receiver_configuration_summary_record() is None

  wrong_order = json.loads(json.dumps(wrong_classification))
  wrong_order["items"][-1]["mandatory"] = False
  wrong_order["items"][0], wrong_order["items"][1] = (
    wrong_order["items"][1],
    wrong_order["items"][0],
  )
  path.write_text(json.dumps(wrong_order))
  assert pigeond.load_receiver_configuration_summary_record() is None


def test_navx5_failure_is_visible_in_durable_summary(monkeypatch, tmp_path):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  summary = complete_configuration_summary(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    navx5_ack_aiding_result=(pigeond.Navx5AckAidingConfigurationResult.WRITE_TIMED_OUT),
  )

  assert persist_current_configuration_summary(summary)
  record = pigeond.load_receiver_configuration_summary_record("v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov", 3)

  assert record is not None
  assert record["navx5_ack_aiding_result"] == "write_timed_out"
  assert record["configuration_degraded"] is True


def test_configuration_summary_persistence_occurs_only_after_start_write(monkeypatch):
  events: list[object] = []
  summary = complete_configuration_summary(
    gnss_start_attempted=False,
    gnss_start_sent=False,
  )
  pigeond.set_receiver_configuration_context(3, "unidentified")
  monkeypatch.setattr(pigeond, "_last_receiver_configuration_summary", summary)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)

  def persist(final_summary):
    events.append(
      (
        "persist",
        final_summary.gnss_start_attempted,
        final_summary.gnss_start_sent,
      )
    )
    return True

  monkeypatch.setattr(pigeond, "persist_receiver_configuration_summary", persist)

  class Pigeon:
    def send(self, message):
      if message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        events.append("start_write")

  with pigeond.install_pre_acquisition_initialization(lambda: None) as initialization:
    with pigeond.paused_gnss_acquisition(cast(pigeond.TTYPigeon, Pigeon())):
      initialization.run()

  assert events == ["start_write", ("persist", True, True)]


def test_failed_start_write_persists_attempted_but_not_sent(monkeypatch):
  persisted: list[pigeond.ReceiverConfigurationSummary] = []
  pigeond.set_receiver_configuration_context(3, "unidentified")
  monkeypatch.setattr(
    pigeond,
    "_last_receiver_configuration_summary",
    complete_configuration_summary(
      gnss_start_attempted=False,
      gnss_start_sent=False,
    ),
  )
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(
    pigeond,
    "persist_receiver_configuration_summary",
    lambda summary: persisted.append(summary) or True,
  )

  class Pigeon:
    def send(self, message):
      if message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        raise OSError("START write failed")

  with pytest.raises(OSError, match="START write failed"):
    with pigeond.install_pre_acquisition_initialization(lambda: None) as initialization:
      with pigeond.paused_gnss_acquisition(cast(pigeond.TTYPigeon, Pigeon())):
        initialization.run()

  assert len(persisted) == 1
  assert persisted[0].gnss_start_attempted
  assert not persisted[0].gnss_start_sent


def test_expired_deadline_skips_optional_final_drain_before_start(monkeypatch):
  events: list[str] = []
  monkeypatch.setattr(pigeond.time, "monotonic", lambda: 45.0)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _delay: None)
  monkeypatch.setattr(pigeond, "_last_receiver_configuration_summary", None)

  class Pigeon:
    def drain_before_transaction(self, _operation):
      events.append("drain")

    def send(self, message):
      if message == pigeond.CONTROLLED_GNSS_START_MESSAGE:
        events.append("start")

  with pigeond.install_pre_acquisition_initialization(
    lambda: None,
    pre_start_deadline=45.0,
  ) as initialization:
    initialization.require_pre_gnss_start_drain()
    with pigeond.paused_gnss_acquisition(cast(pigeond.TTYPigeon, Pigeon())):
      initialization.run()

  assert events == ["start"]


@pytest.mark.parametrize(
  ("field", "invalid_value"),
  (
    ("receiver_cycle", None),
    ("transport_verified", 1),
    ("transport_verified", False),
    ("total_items", 2),
    ("navx5_ack_aiding_result", None),
    ("gnss_start_attempted", False),
  ),
)
def test_receiver_configuration_summary_loader_rejects_invalid_schema(monkeypatch, tmp_path, field, invalid_value):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  summary = complete_configuration_summary()
  assert persist_current_configuration_summary(summary)
  path = pigeond.receiver_configuration_summary_path()
  record = json.loads(path.read_text())
  record[field] = invalid_value
  path.write_text(json.dumps(record))

  assert pigeond.load_receiver_configuration_summary_record() is None


def test_receiver_configuration_summary_loader_rejects_invalid_item_enum(monkeypatch, tmp_path):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  summary = complete_configuration_summary()
  assert persist_current_configuration_summary(summary)
  path = pigeond.receiver_configuration_summary_path()
  record = json.loads(path.read_text())
  record["items"][0]["ack_status"] = "invented"
  path.write_text(json.dumps(record))

  assert pigeond.load_receiver_configuration_summary_record() is None


def test_persistence_failure_invalidates_old_summary_and_is_observable(monkeypatch, tmp_path):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", str(tmp_path / "assistance-cache.json"))
  old_summary = complete_configuration_summary(receiver_cycle=3)
  new_summary = complete_configuration_summary(
    receiver_cycle=4,
    started_at=3.0,
    completed_at=4.0,
  )
  assert persist_current_configuration_summary(old_summary)
  path = pigeond.receiver_configuration_summary_path()
  original_write_text = type(path).write_text

  def fail_write(self, *_args, **_kwargs):
    if self == path.with_suffix(".tmp"):
      raise OSError("storage unavailable")
    return original_write_text(self, *_args, **_kwargs)

  monkeypatch.setattr(type(path), "write_text", fail_write)

  pigeond.set_receiver_configuration_context(4, "unidentified")
  assert not pigeond.persist_receiver_configuration_summary(new_summary)
  status = pigeond.last_receiver_configuration_persistence_status()
  assert status is not None
  assert not status.succeeded
  assert status.error_type == "OSError"
  assert not path.exists()
  assert path.with_suffix(".stale").exists()
  assert pigeond.load_receiver_configuration_summary_record() is None


def test_receiver_configuration_cycle_uses_public_then_private_identity():
  assert pigeond.receiver_configuration_cycle_id(SimpleNamespace(receiver_cycle=8, _receiver_cycle=7)) == 8
  assert pigeond.receiver_configuration_cycle_id(SimpleNamespace(_receiver_cycle=7)) == 7


def test_end_to_end_hpg_1_40_protocol_20_30_initialization(monkeypatch):
  responses = [
    *(cfg_prt_frame(expected_port_config(port)) for port in (3, 0, 1, 4)),
    cfg_rate_frame(),
    cfg_nav5_frame(),
    cfg_odo_frame(),
    cfg_itfm_frame(),
    cfg_msg_frame(0x01, 0x07),
    cfg_msg_frame(0x02, 0x15),
    cfg_msg_frame(0x02, 0x13),
    cfg_msg_frame(0x01, 0x35),
    cfg_msg_frame(0x0A, 0x09),
    cfg_msg_frame(0x0A, 0x0B),
    sos_frame(3, 3),
  ]
  monkeypatch.setattr(
    pigeond,
    "Params",
    lambda: SimpleNamespace(get=lambda _key: None),
  )
  monkeypatch.setattr(
    pigeond,
    "poll_cfg_gnss",
    lambda _pigeon, timeout: GnssConfig(0, 16, 12, (GnssConfigBlock(0, 8, 8, True, 0, 1),)),
  )
  monkeypatch.setattr(pigeond, "poll_cfg_rxm", lambda _pigeon, timeout: RxmConfig(0))
  monkeypatch.setattr(pigeond, "poll_cfg_pm2", lambda _pigeon, timeout: Pm2Config(1, 0, 0, 0, 0, 0, 0, 0, None))
  monkeypatch.setattr(
    pigeond,
    "_ACTIVE_PRE_ACQUISITION_INITIALIZATION",
    pigeond.PreAcquisitionInitialization(
      lambda: None,
      receiver_fingerprint="receiver-fingerprint",
      navx5_ack_aiding_result=(pigeond.Navx5AckAidingConfigurationResult.WRITE_TIMED_OUT),
    ),
  )
  pigeon = ScriptedPigeon(responses)

  assert pigeond.init_pigeon(pigeon)
  summary = pigeond.last_receiver_configuration_summary()
  assert summary is not None
  assert summary.receiver_fingerprint == "receiver-fingerprint"
  assert summary.navx5_ack_aiding_result is pigeond.Navx5AckAidingConfigurationResult.WRITE_TIMED_OUT
  assert summary.configuration_degraded
  assert tuple((item.item_name, item.mandatory) for item in summary.items) == pigeond.RECEIVER_CONFIGURATION_ITEM_INVENTORY
  cfg_prt_polls = [message for message in pigeon.sent if message[2:4] == b"\x06\x00" and message[4:6] == b"\x01\x00"]
  assert cfg_prt_polls == [pigeond.build_cfg_prt_poll_message(port) for port in (3, 0, 1, 4)]
  assert not any(message[2:6] == b"\x06\x00\x00\x00" for message in pigeon.sent)
  assert len(pigeon.responses) == 1

  pigeond.run_post_start_legacy_assistance(pigeon)

  assert not pigeon.responses
  assert not pigeon.available


@pytest.mark.parametrize("flags", [0x01])
def test_cfg_odo_required_low_flag_nibble_passes(flags):
  pigeond.verify_startup_configuration(
    RateConfig(100, 1, 0),
    Nav5Config(4, 3),
    OdoConfig(0, flags, 3),
    ItfmConfig(0xAD62ADFF, 0x0000631E),
    MessageRateConfig(0x01, 0x07, (0, 1, 0, 0, 0, 0)),
    MessageRateConfig(0x02, 0x15, (0, 1, 0, 0, 0, 0)),
  )


@pytest.mark.parametrize("flags", [0x03, 0x05, 0x09])
def test_cfg_odo_other_documented_low_flag_bits_fail(flags):
  with pytest.raises(pigeond.ReceiverConfigurationError, match="CFG-ODO"):
    pigeond.verify_startup_configuration(
      RateConfig(100, 1, 0),
      Nav5Config(4, 3),
      OdoConfig(0, flags, 3),
      ItfmConfig(0xAD62ADFF, 0x0000631E),
      MessageRateConfig(0x01, 0x07, (0, 1, 0, 0, 0, 0)),
      MessageRateConfig(0x02, 0x15, (0, 1, 0, 0, 0, 0)),
    )


@pytest.mark.parametrize(
  ("acknowledgment", "time_assistance_expected"),
  [
    (pigeond.MgaAck(True, 1, 0, 0, 0x40, b"\x10\x00\x00\x80"), True),
    (pigeond.MgaAck(False, 0, 0, 1, 0x40, b"\x10\x00\x00\x80"), False),
    (None, False),
  ],
)
def test_cache_restore_is_independent_of_time_assistance_ack(
  monkeypatch,
  acknowledgment,
  time_assistance_expected,
):
  events = []

  class Diagnostics:
    def start_cycle(self, reason, now):
      pass

    def time_assistance_context(self, now):
      return "cycle=1"

  monkeypatch.setattr(pigeond, "init", lambda pigeon: None)
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda pigeon: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda info: False)
  monkeypatch.setattr(pigeond, "configure_navx5_ack_aiding", lambda *args, **kwargs: None)
  monkeypatch.setattr(pigeond, "read_host_time_observation", network_host_observation)
  monkeypatch.setattr(pigeond, "wait_for_matching_mga_ack", lambda *args, **kwargs: acknowledgment)
  monkeypatch.setattr(
    pigeond,
    "restore_navigation_assistance",
    lambda *args, **kwargs: events.append("restore"),
  )
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda info: False)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *args: None)

  result = pigeond.initialize_receiver_cycle(
    ScriptedPigeon(),
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    Diagnostics(),
    "process_start",
  )
  assert result.trusted_time_assistance_sent is time_assistance_expected
  assert result.navigation_assistance_restore_attempted is True
  assert events == ["restore"]


def test_time_assistance_observation_failure_is_not_success(monkeypatch):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "exception", logs.append)
  monkeypatch.setattr(
    pigeond,
    "wait_for_matching_mga_ack",
    lambda *args, **kwargs: (_ for _ in ()).throw(OSError("read failed")),
  )
  assert not pigeond.send_time_assistance(
    ScriptedPigeon(),
    assistance_time=pigeond.datetime(2026, 7, 10, tzinfo=pigeond.UTC),
  )
  assert "write_result=succeeded" in logs[0]
  assert "ack_result=observation_failed" in logs[0]


def test_real_mga_rejection_then_later_checksum_valid_acceptance():
  assistance_time = datetime(2026, 7, 10, tzinfo=UTC)
  message = pigeond.build_time_assistance_message(assistance_time)
  pigeon = ScriptedPigeon(
    (
      mga_ack(message, accepted=False),
      mga_ack(message, accepted=True),
    )
  )

  assert not pigeond.send_time_assistance(pigeon, assistance_time=assistance_time)
  assert pigeond.send_time_assistance(pigeon, assistance_time=assistance_time)


def test_real_mga_timeout_then_later_checksum_valid_acceptance():
  assistance_time = datetime(2026, 7, 10, tzinfo=UTC)
  message = pigeond.build_time_assistance_message(assistance_time)
  pigeon = ScriptedPigeon((b"", mga_ack(message, accepted=True)))

  assert not pigeond.send_time_assistance(
    pigeon,
    assistance_time=assistance_time,
    ack_timeout=0.005,
  )
  assert pigeond.send_time_assistance(pigeon, assistance_time=assistance_time)


def test_checksum_valid_sos_restore_response():
  pigeon = ScriptedPigeon((sos_frame(3, 2),))
  assert pigeon.poll_backup_restore_status() == 2


def test_corrupt_sos_restore_response_is_ignored():
  corrupt = bytearray(sos_frame(3, 2))
  corrupt[-1] ^= 0xFF
  pigeon = ScriptedPigeon((bytes(corrupt),))
  with pytest.raises(TimeoutError):
    pigeon.poll_backup_restore_status(timeout=0.005)


def test_upd_sos_invalid_responses_are_ignored_until_valid_restore_status():
  bad_checksum = bytearray(sos_frame(3, 2))
  bad_checksum[-1] ^= 0xFF
  invalid = (
    sos_frame(2, 1),
    sos_frame(3, 0),
    ubx_frame(0x01, 0x14, bytes((3, 0, 0, 0, 2, 0, 0, 0))),
    ubx_frame(0x09, 0x13, bytes((3, 0, 0, 0, 2, 0, 0, 0))),
    ubx_frame(0x09, 0x14, bytes((3, 0, 0, 0, 2, 0, 0))),
    bytes(bad_checksum),
  )
  valid = sos_frame(3, 2)
  pigeon = ScriptedPigeon((invalid + (valid,),))

  assert pigeon.poll_backup_restore_status() == 2
  assert b"".join(pigeon.published) == b"".join(invalid) + valid


class TransportPigeon:
  _receiver_cycle_response_state_prepared = False
  _stream_parser = SimpleNamespace()

  def __init__(self, events):
    self.events = events

  def reset_response_state(self):
    self.events.append("reset")

  def send(self, message):
    self.events.append(("send", message))


def configure_transport_test(monkeypatch, events, *, init_baudrate=None, poll_mon_ver=None):
  monkeypatch.setattr(pigeond.signal, "signal", lambda *_args: None)
  monkeypatch.setattr(
    pigeond,
    "set_power",
    lambda enabled: events.append(("power", enabled)),
  )
  monkeypatch.setattr(pigeond.time, "sleep", lambda _duration: None)
  monkeypatch.setattr(
    pigeond,
    "init_baudrate",
    init_baudrate or (lambda _pigeon: events.append("baud_transition")),
  )
  monkeypatch.setattr(
    pigeond,
    "poll_mon_ver",
    poll_mon_ver or (lambda _pigeon, _timeout: SimpleNamespace()),
  )


def test_process_start_transport_can_use_three_attempts(monkeypatch):
  events = []
  probe_results = iter((None, None, SimpleNamespace()))
  configure_transport_test(
    monkeypatch,
    events,
    poll_mon_ver=lambda _pigeon, _timeout: next(probe_results),
  )

  info = pigeond.bootstrap_process_start_transport(TransportPigeon(events))

  assert info is not None
  assert events.count("baud_transition") == 3
  assert events.count(("power", False)) == 3
  assert events.count(("power", True)) == 3


def test_pr67_runtime_recovery_allows_one_transport_attempt(monkeypatch):
  events = []
  configure_transport_test(
    monkeypatch,
    events,
    poll_mon_ver=lambda _pigeon, _timeout: None,
  )

  with pytest.raises(
    pigeond.ReceiverConfigurationError,
    match="after 1 physical receiver attempt",
  ):
    pigeond.start_pigeon_transport(TransportPigeon(events))

  assert events.count(("power", False)) == 1
  assert events.count(("power", True)) == 1


@pytest.mark.parametrize(
  "error_type",
  (OSError, pigeond.ResponseTransactionError),
)
def test_transport_error_retries_at_process_start(monkeypatch, error_type):
  events = []
  attempts = 0

  def init_baudrate(_pigeon):
    nonlocal attempts
    attempts += 1
    if attempts == 1:
      raise error_type("transport unavailable")

  configure_transport_test(
    monkeypatch,
    events,
    init_baudrate=init_baudrate,
  )
  pigeond.bootstrap_process_start_transport(TransportPigeon(events))

  assert attempts == 2
  assert events.count(("power", False)) == 2
  assert events.count(("power", True)) == 2


def test_raw_publication_error_is_not_retried(monkeypatch):
  events = []

  def publication_error(_pigeon):
    raise pigeond.RawPublicationError("publication unavailable")

  configure_transport_test(
    monkeypatch,
    events,
    init_baudrate=publication_error,
  )
  with pytest.raises(
    pigeond.RawPublicationError,
    match="publication unavailable",
  ):
    pigeond.bootstrap_process_start_transport(TransportPigeon(events))

  assert events.count(("power", False)) == 1
  assert events.count(("power", True)) == 1


def test_programming_exception_is_not_swallowed(monkeypatch):
  events = []

  def programming_error(_pigeon):
    raise ValueError("programming error")

  configure_transport_test(
    monkeypatch,
    events,
    init_baudrate=programming_error,
  )
  with pytest.raises(ValueError, match="programming error"):
    pigeond.bootstrap_process_start_transport(TransportPigeon(events))

  assert events.count(("power", False)) == 1
  assert events.count(("power", True)) == 1


def test_process_start_retry_discards_failed_cycle_frames(monkeypatch):
  events = []
  dispatched: list[tuple[object, object, object, list[bytes]]] = []
  attempts = 0

  class BootstrapPigeon(TransportPigeon):
    def __init__(self, transport_events):
      super().__init__(transport_events)
      self._pending_frames: list[bytes] = []
      self._frame_dispatcher = None

    def reset_response_state(self):
      events.append("reset")
      self._pending_frames.clear()

    def set_frame_dispatcher(self, dispatcher):
      self._frame_dispatcher = dispatcher

    def queue_pending_frames(self, frames, _operation):
      self._pending_frames.extend(frames)

    def dispatch_pending_frames(self):
      if self._pending_frames and self._frame_dispatcher is not None:
        frames = list(self._pending_frames)
        self._frame_dispatcher(frames)
        self._pending_frames.clear()

  pigeon = BootstrapPigeon(events)

  def poll_mon_ver(_pigeon, _timeout):
    nonlocal attempts
    attempts += 1
    pigeond._queue_unrelated_frames(
      pigeon,
      [f"attempt-{attempts}".encode()],
      lambda _frame: False,
    )
    if attempts == 1:
      return None
    return SimpleNamespace()

  configure_transport_test(
    monkeypatch,
    events,
    poll_mon_ver=poll_mon_ver,
  )
  info = pigeond.bootstrap_process_start_transport(pigeon)

  fresh_navigation_runtime = object()
  fresh_position_retry_runtime = object()
  fresh_acquisition_guard = object()
  pigeon.set_frame_dispatcher(
    lambda frames: dispatched.append(
      (
        fresh_navigation_runtime,
        fresh_position_retry_runtime,
        fresh_acquisition_guard,
        frames,
      )
    )
  )
  pigeon.dispatch_pending_frames()

  assert info is not None
  assert dispatched == [
    (
      fresh_navigation_runtime,
      fresh_position_retry_runtime,
      fresh_acquisition_guard,
      [b"attempt-2"],
    )
  ]


def test_verified_mon_ver_is_reused_without_second_poll(monkeypatch):
  info = SimpleNamespace()
  logged = []
  monkeypatch.setattr(
    pigeond,
    "poll_mon_ver",
    lambda *_args: pytest.fail("unexpected second MON-VER poll"),
  )
  monkeypatch.setattr(pigeond, "log_mon_ver_info", logged.append)

  assert (
    pigeond.resolve_pre_acquisition_mon_ver(
      object(),
      info,
      True,
    )
    is info
  )
  assert logged == [info]


def test_process_start_transport_bootstrap_support_detection():
  class CompleteTransport:
    def send(self, _message):
      pass

    def reset_response_state(self):
      pass

    def set_frame_dispatcher(self, _dispatcher):
      pass

    def dispatch_pending_frames(self):
      pass

    def receive_transaction_data(self, _transaction):
      return b"", [], []

  class IncompleteTransport:
    pass

  assert pigeond.supports_process_start_transport_bootstrap(CompleteTransport())
  assert not pigeond.supports_process_start_transport_bootstrap(IncompleteTransport())
