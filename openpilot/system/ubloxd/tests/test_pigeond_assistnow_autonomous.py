from pathlib import Path
import struct
from datetime import UTC, datetime
from typing import cast

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import (
  NAVX5_MASK1_ACK_AIDING,
  NAVX5_MASK1_AOP,
  MonVerInfo,
  NavAopStatus,
  Navx5Config,
  add_ubx_checksum,
  build_navx5_ack_aiding_enable_message,
  build_navx5_aop_enable_message,
  parse_nav_aopstatus,
  parse_nav_sat,
  parse_navx5,
)


HPG_1_40_ROVER_MON_VER = MonVerInfo(
  "EXT CORE 3.01 (db0c89)",
  "00080000",
  ("FWVER=HPG 1.40ROV", "PROTVER=20.30", "GPS;GLO"),
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


def expected_port_config(port_id: int) -> pigeond.PortConfig:
  return {
    0: pigeond.PortConfig(0, 0, 0, 0, 0, 0, 0),
    1: pigeond.PortConfig(1, 0, 0x08C0, 460800, 1, 1, 0),
    3: pigeond.PortConfig(3, 0, 0, 0, 1, 1, 0),
    4: pigeond.PortConfig(4, 0, 0, 0, 0, 0, 0),
  }[port_id]


LEGACY_NAVX5_MESSAGE = bytes.fromhex("b56206232800000000040000000000000000000000000001000000000000000000000000000000000000000000005624")
NAVX5_RESERVED_RANGES = (
  (8, 10),
  (13, 14),
  (15, 17),
  (21, 26),
  (28, 30),
  (32, 39),
)


def ubx_frame(message_class: int, message_id: int, payload: bytes) -> bytes:
  header = b"\xb5\x62" + bytes((message_class, message_id)) + len(payload).to_bytes(2, "little")
  return add_ubx_checksum(header + payload)


def navx5_config(*, ack_aiding: bool = False, use_aop: bool = False, threshold: int = 100, version: int = 2) -> Navx5Config:
  payload = bytearray(range(40))
  payload[0:2] = version.to_bytes(2, "little")
  payload[2:4] = (0x0042).to_bytes(2, "little")
  payload[17] = int(ack_aiding)
  payload[27] = (payload[27] | 0x01) if use_aop else (payload[27] & ~0x01)
  payload[30:32] = threshold.to_bytes(2, "little")
  parsed = parse_navx5(ubx_frame(0x06, 0x23, bytes(payload)))
  assert parsed is not None
  return parsed


def mon_ver(
  software_version: str,
  protocol_version: str = "PROTVER=20.30",
  firmware_version: str = "FWVER=HPG 1.40ROV",
) -> MonVerInfo:
  return MonVerInfo(
    software_version,
    "00080000",
    (firmware_version, protocol_version, "GPS;GLO"),
  )


@pytest.fixture
def autonomous_supported(monkeypatch):
  monkeypatch.setattr(
    pigeond,
    "assistnow_autonomous_compatibility",
    lambda info: (True, "test_supported_identity"),
  )


@pytest.mark.parametrize(
  ("software_version", "supported"),
  [
    ("EXT CORE 3.01", True),
    ("EXT CORE 3.01 (db0c89)", True),
    ("EXT CORE 3.01 (BUILD_2026-07.14)", True),
    ("EXT CORE 3.010", False),
    ("EXT CORE 3.01beta", False),
    ("EXT CORE 3.01 arbitrary text", False),
    ("EXT CORE 3.01 ()", False),
    ("EXT CORE 3.01 (db0c89", False),
    (f"EXT CORE 3.01 ({'a' * 33})", False),
  ],
)
def test_navx5_ack_aiding_software_version_compatibility(software_version, supported):
  actual, _ = pigeond.navx5_ack_aiding_compatibility(mon_ver(software_version))
  assert actual is supported


def test_valid_software_with_wrong_protocol_is_rejected():
  assert pigeond.navx5_ack_aiding_compatibility(mon_ver("EXT CORE 3.01 (db0c89)", protocol_version="PROTVER=20.20")) == (False, "unsupported_protocol_version")


def test_valid_software_with_wrong_firmware_is_rejected():
  assert pigeond.navx5_ack_aiding_compatibility(mon_ver("EXT CORE 3.01 (db0c89)", firmware_version="FWVER=HPG 1.40")) == (False, "unsupported_firmware_version")


def test_exact_hpg_receiver_has_separate_feature_compatibility():
  assert pigeond.navx5_ack_aiding_compatibility(HPG_1_40_ROVER_MON_VER) == (True, "m8_hpg_1_40_protver_20_30")
  assert pigeond.assistnow_autonomous_compatibility(HPG_1_40_ROVER_MON_VER) == (
    False,
    "hpg_1_40_rover_assistnow_autonomous_unsupported",
  )


def test_startup_support_logging_separates_ack_aiding_and_autonomous(monkeypatch):
  info_logs = []
  warning_logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", info_logs.append)
  monkeypatch.setattr(pigeond.cloudlog, "warning", warning_logs.append)

  assert pigeond.log_navx5_ack_aiding_support(HPG_1_40_ROVER_MON_VER)
  assert not pigeond.log_assistnow_autonomous_support(HPG_1_40_ROVER_MON_VER)
  assert pigeond.configure_assistnow_autonomous(object(), HPG_1_40_ROVER_MON_VER) is pigeond.AssistNowAutonomousConfigurationResult.UNSUPPORTED

  assert info_logs == [
    "GPS NAVX5 ACK aiding support, supported=true, reason=m8_hpg_1_40_protver_20_30",
  ]
  assert warning_logs == [
    "GPS AssistNow Autonomous support, supported=false, reason=hpg_1_40_rover_assistnow_autonomous_unsupported",
    "GPS AssistNow Autonomous configuration skipped, reason=hpg_1_40_rover_assistnow_autonomous_unsupported",
  ]
  assert all("verification failed" not in message for message in warning_logs)


@pytest.mark.parametrize(
  "info",
  [
    None,
    MonVerInfo("EXT CORE 3.01", "00080000", ("FWVER=HPG 1.40ROV",)),
    MonVerInfo("EXT CORE 3.01", "00080000", ("FWVER=SPG 3.01", "PROTVER=20.30")),
  ],
)
def test_unknown_receiver_is_not_assumed_autonomous_supported(info):
  assert not pigeond.assistnow_autonomous_compatibility(info)[0]


def test_exact_legacy_navx5_payload_selects_ack_aiding_not_aop():
  assert len(LEGACY_NAVX5_MESSAGE) == 48
  assert LEGACY_NAVX5_MESSAGE[2:6] == b"\x06\x23\x28\x00"
  assert add_ubx_checksum(LEGACY_NAVX5_MESSAGE[:-2]) == LEGACY_NAVX5_MESSAGE
  payload = LEGACY_NAVX5_MESSAGE[6:-2]
  assert len(payload) == 40
  assert int.from_bytes(payload[0:2], "little") == 0
  assert NAVX5_MASK1_ACK_AIDING == 0x0400
  assert NAVX5_MASK1_AOP == 0x4000
  assert int.from_bytes(payload[2:4], "little") == NAVX5_MASK1_ACK_AIDING
  assert not int.from_bytes(payload[2:4], "little") & NAVX5_MASK1_AOP
  assert int.from_bytes(payload[4:8], "little") == 0
  assert payload[17] == 1
  assert payload[27] & 0x01 == 0
  # Protocol 20.30 documents version 2; parsing version 0 lets configuration
  # code report it distinctly without treating it as supported.
  assert parse_navx5(LEGACY_NAVX5_MESSAGE).version == 0


def test_navx5_parse_and_enable_preserve_unrelated_fields():
  current = navx5_config(use_aop=False, threshold=100)
  message = build_navx5_aop_enable_message(current)
  resulting = parse_navx5(message)
  assert resulting is not None
  assert resulting.version == 2
  assert resulting.use_aop
  assert resulting.aop_orbit_max_error_m == 100

  expected = bytearray(current.payload)
  expected[2:4] = NAVX5_MASK1_AOP.to_bytes(2, "little")
  expected[4:8] = b"\x00" * 4
  for start, end in NAVX5_RESERVED_RANGES:
    expected[start:end] = b"\x00" * (end - start)
  expected[27] = 0x01
  assert resulting.payload == bytes(expected)


def test_navx5_already_enabled_remains_byte_identical_except_apply_mask():
  current = navx5_config(use_aop=True, threshold=100)
  resulting = parse_navx5(build_navx5_aop_enable_message(current))
  assert resulting is not None
  expected = bytearray(current.payload)
  expected[2:4] = NAVX5_MASK1_AOP.to_bytes(2, "little")
  expected[4:8] = b"\x00" * 4
  for start, end in NAVX5_RESERVED_RANGES:
    expected[start:end] = b"\x00" * (end - start)
  expected[27] = 0x01
  assert resulting.payload == bytes(expected)


def test_navx5_ack_aiding_enable_uses_exact_mask_and_field():
  current = navx5_config(ack_aiding=False, use_aop=True, threshold=0)
  resulting = parse_navx5(build_navx5_ack_aiding_enable_message(current))
  assert resulting is not None
  assert resulting.payload[2:4] == b"\x00\x04"
  assert int.from_bytes(resulting.payload[2:4], "little") == NAVX5_MASK1_ACK_AIDING
  assert resulting.payload[17] == 1
  assert resulting.ack_aiding
  assert resulting.use_aop
  assert resulting.aop_orbit_max_error_m == 0


def test_navx5_aop_enable_uses_exact_mask_and_preserves_default_threshold():
  current = navx5_config(ack_aiding=True, use_aop=False, threshold=0)
  resulting = parse_navx5(build_navx5_aop_enable_message(current))
  assert resulting is not None
  assert resulting.payload[2:4] == b"\x00\x40"
  assert int.from_bytes(resulting.payload[2:4], "little") == NAVX5_MASK1_AOP
  assert resulting.ack_aiding
  assert resulting.use_aop
  assert resulting.aop_orbit_max_error_m == 0


def test_ack_aiding_configuration_acknowledged_and_verified(monkeypatch):
  configs = iter((navx5_config(), navx5_config(ack_aiding=True)))
  sent = []
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: next(configs))
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", lambda *args: True)
  pigeon = type("Pigeon", (), {"send": lambda self, message: sent.append(message)})()
  result = pigeond.configure_navx5_ack_aiding(pigeon, HPG_1_40_ROVER_MON_VER)
  assert result is pigeond.Navx5AckAidingConfigurationResult.ENABLED_AND_VERIFIED
  assert parse_navx5(sent[0]).payload[2:4] == b"\x00\x04"


def test_ack_aiding_already_enabled_does_not_write(monkeypatch):
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: navx5_config(ack_aiding=True))
  pigeon = type("Pigeon", (), {"send": lambda self, message: pytest.fail("must not write NAVX5")})()
  assert pigeond.configure_navx5_ack_aiding(pigeon, HPG_1_40_ROVER_MON_VER) is (pigeond.Navx5AckAidingConfigurationResult.ALREADY_ENABLED)


@pytest.mark.parametrize(
  ("ack", "readback", "expected"),
  [
    (False, None, pigeond.Navx5AckAidingConfigurationResult.WRITE_REJECTED),
    (None, None, pigeond.Navx5AckAidingConfigurationResult.WRITE_TIMED_OUT),
    (True, None, pigeond.Navx5AckAidingConfigurationResult.READBACK_UNAVAILABLE),
    (True, navx5_config(ack_aiding=False), pigeond.Navx5AckAidingConfigurationResult.READBACK_ACK_AIDING_FALSE),
  ],
)
def test_ack_aiding_failure_results_are_distinct(monkeypatch, ack, readback, expected):
  configs = iter((navx5_config(), readback))
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: next(configs))
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", lambda *args: ack)
  pigeon = type("Pigeon", (), {"send": lambda self, message: None})()
  assert pigeond.configure_navx5_ack_aiding(pigeon, HPG_1_40_ROVER_MON_VER) is expected


def test_ack_aiding_deadline_is_rechecked_after_transaction_setup(monkeypatch):
  clock = Clock()
  clock.value = 44.0
  writes: list[bytes] = []
  monkeypatch.setattr(pigeond.time, "monotonic", clock)
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda _pigeon, timeout: navx5_config())

  def begin(
    _pigeon,
    message,
    operation=None,
    before_send=None,
    deadline=None,
  ):
    assert deadline == 45.0
    clock.value = 45.0
    assert before_send is not None
    before_send()
    writes.append(message)
    return pigeond.ResponseTransaction(pigeond.UbxStreamParser())

  monkeypatch.setattr(pigeond, "_begin_response_transaction", begin)

  result = pigeond.configure_navx5_ack_aiding(
    cast(pigeond.TTYPigeon, object()),
    HPG_1_40_ROVER_MON_VER,
    pre_start_deadline=45.0,
  )

  assert result is pigeond.Navx5AckAidingConfigurationResult.DEADLINE_EXHAUSTED
  assert writes == []


def test_navx5_poll_deadline_is_rechecked_at_poll_uart_boundary(monkeypatch):
  clock = Clock()
  clock.value = 44.0

  class Pigeon:
    def __init__(self):
      self.sent: list[bytes] = []

    def begin_response_transaction(self, message, operation, before_send):
      clock.value = 45.0
      before_send()
      self.sent.append(message)
      return pigeond.ResponseTransaction(pigeond.UbxStreamParser())

  monkeypatch.setattr(pigeond.time, "monotonic", clock)
  pigeon = Pigeon()

  with pytest.raises(TimeoutError, match="before UART write"):
    pigeond.poll_navx5_config(
      pigeon,  # type: ignore[arg-type, ty:invalid-argument-type]
      pre_start_deadline=45.0,
    )

  assert pigeon.sent == []


def test_ack_aiding_ack_wait_is_clipped_to_absolute_deadline(monkeypatch):
  clock = Clock()
  clock.value = 44.0
  ack_timeouts: list[float] = []
  monkeypatch.setattr(pigeond.time, "monotonic", clock)
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda _pigeon, timeout: navx5_config())

  def begin(
    _pigeon,
    _message,
    operation=None,
    before_send=None,
    deadline=None,
  ):
    assert deadline == 45.0
    clock.value = 44.8
    assert before_send is not None
    before_send()
    return pigeond.ResponseTransaction(pigeond.UbxStreamParser())

  def wait(
    _pigeon,
    _transaction,
    _message_class,
    _message_id,
    timeout,
    deadline=None,
  ):
    assert deadline == 45.0
    ack_timeouts.append(timeout)
    clock.value += timeout
    return None

  monkeypatch.setattr(pigeond, "_begin_response_transaction", begin)
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", wait)

  result = pigeond.configure_navx5_ack_aiding(
    cast(pigeond.TTYPigeon, object()),
    HPG_1_40_ROVER_MON_VER,
    pre_start_deadline=45.0,
  )

  assert result is pigeond.Navx5AckAidingConfigurationResult.DEADLINE_EXHAUSTED
  assert ack_timeouts == [pytest.approx(0.2)]
  assert clock.value == pytest.approx(45.0)


def test_ack_aiding_initial_and_verification_polls_share_deadline(monkeypatch):
  clock = Clock()
  clock.value = 44.0
  poll_timeouts: list[float] = []
  configs = iter((navx5_config(), navx5_config(ack_aiding=True)))
  monkeypatch.setattr(pigeond.time, "monotonic", clock)

  def poll(_pigeon, timeout):
    poll_timeouts.append(timeout)
    return next(configs)

  def begin(
    _pigeon,
    _message,
    operation=None,
    before_send=None,
    deadline=None,
  ):
    assert deadline == 45.0
    clock.value = 44.7
    assert before_send is not None
    before_send()
    return pigeond.ResponseTransaction(pigeond.UbxStreamParser())

  monkeypatch.setattr(pigeond, "poll_navx5_config", poll)
  monkeypatch.setattr(pigeond, "_begin_response_transaction", begin)
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", lambda *_args, **_kwargs: True)

  result = pigeond.configure_navx5_ack_aiding(
    cast(pigeond.TTYPigeon, object()),
    HPG_1_40_ROVER_MON_VER,
    pre_start_deadline=45.0,
  )

  assert result is pigeond.Navx5AckAidingConfigurationResult.ENABLED_AND_VERIFIED
  assert poll_timeouts == [pytest.approx(0.5), pytest.approx(0.3)]


def test_ack_aiding_navx5_poll_unavailable_is_distinct(monkeypatch):
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: None)
  assert pigeond.configure_navx5_ack_aiding(object(), HPG_1_40_ROVER_MON_VER) is pigeond.Navx5AckAidingConfigurationResult.POLL_UNAVAILABLE


def test_aop_navx5_poll_unavailable_is_distinct(monkeypatch, autonomous_supported):
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: None)
  assert pigeond.configure_assistnow_autonomous(object(), HPG_1_40_ROVER_MON_VER) is pigeond.AssistNowAutonomousConfigurationResult.POLL_UNAVAILABLE


def test_ack_aiding_unsupported_navx5_version_is_distinct(monkeypatch):
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: navx5_config(version=0))
  assert pigeond.configure_navx5_ack_aiding(object(), HPG_1_40_ROVER_MON_VER) is pigeond.Navx5AckAidingConfigurationResult.UNSUPPORTED_NAVX5_VERSION


def test_aop_unsupported_navx5_version_is_distinct(monkeypatch, autonomous_supported):
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: navx5_config(version=0))
  assert pigeond.configure_assistnow_autonomous(object(), HPG_1_40_ROVER_MON_VER) is pigeond.AssistNowAutonomousConfigurationResult.UNSUPPORTED_NAVX5_VERSION


@pytest.mark.parametrize(
  "info",
  [
    None,
    MonVerInfo("EXT CORE 3.01", "00080000", ("FWVER=HPG 1.40ROV",)),
    MonVerInfo("EXT CORE 3.01", "00080000", ("FWVER=SPG 3.01", "PROTVER=20.30")),
  ],
)
def test_unsupported_or_unknown_receiver_skips(monkeypatch, info):
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: pytest.fail("must not poll NAVX5"))
  result = pigeond.configure_assistnow_autonomous(object(), info)
  assert result is pigeond.AssistNowAutonomousConfigurationResult.UNSUPPORTED


@pytest.mark.parametrize(
  "info",
  [
    None,
    MonVerInfo("EXT CORE 3.01", "00080000", ("FWVER=HPG 1.40ROV",)),
    MonVerInfo("EXT CORE 3.01", "00080000", ("FWVER=SPG 3.01", "PROTVER=20.30")),
  ],
)
def test_unsupported_or_unknown_receiver_skips_ack_aiding(monkeypatch, info):
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: pytest.fail("must not poll NAVX5"))
  assert pigeond.configure_navx5_ack_aiding(object(), info) is (pigeond.Navx5AckAidingConfigurationResult.UNSUPPORTED)


def test_hpg_autonomous_skip_performs_no_navx5_poll_write_or_readback(monkeypatch):
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: pytest.fail("must not poll NAVX5"))
  pigeon = type("Pigeon", (), {"send": lambda self, message: pytest.fail("must not write NAVX5")})()
  assert pigeond.configure_assistnow_autonomous(pigeon, HPG_1_40_ROVER_MON_VER) is pigeond.AssistNowAutonomousConfigurationResult.UNSUPPORTED


def test_hpg_autonomous_compatibility_decision_does_not_modify_cache(monkeypatch, tmp_path):
  cache_path = tmp_path / "navigation_cache.json"
  original = b'{"preserve": true}\n'
  cache_path.write_bytes(original)
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", cache_path)
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: pytest.fail("must not poll NAVX5"))

  assert pigeond.configure_assistnow_autonomous(object(), HPG_1_40_ROVER_MON_VER) is pigeond.AssistNowAutonomousConfigurationResult.UNSUPPORTED
  assert cache_path.read_bytes() == original


@pytest.mark.parametrize(
  ("ack", "expected"),
  [
    (False, pigeond.AssistNowAutonomousConfigurationResult.WRITE_REJECTED),
    (None, pigeond.AssistNowAutonomousConfigurationResult.WRITE_TIMED_OUT),
  ],
)
def test_aop_configuration_rejected_or_timed_out(monkeypatch, autonomous_supported, ack, expected):
  current = navx5_config()
  sent = []
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: current)
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", lambda *args: ack)
  pigeon = type("Pigeon", (), {"send": lambda self, message: sent.append(message)})()
  assert pigeond.configure_assistnow_autonomous(pigeon, HPG_1_40_ROVER_MON_VER) is expected
  assert len(sent) == 1


def test_aop_configuration_acknowledged_and_verified(monkeypatch, autonomous_supported):
  current = navx5_config(use_aop=False)
  enabled = navx5_config(use_aop=True)
  configs = iter((current, enabled))
  sent = []
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: next(configs))
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", lambda *args: True)
  pigeon = type("Pigeon", (), {"send": lambda self, message: sent.append(message)})()
  result = pigeond.configure_assistnow_autonomous(pigeon, HPG_1_40_ROVER_MON_VER)
  assert result is pigeond.AssistNowAutonomousConfigurationResult.ENABLED_AND_VERIFIED
  assert parse_navx5(sent[0]).use_aop


@pytest.mark.parametrize(
  ("readback", "expected"),
  [
    (None, pigeond.AssistNowAutonomousConfigurationResult.READBACK_UNAVAILABLE),
    (navx5_config(use_aop=False), pigeond.AssistNowAutonomousConfigurationResult.READBACK_USE_AOP_FALSE),
  ],
)
def test_aop_readback_failure_results_are_distinct(monkeypatch, autonomous_supported, readback, expected):
  configs = iter((navx5_config(use_aop=False), readback))
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: next(configs))
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", lambda *args: True)
  pigeon = type("Pigeon", (), {"send": lambda self, message: None})()
  assert pigeond.configure_assistnow_autonomous(pigeon, HPG_1_40_ROVER_MON_VER) is expected


def test_aop_configuration_rejects_unrelated_verification_change(monkeypatch, autonomous_supported):
  current = navx5_config(use_aop=False)
  payload = bytearray(navx5_config(use_aop=True).payload)
  payload[20] ^= 0x01
  changed = parse_navx5(ubx_frame(0x06, 0x23, bytes(payload)))
  assert changed is not None
  configs = iter((current, changed))
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: next(configs))
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", lambda *args: True)
  pigeon = type("Pigeon", (), {"send": lambda self, message: None})()
  result = pigeond.configure_assistnow_autonomous(pigeon, HPG_1_40_ROVER_MON_VER)
  assert result is (pigeond.AssistNowAutonomousConfigurationResult.READBACK_UNRELATED_FIELDS_CHANGED)


def test_aop_configuration_reports_changed_threshold_distinctly(monkeypatch, autonomous_supported):
  current = navx5_config(use_aop=False, threshold=0)
  changed = navx5_config(use_aop=True, threshold=100)
  configs = iter((current, changed))
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: next(configs))
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", lambda *args: True)
  pigeon = type("Pigeon", (), {"send": lambda self, message: None})()
  assert pigeond.configure_assistnow_autonomous(pigeon, HPG_1_40_ROVER_MON_VER) is (
    pigeond.AssistNowAutonomousConfigurationResult.READBACK_ORBIT_ERROR_THRESHOLD_CHANGED
  )


def test_aop_configuration_accepts_receiver_normalized_reserved_bytes(monkeypatch, autonomous_supported):
  current = navx5_config(use_aop=False)
  payload = bytearray(navx5_config(use_aop=True).payload)
  payload[2:8] = b"\x00" * 6
  payload[27] = 0x01
  for start, end in NAVX5_RESERVED_RANGES:
    payload[start:end] = b"\x00" * (end - start)
  normalized = parse_navx5(ubx_frame(0x06, 0x23, bytes(payload)))
  assert normalized is not None
  configs = iter((current, normalized))
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: next(configs))
  monkeypatch.setattr(pigeond, "wait_for_cfg_ack", lambda *args: True)
  pigeon = type("Pigeon", (), {"send": lambda self, message: None})()
  result = pigeond.configure_assistnow_autonomous(pigeon, HPG_1_40_ROVER_MON_VER)
  assert result is pigeond.AssistNowAutonomousConfigurationResult.ENABLED_AND_VERIFIED


def test_aop_configuration_already_enabled_does_not_write(monkeypatch, autonomous_supported):
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: navx5_config(use_aop=True))
  pigeon = type("Pigeon", (), {"send": lambda self, message: pytest.fail("must not write NAVX5")})()
  result = pigeond.configure_assistnow_autonomous(pigeon, HPG_1_40_ROVER_MON_VER)
  assert result is pigeond.AssistNowAutonomousConfigurationResult.ALREADY_ENABLED


@pytest.mark.parametrize(("ack_id", "expected"), [(0x01, True), (0x00, False)])
def test_matching_navx5_ack_accepted_and_rejected(ack_id, expected):
  response = ubx_frame(0x05, ack_id, b"\x06\x23")
  pigeon = type("Pigeon", (), {"receive": lambda self: response})()
  transaction = pigeond.ResponseTransaction(pigeond.UbxStreamParser())
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x23) is expected


def test_navx5_ack_timeout():
  pigeon = type("Pigeon", (), {"receive": lambda self: b""})()
  transaction = pigeond.ResponseTransaction(pigeond.UbxStreamParser())
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x23, timeout=-1) is None


def test_navx5_ack_ignores_unrelated_ack_before_matching_response():
  response = ubx_frame(0x05, 0x01, b"\x06\x24") + ubx_frame(0x05, 0x01, b"\x06\x23")
  pigeon = type("Pigeon", (), {"receive": lambda self: response})()
  transaction = pigeond.ResponseTransaction(pigeond.UbxStreamParser())
  assert pigeond.wait_for_cfg_ack(pigeon, transaction, 0x06, 0x23)


@pytest.mark.parametrize(("status", "idle"), [(0, True), (1, False), (7, False)])
def test_nav_aopstatus_idle_and_busy(status, idle):
  payload = bytearray(16)
  payload[4] = 1
  payload[5] = status
  parsed = parse_nav_aopstatus(ubx_frame(0x01, 0x60, bytes(payload)))
  assert parsed == NavAopStatus(enabled=True, status=status)
  assert parsed.idle is idle


class Clock:
  def __init__(self):
    self.value = 0.0

  def __call__(self):
    return self.value

  def sleep(self, delay):
    self.value += delay


def test_aop_idle_wait_returns_idle(monkeypatch):
  monkeypatch.setattr(pigeond, "poll_nav_aopstatus", lambda *args, **kwargs: NavAopStatus(True, 0))
  assert pigeond.wait_for_aop_idle(object()) is pigeond.AopCaptureState.IDLE


def test_aop_idle_wait_unavailable(monkeypatch):
  monkeypatch.setattr(pigeond, "poll_nav_aopstatus", lambda *args, **kwargs: None)
  assert pigeond.wait_for_aop_idle(object()) is pigeond.AopCaptureState.UNKNOWN


def test_aop_idle_wait_poll_error_fails_open(monkeypatch):
  def fail(*args, **kwargs):
    raise OSError("serial failure")

  monkeypatch.setattr(pigeond, "poll_nav_aopstatus", fail)
  assert pigeond.wait_for_aop_idle(object()) is pigeond.AopCaptureState.UNKNOWN


def test_aop_busy_wait_is_bounded(monkeypatch):
  clock = Clock()
  polls = []

  def busy(*args, **kwargs):
    polls.append(clock.value)
    return NavAopStatus(True, 1)

  monkeypatch.setattr(pigeond, "poll_nav_aopstatus", busy)
  monkeypatch.setattr(pigeond.time, "monotonic", clock)
  monkeypatch.setattr(pigeond.time, "sleep", clock.sleep)
  result = pigeond.wait_for_aop_idle(object(), timeout=0.12, poll_interval=0.05)
  assert result is pigeond.AopCaptureState.BUSY
  assert clock.value == pytest.approx(0.12)
  assert len(polls) == 3


def test_cache_capture_continues_when_aop_status_unavailable(monkeypatch):
  events = []
  monkeypatch.setattr(pigeond, "wait_for_aop_idle", lambda pigeon: pigeond.AopCaptureState.UNKNOWN)
  collector = type("Collector", (), {"start": lambda self, now: events.append(("start", now))})()
  pigeon = type("Pigeon", (), {"send": lambda self, message: events.append(("send", message))})()
  state = pigeond.NavigationCaptureState(capture_reason="onroad")
  result = pigeond.request_navigation_database_capture(
    pigeon,
    collector,
    state,
    12.0,
    assistnow_autonomous_supported=True,
  )
  assert result is pigeond.AopCaptureState.UNKNOWN
  assert events == [("start", 12.0), ("send", pigeond.build_database_poll_message())]


def test_unsupported_autonomous_status_is_not_polled_and_capture_continues(monkeypatch):
  events = []
  warnings = []
  monkeypatch.setattr(pigeond, "wait_for_aop_idle", lambda pigeon: pytest.fail("must not poll AOP status"))
  monkeypatch.setattr(pigeond.cloudlog, "warning", warnings.append)
  collector = type("Collector", (), {"start": lambda self, now: events.append(("start", now))})()
  pigeon = type("Pigeon", (), {"send": lambda self, message: events.append(("send", message))})()
  state = pigeond.NavigationCaptureState(capture_reason="onroad")

  result = pigeond.request_navigation_database_capture(
    pigeon,
    collector,
    state,
    12.0,
    assistnow_autonomous_supported=False,
  )

  assert result is pigeond.AopCaptureState.UNSUPPORTED
  assert events == [("start", 12.0), ("send", pigeond.build_database_poll_message())]
  assert warnings == []


def test_nav_sat_reports_autonomous_available_and_used():
  satellite = bytearray(12)
  satellite[0] = 0
  flags = (1 << 14) | (4 << 8)
  struct.pack_into("<I", satellite, 8, flags)
  payload = struct.pack("<IBB2x", 0, 1, 1) + satellite
  parsed = parse_nav_sat(ubx_frame(0x01, 0x35, payload))
  assert parsed is not None
  assert parsed.assistnow_autonomous_available == 1
  assert parsed.orbit_source_counts["assistnow_autonomous"] == 1


def test_startup_orders_ack_aiding_before_time_and_autonomous_skip_after_restore(monkeypatch):
  events = []
  ack_deadlines: list[float] = []

  class Diagnostics:
    def start_cycle(self, reason, now):
      events.append("cycle")

    def time_assistance_context(self, now):
      return "context"

  monkeypatch.setattr(pigeond, "init", lambda pigeon: events.append("init"))
  monkeypatch.setattr(pigeond, "log_mon_ver_diagnostics", lambda pigeon: events.append("mon_ver") or HPG_1_40_ROVER_MON_VER)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda info: events.append("ack_support") or True)
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda info: events.append("aop_support") or False)

  def configure_ack_aiding(*_args, **kwargs):
    events.append("ack_aiding")
    ack_deadlines.append(kwargs["pre_start_deadline"])
    return pigeond.Navx5AckAidingConfigurationResult.WRITE_TIMED_OUT

  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    configure_ack_aiding,
  )
  monkeypatch.setattr(pigeond, "read_host_time_observation", network_host_observation)
  monkeypatch.setattr(pigeond, "send_time_assistance", lambda *args, **kwargs: events.append("time") or True)
  monkeypatch.setattr(pigeond, "restore_navigation_assistance", lambda *args, **kwargs: events.append("restore"))
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda *args: events.append("configure"))
  result = pigeond.initialize_receiver_cycle(
    object(),
    "receiver",
    Diagnostics(),
    "process_start",
    collect_mon_ver_diagnostics=True,
  )
  assert events == [
    "cycle",
    "init",
    "mon_ver",
    "ack_support",
    "ack_aiding",
    "restore",
    "time",
    "aop_support",
    "configure",
  ]
  assert result.ack_aiding_configuration_attempted
  assert result.ack_aiding_configuration_result is pigeond.Navx5AckAidingConfigurationResult.WRITE_TIMED_OUT
  assert len(ack_deadlines) == 1
  assert ack_deadlines[0] > 0.0
  assert result.navigation_assistance_restore_attempted
  assert not result.assistnow_autonomous_supported
  assert result.assistnow_autonomous_configuration_attempted


def test_autonomous_configuration_never_requires_online_token(monkeypatch, autonomous_supported):
  current = navx5_config(use_aop=True)
  monkeypatch.setattr(pigeond, "Params", lambda: pytest.fail("Autonomous must not read AssistNowToken"))
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: current)
  result = pigeond.configure_assistnow_autonomous(object(), HPG_1_40_ROVER_MON_VER)
  assert result is pigeond.AssistNowAutonomousConfigurationResult.ALREADY_ENABLED


def test_legacy_assistnow_online_path_is_retired_after_strict_configuration(monkeypatch):
  class Params:
    def get(self, key):
      raise AssertionError(f"AssistNow Online retired; unexpected Params.get({key!r})")

  class Pigeon:
    def __init__(self):
      self.ack_writes = []
      self.backup_polls = 0

    def send(self, message):
      pass

    def send_with_ack(self, message, ack=pigeond.UBLOX_ACK, nack=pigeond.UBLOX_NACK):
      self.ack_writes.append((message, ack, nack))

    def poll_backup_restore_status(self):
      self.backup_polls += 1
      return 3

  monkeypatch.setattr(pigeond, "Params", Params)
  assert not hasattr(pigeond, "get_assistnow_messages")
  monkeypatch.setattr(pigeond, "poll_cfg_prt", lambda _pigeon, port_id: expected_port_config(port_id))
  monkeypatch.setattr(pigeond, "poll_cfg_rate", lambda *args: pigeond.RateConfig(100, 1, 0))
  monkeypatch.setattr(pigeond, "poll_cfg_nav5", lambda *args: pigeond.Nav5Config(4, 3))
  monkeypatch.setattr(pigeond, "poll_cfg_odo", lambda *args: pigeond.OdoConfig(0, 1, 3))
  monkeypatch.setattr(pigeond, "poll_cfg_itfm", lambda *args: pigeond.ItfmConfig(0xAD62ADFF, 0x0000631E))
  monkeypatch.setattr(
    pigeond,
    "poll_cfg_msg",
    lambda _pigeon, message_class, message_id: pigeond.MessageRateConfig(
      message_class,
      message_id,
      (0, 1, 0, 0, 0, 0),
    ),
  )
  pigeon = Pigeon()
  assert pigeond.init_pigeon(pigeon)
  assert pigeon.ack_writes == []

  pigeond.run_post_start_legacy_assistance(cast(pigeond.TTYPigeon, pigeon))

  assert pigeon.backup_polls == 1
  assert pigeon.ack_writes == []
  source = Path(pigeond.__file__).read_text()
  assert "AssistNowToken" not in source
  assert "online-live2.services.u-blox.com" not in source


def test_receiver_recovery_cycle_does_not_retry_aop_transaction(monkeypatch):
  events = []

  class Diagnostics:
    def start_cycle(self, reason, now):
      pass

    def time_assistance_context(self, now):
      return "context"

  monkeypatch.setattr(pigeond, "init", lambda pigeon: events.append("init"))
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda pigeon: HPG_1_40_ROVER_MON_VER)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda info: True)
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: pytest.fail("AOP must not poll NAVX5"))
  monkeypatch.setattr(pigeond, "configure_navx5_ack_aiding", lambda *args, **kwargs: events.append("ack"))
  monkeypatch.setattr(pigeond, "read_host_time_observation", network_host_observation)
  monkeypatch.setattr(pigeond, "send_time_assistance", lambda *args, **kwargs: events.append("time") or True)
  monkeypatch.setattr(pigeond, "restore_navigation_assistance", lambda *args, **kwargs: events.append("restore"))
  for reason in ("process_start", "no_data_watchdog"):
    result = pigeond.initialize_receiver_cycle(object(), "receiver", Diagnostics(), reason)
    assert not result.assistnow_autonomous_supported
    assert result.assistnow_autonomous_configuration_attempted
  assert events == [
    "init",
    "ack",
    "restore",
    "time",
    "init",
    "ack",
    "restore",
    "time",
  ]


def test_navx5_poll_failure_does_not_block_normal_startup(monkeypatch):
  events = []

  class Diagnostics:
    def start_cycle(self, reason, now):
      events.append("cycle")

    def time_assistance_context(self, now):
      return "context"

  monkeypatch.setattr(pigeond, "init", lambda pigeon: events.append("init"))
  monkeypatch.setattr(pigeond, "log_mon_ver_diagnostics", lambda pigeon: HPG_1_40_ROVER_MON_VER)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda info: True)
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda info: False)
  monkeypatch.setattr(pigeond, "configure_navx5_ack_aiding", lambda *args, **kwargs: events.append("ack_aiding"))
  monkeypatch.setattr(pigeond, "read_host_time_observation", network_host_observation)
  monkeypatch.setattr(pigeond, "send_time_assistance", lambda *args, **kwargs: events.append("time") or True)
  monkeypatch.setattr(pigeond, "restore_navigation_assistance", lambda *args, **kwargs: events.append("restore"))
  monkeypatch.setattr(pigeond, "poll_navx5_config", lambda pigeon: None)
  result = pigeond.initialize_receiver_cycle(
    object(),
    "receiver",
    Diagnostics(),
    "process_start",
    collect_mon_ver_diagnostics=True,
  )
  assert events == ["cycle", "init", "ack_aiding", "restore", "time"]
  assert result.navigation_assistance_restore_attempted
  assert result.assistnow_autonomous_configuration_attempted
