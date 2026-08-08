from datetime import UTC, datetime
import struct

from openpilot.system.ubloxd.gps_assistance import (
  NavPvtFix,
  add_ubx_checksum,
  build_time_assistance_message,
)
from openpilot.system.ubloxd.receiver_time_provenance import (
  ReceiverTimeProvenanceTracker,
  ReceiverUtcClassification,
  is_mga_time_assistance_message,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
)


UTC_NOW = datetime(2026, 7, 22, 21, tzinfo=UTC)


def fix(
  *,
  utc_time: datetime | None = UTC_NOW,
  valid_date: bool = True,
  valid_time: bool = True,
  fully_resolved: bool = True,
  time_accuracy_ns: int | None = 25_000_000,
) -> NavPvtFix:
  return NavPvtFix(
    fix_ok=False,
    satellites=0,
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    horizontal_accuracy_cm=100_000,
    vertical_accuracy_cm=100_000,
    utc_time=utc_time,
    valid_date=valid_date,
    valid_time=valid_time,
    fully_resolved=fully_resolved,
    time_accuracy_ns=time_accuracy_ns,
  )


def rawx_frame(
  *,
  week: int = 2_429,
  leap_second_valid: bool = True,
  measurement_count: int = 1,
) -> bytes:
  payload = struct.pack(
    "<dHbBB3s",
    0.0,
    week,
    18,
    measurement_count,
    int(leap_second_valid),
    b"\x00\x00\x00",
  )
  for _ in range(measurement_count):
    payload += struct.pack(
      "<ddfBBBBHBBBBBB",
      0.0,
      0.0,
      0.0,
      0,
      1,
      0,
      0,
      0,
      35,
      0,
      0,
      0,
      0,
      0,
    )
  return add_ubx_checksum(
    b"\xB5\x62\x02\x15"
    + len(payload).to_bytes(2, "little")
    + payload
  )


def tracker() -> ReceiverTimeProvenanceTracker:
  value = ReceiverTimeProvenanceTracker()
  value.start_cycle(1, 100.0)
  return value


def test_unassisted_receiver_utc_requires_fresh_rawx_evidence():
  value = tracker()
  value.note_nav_pvt(fix(), 101.0)

  observation = value.current_observation(101.0)

  assert observation.classification is (
    ReceiverUtcClassification.UNASSISTED_UNCONFIRMED
  )
  assert observation.reason == "nonempty_rawx_unavailable"
  assert not observation.independent


def test_fresh_unassisted_receiver_utc_is_independently_gnss_resolved():
  value = tracker()
  value.note_rawx(rawx_frame(), 101.0)
  value.note_nav_pvt(fix(), 101.1)

  observation = value.current_observation(101.2)

  assert observation.classification is (
    ReceiverUtcClassification.UNASSISTED_GNSS
  )
  assert observation.reason == "fresh_gnss_time_evidence"
  assert observation.utc == UTC_NOW
  assert observation.independent
  assert observation.rawx_measurement_count == 1
  assert observation.gps_week_valid
  assert observation.leap_second_valid


def test_written_time_assistance_prevents_circular_confirmation():
  value = tracker()
  value.note_time_assistance_written(
    source="same_boot_boottime",
    assistance_utc=UTC_NOW,
    uncertainty_seconds=31.0,
    now=100.5,
  )
  value.note_rawx(rawx_frame(), 101.0)
  value.note_nav_pvt(fix(), 101.1)

  observation = value.current_observation(101.2)

  assert observation.classification is (
    ReceiverUtcClassification.ASSISTED
  )
  assert observation.reason == "time_assistance_written_in_cycle"
  assert observation.time_assistance_source == (
    "same_boot_boottime"
  )
  assert not observation.independent


def test_rawx_week_and_leap_evidence_are_both_required():
  for frame, expected_reason in (
    (
      rawx_frame(week=0),
      "gps_week_invalid",
    ),
    (
      rawx_frame(leap_second_valid=False),
      "leap_second_invalid",
    ),
  ):
    value = tracker()
    value.note_rawx(frame, 101.0)
    value.note_nav_pvt(fix(), 101.1)

    observation = value.current_observation(101.2)

    assert observation.classification is (
      ReceiverUtcClassification.UNASSISTED_UNCONFIRMED
    )
    assert observation.reason == expected_reason
    assert not observation.independent


def test_empty_or_stale_rawx_does_not_confirm_receiver_utc():
  empty = tracker()
  empty.note_rawx(rawx_frame(measurement_count=0), 101.0)
  empty.note_nav_pvt(fix(), 101.1)

  assert (
    empty.current_observation(101.2).reason
    == "nonempty_rawx_unavailable"
  )

  stale = tracker()
  stale.note_rawx(rawx_frame(), 101.0)
  stale.note_nav_pvt(fix(), 104.0)

  assert (
    stale.current_observation(104.0).reason
    == "nonempty_rawx_stale"
  )


def test_nav_pvt_validity_and_accuracy_gates_fail_closed():
  cases = (
    (fix(valid_date=False), "valid_date_false"),
    (fix(valid_time=False), "valid_time_false"),
    (fix(fully_resolved=False), "fully_resolved_false"),
    (
      fix(time_accuracy_ns=None),
      "time_accuracy_unavailable",
    ),
    (
      fix(time_accuracy_ns=1_000_000_001),
      "time_accuracy_above_limit",
    ),
  )
  for candidate, expected_reason in cases:
    value = tracker()
    value.note_rawx(rawx_frame(), 101.0)
    value.note_nav_pvt(candidate, 101.1)

    observation = value.current_observation(101.2)

    assert observation.classification is (
      ReceiverUtcClassification.UNASSISTED_UNCONFIRMED
    )
    assert observation.reason == expected_reason
    assert not observation.independent


def test_precycle_frame_timestamps_are_rejected():
  value = tracker()
  value.note_rawx(rawx_frame(), 99.0)
  value.note_nav_pvt(fix(), 99.0)

  observation = value.current_observation(100.0)

  assert observation.classification is (
    ReceiverUtcClassification.UNAVAILABLE
  )
  assert observation.reason == "nav_pvt_unavailable"


def test_receiver_cycle_reset_clears_assistance_and_gnss_evidence():
  value = tracker()
  value.note_time_assistance_written(
    source="system_synchronized",
    assistance_utc=UTC_NOW,
    uncertainty_seconds=30.0,
    now=100.5,
  )
  value.note_rawx(rawx_frame(), 101.0)
  value.note_nav_pvt(fix(), 101.1)

  value.start_cycle(2, 200.0)
  value.note_nav_pvt(fix(), 200.1)
  observation = value.current_observation(200.1)

  assert observation.cycle_id == 2
  assert not observation.time_assistance_written
  assert observation.reason == "nonempty_rawx_unavailable"



def test_initialization_ignores_receiver_frames_but_keeps_assistance():
  value = ReceiverTimeProvenanceTracker()
  value.start_cycle(
    1,
    100.0,
    observations_enabled=False,
  )
  value.note_rawx(rawx_frame(), 100.1)
  value.note_nav_pvt(fix(), 100.2)
  value.note_time_assistance_written(
    source="assistnow_online",
    assistance_utc=None,
    uncertainty_seconds=None,
    now=100.3,
  )

  before = value.current_observation(100.4)
  assert before.reason == "nav_pvt_unavailable"
  assert before.time_assistance_written

  value.enable_receiver_observations(100.5)
  value.note_rawx(rawx_frame(), 100.6)
  value.note_nav_pvt(fix(), 100.7)
  after = value.current_observation(100.8)

  assert after.classification is ReceiverUtcClassification.ASSISTED
  assert after.reason == "time_assistance_written_in_cycle"
  assert after.time_assistance_source == "assistnow_online"

def test_changed_observation_reports_only_meaningful_transitions():
  value = tracker()

  first = value.changed_observation(100.0)
  repeated = value.changed_observation(100.1)
  value.note_nav_pvt(fix(), 100.2)
  changed = value.changed_observation(100.2)

  assert first is not None
  assert repeated is None
  assert changed is not None
  assert changed.classification is (
    ReceiverUtcClassification.UNASSISTED_UNCONFIRMED
  )


def test_mga_time_message_detection_is_specific():
  time_message = build_time_assistance_message(UTC_NOW)
  other = bytearray(time_message)
  other[6] = 0x01

  assert is_mga_time_assistance_message(time_message)
  assert not is_mga_time_assistance_message(bytes(other))
  assert not is_mga_time_assistance_message(b"")


def test_assistance_observation_records_comparison_evidence():
  value = tracker()
  value.note_time_assistance_written(
    source="same_boot_boottime",
    assistance_utc=UTC_NOW,
    uncertainty_seconds=31.0,
    now=100.5,
    written_boottime_seconds=50.0,
    independent=False,
    provenance=TimeProvenance.EXTERNAL_OR_UNKNOWN,
  )

  observation = value.time_assistance_observation
  assert observation.cycle_id == 1
  assert observation.written
  assert observation.source == "same_boot_boottime"
  assert observation.utc == UTC_NOW
  assert observation.uncertainty_seconds == 31.0
  assert observation.written_at == 100.5
  assert observation.written_boottime_seconds == 50.0
  assert observation.independent is False
  assert observation.provenance is (
    TimeProvenance.EXTERNAL_OR_UNKNOWN
  )
  assert not observation.correction_written


def test_correction_updates_assistance_and_is_bounded_per_cycle():
  value = tracker()
  value.note_time_assistance_written(
    source="same_boot_boottime",
    assistance_utc=UTC_NOW,
    uncertainty_seconds=31.0,
    now=100.5,
    written_boottime_seconds=50.0,
    independent=False,
    provenance=TimeProvenance.EXTERNAL_OR_UNKNOWN,
  )
  corrected_utc = datetime(2026, 7, 22, 21, 0, 5, tzinfo=UTC)
  value.note_time_assistance_written(
    source="system_synchronized",
    assistance_utc=corrected_utc,
    uncertainty_seconds=30.0,
    now=101.0,
    written_boottime_seconds=55.0,
    independent=True,
    provenance=TimeProvenance.EXTERNAL_OR_UNKNOWN,
    correction=True,
  )

  observation = value.time_assistance_observation
  assert value.correction_written
  assert observation.correction_written
  assert observation.source == "system_synchronized"
  assert observation.utc == corrected_utc
  assert observation.written_boottime_seconds == 55.0
  assert observation.independent is True

  value.start_cycle(2, 200.0)
  reset = value.time_assistance_observation
  assert not value.correction_written
  assert not reset.written
  assert not reset.correction_written
