import base64
import json
import struct
from datetime import UTC, datetime, timedelta

import pytest

from openpilot.system.ubloxd.gps_assistance import (
  CacheAgeEvidence,
  CacheValidationError,
  GLONASS_EPHEMERIS_FRESHNESS_SECONDS,
  GPS_EPHEMERIS_FRESHNESS_SECONDS,
  GpsAssistanceCache,
  MAX_RTC_ASSISTANCE_ELAPSED_SECONDS,
  MIN_RESTORE_POSITION_ACCURACY_CM,
  NavPvtFix,
  NavigationQuality,
  RTC_BASE_TIME_UNCERTAINTY_SECONDS,
  RTC_DRIFT_PARTS_PER_MILLION,
  RtcEstimateRejection,
  RtcEstimateRejectionReason,
  RtcEstimateSuccess,
  UbxStreamParser,
  add_ubx_checksum,
  build_position_assistance_message,
  build_time_assistance_message,
  create_cache,
  effective_restored_navigation_quality,
  estimate_utc_from_rtc,
  evaluate_utc_from_rtc,
  load_cache,
  refresh_restored_navigation_quality,
  save_cache,
  split_ubx_frames,
  validate_ubx_frame,
)


def build_dbd_frame(sequence: int) -> bytes:
  payload = (
    bytes((sequence & 0xFF, 0x00, 0x00, 0x00))
    + sequence.to_bytes(4, "little")
  )

  return add_ubx_checksum(
    b"\xB5\x62\x13\x80"
    + len(payload).to_bytes(2, "little")
    + payload
  )


def reliable_fix(
  latitude_e7: int = 280_000_000,
  longitude_e7: int = -820_000_000,
) -> NavPvtFix:
  return NavPvtFix(
    fix_ok=True,
    satellites=7,
    latitude_e7=latitude_e7,
    longitude_e7=longitude_e7,
    altitude_cm=1_500,
    horizontal_accuracy_cm=250,
    vertical_accuracy_cm=400,
  )


def test_ubx_frame_validation_and_split():
  frames = (
    build_dbd_frame(1),
    build_dbd_frame(2),
  )
  database = b"".join(frames)

  assert all(validate_ubx_frame(frame) for frame in frames)
  assert split_ubx_frames(database) == frames


def test_stream_parser_handles_partial_data_and_noise():
  frame = build_dbd_frame(3)
  parser = UbxStreamParser()

  assert parser.feed(b"noise" + frame[:5]) == []
  assert parser.feed(frame[5:]) == [frame]


def test_bad_checksum_is_rejected():
  frame = bytearray(build_dbd_frame(4))
  frame[-1] ^= 0xFF

  assert not validate_ubx_frame(bytes(frame))

  with pytest.raises(
    CacheValidationError,
    match="checksum",
  ):
    split_ubx_frames(bytes(frame))


def test_cache_round_trip_and_atomic_overwrite(tmp_path):
  path = tmp_path / "gps_assistance.json"
  frames = (
    build_dbd_frame(1),
    build_dbd_frame(2),
  )

  first_cache = create_cache(
    receiver_fingerprint="v1|neo-m8p|sw=hpg1.40|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=frames,
    saved_at_utc=datetime(2026, 7, 1, tzinfo=UTC),
  )
  save_cache(path, first_cache)

  second_cache = create_cache(
    receiver_fingerprint="v1|neo-m8p|sw=hpg1.40|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(latitude_e7=281_000_000),
    database_frames=frames,
    saved_at_utc=datetime(2026, 7, 2, tzinfo=UTC),
  )
  save_cache(path, second_cache)

  loaded = load_cache(
    path,
    expected_receiver_fingerprint="v1|neo-m8p|sw=hpg1.40|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    now_utc=datetime(2026, 7, 3, tzinfo=UTC),
  )

  assert loaded == second_cache
  assert list(tmp_path.iterdir()) == [path]


def test_cache_rejects_database_checksum_mismatch(tmp_path):
  path = tmp_path / "gps_assistance.json"
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 1, tzinfo=UTC),
  )
  save_cache(path, cache)

  raw = json.loads(path.read_text())
  database = bytearray(
    base64.b64decode(raw["database"]["ubx_base64"])
  )
  database[-1] ^= 0xFF
  raw["database"]["ubx_base64"] = base64.b64encode(
    database
  ).decode("ascii")
  path.write_text(json.dumps(raw))

  with pytest.raises(
    CacheValidationError,
    match="checksum",
  ):
    load_cache(path)


def test_cache_rejects_wrong_receiver(tmp_path):
  path = tmp_path / "gps_assistance.json"
  cache = create_cache(
    receiver_fingerprint="receiver-a",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
  )
  save_cache(path, cache)

  with pytest.raises(
    CacheValidationError,
    match="different receiver",
  ):
    load_cache(
      path,
      expected_receiver_fingerprint="receiver-b",
    )


def test_cache_age_is_checked_only_when_current_time_is_supplied(
  tmp_path,
):
  path = tmp_path / "gps_assistance.json"
  saved_at = datetime(2026, 1, 1, tzinfo=UTC)

  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=saved_at,
  )
  save_cache(path, cache)

  # Startup may occur before NTP, so structural validation can run
  # without trusting the incorrect system clock.
  assert load_cache(path).saved_at_utc == saved_at

  with pytest.raises(
    CacheValidationError,
    match="too old",
  ):
    load_cache(
      path,
      now_utc=saved_at + timedelta(days=8),
    )


def test_position_assistance_uses_conservative_accuracy():
  message = build_position_assistance_message(
    latitude_e7=280_000_000,
    longitude_e7=-820_000_000,
    altitude_cm=1_500,
    position_accuracy_cm=250,
  )

  assert validate_ubx_frame(message)
  assert message[2:4] == b"\x13\x40"

  payload = message[6:-2]
  message_type, version, latitude, longitude, altitude, accuracy = (
    struct.unpack("<BBxxiiiI", payload)
  )

  assert message_type == 1
  assert version == 0
  assert latitude == 280_000_000
  assert longitude == -820_000_000
  assert altitude == 1_500
  assert accuracy == MIN_RESTORE_POSITION_ACCURACY_CM


def test_create_cache_requires_reliable_fix():
  bad_fix = NavPvtFix(
    fix_ok=False,
    satellites=0,
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    horizontal_accuracy_cm=100_000,
    vertical_accuracy_cm=100_000,
  )

  with pytest.raises(
    CacheValidationError,
    match="reliable GPS fix",
  ):
    create_cache(
      receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
      fix=bad_fix,
      database_frames=(build_dbd_frame(1),),
    )


def test_save_cache_rejects_non_dbd_message(tmp_path):
  payload = b"\x00\x00"
  invalid_database_frame = add_ubx_checksum(
    b"\xB5\x62\x01\x07"
    + len(payload).to_bytes(2, "little")
    + payload
  )

  cache = GpsAssistanceCache(
    saved_at_utc=datetime.now(UTC),
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    position_accuracy_cm=1_000,
    database_frames=(invalid_database_frame,),
  )

  with pytest.raises(
    CacheValidationError,
    match="invalid message",
  ):
    save_cache(
      tmp_path / "gps_assistance.json",
      cache,
    )


def build_mga_ack_frame(
  accepted: bool,
  info_code: int,
  message_id: int,
  payload_start: bytes,
  version: int = 0,
) -> bytes:
  payload = bytes((
    1 if accepted else 0,
    version,
    info_code,
    message_id,
  )) + payload_start[:4].ljust(4, b"\x00")

  return add_ubx_checksum(
    b"\xB5\x62\x13\x60"
    + len(payload).to_bytes(2, "little")
    + payload
  )


def test_database_poll_message():
  from openpilot.system.ubloxd.gps_assistance import (
    build_database_poll_message,
  )

  message = build_database_poll_message()

  assert validate_ubx_frame(message)
  assert message[2:4] == b"\x13\x80"
  assert message[4:6] == b"\x00\x00"


def test_parse_mga_ack():
  from openpilot.system.ubloxd.gps_assistance import parse_mga_ack

  frame = build_mga_ack_frame(
    accepted=True,
    info_code=0,
    message_id=0x80,
    payload_start=b"\x22\x00\x00\x00",
  )

  acknowledgment = parse_mga_ack(frame)

  assert acknowledgment is not None
  assert acknowledgment.accepted
  assert acknowledgment.acknowledgment_type == 1
  assert acknowledgment.version == 0
  assert acknowledgment.info_code == 0
  assert acknowledgment.message_id == 0x80
  assert acknowledgment.message_payload_start == b"\x22\x00\x00\x00"


def test_parse_rejected_mga_ack():
  from openpilot.system.ubloxd.gps_assistance import parse_mga_ack

  frame = build_mga_ack_frame(
    accepted=False,
    info_code=2,
    message_id=0x40,
    payload_start=b"\x01\x00\x00\x00",
  )

  acknowledgment = parse_mga_ack(frame)

  assert acknowledgment is not None
  assert not acknowledgment.accepted
  assert acknowledgment.acknowledgment_type == 0
  assert acknowledgment.version == 0
  assert acknowledgment.info_code == 2
  assert acknowledgment.message_id == 0x40


def test_nonzero_mga_ack_version_is_never_accepted():
  from openpilot.system.ubloxd.gps_assistance import parse_mga_ack

  acknowledgment = parse_mga_ack(build_mga_ack_frame(
    accepted=True,
    info_code=0,
    message_id=0x80,
    payload_start=b"\x01\x00\x00\x00",
    version=1,
  ))

  assert acknowledgment is not None
  assert acknowledgment.version == 1
  assert not acknowledgment.accepted


def build_nav_pvt_frame(
  *,
  valid_time_flags: int = 0x07,
  year: int = 2026,
  month: int = 7,
  day: int = 3,
  hour: int = 14,
  minute: int = 25,
  second: int = 30,
  time_accuracy_ns: int = 25_000_000,
  nano: int = 0,
) -> bytes:
  payload = bytearray(92)

  struct.pack_into("<H", payload, 4, year)
  payload[6] = month
  payload[7] = day
  payload[8] = hour
  payload[9] = minute
  payload[10] = second
  payload[11] = valid_time_flags
  struct.pack_into("<I", payload, 12, time_accuracy_ns)
  struct.pack_into("<i", payload, 16, nano)

  payload[20] = 3
  payload[21] = 0x01
  payload[23] = 7

  struct.pack_into("<i", payload, 24, -820_000_000)
  struct.pack_into("<i", payload, 28, 280_000_000)
  struct.pack_into("<i", payload, 32, 15_000)
  struct.pack_into("<I", payload, 40, 2_500)
  struct.pack_into("<I", payload, 44, 4_000)

  return add_ubx_checksum(
    b"\xB5\x62\x01\x07"
    + len(payload).to_bytes(2, "little")
    + payload
  )


def test_parse_nav_pvt_uses_receiver_utc_time():
  from openpilot.system.ubloxd.gps_assistance import parse_nav_pvt

  fix = parse_nav_pvt(build_nav_pvt_frame())

  assert fix is not None
  assert fix.reliable
  assert fix.utc_time == datetime(
    2026,
    7,
    3,
    14,
    25,
    30,
    tzinfo=UTC,
  )
  assert fix.valid_date
  assert fix.valid_time
  assert fix.fully_resolved
  assert fix.time_accuracy_ns == 25_000_000


def test_parse_nav_pvt_applies_signed_nanoseconds():
  from openpilot.system.ubloxd.gps_assistance import parse_nav_pvt

  positive = parse_nav_pvt(
    build_nav_pvt_frame(nano=250_000_000)
  )
  negative = parse_nav_pvt(
    build_nav_pvt_frame(nano=-250_000_000)
  )

  assert positive is not None
  assert negative is not None
  assert positive.utc_time == datetime(
    2026,
    7,
    3,
    14,
    25,
    30,
    250_000,
    tzinfo=UTC,
  )
  assert negative.utc_time == datetime(
    2026,
    7,
    3,
    14,
    25,
    29,
    750_000,
    tzinfo=UTC,
  )


def test_parse_nav_pvt_rejects_out_of_range_nanoseconds():
  from openpilot.system.ubloxd.gps_assistance import parse_nav_pvt

  fix = parse_nav_pvt(
    build_nav_pvt_frame(nano=1_000_000_001)
  )

  assert fix is not None
  assert fix.utc_time is None
  assert fix.valid_date
  assert fix.valid_time
  assert fix.fully_resolved


def test_parse_nav_pvt_requires_fully_resolved_receiver_time():
  from openpilot.system.ubloxd.gps_assistance import parse_nav_pvt

  fix = parse_nav_pvt(
    build_nav_pvt_frame(valid_time_flags=0x03)
  )

  assert fix is not None
  assert fix.reliable
  assert fix.utc_time is None
  assert fix.valid_date
  assert fix.valid_time
  assert not fix.fully_resolved


def test_parse_nav_pvt_ignores_invalid_receiver_time():
  from openpilot.system.ubloxd.gps_assistance import parse_nav_pvt

  fix = parse_nav_pvt(
    build_nav_pvt_frame(valid_time_flags=0)
  )

  assert fix is not None
  assert fix.reliable
  assert fix.utc_time is None
  assert not fix.valid_date
  assert not fix.valid_time
  assert not fix.fully_resolved


def test_reliable_fix_tracker_requires_continuous_stability():
  from openpilot.system.ubloxd.gps_assistance import (
    ReliableFixTracker,
  )

  tracker = ReliableFixTracker(
    stability_seconds=20,
    maximum_gap_seconds=2,
  )
  fix = reliable_fix()

  for second in range(20):
    tracker.update(fix, second)

  assert tracker.stable_fix(19) is None

  tracker.update(fix, 20)
  assert tracker.stable_fix(20) == fix


def test_reliable_fix_tracker_resets_after_bad_fix():
  from openpilot.system.ubloxd.gps_assistance import (
    ReliableFixTracker,
  )

  tracker = ReliableFixTracker(
    stability_seconds=20,
    maximum_gap_seconds=2,
  )
  good_fix = reliable_fix()
  bad_fix = NavPvtFix(
    fix_ok=False,
    satellites=0,
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    horizontal_accuracy_cm=100_000,
    vertical_accuracy_cm=100_000,
  )

  for second in range(21):
    tracker.update(good_fix, second)

  assert tracker.stable_fix(20) == good_fix

  tracker.update(bad_fix, 21)
  assert tracker.stable_fix(21) is None


def test_reliable_fix_tracker_restarts_after_message_gap():
  from openpilot.system.ubloxd.gps_assistance import (
    ReliableFixTracker,
  )

  tracker = ReliableFixTracker(
    stability_seconds=20,
    maximum_gap_seconds=2,
  )
  fix = reliable_fix()

  tracker.update(fix, 0)
  tracker.update(fix, 1)

  # A long gap must restart the stability timer.
  tracker.update(fix, 10)

  for second in range(11, 30):
    tracker.update(fix, second)

  assert tracker.stable_fix(29) is None

  tracker.update(fix, 30)
  assert tracker.stable_fix(30) == fix


def test_database_dump_collector_accepts_complete_dump():
  from openpilot.system.ubloxd.gps_assistance import (
    NavigationDatabaseDumpCollector,
  )

  collector = NavigationDatabaseDumpCollector()
  frames = (
    build_dbd_frame(1),
    build_dbd_frame(2),
  )

  collector.start(100)

  assert collector.feed(frames[0]) is None
  assert collector.feed(frames[1]) is None

  acknowledgment = build_mga_ack_frame(
    accepted=True,
    info_code=0,
    message_id=0x80,
    payload_start=(2).to_bytes(4, "little"),
  )

  assert collector.feed(acknowledgment) == frames
  assert not collector.active


def test_database_dump_collector_rejects_count_mismatch():
  from openpilot.system.ubloxd.gps_assistance import (
    NavigationDatabaseDumpCollector,
  )

  collector = NavigationDatabaseDumpCollector()
  collector.start(100)
  collector.feed(build_dbd_frame(1))

  acknowledgment = build_mga_ack_frame(
    accepted=True,
    info_code=0,
    message_id=0x80,
    payload_start=(2).to_bytes(4, "little"),
  )

  with pytest.raises(
    CacheValidationError,
    match="expected 2, received 1",
  ):
    collector.feed(acknowledgment)

  assert not collector.active


def test_database_dump_collector_rejects_nack():
  from openpilot.system.ubloxd.gps_assistance import (
    NavigationDatabaseDumpCollector,
  )

  collector = NavigationDatabaseDumpCollector()
  collector.start(100)

  acknowledgment = build_mga_ack_frame(
    accepted=False,
    info_code=2,
    message_id=0x80,
    payload_start=b"\x00\x00\x00\x00",
  )

  with pytest.raises(
    CacheValidationError,
    match="infoCode 2",
  ):
    collector.feed(acknowledgment)

  assert not collector.active


def test_database_dump_collector_rejects_nonzero_ack_version():
  from openpilot.system.ubloxd.gps_assistance import NavigationDatabaseDumpCollector

  collector = NavigationDatabaseDumpCollector()
  collector.start(100)
  collector.feed(build_dbd_frame(1))
  acknowledgment = build_mga_ack_frame(
    accepted=True,
    info_code=0,
    message_id=0x80,
    payload_start=(1).to_bytes(4, "little"),
    version=1,
  )

  with pytest.raises(CacheValidationError, match="rejected"):
    collector.feed(acknowledgment)
  assert not collector.active


def test_database_dump_collector_timeout():
  from openpilot.system.ubloxd.gps_assistance import (
    NavigationDatabaseDumpCollector,
  )

  collector = NavigationDatabaseDumpCollector(
    timeout_seconds=5,
  )
  collector.start(100)

  assert not collector.expired(104.9)
  assert collector.expired(105)

  collector.cancel()
  assert not collector.active


def test_cache_round_trip_preserves_rtc_anchor(tmp_path):
  path = tmp_path / "gps_assistance.json"
  saved_at = datetime(2026, 7, 6, 17, 45, 16, tzinfo=UTC)

  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=saved_at,
    rtc_counter_seconds=24_000,
  )
  save_cache(path, cache)

  loaded = load_cache(path)

  assert loaded.rtc_counter_seconds == 24_000
  assert loaded.saved_at_utc == saved_at


def test_load_cache_accepts_legacy_file_without_rtc_anchor(
  tmp_path,
):
  path = tmp_path / "gps_assistance.json"

  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 6, tzinfo=UTC),
  )
  save_cache(path, cache)

  raw = json.loads(path.read_text())
  raw.pop("rtc_counter_seconds")
  path.write_text(json.dumps(raw))

  assert load_cache(path).rtc_counter_seconds is None


def test_rtc_anchor_estimates_current_utc():
  saved_at = datetime(
    2026,
    7,
    6,
    17,
    45,
    16,
    tzinfo=UTC,
  )
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=saved_at,
    rtc_counter_seconds=24_000,
  )

  estimate = estimate_utc_from_rtc(
    cache,
    current_rtc_seconds=24_900,
  )

  assert estimate is not None
  estimated_utc, uncertainty_seconds = estimate
  assert estimated_utc == saved_at + timedelta(seconds=900)
  assert uncertainty_seconds >= 60


@pytest.mark.parametrize(("rtc_anchor", "current_rtc", "reason", "elapsed"), [
  (None, 100, RtcEstimateRejectionReason.MISSING_CACHED_RTC_ANCHOR, None),
  (100, None, RtcEstimateRejectionReason.CURRENT_RTC_UNAVAILABLE, None),
  (100, 99, RtcEstimateRejectionReason.RTC_ROLLBACK, -1),
  (
    100,
    100 + MAX_RTC_ASSISTANCE_ELAPSED_SECONDS + 1,
    RtcEstimateRejectionReason.ELAPSED_TIME_ABOVE_MAXIMUM,
    MAX_RTC_ASSISTANCE_ELAPSED_SECONDS + 1,
  ),
])
def test_rtc_evaluator_rejection_reasons(
  rtc_anchor,
  current_rtc,
  reason,
  elapsed,
):
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 6, tzinfo=UTC),
    rtc_counter_seconds=rtc_anchor,
  )

  result = evaluate_utc_from_rtc(
    cache,
    current_rtc_seconds=current_rtc,
  )

  assert result == RtcEstimateRejection(reason, elapsed)


@pytest.mark.parametrize(
  ("supplied_rtc", "expected_reads"),
  [pytest.param("omitted", 1, id="omitted"), (None, 0), (100, 0)],
)
def test_rtc_evaluator_read_semantics(
  monkeypatch,
  supplied_rtc,
  expected_reads,
):
  reads = 0

  def read_rtc():
    nonlocal reads
    reads += 1
    return 100

  monkeypatch.setattr(
    "openpilot.system.ubloxd.gps_assistance.read_rtc_counter_seconds",
    read_rtc,
  )
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 6, tzinfo=UTC),
    rtc_counter_seconds=100,
  )

  result = (
    evaluate_utc_from_rtc(cache)
    if supplied_rtc == "omitted"
    else evaluate_utc_from_rtc(
      cache,
      current_rtc_seconds=supplied_rtc,
    )
  )

  assert reads == expected_reads
  if supplied_rtc is None:
    assert result == RtcEstimateRejection(
      RtcEstimateRejectionReason.CURRENT_RTC_UNAVAILABLE
    )
  else:
    assert isinstance(result, RtcEstimateSuccess)


@pytest.mark.parametrize(
  "elapsed_seconds",
  [0, MAX_RTC_ASSISTANCE_ELAPSED_SECONDS],
)
def test_rtc_evaluator_accepts_elapsed_boundaries(elapsed_seconds):
  saved_at = datetime(2026, 7, 6, tzinfo=UTC)
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=saved_at,
    rtc_counter_seconds=100,
  )

  result = evaluate_utc_from_rtc(
    cache,
    current_rtc_seconds=100 + elapsed_seconds,
  )

  assert isinstance(result, RtcEstimateSuccess)
  assert result.elapsed_seconds == elapsed_seconds
  assert result.estimated_utc == saved_at + timedelta(
    seconds=elapsed_seconds
  )
  expected_drift = (
    elapsed_seconds * RTC_DRIFT_PARTS_PER_MILLION
    + 999_999
  ) // 1_000_000
  assert result.uncertainty_seconds == min(
    65_535,
    RTC_BASE_TIME_UNCERTAINTY_SECONDS + expected_drift,
  )


def test_rtc_evaluator_rejects_estimate_after_supported_maximum():
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 6, tzinfo=UTC),
    rtc_counter_seconds=0,
  )

  # Exceed representable GPS UTC ceiling (PR82); former 700e6s from 2026 no longer past MAX_DATE.
  elapsed = 40_000_000_000
  result = evaluate_utc_from_rtc(
    cache,
    current_rtc_seconds=elapsed,
    max_elapsed_seconds=elapsed,
  )

  assert result == RtcEstimateRejection(
    RtcEstimateRejectionReason.UTC_AFTER_SUPPORTED_MAXIMUM,
    elapsed,
  )


@pytest.mark.parametrize(
  ("supplied_rtc", "expected_reads"),
  [pytest.param("omitted", 1, id="omitted"), (None, 1), (100, 0)],
)
def test_rtc_estimate_compatibility_read_semantics(
  monkeypatch,
  supplied_rtc,
  expected_reads,
):
  reads = 0

  def read_rtc():
    nonlocal reads
    reads += 1
    return 100

  monkeypatch.setattr(
    "openpilot.system.ubloxd.gps_assistance.read_rtc_counter_seconds",
    read_rtc,
  )
  saved_at = datetime(2026, 7, 6, tzinfo=UTC)
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=saved_at,
    rtc_counter_seconds=100,
  )

  result = (
    estimate_utc_from_rtc(cache)
    if supplied_rtc == "omitted"
    else estimate_utc_from_rtc(
      cache,
      current_rtc_seconds=supplied_rtc,
    )
  )

  assert reads == expected_reads
  assert result == (saved_at, RTC_BASE_TIME_UNCERTAINTY_SECONDS)


def test_rtc_evaluator_rejects_malformed_input_without_raising():
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 6, tzinfo=UTC),
    rtc_counter_seconds=100,
  )

  assert evaluate_utc_from_rtc(
    cache,
    current_rtc_seconds="invalid",  # type: ignore[arg-type]
  ) == RtcEstimateRejection(RtcEstimateRejectionReason.INVALID_RTC_ESTIMATE)


@pytest.mark.parametrize(("rtc_anchor", "current_rtc"), [
  (None, 100),
  (100, None),
  (100, 99),
  (100, 201),
])
def test_rtc_estimate_compatibility_rejections(
  monkeypatch,
  rtc_anchor,
  current_rtc,
):
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 6, tzinfo=UTC),
    rtc_counter_seconds=rtc_anchor,
  )
  monkeypatch.setattr(
    "openpilot.system.ubloxd.gps_assistance.read_rtc_counter_seconds",
    lambda: current_rtc,
  )

  assert estimate_utc_from_rtc(
    cache,
    max_elapsed_seconds=100,
  ) is None


def test_rtc_anchor_rejects_counter_rollback():
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 6, tzinfo=UTC),
    rtc_counter_seconds=24_000,
  )

  assert estimate_utc_from_rtc(
    cache,
    current_rtc_seconds=23_999,
  ) is None


def test_rtc_anchor_rejects_excessive_elapsed_time():
  cache = create_cache(
    receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix=reliable_fix(),
    database_frames=(build_dbd_frame(1),),
    saved_at_utc=datetime(2026, 7, 6, tzinfo=UTC),
    rtc_counter_seconds=24_000,
  )

  assert estimate_utc_from_rtc(
    cache,
    current_rtc_seconds=24_101,
    max_elapsed_seconds=100,
  ) is None


def test_time_assistance_encodes_supplied_accuracy():
  message = build_time_assistance_message(
    datetime(2026, 7, 6, 17, 45, 16, tzinfo=UTC),
    accuracy_seconds=123,
  )

  payload = message[6:-2]
  assert struct.unpack_from("<H", payload, 16)[0] == 123


def startup_ready_quality() -> NavigationQuality:
  return NavigationQuality(
    quality_version=1,
    policy_version=1,
    capture_context="onroad",
    continuous_reliable_fix_seconds=60.0,
    continuous_orbit_quality_seconds=10.0,
    gps_satellites_known=14,
    glonass_satellites_known=10,
    gps_ephemeris_available=5,
    glonass_ephemeris_available=6,
    satellites_used=9,
    gps_almanac_available=12,
    glonass_almanac_available=10,
    assistnow_offline_available=4,
    orbit_source_counts={"ephemeris": 24},
  )


@pytest.mark.parametrize(
  (
    "age_seconds",
    "expected_gps_ephemeris",
    "expected_glonass_ephemeris",
    "expected_startup_ready",
    "expected_gps_fresh",
    "expected_glonass_fresh",
    "expected_reasons",
  ),
  (
    (
      GLONASS_EPHEMERIS_FRESHNESS_SECONDS - 60,
      5,
      6,
      True,
      True,
      True,
      (),
    ),
    (
      GLONASS_EPHEMERIS_FRESHNESS_SECONDS,
      5,
      6,
      True,
      True,
      True,
      (),
    ),
    (
      GLONASS_EPHEMERIS_FRESHNESS_SECONDS + 1,
      5,
      0,
      False,
      True,
      False,
      ("glonass_ephemeris_expired",),
    ),
    (
      GPS_EPHEMERIS_FRESHNESS_SECONDS - 60,
      5,
      0,
      False,
      True,
      False,
      ("glonass_ephemeris_expired",),
    ),
    (
      GPS_EPHEMERIS_FRESHNESS_SECONDS,
      5,
      0,
      False,
      True,
      False,
      ("glonass_ephemeris_expired",),
    ),
    (
      GPS_EPHEMERIS_FRESHNESS_SECONDS + 1,
      0,
      0,
      False,
      False,
      False,
      (
        "gps_ephemeris_expired",
        "glonass_ephemeris_expired",
      ),
    ),
  ),
)
def test_effective_restored_quality_uses_constellation_freshness(
  age_seconds,
  expected_gps_ephemeris,
  expected_glonass_ephemeris,
  expected_startup_ready,
  expected_gps_fresh,
  expected_glonass_fresh,
  expected_reasons,
):
  saved_at = datetime(2026, 7, 23, 10, tzinfo=UTC)
  result = effective_restored_navigation_quality(
    startup_ready_quality(),
    saved_at,
    saved_at + timedelta(seconds=age_seconds),
    CacheAgeEvidence.TRUSTED_UTC,
  )

  assert result.cache_age_seconds == age_seconds
  assert result.age_verified
  assert result.captured_gps_ephemeris_available == 5
  assert result.captured_glonass_ephemeris_available == 6
  assert result.captured_gps_startup_ready
  assert (
    result.effective_gps_ephemeris_available
    == expected_gps_ephemeris
  )
  assert (
    result.effective_glonass_ephemeris_available
    == expected_glonass_ephemeris
  )
  assert result.effective_gps_startup_ready is expected_startup_ready
  assert result.gps_ephemeris_fresh is expected_gps_fresh
  assert (
    result.glonass_ephemeris_fresh
    is expected_glonass_fresh
  )
  assert result.expiration_reasons == expected_reasons


@pytest.mark.parametrize(
  ("trusted_now", "age_evidence", "expected_reason"),
  (
    (
      None,
      CacheAgeEvidence.UNVERIFIED,
      "cache_age_unverified",
    ),
    (
      datetime(2026, 7, 23, 9, tzinfo=UTC),
      CacheAgeEvidence.TRUSTED_UTC,
      "cache_timestamp_in_future",
    ),
  ),
)
def test_effective_restored_quality_fails_closed_without_valid_age(
  trusted_now,
  age_evidence,
  expected_reason,
):
  result = effective_restored_navigation_quality(
    startup_ready_quality(),
    datetime(2026, 7, 23, 10, tzinfo=UTC),
    trusted_now,
    age_evidence,
  )

  assert result.cache_age_seconds is None
  assert not result.age_verified
  assert result.captured_gps_ephemeris_available == 5
  assert result.captured_glonass_ephemeris_available == 6
  assert result.captured_gps_startup_ready
  assert result.effective_gps_ephemeris_available == 0
  assert result.effective_glonass_ephemeris_available == 0
  assert not result.effective_gps_startup_ready
  assert result.gps_ephemeris_fresh is False
  assert result.glonass_ephemeris_fresh is False
  assert result.expiration_reasons == (expected_reason,)


def test_refresh_restored_quality_uses_late_trusted_time_without_mutating_capture():
  saved_at = datetime(2026, 7, 23, 10, tzinfo=UTC)
  initial = effective_restored_navigation_quality(
    startup_ready_quality(),
    saved_at,
    None,
    CacheAgeEvidence.UNVERIFIED,
  )
  captured = (
    initial.captured_gps_ephemeris_available,
    initial.captured_glonass_ephemeris_available,
    initial.captured_gps_startup_ready,
  )

  refreshed = refresh_restored_navigation_quality(
    initial,
    saved_at,
    saved_at + timedelta(minutes=30),
    CacheAgeEvidence.TRUSTED_UTC,
  )

  assert captured == (
    refreshed.captured_gps_ephemeris_available,
    refreshed.captured_glonass_ephemeris_available,
    refreshed.captured_gps_startup_ready,
  )
  assert refreshed.cache_age_seconds == 30 * 60
  assert refreshed.age_verified
  assert refreshed.effective_gps_ephemeris_available == 5
  assert refreshed.effective_glonass_ephemeris_available == 6
  assert refreshed.effective_gps_startup_ready
  assert refreshed.gps_ephemeris_fresh
  assert refreshed.glonass_ephemeris_fresh
  assert refreshed.expiration_reasons == ()


def test_effective_restored_quality_preserves_legacy_unknowns():
  result = effective_restored_navigation_quality(
    None,
    datetime(2026, 7, 23, 10, tzinfo=UTC),
    datetime(2026, 7, 23, 11, tzinfo=UTC),
    CacheAgeEvidence.TRUSTED_UTC,
  )

  assert result.cache_age_seconds == 3600
  assert result.age_verified
  assert result.captured_gps_ephemeris_available is None
  assert result.effective_gps_ephemeris_available is None
  assert result.effective_gps_startup_ready is None
  assert result.gps_ephemeris_fresh is None
  assert result.glonass_ephemeris_fresh is None
  assert result.expiration_reasons == ()
