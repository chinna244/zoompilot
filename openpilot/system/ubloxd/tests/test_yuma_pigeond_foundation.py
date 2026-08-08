from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import (
  CaptureQualityTracker,
  NavPvtFix,
  NavSatQuality,
  add_ubx_checksum,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
  TrustedTimeSource,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AuthorizedTime,
  TimeAuthorizationEvidence,
)


SAVED_AT = datetime(2026, 7, 21, 10, tzinfo=UTC)


def network_host_observation():
  return pigeond.HostTimeObservation(
    utc=datetime(2026, 7, 23, 12, tzinfo=UTC),
    observed_boottime_seconds=100.0,
    uncertainty_seconds=30.0,
    source=pigeond.HostTimeSource.NETWORK_SYNCHRONIZED,
    independent=True,
    generation="network:test",
  )


def database_frame() -> bytes:
  payload = b"database"
  return add_ubx_checksum(b"\xb5\x62\x13\x80" + len(payload).to_bytes(2, "little") + payload)


def test_restore_result_exposes_selected_cache_timestamp(
  monkeypatch,
):
  cache = SimpleNamespace(
    saved_at_utc=SAVED_AT,
    rtc_counter_seconds=1_000,
    quality=SimpleNamespace(
      gps_almanac_available=10,
      glonass_almanac_available=9,
      gps_ephemeris_available=0,
      glonass_ephemeris_available=5,
      satellites_used=5,
      gps_startup_ready=False,
      gps_almanac_satellite_ids=tuple(range(1, 11)),
    ),
    database_frames=(database_frame(),),
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    position_accuracy_cm=1_000,
  )
  selection = SimpleNamespace(
    generation="primary",
    reason="test_selection",
    cache=cache,
  )
  valid_state = SimpleNamespace(name="VALID")
  inventory = SimpleNamespace(
    primary=SimpleNamespace(state=valid_state, cache=cache),
    previous=SimpleNamespace(state=valid_state, cache=None),
  )

  class Store:
    def __init__(self, path, loader):
      pass

    def remove_stale_candidate(self):
      return None

    def select_best(
      self,
      receiver_fingerprint,
      trusted_now,
      *,
      age_evidence,
    ):
      return selection, inventory

  monkeypatch.setattr(pigeond, "NavigationCacheStore", Store)
  monkeypatch.setattr(
    pigeond,
    "send_mga_with_strict_ack",
    lambda *args, **kwargs: None,
  )

  result = pigeond.restore_navigation_assistance(
    object(),
    "receiver",
    trusted_now=datetime(2026, 7, 21, 12, tzinfo=UTC),
    time_assistance_source="synchronized",
    allow_legacy_direct_restore=True,
  )

  assert result.status is pigeond.NavigationAssistanceRestoreStatus.COMPLETE
  assert result.cache_saved_at_utc == SAVED_AT
  assert result.restored_cache_generation == "primary"
  assert result.restored_cache_selection_reason == "test_selection"
  assert result.restored_cache_age_seconds == 2 * 60 * 60
  assert result.restored_cache_age_evidence == "trusted_utc"
  assert result.restored_cache_age_verified
  assert result.captured_gps_ephemeris_available == 0
  assert result.captured_glonass_ephemeris_available == 5
  assert result.captured_gps_startup_ready is False
  assert result.restored_gps_ephemeris_fresh
  assert not result.restored_glonass_ephemeris_fresh
  assert result.restored_quality_expiration_reasons == ("glonass_ephemeris_expired",)
  assert result.restored_navigation_quality is not None
  assert result.restored_gps_almanac_available == 10
  assert result.restored_glonass_almanac_available == 9
  assert result.restored_gps_ephemeris_available == 0
  assert result.restored_glonass_ephemeris_available == 0
  assert result.restored_satellites_used == 5
  assert result.restored_gps_startup_ready is False
  assert result.restored_gps_almanac_satellite_ids == tuple(range(1, 11))

  summary = pigeond.format_navigation_assistance_restore_summary(
    result,
    attempted=True,
    time_assistance_source="network_synchronized",
  )
  assert "restored_cache_age_seconds=7200.0" in summary
  assert "restored_cache_age_evidence=trusted_utc" in summary
  assert "restored_cache_age_verified=true" in summary
  assert "captured_glonass_ephemeris_available=5" in summary
  assert "restored_glonass_ephemeris_available=0" in summary
  assert "restored_gps_ephemeris_fresh=True" in summary
  assert "restored_glonass_ephemeris_fresh=False" in summary
  assert "glonass_ephemeris_expired" in summary

  runtime = pigeond.create_yuma_supplementation_runtime(
    SimpleNamespace(
      navigation_assistance_restore_result=result,
      completed_at=100.0,
      yuma_time_anchor_utc=None,
      yuma_time_anchor_source=None,
      yuma_time_anchor_monotonic=None,
    )
  )
  assert runtime.restored_navigation_quality is result.restored_navigation_quality


def test_restore_result_downgrades_stale_captured_startup_quality(
  monkeypatch,
):
  cache = SimpleNamespace(
    saved_at_utc=SAVED_AT,
    rtc_counter_seconds=1_000,
    quality=SimpleNamespace(
      gps_almanac_available=31,
      glonass_almanac_available=10,
      gps_ephemeris_available=5,
      glonass_ephemeris_available=6,
      satellites_used=9,
      gps_startup_ready=True,
      gps_almanac_satellite_ids=None,
    ),
    database_frames=(database_frame(),),
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    position_accuracy_cm=1_000,
  )
  selection = SimpleNamespace(
    generation="previous",
    reason="stale_but_eligible",
    cache=cache,
  )
  valid_state = SimpleNamespace(name="VALID")
  inventory = SimpleNamespace(
    primary=SimpleNamespace(state=valid_state, cache=None),
    previous=SimpleNamespace(state=valid_state, cache=cache),
  )

  class Store:
    def __init__(self, path, loader):
      pass

    def remove_stale_candidate(self):
      return None

    def select_best(
      self,
      receiver_fingerprint,
      trusted_now,
      *,
      age_evidence,
    ):
      return selection, inventory

  monkeypatch.setattr(pigeond, "NavigationCacheStore", Store)
  monkeypatch.setattr(
    pigeond,
    "send_mga_with_strict_ack",
    lambda *args, **kwargs: None,
  )

  result = pigeond.restore_navigation_assistance(
    object(),
    "receiver",
    trusted_now=SAVED_AT + timedelta(hours=16, minutes=47),
    time_assistance_source="network_synchronized",
    allow_legacy_direct_restore=True,
  )

  assert result.captured_gps_ephemeris_available == 5
  assert result.captured_glonass_ephemeris_available == 6
  assert result.captured_gps_startup_ready
  assert result.restored_gps_ephemeris_available == 0
  assert result.restored_glonass_ephemeris_available == 0
  assert not result.restored_gps_startup_ready
  assert not result.restored_gps_ephemeris_fresh
  assert not result.restored_glonass_ephemeris_fresh
  assert result.restored_quality_expiration_reasons == (
    "gps_ephemeris_expired",
    "glonass_ephemeris_expired",
  )
  assert result.restored_navigation_quality is not None


def test_initialize_receiver_cycle_preserves_restore_result(
  monkeypatch,
):
  restore_result = pigeond.NavigationAssistanceRestoreResult(
    status=pigeond.NavigationAssistanceRestoreStatus.COMPLETE,
    total_frame_count=1,
    accepted_frame_count=1,
    cache_saved_at_utc=SAVED_AT,
  )

  class Diagnostics:
    def start_cycle(self, reason, now):
      pass

    def time_assistance_context(self, now):
      return "test_context"

  monkeypatch.setattr(pigeond, "init", lambda pigeon: None)
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "log_navx5_ack_aiding_support",
    lambda info: None,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda pigeon, info: None,
  )
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    network_host_observation,
  )
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    lambda *args, **kwargs: True,
  )
  monkeypatch.setattr(
    pigeond,
    "restore_navigation_assistance",
    lambda *args, **kwargs: restore_result,
  )
  monkeypatch.setattr(
    pigeond,
    "log_assistnow_autonomous_support",
    lambda info: False,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_assistnow_autonomous",
    lambda pigeon, info: None,
  )

  initialization = pigeond.initialize_receiver_cycle(
    object(),
    "receiver",
    Diagnostics(),
    "process_start",
  )

  assert initialization.navigation_assistance_restore_result is restore_result


def _stub_receiver_initialization_dependencies(
  monkeypatch,
  *,
  trusted_time: bool,
  send_time_result: bool,
  rtc_assistance=None,
):
  class InlineThread:
    def __init__(self, *, target, **_kwargs):
      self.target = target

    def start(self):
      self.target()

  monkeypatch.setattr(pigeond, "Thread", InlineThread)
  monkeypatch.setattr(pigeond, "init", lambda pigeon: None)
  monkeypatch.setattr(pigeond, "poll_mon_ver", lambda pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "log_navx5_ack_aiding_support",
    lambda info: None,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_navx5_ack_aiding",
    lambda pigeon, info: None,
  )
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: network_host_observation() if trusted_time else None,
  )
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    lambda *args, **kwargs: send_time_result,
  )
  authorized = (
    AuthorizedTime(
      utc=datetime(2026, 7, 23, 13, 0, tzinfo=UTC),
      uncertainty_seconds=30.0,
      source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
      provenance=TimeProvenance.NETWORK_INDEPENDENT,
      independent=True,
      evidence=TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED,
      observed_boottime_seconds=100.0,
    )
    if trusted_time
    else None
  )
  monkeypatch.setattr(
    pigeond,
    "evaluate_time_authority",
    lambda *_args, **_kwargs: SimpleNamespace(authorized_time=authorized),
  )
  monkeypatch.setattr(
    pigeond,
    "cached_rtc_time_assistance",
    lambda receiver_fingerprint: rtc_assistance,
  )
  monkeypatch.setattr(
    pigeond,
    "log_assistnow_autonomous_support",
    lambda info: False,
  )
  monkeypatch.setattr(
    pigeond,
    "configure_assistnow_autonomous",
    lambda pigeon, info: None,
  )


class _Diagnostics:
  def start_cycle(self, reason, now):
    pass

  def time_assistance_context(self, now):
    return "test_context"


def test_synchronized_yuma_anchor_survives_receiver_time_ack_failure(
  monkeypatch,
):
  _stub_receiver_initialization_dependencies(
    monkeypatch,
    trusted_time=True,
    send_time_result=False,
  )
  restore_calls = []
  monkeypatch.setattr(
    pigeond,
    "restore_navigation_assistance",
    lambda *args, **kwargs: (
      restore_calls.append(kwargs)
      or pigeond.NavigationAssistanceRestoreResult(
        status=pigeond.NavigationAssistanceRestoreStatus.FAILED,
        total_frame_count=0,
        accepted_frame_count=0,
      )
    ),
  )

  initialization = pigeond.initialize_receiver_cycle(
    object(),
    "receiver",
    _Diagnostics(),
    "process_start",
  )
  runtime = pigeond.create_yuma_supplementation_runtime(initialization)

  assert not initialization.trusted_time_assistance_sent
  assert initialization.time_assistance_utc is None
  assert initialization.time_assistance_source is None
  assert initialization.yuma_time_anchor_utc is not None
  assert initialization.yuma_time_anchor_source == "system_synchronized"
  assert initialization.yuma_time_anchor_monotonic is not None
  assert runtime.time_anchor_source == "system_synchronized"
  assert restore_calls[0]["trusted_now"] == initialization.yuma_time_anchor_utc


def test_cross_boot_rtc_is_not_authorized_for_yuma(
  monkeypatch,
):
  rtc_utc = datetime(2026, 7, 21, 11, tzinfo=UTC)
  _stub_receiver_initialization_dependencies(
    monkeypatch,
    trusted_time=False,
    send_time_result=False,
    rtc_assistance=(rtc_utc, 60),
  )
  restore_calls = []
  monkeypatch.setattr(
    pigeond,
    "restore_navigation_assistance",
    lambda *args, **kwargs: (
      restore_calls.append(kwargs)
      or pigeond.NavigationAssistanceRestoreResult(
        status=pigeond.NavigationAssistanceRestoreStatus.FAILED,
        total_frame_count=0,
        accepted_frame_count=0,
      )
    ),
  )

  initialization = pigeond.initialize_receiver_cycle(
    object(),
    "receiver",
    _Diagnostics(),
    "process_start",
  )
  runtime = pigeond.create_yuma_supplementation_runtime(initialization)

  assert not initialization.trusted_time_assistance_sent
  assert initialization.time_assistance_utc is None
  assert initialization.time_assistance_source is None
  assert initialization.yuma_time_anchor_utc is None
  assert initialization.yuma_time_anchor_source is None
  assert initialization.yuma_time_anchor_monotonic is None
  assert runtime.time_anchor_source is None
  assert restore_calls == []
  assert initialization.poll_deferred_assistance_state is not None


def test_capture_tracker_exposes_latest_nav_sat_and_time():
  tracker = CaptureQualityTracker()
  report = NavSatQuality(
    4,
    0,
    0,
    0,
    0,
    2,
    0,
    0,
    {},
    gps_satellite_ids=frozenset((1, 2, 3, 4)),
    gps_healthy_satellite_ids=frozenset((1, 2, 4)),
    gps_almanac_satellite_ids=frozenset((1, 3)),
  )

  tracker.update_fix(
    NavPvtFix(
      fix_ok=True,
      satellites=4,
      latitude_e7=0,
      longitude_e7=0,
      altitude_cm=0,
      horizontal_accuracy_cm=1_000,
      vertical_accuracy_cm=2_000,
    ),
    12.0,
  )
  tracker.update_nav_sat(report, 12.5)
  quality = tracker.quality(12.5, "onroad")

  assert tracker.latest_nav_sat is report
  assert tracker.latest_nav_sat_time == 12.5
  assert quality is not None
  assert quality.gps_almanac_satellite_ids == (1, 3)

  tracker.reset()

  assert tracker.latest_nav_sat is None
  assert tracker.latest_nav_sat_time is None


def _mga_message() -> bytes:
  payload = bytes((0x01, 0x00, 0x00, 0x00))
  return add_ubx_checksum(b"\xb5\x62\x13\x40" + len(payload).to_bytes(2, "little") + payload)


def test_strict_mga_ack_classifies_receiver_write_failure(monkeypatch):
  monkeypatch.setattr(
    pigeond,
    "_begin_response_transaction",
    lambda pigeon, message: (_ for _ in ()).throw(OSError("injected write failure")),
  )

  with pytest.raises(pigeond.MgaWriteError) as raised:
    pigeond.send_mga_with_strict_ack(object(), _mga_message())

  assert raised.value.message_id == 0x40
  assert raised.value.message_type == 0x01


def test_strict_mga_ack_classifies_transaction_failure(monkeypatch):
  monkeypatch.setattr(
    pigeond,
    "_begin_response_transaction",
    lambda pigeon, message: object(),
  )
  monkeypatch.setattr(
    pigeond,
    "wait_for_matching_mga_ack",
    lambda *args, **kwargs: (_ for _ in ()).throw(pigeond.ResponseTransactionError("injected transaction failure")),
  )

  with pytest.raises(pigeond.MgaTransactionError) as raised:
    pigeond.send_mga_with_strict_ack(object(), _mga_message())

  assert raised.value.message_id == 0x40
  assert raised.value.message_type == 0x01
  assert raised.value.write_succeeded


def test_strict_mga_ack_does_not_wrap_programming_type_error(
  monkeypatch,
):
  monkeypatch.setattr(
    pigeond,
    "_begin_response_transaction",
    lambda pigeon, message: (_ for _ in ()).throw(TypeError("injected programming failure")),
  )

  with pytest.raises(TypeError, match="programming failure"):
    pigeond.send_mga_with_strict_ack(object(), _mga_message())


def test_strict_mga_ack_classifies_timeout(monkeypatch):
  monkeypatch.setattr(
    pigeond,
    "_begin_response_transaction",
    lambda pigeon, message: object(),
  )
  monkeypatch.setattr(
    pigeond,
    "wait_for_matching_mga_ack",
    lambda *args, **kwargs: None,
  )

  with pytest.raises(TimeoutError):
    pigeond.send_mga_with_strict_ack(object(), _mga_message())


def test_strict_mga_ack_preserves_raised_timeout(monkeypatch):
  monkeypatch.setattr(
    pigeond,
    "_begin_response_transaction",
    lambda pigeon, message: object(),
  )
  monkeypatch.setattr(
    pigeond,
    "wait_for_matching_mga_ack",
    lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("injected ACK timeout")),
  )

  with pytest.raises(TimeoutError, match="injected ACK timeout"):
    pigeond.send_mga_with_strict_ack(object(), _mga_message())


def test_strict_mga_ack_classifies_receiver_nack(monkeypatch):
  acknowledgment = SimpleNamespace(
    accepted=False,
    acknowledgment_type=1,
    version=0,
    info_code=1,
    message_id=0x40,
  )
  monkeypatch.setattr(
    pigeond,
    "_begin_response_transaction",
    lambda pigeon, message: object(),
  )
  monkeypatch.setattr(
    pigeond,
    "wait_for_matching_mga_ack",
    lambda *args, **kwargs: acknowledgment,
  )

  with pytest.raises(pigeond.MgaReceiverNackError) as raised:
    pigeond.send_mga_with_strict_ack(object(), _mga_message())

  assert raised.value.message_id == 0x40
  assert raised.value.message_type == 0x01
  assert raised.value.ack_type == 1
  assert raised.value.ack_version == 0
  assert raised.value.info_code == 1
  assert raised.value.rejected_message_id == 0x40


def test_strict_mga_ack_keeps_malformed_message_nonretryable():
  with pytest.raises(
    pigeond.CacheValidationError,
    match="truncated",
  ) as raised:
    pigeond.send_mga_with_strict_ack(object(), b"\xb5\x62")

  assert not isinstance(
    raised.value,
    (
      pigeond.MgaReceiverNackError,
      pigeond.MgaTransactionError,
      pigeond.MgaWriteError,
      TimeoutError,
    ),
  )


@pytest.mark.parametrize(
  ("completion_monotonic", "gnss_start_sent_at", "expected"),
  (
    (0.5, 1.0, True),
    (2.0, 1.0, False),
    (None, 1.0, None),
  ),
)
def test_yuma_outcome_explicitly_compares_completion_to_gnss_start(
  completion_monotonic,
  gnss_start_sent_at,
  expected,
):
  feature = object.__new__(pigeond.YumaSupplementationFeature)
  feature._receiver_cycle = 3
  feature._initialization = SimpleNamespace(
    gnss_start_sent_at=gnss_start_sent_at
  )
  outcome = pigeond.YumaSupplementationRuntimeOutcome(
    plan=SimpleNamespace(
      reason=pigeond.YumaSupplementationReason.FEATURE_DISABLED
    ),
    completion_monotonic=completion_monotonic,
  )

  contextualized = feature._contextualize_outcome(outcome)

  assert (
    contextualized.gnss_start_sent_at_monotonic
    == gnss_start_sent_at
  )
  assert contextualized.completed_before_gnss_start is expected


def test_yuma_log_labels_captured_and_effective_quality(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  outcome = pigeond.YumaSupplementationRuntimeOutcome(
    plan=SimpleNamespace(
      action=SimpleNamespace(value="send_missing"),
      reason=SimpleNamespace(value="database_restore_incomplete"),
      satellite_ids=frozenset((1, 2)),
      unavailable_satellite_ids=frozenset(),
    ),
    restored_cache_age_evidence="trusted_utc",
    restored_cache_age_verified=True,
    captured_gps_ephemeris_available=6,
    captured_glonass_ephemeris_available=8,
    captured_gps_startup_ready=True,
    restored_gps_ephemeris_available=6,
    restored_glonass_ephemeris_available=8,
    restored_gps_startup_ready=True,
    completion_monotonic=2.757,
    gnss_start_sent_at_monotonic=1.310,
    completed_before_gnss_start=False,
    terminal=True,
  )

  pigeond.log_yuma_supplementation_outcome(outcome)

  message = logs[-1]
  assert "quality_evaluation_stage=yuma_runtime" in message
  assert "restored_cache_age_evidence=trusted_utc" in message
  assert "captured_gps_ephemeris_available=6" in message
  assert "captured_glonass_ephemeris_available=8" in message
  assert "effective_gps_ephemeris_available=6" in message
  assert "effective_glonass_ephemeris_available=8" in message
  assert "gnss_start_sent_at_monotonic=1.31" in message
  assert "completion_monotonic=2.757" in message
  assert "captured_gps_startup_ready=True" in message
  assert "effective_gps_startup_ready=True" in message
  assert "completed_before_gnss_start=False" in message


def test_receiver_utc_anchor_accepts_fresh_unreliable_nav_pvt():
  fix = NavPvtFix(
    fix_ok=False,
    satellites=0,
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    horizontal_accuracy_cm=1_000_000,
    vertical_accuracy_cm=1_000_000,
    utc_time=datetime(2026, 7, 22, 12, tzinfo=UTC),
  )
  diagnostics = SimpleNamespace(
    latest_fix=fix,
    latest_fix_time=257.0,
  )

  assert pigeond.fresh_receiver_utc_time_anchor(
    diagnostics,
    258.0,
  ) == (fix.utc_time, 257.0)
  assert (
    pigeond.fresh_receiver_utc_time_anchor(
      diagnostics,
      260.0,
    )
    is None
  )


def test_startup_diagnostics_resets_receiver_utc_anchor_state(
  monkeypatch,
):
  monkeypatch.setattr(pigeond.cloudlog, "info", lambda message: None)
  diagnostics = pigeond.GpsStartupDiagnostics(10.0)
  fix = NavPvtFix(
    fix_ok=False,
    satellites=0,
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    horizontal_accuracy_cm=1_000_000,
    vertical_accuracy_cm=1_000_000,
    utc_time=datetime(2026, 7, 22, 12, tzinfo=UTC),
  )

  diagnostics.note_nav_pvt(fix, 257.0)

  assert diagnostics.latest_fix is fix
  assert diagnostics.latest_fix_time == 257.0

  diagnostics.start_cycle("reset", 300.0)

  assert diagnostics.latest_fix is None
  assert diagnostics.latest_fix_time is None


def test_receiver_utc_anchor_handles_diagnostics_without_nav_pvt_state():
  assert (
    pigeond.fresh_receiver_utc_time_anchor(
      SimpleNamespace(),
      100.0,
    )
    is None
  )
