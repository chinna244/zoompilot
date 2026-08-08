from datetime import UTC, datetime

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import (
  GnssConfig,
  GnssConfigBlock,
  MonVerInfo,
  Pm2Config,
  RxmConfig,
  add_ubx_checksum,
  build_cfg_gnss_poll_message,
  build_cfg_pm2_poll_message,
  build_cfg_rxm_poll_message,
  normalized_receiver_identity,
  parse_cfg_gnss,
  parse_cfg_pm2,
  parse_cfg_rxm,
  parse_mon_ver,
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


def build_mon_ver(
  software="EXT CORE 3.01",
  hardware="00080000",
  extensions=("PROTVER=18.00", "FWVER=SPG 3.01", "GPS", "GLO"),
):
  def field(value, length):
    return value.encode("ascii").ljust(length, b"\0")

  payload = field(software, 30) + field(hardware, 10)
  payload += b"".join(field(value, 30) for value in extensions)
  return add_ubx_checksum(b"\xb5\x62\x0a\x04" + len(payload).to_bytes(2, "little") + payload)


def build_ubx_frame(message_class, message_id, payload):
  return add_ubx_checksum(b"\xb5\x62" + bytes((message_class, message_id)) + len(payload).to_bytes(2, "little") + payload)


def build_cfg_gnss(blocks, *, version=0, block_count=None):
  count = len(blocks) if block_count is None else block_count
  payload = bytearray((version, 32, 28, count))
  for gnss_id, reserved_channels, maximum_channels, flags in blocks:
    payload.extend((gnss_id, reserved_channels, maximum_channels, 0))
    payload.extend(flags.to_bytes(4, "little"))
  return build_ubx_frame(0x06, 0x3E, bytes(payload))


def build_cfg_rxm(low_power_mode):
  return build_ubx_frame(0x06, 0x11, bytes((0, low_power_mode)))


def build_cfg_pm2(*, version=2, length=None):
  payload_length = {1: 44, 2: 48}.get(version, 44) if length is None else length
  payload = bytearray(payload_length)
  if payload:
    payload[0] = version
  if payload_length >= 24:
    payload[2] = 45
    flags = (1 << 4) | (1 << 5) | (1 << 7) | (1 << 11) | (1 << 12) | (1 << 17)
    payload[4:8] = flags.to_bytes(4, "little")
    payload[8:12] = (10_000).to_bytes(4, "little")
    payload[12:16] = (20_000).to_bytes(4, "little")
    payload[16:20] = (3_000).to_bytes(4, "little")
    payload[20:22] = (8).to_bytes(2, "little")
    payload[22:24] = (5).to_bytes(2, "little")
  if version == 2 and payload_length >= 48:
    payload[44:48] = (60_000).to_bytes(4, "little")
  return build_ubx_frame(0x06, 0x3B, bytes(payload))


class ResponsePigeon:
  def __init__(self, responses):
    self.responses = iter(responses)
    self.sent = []

  def send(self, message):
    self.sent.append(message)

  def receive(self):
    return next(self.responses, b"")


def test_parse_cfg_gnss_multiple_enabled_and_disabled_blocks():
  frame = build_cfg_gnss(
    (
      (0, 8, 16, 0x00010001),
      (2, 4, 8, 0x00030000),
      (6, 6, 14, 0x00020001),
    )
  )

  assert parse_cfg_gnss(frame) == GnssConfig(
    version=0,
    hardware_tracking_channels=32,
    configured_tracking_channels=28,
    blocks=(
      GnssConfigBlock(0, 8, 16, True, 0x01, 0x00010001),
      GnssConfigBlock(2, 4, 8, False, 0x03, 0x00030000),
      GnssConfigBlock(6, 6, 14, True, 0x02, 0x00020001),
    ),
  )


@pytest.mark.parametrize(
  "frame",
  [
    build_cfg_gnss(((0, 8, 16, 1),), block_count=2),
    build_ubx_frame(0x06, 0x3E, b"\0\x20\x1c\x01\0\x08\x10"),
    build_cfg_gnss(((0, 8, 16, 1),), version=1),
  ],
)
def test_parse_cfg_gnss_rejects_bad_block_count_truncation_and_version(frame):
  assert parse_cfg_gnss(frame) is None


@pytest.mark.parametrize(
  "frame",
  [
    build_ubx_frame(0x05, 0x3E, b"\0\x20\x1c\0"),
    build_ubx_frame(0x06, 0x3F, b"\0\x20\x1c\0"),
  ],
)
def test_parse_cfg_gnss_rejects_wrong_class_or_message_id(frame):
  assert parse_cfg_gnss(frame) is None


def test_parse_cfg_gnss_rejects_bad_checksum():
  frame = bytearray(build_cfg_gnss(((0, 8, 16, 1),)))
  frame[-1] ^= 0xFF
  assert parse_cfg_gnss(bytes(frame)) is None


@pytest.mark.parametrize(
  ("mode", "expected"),
  [
    (0, RxmConfig(0)),
    (4, RxmConfig(4)),
    (1, RxmConfig(1)),
    (0x7F, RxmConfig(0x7F)),
  ],
)
def test_parse_cfg_rxm_continuous_power_save_and_unknown_modes(mode, expected):
  assert parse_cfg_rxm(build_cfg_rxm(mode)) == expected


@pytest.mark.parametrize(
  "frame",
  [
    build_ubx_frame(0x05, 0x11, b"\0\0"),
    build_ubx_frame(0x06, 0x12, b"\0\0"),
    build_ubx_frame(0x06, 0x11, b"\0"),
    build_ubx_frame(0x06, 0x11, b"\0\0\0"),
  ],
)
def test_parse_cfg_rxm_rejects_wrong_class_id_and_length(frame):
  assert parse_cfg_rxm(frame) is None


def test_parse_cfg_rxm_rejects_bad_checksum():
  frame = bytearray(build_cfg_rxm(0))
  frame[-1] ^= 0xFF
  assert parse_cfg_rxm(bytes(frame)) is None


@pytest.mark.parametrize(
  ("mode", "interpretation"),
  [
    (0, "continuous"),
    (4, "continuous"),
    (1, "power_save"),
    (0x7F, "unknown"),
  ],
)
def test_cfg_rxm_logging_interpretation(monkeypatch, mode, interpretation):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", lambda message: logs.append(message))

  pigeond._log_cfg_rxm(RxmConfig(mode))

  assert logs == [
    ", ".join(
      (
        "GPS acquisition configuration CFG-RXM",
        f"low_power_mode={mode}",
        f"low_power_mode_interpretation={interpretation}",
      )
    )
  ]


def test_parse_cfg_pm2_version_two():
  assert parse_cfg_pm2(build_cfg_pm2()) == Pm2Config(
    version=2,
    maximum_startup_state_duration_s=45,
    flags=0x000218B0,
    update_period_ms=10_000,
    search_period_ms=20_000,
    grid_offset_ms=3_000,
    on_time_s=8,
    minimum_acquisition_time_s=5,
    external_interrupt_inactivity_ms=60_000,
  )


def test_parse_cfg_pm2_version_one_has_no_external_interrupt_inactivity():
  config = parse_cfg_pm2(build_cfg_pm2(version=1))
  assert config is not None
  assert config.version == 1
  assert config.external_interrupt_inactivity_ms is None


@pytest.mark.parametrize(
  "frame",
  [
    build_cfg_pm2(version=2, length=44),
    build_cfg_pm2(version=1, length=48),
    build_cfg_pm2(version=0, length=44),
    build_cfg_pm2(version=2, length=23),
  ],
)
def test_parse_cfg_pm2_rejects_malformed_version_and_length(frame):
  assert parse_cfg_pm2(frame) is None


@pytest.mark.parametrize(
  "frame",
  [
    build_ubx_frame(0x05, 0x3B, build_cfg_pm2()[6:-2]),
    build_ubx_frame(0x06, 0x3C, build_cfg_pm2()[6:-2]),
  ],
)
def test_parse_cfg_pm2_rejects_wrong_class_or_message_id(frame):
  assert parse_cfg_pm2(frame) is None


def test_parse_cfg_pm2_rejects_bad_checksum():
  frame = bytearray(build_cfg_pm2())
  frame[-1] ^= 0xFF
  assert parse_cfg_pm2(bytes(frame)) is None


def test_acquisition_poll_handles_fragmented_response_after_unrelated_frame():
  requested = build_cfg_gnss(((0, 8, 16, 0x00010001),))
  unrelated = build_mon_ver()
  pigeon = ResponsePigeon(
    (
      unrelated + requested[:5],
      requested[5:11],
      requested[11:],
    )
  )

  config = pigeond.poll_cfg_gnss(pigeon, timeout=0.05)
  assert config is not None
  assert config.blocks[0].gnss_id == 0
  assert pigeon.sent == [build_cfg_gnss_poll_message()]


def test_acquisition_poll_timeout_sends_only_poll_message():
  pigeon = ResponsePigeon(())
  assert pigeond.poll_cfg_pm2(pigeon, timeout=0) is None
  assert pigeon.sent == [build_cfg_pm2_poll_message()]


def test_acquisition_diagnostic_poll_failure_does_not_prevent_remaining_polls(monkeypatch):
  calls = []

  def fail_gnss(_pigeon):
    calls.append("gnss")
    raise OSError("serial failure")

  monkeypatch.setattr(pigeond, "poll_cfg_gnss", fail_gnss)
  monkeypatch.setattr(
    pigeond,
    "poll_cfg_rxm",
    lambda pigeon: calls.append("rxm") or RxmConfig(0),
  )
  monkeypatch.setattr(
    pigeond,
    "poll_cfg_pm2",
    lambda pigeon: calls.append("pm2") or None,
  )
  monkeypatch.setattr(pigeond, "_log_cfg_rxm", lambda config: calls.append("rxm_log"))

  pigeond.log_acquisition_configuration_diagnostics(object(), None)

  assert calls == ["gnss", "rxm", "rxm_log", "pm2"]


def test_acquisition_diagnostics_send_only_zero_payload_cfg_polls():
  pigeon = ResponsePigeon(
    (
      build_cfg_gnss(((0, 8, 16, 1),)),
      build_cfg_rxm(0),
      build_cfg_pm2(),
    )
  )

  pigeond.log_acquisition_configuration_diagnostics(pigeon, None)

  assert pigeon.sent == [
    build_cfg_gnss_poll_message(),
    build_cfg_rxm_poll_message(),
    build_cfg_pm2_poll_message(),
  ]
  assert all(message[4:6] == b"\0\0" and len(message) == 8 for message in pigeon.sent)


def test_hpg_acquisition_diagnostics_skip_pm2_at_info_level(monkeypatch):
  info_logs = []
  warning_logs = []
  pigeon = ResponsePigeon(
    (
      build_cfg_gnss(((0, 8, 16, 1),)),
      build_cfg_rxm(0),
    )
  )
  info = MonVerInfo(
    "EXT CORE 3.01",
    "00080000",
    ("FWVER=HPG 1.40ROV", "PROTVER=20.30"),
  )
  monkeypatch.setattr(pigeond.cloudlog, "info", lambda message: info_logs.append(message))
  monkeypatch.setattr(pigeond.cloudlog, "warning", lambda message: warning_logs.append(message))

  pigeond.log_acquisition_configuration_diagnostics(pigeon, info)

  assert pigeon.sent == [
    build_cfg_gnss_poll_message(),
    build_cfg_rxm_poll_message(),
  ]
  skip_message = "GPS acquisition configuration CFG-PM2 skipped, supported=false, reason=hpg_product_unsupported"
  assert skip_message in info_logs
  assert skip_message not in warning_logs


@pytest.mark.parametrize(
  "info",
  [
    None,
    MonVerInfo(
      "EXT CORE 3.01",
      "00080000",
      ("FWVER=SPG 3.01", "PROTVER=20.30"),
    ),
  ],
)
def test_non_hpg_or_unavailable_mon_ver_still_polls_pm2(info):
  pigeon = ResponsePigeon(
    (
      build_cfg_gnss(((0, 8, 16, 1),)),
      build_cfg_rxm(0),
      build_cfg_pm2(),
    )
  )

  pigeond.log_acquisition_configuration_diagnostics(pigeon, info)

  assert pigeon.sent == [
    build_cfg_gnss_poll_message(),
    build_cfg_rxm_poll_message(),
    build_cfg_pm2_poll_message(),
  ]


def test_parse_valid_mon_ver_with_multiple_extensions():
  info = parse_mon_ver(
    build_mon_ver(
      extensions=(
        "PROTVER=18.00",
        "FWVER=SPG 3.01",
        "MOD=NEO-M8L-0",
        "GPS;GLO;GAL;BDS",
        "SBAS;IMES;QZSS;GPS",
      )
    )
  )
  assert info == MonVerInfo(
    software_version="EXT CORE 3.01",
    hardware_version="00080000",
    extensions=(
      "PROTVER=18.00",
      "FWVER=SPG 3.01",
      "MOD=NEO-M8L-0",
      "GPS;GLO;GAL;BDS",
      "SBAS;IMES;QZSS;GPS",
    ),
  )
  assert info.protocol_versions == ("PROTVER=18.00",)
  assert info.firmware_versions == ("FWVER=SPG 3.01",)
  assert info.module_identifiers == ("MOD=NEO-M8L-0",)
  assert info.supported_gnss == ("GPS", "GLO", "GAL", "BDS", "SBAS", "IMES", "QZSS")


def test_mon_ver_rejects_malformed_length_and_bad_checksum():
  malformed_payload = b"x" * 41
  malformed = add_ubx_checksum(b"\xb5\x62\x0a\x04" + len(malformed_payload).to_bytes(2, "little") + malformed_payload)
  assert parse_mon_ver(malformed) is None
  bad_checksum = bytearray(build_mon_ver())
  bad_checksum[-1] ^= 0xFF
  assert parse_mon_ver(bytes(bad_checksum)) is None


def test_unknown_mon_ver_extensions_are_preserved():
  info = parse_mon_ver(build_mon_ver(extensions=("VENDOR=unexpected", "XYZ")))
  assert info is not None
  assert info.extensions == ("VENDOR=unexpected", "XYZ")
  assert info.protocol_versions == ()
  assert info.supported_gnss == ()


def test_receiver_identity_normalization_is_deterministic():
  first = MonVerInfo(" EXT   CORE 3.01 ", "00080000", ("GPS", "PROTVER=18.00"))
  second = MonVerInfo("ext core 3.01", "00080000", ("protver=18.00", "gps"))
  assert normalized_receiver_identity(first) == normalized_receiver_identity(second)
  assert normalized_receiver_identity(first) == ("sw=ext core 3.01|hw=00080000|ext=gps;protver=18.00")


def test_mon_ver_poll_uses_one_startup_poll_and_parses_response():
  class Pigeon:
    def __init__(self):
      self.sent = []
      self.response = build_mon_ver()

    def send(self, message):
      self.sent.append(message)

    def receive(self):
      response, self.response = self.response, b""
      return response

  pigeon = Pigeon()
  info = pigeond.poll_mon_ver(pigeon, timeout=0.01)
  assert info is not None
  assert pigeon.sent == [pigeond.build_mon_ver_poll_message()]


@pytest.mark.parametrize("poll_outcome", [None, TimeoutError("timeout")])
def test_mon_ver_is_polled_each_receiver_cycle_and_failure_does_not_block_assistance(
  monkeypatch,
  poll_outcome,
):
  calls = []

  class Diagnostics:
    def start_cycle(self, reason, now):
      calls.append(("cycle", reason))

    def time_assistance_context(self, now):
      return "cycle_context"

  def fake_poll(pigeon):
    calls.append(("mon_ver",))
    if isinstance(poll_outcome, Exception):
      raise poll_outcome
    return poll_outcome

  monkeypatch.setattr(pigeond, "init", lambda pigeon: calls.append(("init",)))
  monkeypatch.setattr(pigeond, "poll_mon_ver", fake_poll)
  monkeypatch.setattr(pigeond, "read_host_time_observation", network_host_observation)
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    lambda pigeon, **kwargs: calls.append(("time_assistance",)) or True,
  )
  monkeypatch.setattr(
    pigeond,
    "restore_navigation_assistance",
    lambda pigeon, fingerprint, **kwargs: calls.append(("restore",)),
  )

  diagnostics = Diagnostics()
  pigeon = object()
  process_start_result = pigeond.initialize_receiver_cycle(
    pigeon,
    "receiver",
    diagnostics,
    "process_start",
    collect_mon_ver_diagnostics=True,
  )
  recovery_result = pigeond.initialize_receiver_cycle(
    pigeon,
    "receiver",
    diagnostics,
    "no_data_watchdog",
  )

  assert calls.count(("mon_ver",)) == 2
  assert calls.count(("time_assistance",)) == 2
  assert calls.count(("restore",)) == 2
  assert calls == [
    ("cycle", "process_start"),
    ("init",),
    ("mon_ver",),
    ("restore",),
    ("time_assistance",),
    ("cycle", "no_data_watchdog"),
    ("init",),
    ("mon_ver",),
    ("restore",),
    ("time_assistance",),
  ]
  assert process_start_result.trusted_time_assistance_sent
  assert process_start_result.navigation_assistance_restore_attempted
  assert recovery_result.trusted_time_assistance_sent
  assert recovery_result.navigation_assistance_restore_attempted


def test_acquisition_diagnostics_run_only_on_initial_diagnostic_cycle_and_run_last(monkeypatch):
  calls = []

  class Diagnostics:
    def start_cycle(self, reason, now):
      calls.append(f"cycle:{reason}")

    def time_assistance_context(self, now):
      return "context"

  monkeypatch.setattr(pigeond, "init", lambda pigeon: calls.append("init"))
  monkeypatch.setattr(
    pigeond,
    "log_mon_ver_diagnostics",
    lambda pigeon: calls.append("mon_ver") or None,
  )
  monkeypatch.setattr(
    pigeond,
    "poll_mon_ver",
    lambda pigeon: calls.append("mon_ver") or None,
  )
  monkeypatch.setattr(
    pigeond,
    "log_navx5_ack_aiding_support",
    lambda info: calls.append("ack_support"),
  )
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda pigeon, info: calls.append("ack_config"),
  )
  monkeypatch.setattr(pigeond, "read_host_time_observation", network_host_observation)
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    lambda pigeon, **kwargs: calls.append("time_assistance") or True,
  )
  monkeypatch.setattr(
    pigeond,
    "restore_navigation_assistance",
    lambda pigeon, fingerprint, **kwargs: calls.append("restore"),
  )
  monkeypatch.setattr(
    pigeond,
    "log_assistnow_autonomous_support",
    lambda info: calls.append("aop_support") or False,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_assistnow_autonomous",
    lambda pigeon, info: calls.append("aop_config"),
  )
  monkeypatch.setattr(
    pigeond,
    "log_acquisition_configuration_diagnostics",
    lambda pigeon, info: calls.append("acquisition_diagnostics"),
  )

  diagnostics = Diagnostics()
  pigeond.initialize_receiver_cycle(
    object(),
    "receiver",
    diagnostics,
    "process_start",
    collect_mon_ver_diagnostics=True,
  )
  pigeond.initialize_receiver_cycle(
    object(),
    "receiver",
    diagnostics,
    "no_data_watchdog",
    collect_mon_ver_diagnostics=False,
  )

  assert calls == [
    "cycle:process_start",
    "init",
    "mon_ver",
    "ack_support",
    "ack_config",
    "restore",
    "time_assistance",
    "aop_support",
    "aop_config",
    "acquisition_diagnostics",
    "cycle:no_data_watchdog",
    "init",
    "mon_ver",
    "ack_support",
    "ack_config",
    "restore",
    "time_assistance",
    "aop_support",
    "aop_config",
  ]


def test_acquisition_diagnostic_failure_does_not_block_initialized_result(monkeypatch):
  calls = []

  class Diagnostics:
    def start_cycle(self, reason, now):
      pass

    def time_assistance_context(self, now):
      return "context"

  monkeypatch.setattr(pigeond, "init", lambda pigeon: None)
  monkeypatch.setattr(pigeond, "log_mon_ver_diagnostics", lambda pigeon: None)
  monkeypatch.setattr(pigeond, "log_navx5_ack_aiding_support", lambda info: None)
  monkeypatch.setattr(pigeond, "configure_navx5_ack_aiding", lambda pigeon, info: None)
  monkeypatch.setattr(pigeond, "read_host_time_observation", network_host_observation)
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    lambda pigeon, **kwargs: calls.append("time_assistance") or True,
  )
  monkeypatch.setattr(
    pigeond,
    "restore_navigation_assistance",
    lambda pigeon, fingerprint, **kwargs: calls.append("restore"),
  )
  monkeypatch.setattr(pigeond, "log_assistnow_autonomous_support", lambda info: False)
  monkeypatch.setattr(pigeond, "configure_assistnow_autonomous", lambda pigeon, info: None)

  def fail_diagnostics(pigeon, info):
    calls.append("acquisition_diagnostics")
    raise RuntimeError("diagnostic failure")

  monkeypatch.setattr(pigeond, "log_acquisition_configuration_diagnostics", fail_diagnostics)

  result = pigeond.initialize_receiver_cycle(
    object(),
    "receiver",
    Diagnostics(),
    "process_start",
    collect_mon_ver_diagnostics=True,
  )

  assert calls == ["restore", "time_assistance", "acquisition_diagnostics"]
  assert result.trusted_time_assistance_sent
  assert result.navigation_assistance_restore_attempted


def test_restore_summary_success_partial_timeout_and_no_attempt():
  success = pigeond.NavigationAssistanceRestoreResult(
    pigeond.NavigationAssistanceRestoreStatus.COMPLETE,
    3,
    3,
  )
  partial = pigeond.NavigationAssistanceRestoreResult(
    pigeond.NavigationAssistanceRestoreStatus.PARTIAL,
    3,
    2,
    initially_rejected_indexes=(1,),
    permanently_rejected_indexes=(1,),
  )
  timeout = pigeond.NavigationAssistanceRestoreResult(
    pigeond.NavigationAssistanceRestoreStatus.FAILED,
    2,
    0,
    initially_timed_out_indexes=(0, 1),
    permanently_timed_out_indexes=(0, 1),
  )

  success_text = pigeond.format_navigation_assistance_restore_summary(
    success,
    attempted=True,
    time_assistance_source="synchronized",
  )
  assert "restore_attempted=True" in success_text
  assert "total_frames=3" in success_text
  assert "accepted_frames=3" in success_text
  assert "terminal_result=complete" in success_text
  assert "time_assistance_source=synchronized" in success_text
  assert "database_terminal_ack_count_matched=not_applicable_per_frame_restore" in success_text

  partial_text = pigeond.format_navigation_assistance_restore_summary(
    partial,
    attempted=True,
    time_assistance_source="rtc_estimate",
  )
  assert "rejected_frames=1" in partial_text
  assert "retry_attempts=1" in partial_text
  assert "terminal_result=partial" in partial_text

  timeout_text = pigeond.format_navigation_assistance_restore_summary(
    timeout,
    attempted=True,
    time_assistance_source="synchronized",
  )
  assert "timeout_events=4" in timeout_text
  assert "terminal_result=failed" in timeout_text

  no_attempt_text = pigeond.format_navigation_assistance_restore_summary(
    None,
    attempted=False,
    time_assistance_source=None,
  )
  assert "restore_attempted=False" in no_attempt_text
  assert "terminal_result=not_attempted" in no_attempt_text
  assert "time_assistance_source=none" in no_attempt_text
