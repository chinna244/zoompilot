from __future__ import annotations

import inspect

from openpilot.system.ubloxd import rf_observability
from openpilot.system.ubloxd import ubloxd


def frame(message_class: int, message_id: int, payload: bytes) -> bytes:
  return b"\xb5\x62" + bytes((message_class, message_id)) + len(payload).to_bytes(2, "little") + payload + b"\x00\x00"


def valid_frame(message_class: int, message_id: int, payload: bytes) -> bytes:
  body = bytes((message_class, message_id)) + len(payload).to_bytes(2, "little") + payload
  ck_a = 0
  ck_b = 0
  for value in body:
    ck_a = (ck_a + value) & 0xFF
    ck_b = (ck_b + ck_a) & 0xFF
  return b"\xb5\x62" + body + bytes((ck_a, ck_b))


def rawx_payload(measurements: tuple[tuple[int, int, int], ...]) -> bytes:
  payload = bytearray(16 + 32 * len(measurements))
  payload[8:10] = (2429).to_bytes(2, "little")
  payload[10] = 18
  payload[11] = len(measurements)
  payload[12] = 0x01
  for index, (gnss_id, cno, tracking_status) in enumerate(measurements):
    offset = 16 + index * 32
    payload[offset + 20] = gnss_id
    payload[offset + 21] = index + 1
    payload[offset + 26] = cno
    payload[offset + 30] = tracking_status
  return bytes(payload)


def nav_sat_payload(satellites: tuple[tuple[int, int, int, int], ...]) -> bytes:
  payload = bytearray(8 + 12 * len(satellites))
  payload[4] = 1
  payload[5] = len(satellites)
  for index, (gnss_id, sv_id, cno, flags) in enumerate(satellites):
    offset = 8 + index * 12
    payload[offset] = gnss_id
    payload[offset + 1] = sv_id
    payload[offset + 2] = cno
    payload[offset + 3] = 30
    payload[offset + 4:offset + 6] = (90).to_bytes(2, "little", signed=True)
    payload[offset + 8:offset + 12] = flags.to_bytes(4, "little")
  return bytes(payload)


def test_parser_error_is_structured_bounded_and_rate_limited() -> None:
  value = rf_observability.UbloxRfObservability(log_interval_seconds=30.0)
  broken = frame(0x01, 0x35, b"\x00" * 8)
  first = value.observe_parser_error(broken, ValueError("bad\n" + "x" * 300), 1.0)
  assert first is not None
  assert "message_class=0x01" in first
  assert "message_id=0x35" in first
  assert "declared_payload_length=8" in first
  assert "message_error_count=1" in first
  assert "total_error_count=1" in first
  assert "exception_type=ValueError" in first
  assert "\n" not in first
  assert value.observe_parser_error(broken, ValueError("again"), 2.0) is None
  later = value.observe_parser_error(broken, ValueError("again"), 31.0)
  assert later is not None
  assert "message_error_count=3" in later
  assert "total_error_count=3" in later


def test_rawx_average_excludes_zero_cno_measurements() -> None:
  value = rf_observability.UbloxRfObservability()
  line = value.observe_frame(
    frame(0x02, 0x15, rawx_payload(((0, 35, 1), (0, 33, 1), (6, 0, 0), (6, 0, 0)))),
    1.0,
  )[0]
  assert "signal_measurements=2" in line
  assert "zero_cno_count=2" in line
  assert "average_signal_cno_dbhz=34.0" in line
  assert "max_cno_dbhz=35" in line


def test_rawx_logs_progress_highwater_and_interval() -> None:
  value = rf_observability.UbloxRfObservability(log_interval_seconds=30.0)
  first = value.observe_frame(
    frame(0x02, 0x15, rawx_payload(((0, 25, 1), (6, 24, 0)))),
    1.0,
  )
  assert first
  assert "measurement_count=2" in first[0]
  assert value.observe_frame(
    frame(0x02, 0x15, rawx_payload(((0, 24, 1), (6, 23, 0)))),
    2.0,
  ) == ()
  improved = value.observe_frame(
    frame(0x02, 0x15, rawx_payload(((0, 27, 1), (6, 24, 1), (0, 22, 1)))),
    3.0,
  )
  assert improved
  periodic = value.observe_frame(
    frame(0x02, 0x15, rawx_payload(((0, 20, 1),))),
    34.0,
  )
  assert periodic


def test_nav_sat_reports_acquired_code_locked_used_and_nonzero_cno_average() -> None:
  value = rf_observability.UbloxRfObservability()
  code_lock_used_eph_alm = 4 | (1 << 3) | (1 << 11) | (1 << 12)
  carrier_lock = 5
  acquired = 2
  line = value.observe_frame(
    frame(
      0x01,
      0x35,
      nav_sat_payload((
        (0, 1, 35, code_lock_used_eph_alm),
        (6, 2, 33, carrier_lock),
        (0, 3, 30, acquired),
        (6, 4, 28, acquired),
        (0, 5, 0, 0),
        (6, 6, 0, 0),
        (0, 7, 0, 0),
        (6, 8, 0, 0),
      )),
    ),
    1.0,
  )[0]
  assert "signal_satellites=4" in line
  assert "zero_cno_count=4" in line
  assert "acquired_satellites=4" in line
  assert "code_locked_satellites=2" in line
  assert "used_satellites=1" in line
  assert "ephemeris_available=1" in line
  assert "almanac_available=1" in line
  assert "average_signal_cno_dbhz=31.5" in line
  assert "acquired_by_gnss=0:2|6:2" in line
  assert "code_locked_by_gnss=0:1|6:1" in line


def test_mon_hw_logs_antenna_agc_noise_and_jamming() -> None:
  value = rf_observability.UbloxRfObservability()
  payload = bytearray(60)
  payload[16:18] = (120).to_bytes(2, "little")
  payload[18:20] = (4321).to_bytes(2, "little")
  payload[20] = 2
  payload[21] = 1
  payload[22] = 0x05
  payload[45] = 17
  line = value.observe_frame(frame(0x0A, 0x09, bytes(payload)), 1.0)[0]
  assert "antenna_status=ok" in line
  assert "antenna_power=on" in line
  assert "agc_count=4321" in line
  assert "noise_per_ms=120" in line
  assert "jam_indicator=17" in line


def test_mon_hw_antenna_change_logs_immediately() -> None:
  value = rf_observability.UbloxRfObservability(log_interval_seconds=30.0)
  payload = bytearray(60)
  payload[20] = 2
  payload[21] = 1
  assert value.observe_frame(frame(0x0A, 0x09, bytes(payload)), 1.0)
  assert value.observe_frame(frame(0x0A, 0x09, bytes(payload)), 2.0) == ()
  payload[20] = 4
  changed = value.observe_frame(frame(0x0A, 0x09, bytes(payload)), 3.0)
  assert changed
  assert "antenna_status=open" in changed[0]


def test_mon_hw2_logs_frontend_and_post_status() -> None:
  value = rf_observability.UbloxRfObservability()
  payload = bytearray(28)
  payload[0] = 0xFE
  payload[1] = 10
  payload[2] = 0x03
  payload[3] = 11
  payload[4] = 102
  payload[8:12] = (0x12345678).to_bytes(4, "little")
  payload[20:24] = (0x89ABCDEF).to_bytes(4, "little")
  line = value.observe_frame(frame(0x0A, 0x0B, bytes(payload)), 1.0)[0]
  assert "ofs_i=-2" in line
  assert "mag_i=10" in line
  assert "ofs_q=3" in line
  assert "config_source=102" in line
  assert "low_level_config=0x12345678" in line
  assert "post_status=0x89abcdef" in line


def test_sfrbx_progress_is_observable_without_parser_output() -> None:
  value = rf_observability.UbloxRfObservability(log_interval_seconds=30.0)
  gps = bytearray(16)
  gps[0] = 0
  gps[1] = 3
  gps[4] = 2
  first = value.observe_frame(frame(0x02, 0x13, bytes(gps)), 1.0)
  assert first
  assert "unique_satellites=1" in first[0]
  assert "latest_structurally_complete=True" in first[0]

  glo = bytearray(16)
  glo[0] = 6
  glo[1] = 7
  glo[4] = 2
  second = value.observe_frame(frame(0x02, 0x13, bytes(glo)), 2.0)
  assert second
  assert "unique_satellites=2" in second[0]
  assert "messages_by_gnss=0:1|6:1" in second[0]


def test_ubx_framer_counts_valid_checksum_discard_and_resync() -> None:
  framer = ubloxd.UbxFramer()
  good = valid_frame(0x01, 0x07, b"\x00" * 4)

  assert framer.add_data(1.0, b"garbage" + good) == [good]
  assert framer.frames_valid == 1
  assert framer.discarded_prefix_bytes == len(b"garbage")
  assert framer.resync_events == 1
  assert framer.checksum_failures == 0

  bad = bytearray(good)
  bad[-1] ^= 0xFF
  assert framer.add_data(2.0, bytes(bad) + good) == [good]
  assert framer.frames_valid == 2
  assert framer.checksum_failures == 1
  assert framer.resync_events >= 2
  assert framer.discarded_prefix_bytes >= len(b"garbage")


def test_framer_health_logs_first_periodic_and_error_change() -> None:
  value = rf_observability.UbloxRfObservability(log_interval_seconds=30.0)
  first = value.observe_framer_health(
    frames_valid=10,
    checksum_failures=0,
    discarded_prefix_bytes=0,
    resync_events=0,
    buffered_bytes=0,
    now=1.0,
  )
  assert first is not None
  assert "frames_valid=10" in first
  assert value.observe_framer_health(
    frames_valid=20,
    checksum_failures=0,
    discarded_prefix_bytes=0,
    resync_events=0,
    buffered_bytes=0,
    now=2.0,
  ) is None
  error = value.observe_framer_health(
    frames_valid=20,
    checksum_failures=1,
    discarded_prefix_bytes=5,
    resync_events=2,
    buffered_bytes=3,
    now=3.0,
  )
  assert error is not None
  assert "checksum_failures=1" in error
  assert "discarded_prefix_bytes=5" in error

  # Repeated corruption must not log on every counter increment.
  assert value.observe_framer_health(
    frames_valid=21,
    checksum_failures=2,
    discarded_prefix_bytes=9,
    resync_events=4,
    buffered_bytes=2,
    now=4.0,
  ) is None

  periodic = value.observe_framer_health(
    frames_valid=50,
    checksum_failures=3,
    discarded_prefix_bytes=12,
    resync_events=6,
    buffered_bytes=0,
    now=34.0,
  )
  assert periodic is not None
  assert "checksum_failures=3" in periodic


def test_ubloxd_observes_valid_frames_before_parser_and_retains_publication() -> None:
  source = inspect.getsource(ubloxd.main)
  observe_index = source.index("observability.observe_frame(frame, log_time)")
  parse_index = source.index("parser.parse_frame(frame, measurement_mono_ns=int(msg.logMonoTime))")
  assert observe_index < parse_index
  assert "observability.observe_framer_health(" in source
  assert "observability.observe_parser_error(frame, exc, log_time)" in source
  assert "pm.send(service, dat)" in source
  for forbidden in ("send_with_ack", "reset_device", "initialize_receiver_cycle", "CFG_"):
    assert forbidden not in source


def test_truncated_and_unrelated_frames_are_ignored() -> None:
  value = rf_observability.UbloxRfObservability()
  assert value.observe_frame(b"", 1.0) == ()
  assert value.observe_frame(frame(0x01, 0x07, b"\x00" * 92), 1.0) == ()
  assert value.observe_frame(frame(0x01, 0x35, b"\x00"), 1.0) == ()
