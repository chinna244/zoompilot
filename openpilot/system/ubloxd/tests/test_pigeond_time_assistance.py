from datetime import UTC, datetime
import struct
from types import SimpleNamespace

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import CachePromotionStage, RtcEstimateSuccess
from openpilot.system.ubloxd.rtc_time_observation import (
  CrossBootRtcObservation,
  RtcObservationCandidate,
  RtcObservationReason,
  RtcObservationState,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  TimeAuthorizationEvidence,
)


def network_host_observation(
  *,
  utc: datetime = datetime(2026, 7, 10, tzinfo=UTC),
  boottime: float = 100.0,
  generation: str = "network:1",
):
  return pigeond.HostTimeObservation(
    utc=utc,
    observed_boottime_seconds=boottime,
    uncertainty_seconds=30.0,
    source=pigeond.HostTimeSource.NETWORK_SYNCHRONIZED,
    independent=True,
    generation=generation,
  )


def build_mga_ack(
  message: bytes,
  *,
  acknowledgment_type: int = 1,
  version: int = 0,
  info_code: int = 0,
) -> bytes:
  payload = bytes(
    (
      acknowledgment_type,
      version,
      info_code,
      message[3],
    )
  ) + message[6:10].ljust(4, b"\x00")
  return pigeond.add_ubx_checksum(b"\xb5\x62\x13\x60" + len(payload).to_bytes(2, "little") + payload)


class FakePigeon:
  def __init__(self, *, auto_ack: bool = True):
    self.sent: list[bytes] = []
    self.responses: list[bytes] = []
    self.auto_ack = auto_ack

  def send(self, message: bytes) -> None:
    self.sent.append(message)
    if self.auto_ack:
      self.responses.append(build_mga_ack(message))

  def receive(self) -> bytes:
    return self.responses.pop(0) if self.responses else b""


def build_rawx_frame(
  *,
  week: int,
  leap_seconds_valid: bool,
  measurements: tuple[tuple[int, int], ...],
) -> bytes:
  payload = struct.pack(
    "<dHbBB3s",
    0.0,
    week,
    18,
    len(measurements),
    int(leap_seconds_valid),
    b"\x00\x00\x00",
  )
  for gnss_id, cno in measurements:
    payload += struct.pack(
      "<ddfBBBBHBBBBBB",
      0.0,
      0.0,
      0.0,
      gnss_id,
      1,
      0,
      0,
      0,
      cno,
      0,
      0,
      0,
      0,
      0,
    )

  return pigeond.add_ubx_checksum(b"\xb5\x62\x02\x15" + len(payload).to_bytes(2, "little") + payload)


def diagnostic_fix(
  *,
  fix_ok: bool = False,
  satellites: int = 0,
  horizontal_accuracy_cm: int = 100_000,
  utc_time: datetime | None = None,
) -> pigeond.NavPvtFix:
  return pigeond.NavPvtFix(
    fix_ok=fix_ok,
    satellites=satellites,
    latitude_e7=123_456_789,
    longitude_e7=-987_654_321,
    altitude_cm=7_654_321,
    horizontal_accuracy_cm=horizontal_accuracy_cm,
    vertical_accuracy_cm=horizontal_accuracy_cm,
    utc_time=utc_time,
  )


def run_navigation_restore_with_outcomes(
  monkeypatch,
  outcomes,
  *,
  frame_count=3,
  position_outcome=None,
  diagnostic_context=None,
):
  cache = SimpleNamespace(
    saved_at_utc=datetime(2026, 7, 10, tzinfo=UTC),
    rtc_counter_seconds=100,
    database_frames=tuple(bytes((index,)) for index in range(frame_count)),
    latitude_e7=0,
    longitude_e7=0,
    altitude_cm=0,
    position_accuracy_cm=1_000,
    quality=None,
  )
  calls = []
  sleeps = []
  logs = []

  def record_log(level):
    def append_log(message, *args, **kwargs):
      logs.append((level, message))

    return append_log

  def fake_send_mga_with_strict_ack(
    pigeon,
    message,
    timeout=pigeond.GPS_ASSISTANCE_ACK_TIMEOUT,
    database_frame_index=None,
  ):
    calls.append(database_frame_index)
    if database_frame_index is None:
      outcome = position_outcome
    else:
      frame_outcomes = outcomes.get(
        database_frame_index,
        [],
      )
      outcome = frame_outcomes.pop(0) if frame_outcomes else None

    if outcome is not None:
      raise outcome

  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: None,
  )
  monkeypatch.setattr(
    pigeond,
    "load_cache",
    lambda *args, **kwargs: cache,
  )
  monkeypatch.setattr(
    pigeond,
    "send_mga_with_strict_ack",
    fake_send_mga_with_strict_ack,
  )
  monkeypatch.setattr(pigeond.time, "sleep", sleeps.append)
  monkeypatch.setattr(pigeond.cloudlog, "info", record_log("info"))
  monkeypatch.setattr(
    pigeond.cloudlog,
    "warning",
    record_log("warning"),
  )
  monkeypatch.setattr(pigeond.cloudlog, "error", record_log("error"))

  result = pigeond.restore_navigation_assistance(
    object(),
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    diagnostic_context=diagnostic_context,
    trusted_now=datetime(2026, 7, 10, 0, 5, tzinfo=UTC),
    time_assistance_source="system_synchronized",
    allow_legacy_direct_restore=True,
  )
  return result, calls, sleeps, logs


def run_receiving_with_fakes(
  monkeypatch,
  *,
  duration: int,
  data: bytes = b"",
  watchdog_recovery: bool = False,
  trusted_time=False,
  rtc_assistance=None,
  send_success: bool = True,
  restore_result=None,
  inline_assistance_worker: bool = True,
  cycle_initializer=None,
  frame_processor=None,
  yuma_feature_class=None,
  parsed_frames=(),
):
  events = []
  send_calls = []

  class Clock:
    def __init__(self):
      self.value = -1.0

    def __call__(self):
      self.value += 1.0
      return self.value

  class RecordingDiagnostics:
    def __init__(self, process_start_time):
      self.process_start_time = process_start_time
      self.cycle_number = 0
      self.cycle_reason = ""
      self.cycle_start_time = process_start_time

    def start_cycle(self, reason, now):
      self.cycle_number += 1
      self.cycle_reason = reason
      self.cycle_start_time = now
      events.append(("cycle_start", self.cycle_number, reason, now))

    def initialization_complete(self, now):
      events.append(
        (
          "cycle_complete",
          self.cycle_number,
          self.cycle_reason,
          now,
          now - self.cycle_start_time,
        )
      )

    def time_assistance_context(self, now):
      return f"cycle={self.cycle_number}, reason={self.cycle_reason}"

    def log_acquisition_status(self, now):
      pass

    def note_nav_pvt(self, fix, now):
      pass

    def note_rawx(self, frame, now):
      pass

  class FakePigeon:
    def __init__(self, raw_publisher=None, frame_dispatcher=None):
      self.raw_publisher = raw_publisher
      self.frame_dispatcher = frame_dispatcher

    def receive(self):
      return data

  class FakeParser:
    def reset(self):
      events.append(("parser_reset",))

    def feed(self, received):
      return list(parsed_frames)

  class FakeFixTracker:
    def reset(self):
      events.append(("fix_tracker_reset",))

    def stable_fix(self, now):
      return None

  class FakeDumpCollector:
    active = False

    def cancel(self):
      events.append(("dump_collector_cancel",))

    def expired(self, now):
      return False

  class FakeWatchdog:
    max_recoveries = 1
    recovery_cooldown_seconds = 30.0
    healthy_rearm_seconds = 60.0

    def __init__(self):
      self.recoveries = 0

    def check(self, now):
      events.append(("watchdog_check",))
      if watchdog_recovery:
        self.recoveries += 1
      return watchdog_recovery

    def request_recovery(self, reason, now):
      self.recoveries += 1
      return True

    def note_data(self, now, *, healthy=True):
      events.append(("watchdog_note_data",))
      return False

    def recovery_completed(self, now):
      events.append(("watchdog_recovery_complete",))

  class FakeSubMaster:
    def __init__(self, services):
      self.updated = {"deviceState": False}

    def update(self, timeout):
      pass

  class FakePubMaster:
    def __init__(self, services):
      pass

    def send(self, service, message):
      pass

  class InlineThread:
    def __init__(self, *, target, **_kwargs):
      self.target = target

    def start(self):
      self.target()

  def fake_init(pigeon):
    events.append(("init",))
    initialization = pigeond._ACTIVE_PRE_ACQUISITION_INITIALIZATION
    if initialization is not None:
      initialization.run()
      initialization.note_gnss_start_sent(clock())

  def fake_restore(
    pigeon,
    receiver_fingerprint,
    diagnostic_context=None,
    time_assistance_source=None,
    trusted_now=None,
    **_kwargs,
  ):
    assert time_assistance_source in ("system_synchronized", "same_boot_boottime", None)
    events.append(("restore", trusted_now))
    return restore_result

  def fake_send_time_assistance(pigeon, **kwargs):
    send_calls.append(kwargs)
    events.append(
      (
        "time_assistance_send",
        kwargs.get("source", "synchronized"),
        clock.value,
      )
    )
    return send_success

  class FakeRtcObserver:
    def changed_observation(self, now):
      return None

  class FakeTimeAuthority:
    def create_cross_boot_rtc_observer(self):
      return FakeRtcObserver()

    def current_authorized_time(
      self,
      *,
      host_time_observation,
    ):
      host_independent = host_time_observation is not None and host_time_observation.independent
      events.append(
        (
          "time_authority_evaluate",
          clock.value,
          host_independent,
        )
      )
      if host_independent:
        utc_value = datetime(2026, 7, 10, tzinfo=UTC)
        uncertainty = 30
        independent = True
      elif rtc_assistance is not None:
        utc_value, uncertainty = rtc_assistance
        independent = False
      else:
        return SimpleNamespace(
          authorized_time=None,
          rejection_reason=SimpleNamespace(value="anchor_unavailable"),
          anchor_write_status=(pigeond.AnchorWriteStatus.NOT_REQUIRED),
          anchor_write_error=None,
          selected_anchor_generation=None,
          selected_anchor_sequence=None,
          anchor_write_reason=None,
          anchor_comparison=None,
        )
      authorized = pigeond.AuthorizedTime(
        utc=utc_value,
        uncertainty_seconds=float(uncertainty),
        source=pigeond.TrustedTimeSource.SYSTEM_SYNCHRONIZED,
        provenance=(pigeond.TimeProvenance.NETWORK_INDEPENDENT if independent else pigeond.TimeProvenance.EXTERNAL_OR_UNKNOWN),
        independent=independent,
        evidence=(TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED if independent else TimeAuthorizationEvidence.SAME_BOOT_BOOTTIME),
        observed_boottime_seconds=100.0,
      )
      return SimpleNamespace(
        authorized_time=authorized,
        rejection_reason=None,
        anchor_write_status=(pigeond.AnchorWriteStatus.SAVED if host_independent else pigeond.AnchorWriteStatus.NOT_REQUIRED),
        anchor_write_error=None,
        selected_anchor_generation=None,
        selected_anchor_sequence=None,
        anchor_write_reason=None,
        anchor_comparison=None,
      )

  clock = Clock()
  monkeypatch.setattr(pigeond.time, "monotonic", clock)
  monkeypatch.setattr(pigeond, "GpsStartupDiagnostics", RecordingDiagnostics)
  monkeypatch.setattr(pigeond.messaging, "PubMaster", FakePubMaster)
  monkeypatch.setattr(pigeond.messaging, "SubMaster", FakeSubMaster)
  monkeypatch.setattr(
    pigeond.messaging,
    "new_message",
    lambda *args, **kwargs: SimpleNamespace(ubloxRaw=None),
  )
  monkeypatch.setattr(pigeond, "Params", lambda: object())
  monkeypatch.setattr(
    pigeond,
    "gps_assistance_receiver_fingerprint",
    lambda params, mon_ver_info=None: "receiver",
  )
  monkeypatch.setattr(pigeond, "TTYPigeon", FakePigeon)
  if inline_assistance_worker:
    monkeypatch.setattr(pigeond, "Thread", InlineThread)
  monkeypatch.setattr(pigeond, "init", fake_init)
  monkeypatch.setattr(pigeond, "log_mon_ver_diagnostics", lambda pigeon: None)
  monkeypatch.setattr(
    pigeond,
    "restore_navigation_assistance",
    fake_restore,
  )
  monkeypatch.setattr(pigeond, "UbxStreamParser", FakeParser)
  monkeypatch.setattr(pigeond, "ReliableFixTracker", FakeFixTracker)
  monkeypatch.setattr(
    pigeond,
    "NavigationDatabaseDumpCollector",
    FakeDumpCollector,
  )
  monkeypatch.setattr(pigeond, "UbloxDataWatchdog", FakeWatchdog)
  if cycle_initializer is not None:
    monkeypatch.setattr(
      pigeond,
      "initialize_receiver_cycle",
      cycle_initializer,
    )
  if frame_processor is not None:
    monkeypatch.setattr(
      pigeond,
      "process_receiver_frames",
      frame_processor,
    )
  if yuma_feature_class is not None:
    monkeypatch.setattr(
      pigeond,
      "YumaSupplementationFeature",
      yuma_feature_class,
    )

  def current_host_time():
    trusted = trusted_time() if callable(trusted_time) else trusted_time
    return network_host_observation() if trusted else None

  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    current_host_time,
  )
  monkeypatch.setattr(
    pigeond,
    "TimeAuthority",
    FakeTimeAuthority,
  )
  monkeypatch.setattr(
    pigeond,
    "send_time_assistance",
    fake_send_time_assistance,
  )

  pigeond.run_receiving(duration=duration)
  return events, send_calls


def test_time_assistance_rejects_untrusted_clock(
  monkeypatch,
):
  receiver = FakePigeon()

  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: None,
  )

  assert not pigeond.send_time_assistance(receiver)
  assert receiver.sent == []


def test_time_assistance_accepts_trusted_clock(
  monkeypatch,
):
  receiver = FakePigeon()

  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    network_host_observation,
  )

  assert pigeond.send_time_assistance(receiver)
  assert len(receiver.sent) == 1
  assert receiver.sent[0][2:4] == b"\x13\x40"


def test_time_assistance_write_marks_receiver_cycle_before_ack(
  monkeypatch,
):
  receiver = FakePigeon(auto_ack=False)
  provenance = pigeond.ReceiverTimeProvenanceTracker()
  provenance.start_cycle(1, 100.0)
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    network_host_observation,
  )
  monkeypatch.setattr(
    pigeond.time,
    "monotonic",
    lambda: 101.0,
  )

  assert not pigeond.send_time_assistance(
    receiver,
    assistance_time=datetime(2026, 7, 22, tzinfo=UTC),
    ack_timeout=0.0,
    time_provenance=provenance,
  )
  assert provenance.time_assistance_written


def test_failed_serial_write_does_not_mark_receiver_cycle(
  monkeypatch,
):
  class WriteFailure:
    def send(self, message):
      raise OSError("serial unavailable")

  provenance = pigeond.ReceiverTimeProvenanceTracker()
  provenance.start_cycle(1, 100.0)
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    network_host_observation,
  )

  assert not pigeond.send_time_assistance(
    WriteFailure(),
    assistance_time=datetime(2026, 7, 22, tzinfo=UTC),
    time_provenance=provenance,
  )
  assert not provenance.time_assistance_written


def test_time_assistance_logs_matching_accepted_ack(monkeypatch):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  receiver = FakePigeon()

  assert pigeond.send_time_assistance(
    receiver,
    assistance_time=datetime(2026, 7, 10, tzinfo=UTC),
    diagnostic_context="cycle=1, reason=process_start",
  )

  assert len(logs) == 1
  assert "Time assistance written and accepted by ublox" in logs[0]
  assert "source=synchronized" in logs[0]
  assert "write_result=succeeded" in logs[0]
  assert "ack_result=accepted" in logs[0]
  assert "ack_type=1" in logs[0]
  assert "ack_version=0" in logs[0]
  assert "ack_infoCode=0" in logs[0]
  assert "ack_message_id=0x40" in logs[0]
  assert "cycle=1, reason=process_start" in logs[0]


def test_time_assistance_logs_matching_rejected_ack(monkeypatch):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "warning", logs.append)
  receiver = FakePigeon(auto_ack=False)
  message = pigeond.build_time_assistance_message(datetime(2026, 7, 10, tzinfo=UTC))
  receiver.responses.append(build_mga_ack(message, info_code=255))

  assert not pigeond.send_time_assistance(
    receiver,
    assistance_time=datetime(2026, 7, 10, tzinfo=UTC),
  )

  assert len(logs) == 1
  assert "Time assistance written but rejected by ublox" in logs[0]
  assert "write_result=succeeded" in logs[0]
  assert "ack_result=rejected" in logs[0]
  assert "ack_infoCode=255" in logs[0]


def test_time_assistance_rejects_nonzero_mga_ack_version():
  receiver = FakePigeon(auto_ack=False)
  assistance_time = datetime(2026, 7, 10, tzinfo=UTC)
  message = pigeond.build_time_assistance_message(assistance_time)
  receiver.responses.append(build_mga_ack(message, version=1))

  assert not pigeond.send_time_assistance(
    receiver,
    assistance_time=assistance_time,
  )


def test_time_assistance_logs_matching_ack_timeout(monkeypatch):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "warning", logs.append)
  receiver = FakePigeon(auto_ack=False)

  assert not pigeond.send_time_assistance(
    receiver,
    assistance_time=datetime(2026, 7, 10, tzinfo=UTC),
    ack_timeout=0.0,
  )

  assert len(logs) == 1
  assert "matching ublox ACK timed out" in logs[0]
  assert "write_result=succeeded" in logs[0]
  assert "ack_result=timed_out" in logs[0]


@pytest.mark.parametrize(
  "unrelated_message",
  [
    pigeond.build_position_assistance_message(0, 0, 0, 1_000),
    pigeond.add_ubx_checksum(b"\xb5\x62\x13\x80\x04\x00\x22\x00\x00\x00"),
  ],
)
def test_time_assistance_ignores_unrelated_ack_before_match(
  monkeypatch,
  unrelated_message,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  assistance_time = datetime(2026, 7, 10, tzinfo=UTC)
  time_message = pigeond.build_time_assistance_message(assistance_time)
  receiver = FakePigeon(auto_ack=False)
  receiver.responses.extend(
    (
      build_mga_ack(unrelated_message),
      build_mga_ack(time_message),
    )
  )

  assert pigeond.send_time_assistance(
    receiver,
    assistance_time=assistance_time,
  )
  assert "ack_result=accepted" in logs[0]
  assert receiver.responses == []


def test_time_assistance_ack_is_observed_before_position_restore():
  assistance_time = datetime(2026, 7, 10, tzinfo=UTC)
  time_message = pigeond.build_time_assistance_message(assistance_time)
  receiver = FakePigeon(auto_ack=False)
  receiver.responses.append(build_mga_ack(time_message))

  assert pigeond.send_time_assistance(
    receiver,
    assistance_time=assistance_time,
  )
  assert receiver.receive() == b""


@pytest.mark.parametrize("trusted", [False, True])
def test_cache_restore_age_uses_only_trusted_time(
  monkeypatch,
  tmp_path,
  trusted,
):
  observed = {}
  cache_path = tmp_path / "navigation_cache.json"
  original_cache = b"cache must remain byte-identical"
  cache_path.write_bytes(original_cache)
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", cache_path)

  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: network_host_observation() if trusted else None,
  )

  def fake_load_cache(*args, **kwargs):
    observed["now_utc"] = kwargs.get("now_utc")
    raise OSError("stop after inspecting timestamp")

  monkeypatch.setattr(
    pigeond,
    "load_cache",
    fake_load_cache,
  )

  result = pigeond.restore_navigation_assistance(
    object(),
    "receiver",
    allow_legacy_direct_restore=True,
  )

  assert result.status is (pigeond.NavigationAssistanceRestoreStatus.FAILED)
  assert not result.usable
  assert result.failure_phase is pigeond.NavigationAssistanceRestoreFailurePhase.CACHE_LOAD
  assert (observed["now_utc"] is not None) is trusted
  assert cache_path.read_bytes() == original_cache


def test_cache_save_fallback_accepts_trusted_time(
  monkeypatch,
):
  observed = {}

  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    network_host_observation,
  )

  def fake_create_cache(**kwargs):
    observed.update(kwargs)
    return object()

  monkeypatch.setattr(
    pigeond,
    "create_cache",
    fake_create_cache,
  )

  def fake_promote(self, candidate, *args):
    return SimpleNamespace(
      status=pigeond.CachePromotionStatus.SAVED,
      reason="test_saved",
      selected=SimpleNamespace(generation="primary"),
      inventory=SimpleNamespace(
        primary=SimpleNamespace(cache=None),
        previous=SimpleNamespace(cache=None),
      ),
      stage=CachePromotionStage.PRIMARY_CANDIDATE_COMPARISON,
      fallback_generation=None,
      selection_reason="no_eligible_stored_cache",
      cleanup_failure=None,
    )

  monkeypatch.setattr(pigeond.NavigationCacheStore, "promote", fake_promote)
  monkeypatch.setattr(
    pigeond,
    "read_rtc_counter_seconds",
    lambda: 24_000,
  )

  fix = SimpleNamespace(utc_time=None)
  quality = SimpleNamespace(passes_policy=True, usable_for_capture=True)

  assert (
    pigeond.write_navigation_assistance_cache(
      "receiver",
      fix,
      (),
      quality,
    )
    is pigeond.NavigationAssistanceCacheResult.SAVED
  )

  saved_at = observed["saved_at_utc"]
  assert saved_at.tzinfo is not None
  assert observed["rtc_counter_seconds"] == 24_000


def test_promotion_terminal_log_has_exact_stage_fallback_and_selection(monkeypatch):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  monkeypatch.setattr(pigeond.cloudlog, "warning", lambda _message: None)
  monkeypatch.setattr(pigeond, "read_host_time_observation", network_host_observation)
  monkeypatch.setattr(pigeond, "create_cache", lambda **_kwargs: object())
  monkeypatch.setattr(pigeond, "read_rtc_counter_seconds", lambda: 100)

  def failed_promotion(self, candidate, *args):
    return SimpleNamespace(
      status=pigeond.CachePromotionStatus.FAILED,
      reason="preserve_directory_fsync_failed:OSError:injected",
      selected=SimpleNamespace(generation="previous"),
      inventory=SimpleNamespace(
        primary=SimpleNamespace(cache=None),
        previous=SimpleNamespace(cache=None),
      ),
      stage=CachePromotionStage.PRESERVE_DIRECTORY_FSYNC,
      fallback_generation="previous",
      selection_reason="primary_ineligible_fallback",
      cleanup_failure=None,
    )

  monkeypatch.setattr(pigeond.NavigationCacheStore, "promote", failed_promotion)
  result = pigeond.write_navigation_assistance_cache(
    "receiver",
    SimpleNamespace(utc_time=datetime(2026, 7, 16, tzinfo=UTC)),
    (),
    SimpleNamespace(passes_policy=True, usable_for_capture=True),
  )

  assert result is pigeond.NavigationAssistanceCacheResult.FAILED
  terminal = next(message for message in logs if "promotion result" in message)
  assert "promotion_stage=preserve_directory_fsync" in terminal
  assert "fallback_generation=previous" in terminal
  assert "selection_reason=primary_ineligible_fallback" in terminal
  assert "candidate_validation_failed" not in terminal


def test_cache_save_fallback_rejects_untrusted_time(
  monkeypatch,
):
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: None,
  )

  def unexpected_create_cache(**kwargs):
    pytest.fail("create_cache must not use an untrusted clock")

  monkeypatch.setattr(
    pigeond,
    "create_cache",
    unexpected_create_cache,
  )

  fix = SimpleNamespace(utc_time=None)
  quality = SimpleNamespace(passes_policy=True, usable_for_capture=True)

  assert (
    pigeond.write_navigation_assistance_cache(
      "receiver",
      fix,
      (),
      quality,
    )
    is pigeond.NavigationAssistanceCacheResult.FAILED
  )


def test_cache_promotion_rejects_assisted_receiver_utc(
  monkeypatch,
):
  receiver_utc = datetime(2026, 7, 22, tzinfo=UTC)
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: None,
  )

  assert (
    pigeond.cache_promotion_trusted_now(
      receiver_utc,
      1,
      1,
      receiver_utc_fresh=True,
      receiver_utc_independent=False,
    )
    is None
  )


def test_cache_promotion_accepts_independent_receiver_utc(
  monkeypatch,
):
  receiver_utc = datetime(2026, 7, 22, tzinfo=UTC)
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: None,
  )

  assert (
    pigeond.cache_promotion_trusted_now(
      receiver_utc,
      1,
      1,
      receiver_utc_fresh=True,
      receiver_utc_independent=True,
    )
    == receiver_utc
  )


def test_cache_promotion_prefers_central_authorized_time(
  monkeypatch,
):
  authorized_utc = datetime(2026, 7, 22, tzinfo=UTC)
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: None,
  )

  assert (
    pigeond.cache_promotion_trusted_now(
      None,
      1,
      1,
      receiver_utc_fresh=False,
      receiver_utc_independent=False,
      authorized_utc=authorized_utc,
    )
    == authorized_utc
  )


def test_independent_receiver_time_anchor_requires_gnss_provenance():
  provenance = pigeond.ReceiverTimeProvenanceTracker()
  provenance.start_cycle(1, 100.0)
  provenance.note_rawx(
    build_rawx_frame(
      week=2_429,
      leap_seconds_valid=True,
      measurements=((0, 35),),
    ),
    101.0,
  )
  provenance.note_nav_pvt(
    pigeond.NavPvtFix(
      fix_ok=False,
      satellites=0,
      latitude_e7=0,
      longitude_e7=0,
      altitude_cm=0,
      horizontal_accuracy_cm=100_000,
      vertical_accuracy_cm=100_000,
      utc_time=datetime(2026, 7, 22, tzinfo=UTC),
      valid_date=True,
      valid_time=True,
      fully_resolved=True,
      time_accuracy_ns=25_000_000,
    ),
    101.1,
  )

  assert pigeond.fresh_independent_receiver_utc_time_anchor(
    provenance,
    101.2,
  ) == (
    datetime(2026, 7, 22, tzinfo=UTC),
    101.1,
  )

  provenance.note_time_assistance_written(
    source="same_boot_boottime",
    assistance_utc=datetime(2026, 7, 22, tzinfo=UTC),
    uncertainty_seconds=31.0,
    now=101.3,
  )

  assert (
    pigeond.fresh_independent_receiver_utc_time_anchor(
      provenance,
      101.4,
    )
    is None
  )


def test_explicit_rtc_time_assistance_does_not_require_trusted_clock(
  monkeypatch,
):
  receiver = FakePigeon()

  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    lambda: None,
  )

  estimated_utc = datetime(
    2026,
    7,
    6,
    17,
    45,
    16,
    tzinfo=UTC,
  )

  assert pigeond.send_time_assistance(
    receiver,
    assistance_time=estimated_utc,
    accuracy_seconds=120,
    source="rtc_estimate",
  )

  assert len(receiver.sent) == 1
  payload = receiver.sent[0][6:-2]
  assert struct.unpack_from("<H", payload, 16)[0] == 120


def test_cached_rtc_time_assistance_uses_receiver_cache(
  monkeypatch,
):
  cache = SimpleNamespace(rtc_counter_seconds=24_000)
  current_rtc_seconds = 24_900
  expected = (
    datetime(
      2026,
      7,
      6,
      17,
      45,
      16,
      tzinfo=UTC,
    ),
    60,
  )
  observed = {}
  observed_paths = []

  def fake_load_cache(path, **kwargs):
    observed_paths.append(path)
    observed.update(kwargs)
    return cache

  def fake_select_rtc_estimate(inventory, current_rtc_seconds):
    observed["estimate_cache"] = inventory.primary.cache
    observed["current_rtc_seconds"] = current_rtc_seconds
    estimate = RtcEstimateSuccess(
      estimated_utc=expected[0],
      uncertainty_seconds=expected[1],
      elapsed_seconds=900,
    )
    return SimpleNamespace(
      generation="primary",
      estimate=estimate,
    ), ()

  monkeypatch.setattr(
    pigeond,
    "load_cache",
    fake_load_cache,
  )
  monkeypatch.setattr(
    pigeond,
    "read_rtc_counter_seconds",
    lambda: current_rtc_seconds,
  )
  monkeypatch.setattr(
    pigeond,
    "select_rtc_estimate",
    fake_select_rtc_estimate,
  )

  result = pigeond.cached_rtc_time_assistance("receiver-a")

  assert result == expected
  assert pigeond.GPS_ASSISTANCE_CACHE_PATH in observed_paths
  assert observed["expected_receiver_fingerprint"] == "receiver-a"
  assert observed["estimate_cache"] is cache
  assert observed["current_rtc_seconds"] == current_rtc_seconds


def test_cached_rtc_time_assistance_rejects_invalid_cache(
  monkeypatch,
):
  def reject_cache(*args, **kwargs):
    raise pigeond.CacheValidationError("bad cache")

  monkeypatch.setattr(
    pigeond,
    "load_cache",
    reject_cache,
  )

  assert pigeond.cached_rtc_time_assistance("receiver") is None


def test_startup_diagnostics_initial_cycle_timing(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)

  diagnostics = pigeond.GpsStartupDiagnostics(
    process_start_time=100.0,
  )
  diagnostics.start_cycle("process_start", 101.0)
  diagnostics.initialization_complete(106.0)

  assert diagnostics.cycle_number == 1
  assert diagnostics.cycle_reason == "process_start"
  assert "cycle=1" in logs[0]
  assert "reason=process_start" in logs[0]
  assert "process_elapsed_seconds=1.0" in logs[0]
  assert "process_elapsed_seconds=6.0" in logs[1]
  assert "cycle_initialization_elapsed_seconds=5.0" in logs[1]


def test_run_receiving_configures_before_initial_time_evaluation(
  monkeypatch,
):
  events, _ = run_receiving_with_fakes(
    monkeypatch,
    duration=-1,
    trusted_time=True,
  )

  assert [event[0] for event in events] == [
    "cycle_start",
    "init",
    "time_authority_evaluate",
    "restore",
    "time_assistance_send",
    "cycle_complete",
  ]
  assert events[0][1:3] == (1, "process_start")
  assert events[5][1:3] == (1, "process_start")


def test_pending_deferred_assistance_does_not_block_receiver_processing_or_yuma(
  monkeypatch,
):
  initializer_calls = []
  poll_calls = []
  processed_batches = []
  yuma_calls = []

  def initialize_cycle(*_args, **_kwargs):
    initializer_calls.append("initialize")

    def poll_pending_worker():
      poll_calls.append("poll")
      return None

    return pigeond.ReceiverCycleInitialization(
      trusted_time_assistance_sent=False,
      next_time_assistance_attempt=1_000_000.0,
      navigation_assistance_restore_attempted=False,
      mon_ver_info=None,
      ack_aiding_configuration_attempted=False,
      assistnow_autonomous_supported=False,
      assistnow_autonomous_configuration_attempted=False,
      completed_at=0.0,
      gnss_start_sent_at=0.0,
      poll_deferred_assistance_state=poll_pending_worker,
    )

  def process_frames(frames, *_args, **_kwargs):
    processed_batches.append(tuple(frames))
    return None

  class NoYumaBeforeAdoption:
    cycle_injection_consumed = False

    def __init__(self, *_args, **_kwargs):
      pass

    def evaluate_provisional(self, *_args, **_kwargs):
      yuma_calls.append("provisional")

    def evaluate(self, *_args, **_kwargs):
      yuma_calls.append("normal")

  events, _ = run_receiving_with_fakes(
    monkeypatch,
    duration=10,
    data=b"\x01",
    inline_assistance_worker=False,
    cycle_initializer=initialize_cycle,
    frame_processor=process_frames,
    yuma_feature_class=NoYumaBeforeAdoption,
    parsed_frames=(b"NAV-PVT", b"RXM-RAWX"),
  )

  assert initializer_calls == ["initialize"]
  assert poll_calls
  assert processed_batches
  assert all(batch == (b"NAV-PVT", b"RXM-RAWX") for batch in processed_batches)
  assert yuma_calls == []
  assert not any(event[0] == "watchdog_recovery_complete" for event in events)


@pytest.mark.parametrize(
  ("trusted_time", "rtc_assistance", "expected_events"),
  [
    (
      True,
      None,
      [
        "init",
        "time_authority_evaluate",
        "restore",
        "time_assistance_send",
      ],
    ),
    (
      False,
      (datetime(2026, 7, 10, tzinfo=UTC), 60),
      [
        "init",
        "time_authority_evaluate",
      ],
    ),
    (
      False,
      None,
      ["init", "time_authority_evaluate"],
    ),
  ],
)
def test_run_receiving_restores_cache_only_after_acceptable_time(
  monkeypatch,
  trusted_time,
  rtc_assistance,
  expected_events,
):
  events, _ = run_receiving_with_fakes(
    monkeypatch,
    duration=-1,
    trusted_time=trusted_time,
    rtc_assistance=rtc_assistance,
  )
  event_names = [event[0] for event in events]

  assert event_names[1:-1] == expected_events
  assert event_names.count("restore") == expected_events.count("restore")


@pytest.mark.parametrize(
  ("data", "reason", "watchdog_completion"),
  [
    (b"", "no_data_watchdog", True),
    (b"\x00", "all_zero_data", True),
  ],
)
def test_run_receiving_wires_recovery_cycle_and_reset_order(
  monkeypatch,
  data,
  reason,
  watchdog_completion,
):
  events, send_calls = run_receiving_with_fakes(
    monkeypatch,
    duration=5,
    data=data,
    watchdog_recovery=True,
    trusted_time=True,
  )
  event_names = [event[0] for event in events]
  recovery_start = next(index for index, event in enumerate(events) if event[0] == "cycle_start" and event[1] == 2)

  expected_order = [
    "cycle_start",
    "init",
    "time_authority_evaluate",
    "restore",
    "time_assistance_send",
    "parser_reset",
    "fix_tracker_reset",
    "dump_collector_cancel",
  ]
  if watchdog_completion:
    expected_order.append("watchdog_recovery_complete")
  expected_order.append("cycle_complete")

  assert event_names[recovery_start : recovery_start + len(expected_order)] == expected_order
  assert events[recovery_start][1:3] == (2, reason)
  assert events[recovery_start + len(expected_order) - 1][1:3] == (2, reason)
  assert send_calls[-1]["diagnostic_context"] == (f"cycle=2, reason={reason}")


@pytest.mark.parametrize(
  ("trusted_time", "rtc_assistance", "expected_source"),
  [
    (True, None, "system_synchronized"),
    (
      False,
      (datetime(2026, 7, 10, tzinfo=UTC), 60),
      "same_boot_boottime",
    ),
  ],
)
def test_run_receiving_passes_cycle_context_to_time_assistance(
  monkeypatch,
  trusted_time,
  rtc_assistance,
  expected_source,
):
  _, send_calls = run_receiving_with_fakes(
    monkeypatch,
    duration=5,
    data=b"\x01",
    trusted_time=trusted_time,
    rtc_assistance=rtc_assistance,
  )

  assert len(send_calls) == 1
  assert send_calls[0].get("source") == expected_source
  assert send_calls[0]["diagnostic_context"] == ("cycle=1, reason=process_start")


def test_same_boot_time_is_forwarded_exactly_to_post_start_assistance(monkeypatch):
  estimated_utc = datetime(2026, 7, 10, 12, 34, 56, tzinfo=UTC)
  events, send_calls = run_receiving_with_fakes(
    monkeypatch,
    duration=20,
    data=b"\x01",
    trusted_time=False,
    rtc_assistance=(estimated_utc, 60),
  )

  # Deferred assistance may call restore once for independent position send;
  # same-boot RTC must not trigger a second DBD/cache restore path.
  assert sum(event[0] == "restore" for event in events) == 1
  assert len(send_calls) == 1
  assert send_calls[0]["assistance_time"] == estimated_utc
  assert send_calls[0]["source"] == "same_boot_boottime"


def test_run_receiving_retries_failed_same_boot_assistance_at_interval(
  monkeypatch,
):
  events, _ = run_receiving_with_fakes(
    monkeypatch,
    duration=70,
    data=b"\x01",
    rtc_assistance=(datetime(2026, 7, 10, tzinfo=UTC), 60),
    send_success=False,
  )
  evaluation_times = [event[1] for event in events if event[0] == "time_authority_evaluate"]
  send_times = [event[2] for event in events if event[0] == "time_assistance_send"]

  assert len(evaluation_times) >= 2
  assert len(send_times) >= 2
  assert 30 <= send_times[1] - send_times[0] <= 36


def test_run_receiving_retries_failed_synchronized_assistance_at_interval(
  monkeypatch,
):
  events, _ = run_receiving_with_fakes(
    monkeypatch,
    duration=70,
    data=b"\x01",
    trusted_time=True,
    send_success=False,
  )
  send_times = [event[2] for event in events if event[:2] == ("time_assistance_send", "system_synchronized")]

  assert len(send_times) >= 2
  assert 30 <= send_times[1] - send_times[0] <= 36


def test_run_receiving_restores_cache_after_later_synchronized_time(
  monkeypatch,
):
  trusted_checks = iter((False, True))

  events, _ = run_receiving_with_fakes(
    monkeypatch,
    duration=20,
    data=b"\x01",
    trusted_time=lambda: next(trusted_checks, True),
  )
  event_names = [event[0] for event in events]

  assert event_names.index("time_authority_evaluate") < (event_names.index("time_assistance_send"))
  # One deferred position-assistance restore is allowed; later synchronized
  # time must not open a second DBD/cache restore window.
  assert event_names.count("restore") == 1


def test_run_receiving_does_not_restore_cache_twice_after_rtc_time(
  monkeypatch,
):
  events, send_calls = run_receiving_with_fakes(
    monkeypatch,
    duration=20,
    data=b"\x01",
    trusted_time=False,
    rtc_assistance=(datetime(2026, 7, 10, tzinfo=UTC), 60),
  )

  assert [call.get("source") for call in send_calls] == ["same_boot_boottime"]
  assert sum(event[0] == "restore" for event in events) == 1


def test_mga_info_code_255_remains_a_strict_failure():
  database_payload = b"\x22\x00\x00\x00"
  message = pigeond.add_ubx_checksum(b"\xb5\x62\x13\x80" + len(database_payload).to_bytes(2, "little") + database_payload)
  payload = bytes((1, 0, 255, 0x80)) + database_payload
  acknowledgment = pigeond.add_ubx_checksum(b"\xb5\x62\x13\x60" + len(payload).to_bytes(2, "little") + payload)

  class AcknowledgingPigeon:
    def send(self, sent_message):
      assert sent_message is message

    def receive(self):
      return acknowledgment

  with pytest.raises(
    pigeond.CacheValidationError,
    match=(
      "".join(
        (
          r"mga_message_type=0x22, message_id=0x80, ",
          r"ack_type=1, ack_version=0, ack_infoCode=255, ",
          r"rejected_message_id=0x80, database_frame_index=3",
        )
      )
    ),
  ):
    pigeond.send_mga_with_strict_ack(
      AcknowledgingPigeon(),
      message,
      database_frame_index=3,
    )


def test_navigation_restore_accepts_all_frames_first_pass(
  monkeypatch,
):
  result, calls, sleeps, logs = run_navigation_restore_with_outcomes(
    monkeypatch,
    {},
    diagnostic_context="cycle=2, reason=no_data_watchdog",
  )

  assert result.status is (pigeond.NavigationAssistanceRestoreStatus.COMPLETE)
  assert result.usable
  assert result.accepted_frame_count == 3
  assert calls == [None, 0, 1, 2]
  assert sleeps == []
  final_level, final_log = next((level, message) for level, message in logs if message.startswith("GPS navigation assistance restore result"))
  assert final_level == "info"
  assert "restore_status=complete" in final_log
  assert "total_frame_count=3" in final_log
  assert "accepted_frame_count=3" in final_log
  assert "cycle=2, reason=no_data_watchdog" in final_log


@pytest.mark.parametrize(
  ("initial_outcome", "initial_field"),
  [
    (
      pigeond.CacheValidationError("rejected"),
      "initially_rejected_indexes",
    ),
    (
      TimeoutError("timed out"),
      "initially_timed_out_indexes",
    ),
  ],
)
def test_navigation_restore_retries_one_failed_frame_successfully(
  monkeypatch,
  initial_outcome,
  initial_field,
):
  result, calls, sleeps, _ = run_navigation_restore_with_outcomes(
    monkeypatch,
    {1: [initial_outcome, None]},
  )

  assert result.status is (pigeond.NavigationAssistanceRestoreStatus.COMPLETE)
  assert result.accepted_frame_count == 3
  assert getattr(result, initial_field) == (1,)
  assert result.retry_accepted_indexes == (1,)
  assert calls == [None, 0, 1, 2, 1]
  assert sleeps == [pigeond.GPS_ASSISTANCE_FRAME_RETRY_DELAY]


@pytest.mark.parametrize(
  ("initial_outcome", "retry_outcome", "permanent_field"),
  [
    (
      pigeond.CacheValidationError("initial rejection"),
      pigeond.CacheValidationError("rejected again"),
      "permanently_rejected_indexes",
    ),
    (
      TimeoutError("initial timeout"),
      TimeoutError("timed out again"),
      "permanently_timed_out_indexes",
    ),
  ],
)
def test_navigation_restore_continues_after_permanent_frame_failure(
  monkeypatch,
  initial_outcome,
  retry_outcome,
  permanent_field,
):
  result, calls, _, _ = run_navigation_restore_with_outcomes(
    monkeypatch,
    {
      1: [
        initial_outcome,
        retry_outcome,
      ],
    },
  )

  assert result.status is (pigeond.NavigationAssistanceRestoreStatus.PARTIAL)
  assert result.usable
  assert result.accepted_frame_count == 2
  assert getattr(result, permanent_field) == (1,)
  assert calls == [None, 0, 1, 2, 1]


def test_navigation_restore_tracks_mixed_retry_outcomes(
  monkeypatch,
):
  result, calls, _, logs = run_navigation_restore_with_outcomes(
    monkeypatch,
    {
      1: [pigeond.CacheValidationError("rejected"), None],
      2: [
        TimeoutError("timed out"),
        pigeond.CacheValidationError("rejected on retry"),
      ],
      4: [
        pigeond.CacheValidationError("rejected"),
        TimeoutError("timed out on retry"),
      ],
    },
    frame_count=5,
  )

  assert result.status is (pigeond.NavigationAssistanceRestoreStatus.PARTIAL)
  assert result.accepted_frame_count == 3
  assert result.initially_rejected_indexes == (1, 4)
  assert result.initially_timed_out_indexes == (2,)
  assert result.retry_accepted_indexes == (1,)
  assert result.permanently_rejected_indexes == (2,)
  assert result.permanently_timed_out_indexes == (4,)
  assert calls == [None, 0, 1, 2, 3, 4, 1, 2, 4]
  final_level, final_log = next((level, message) for level, message in logs if message.startswith("GPS navigation assistance restore result"))
  assert final_level == "warning"
  assert "restore_status=partial" in final_log
  assert "initially_rejected_indexes=[1, 4]" in final_log
  assert "initially_timed_out_indexes=[2]" in final_log
  assert "retry_accepted_indexes=[1]" in final_log
  assert "permanently_rejected_indexes=[2]" in final_log
  assert "permanently_timed_out_indexes=[4]" in final_log


@pytest.mark.parametrize(
  ("position_outcome", "expected_phase"),
  [
    (
      pigeond.CacheValidationError("position rejected"),
      pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_REJECTED,
    ),
    (
      TimeoutError("position timed out"),
      pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_TIMEOUT,
    ),
    (
      pigeond.MgaWriteError("position write failed"),
      pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_WRITE,
    ),
  ],
)
def test_navigation_restore_position_failure_is_phase_specific_and_read_only(
  monkeypatch,
  tmp_path,
  position_outcome,
  expected_phase,
):
  cache_path = tmp_path / "navigation_cache.json"
  original_cache = b"cache must remain byte-identical"
  cache_path.write_bytes(original_cache)
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", cache_path)
  result, calls, sleeps, logs = run_navigation_restore_with_outcomes(
    monkeypatch,
    {},
    position_outcome=position_outcome,
  )

  assert result.status is (pigeond.NavigationAssistanceRestoreStatus.FAILED)
  assert not result.usable
  assert result.failure_phase is expected_phase
  assert result.accepted_frame_count == 0
  assert calls == [None]
  assert sleeps == []
  assert cache_path.read_bytes() == original_cache
  summary = next(message for _, message in logs if message.startswith("GPS navigation assistance restore result"))
  assert f"failure_phase={expected_phase.value}" in summary
  assert "accepted_frames=0" in summary
  assert "timeout_events=0" in summary


def test_navigation_restore_position_build_failure_is_phase_specific_and_read_only(
  monkeypatch,
  tmp_path,
):
  cache_path = tmp_path / "navigation_cache.json"
  original_cache = b"cache must remain byte-identical"
  cache_path.write_bytes(original_cache)
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", cache_path)
  monkeypatch.setattr(
    pigeond,
    "build_position_assistance_message",
    lambda **kwargs: (_ for _ in ()).throw(ValueError("position build failed")),
  )

  result, calls, sleeps, _ = run_navigation_restore_with_outcomes(monkeypatch, {})

  assert result.failure_phase is (pigeond.NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_BUILD)
  assert calls == []
  assert sleeps == []
  assert cache_path.read_bytes() == original_cache


def test_navigation_restore_zero_accepted_frames_is_hard(
  monkeypatch,
):
  result, calls, _, _ = run_navigation_restore_with_outcomes(
    monkeypatch,
    {
      0: [
        pigeond.CacheValidationError("rejected"),
        pigeond.CacheValidationError("rejected again"),
      ],
      1: [
        TimeoutError("timed out"),
        TimeoutError("timed out again"),
      ],
    },
    frame_count=2,
  )

  assert result.status is (pigeond.NavigationAssistanceRestoreStatus.FAILED)
  assert not result.usable
  assert result.accepted_frame_count == 0
  assert result.failure_phase is (pigeond.NavigationAssistanceRestoreFailurePhase.DATABASE_FRAME_RESTORE)
  assert calls == [None, 0, 1, 0, 1]


def test_navigation_restore_empty_database_is_hard(
  monkeypatch,
):
  result, calls, sleeps, logs = run_navigation_restore_with_outcomes(
    monkeypatch,
    {},
    frame_count=0,
  )

  assert result.status is (pigeond.NavigationAssistanceRestoreStatus.FAILED)
  assert not result.usable
  assert result.total_frame_count == 0
  assert result.accepted_frame_count == 0
  assert calls == [None]
  assert sleeps == []
  final_level, final_log = next((level, message) for level, message in logs if message.startswith("GPS navigation assistance restore result"))
  assert final_level == "error"
  assert "restore_status=failed" in final_log
  assert "total_frame_count=0" in final_log
  assert "accepted_frame_count=0" in final_log


def test_partial_navigation_restore_does_not_delete_cache(
  monkeypatch,
  tmp_path,
):
  cache_path = tmp_path / "gps_assistance.json"
  cache_path.write_text("cached", encoding="utf-8")
  monkeypatch.setattr(
    pigeond,
    "GPS_ASSISTANCE_CACHE_PATH",
    cache_path,
  )

  result, _, _, _ = run_navigation_restore_with_outcomes(
    monkeypatch,
    {
      1: [
        pigeond.CacheValidationError("rejected"),
        pigeond.CacheValidationError("rejected again"),
      ],
    },
  )

  assert result.status is (pigeond.NavigationAssistanceRestoreStatus.PARTIAL)
  assert cache_path.read_text(encoding="utf-8") == "cached"


def test_partial_navigation_restore_is_not_repeated_in_cycle(
  monkeypatch,
):
  partial_result = pigeond.NavigationAssistanceRestoreResult(
    status=pigeond.NavigationAssistanceRestoreStatus.PARTIAL,
    total_frame_count=3,
    accepted_frame_count=2,
    permanently_rejected_indexes=(1,),
  )

  events, send_calls = run_receiving_with_fakes(
    monkeypatch,
    duration=20,
    data=b"\x01",
    trusted_time=True,
    restore_result=partial_result,
  )

  assert [call.get("source") for call in send_calls] == ["system_synchronized"]
  assert sum(event[0] == "restore" for event in events) == 1


def test_partial_restore_never_switches_to_alternate_generation(monkeypatch):
  primary_frames = (b"primary-0", b"primary-1")
  previous_frames = (b"previous-0",)
  common = {
    "saved_at_utc": datetime(2026, 7, 10, tzinfo=UTC),
    "rtc_counter_seconds": 100,
    "latitude_e7": 0,
    "longitude_e7": 0,
    "altitude_cm": 0,
    "position_accuracy_cm": 1_000,
    "quality": None,
  }
  primary = SimpleNamespace(database_frames=primary_frames, **common)
  previous = SimpleNamespace(database_frames=previous_frames, **common)
  load_calls = []

  def load_generation(path, **_kwargs):
    load_calls.append(path.name)
    return previous if "previous" in path.name else primary

  sends = []

  def send_with_outcome(_pigeon, message, **kwargs):
    sends.append(message)
    if message == primary_frames[0]:
      raise pigeond.CacheValidationError("primary frame rejected")

  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(pigeond, "load_cache", load_generation)
  monkeypatch.setattr(pigeond, "send_mga_with_strict_ack", send_with_outcome)
  monkeypatch.setattr(pigeond.time, "sleep", lambda _seconds: None)

  result = pigeond.restore_navigation_assistance(object(), "receiver", allow_legacy_direct_restore=True)

  assert result.status is pigeond.NavigationAssistanceRestoreStatus.PARTIAL
  assert load_calls.count("navigation_cache.json") == 1
  assert load_calls.count("navigation_cache_previous.json") == 1
  assert primary_frames[0] in sends and primary_frames[1] in sends
  assert previous_frames[0] not in sends


def test_startup_diagnostics_milestones_once_per_cycle(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)

  diagnostics = pigeond.GpsStartupDiagnostics(0.0)
  diagnostics.start_cycle("process_start", 0.0)

  fixes = (
    diagnostic_fix(),
    diagnostic_fix(
      fix_ok=True,
      satellites=3,
      horizontal_accuracy_cm=10_000,
    ),
    diagnostic_fix(
      fix_ok=True,
      satellites=3,
      horizontal_accuracy_cm=10_000,
      utc_time=datetime(2026, 7, 10, tzinfo=UTC),
    ),
    diagnostic_fix(
      fix_ok=True,
      satellites=7,
      horizontal_accuracy_cm=2_500,
      utc_time=datetime(2026, 7, 10, tzinfo=UTC),
    ),
  )

  for monotonic_time, fix in enumerate(fixes, start=1):
    diagnostics.note_nav_pvt(fix, float(monotonic_time))
    diagnostics.note_nav_pvt(fix, float(monotonic_time))

  milestone_logs = [message for message in logs if message.startswith("GPS acquisition milestone=")]
  for milestone in (
    "first_nav_pvt",
    "first_fix_ok",
    "first_receiver_utc",
    "first_reliable_fix",
  ):
    assert sum(f"milestone={milestone}" in message for message in milestone_logs) == 1

  diagnostics.start_cycle("no_data_watchdog", 10.0)
  diagnostics.note_nav_pvt(fixes[-1], 11.0)

  cycle_two_logs = [message for message in logs if "GPS acquisition milestone=" in message and "cycle=2" in message]
  assert len(cycle_two_logs) == 4
  assert all("reason=no_data_watchdog" in message for message in cycle_two_logs)


def test_startup_diagnostics_status_is_bounded_and_stops_after_fix(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)

  diagnostics = pigeond.GpsStartupDiagnostics(
    process_start_time=0.0,
    status_interval=30.0,
  )
  diagnostics.start_cycle("process_start", 0.0)
  diagnostics.note_nav_pvt(
    diagnostic_fix(
      fix_ok=True,
      satellites=3,
      horizontal_accuracy_cm=10_000,
    ),
    5.0,
  )

  diagnostics.log_acquisition_status(29.9)
  diagnostics.log_acquisition_status(30.0)
  diagnostics.log_acquisition_status(59.9)
  diagnostics.log_acquisition_status(60.0)

  status_logs = [message for message in logs if message.startswith("GPS acquisition status")]
  assert len(status_logs) == 2
  assert all("nav_pvt_seen=True" in message for message in status_logs)
  assert all("satellites=3" in message for message in status_logs)

  diagnostics.note_nav_pvt(
    diagnostic_fix(
      fix_ok=True,
      satellites=7,
      horizontal_accuracy_cm=2_500,
    ),
    61.0,
  )
  diagnostics.log_acquisition_status(90.0)

  assert sum(message.startswith("GPS acquisition status") for message in logs) == 2


def test_startup_diagnostics_logs_exclude_position(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)

  diagnostics = pigeond.GpsStartupDiagnostics(0.0)
  diagnostics.start_cycle("all_zero_data", 0.0)
  diagnostics.note_nav_pvt(diagnostic_fix(), 1.0)
  diagnostics.log_acquisition_status(30.0)

  combined_logs = "\n".join(logs)
  assert "123456789" not in combined_logs
  assert "-987654321" not in combined_logs
  assert "7654321" not in combined_logs
  assert "latitude" not in combined_logs
  assert "longitude" not in combined_logs
  assert "altitude" not in combined_logs


def test_startup_diagnostics_logs_first_rawx_after_initialization_once_per_cycle(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  diagnostics = pigeond.GpsStartupDiagnostics(100.0)
  rawx = build_rawx_frame(
    week=2427,
    leap_seconds_valid=True,
    measurements=((0, 28), (0, 34), (6, 31)),
  )

  diagnostics.start_cycle("process_start", 101.0)
  diagnostics.note_rawx(rawx, 106.0)
  diagnostics.note_rawx(rawx, 107.0)

  rawx_logs = [message for message in logs if "milestone=first_rawx_after_initialization" in message]
  assert len(rawx_logs) == 1
  assert "cycle=1" in rawx_logs[0]
  assert "reason=process_start" in rawx_logs[0]
  assert "process_elapsed_seconds=6.0" in rawx_logs[0]
  assert "cycle_elapsed_seconds=5.0" in rawx_logs[0]
  assert "gps_week_valid=True" in rawx_logs[0]
  assert "leap_second_valid=True" in rawx_logs[0]
  assert "measurement_count=3" in rawx_logs[0]
  assert "measurement_counts_by_gnss={0: 2, 6: 1}" in rawx_logs[0]
  assert "maximum_cno_by_gnss={0: 34, 6: 31}" in rawx_logs[0]

  diagnostics.start_cycle("no_data_watchdog", 110.0)
  diagnostics.note_rawx(
    build_rawx_frame(
      week=0,
      leap_seconds_valid=False,
      measurements=(),
    ),
    111.0,
  )
  assert sum("milestone=first_rawx_after_initialization" in message for message in logs) == 2
  assert "cycle=2" in logs[-1]
  assert "reason=no_data_watchdog" in logs[-1]
  assert "gps_week_valid=False" in logs[-1]
  assert "measurement_count=0" in logs[-1]


def test_startup_diagnostics_logs_signal_milestones_once_and_resets(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  diagnostics = pigeond.GpsStartupDiagnostics(0.0)

  def milestone_logs(name):
    return [message for message in logs if f"milestone={name}," in message]

  diagnostics.start_cycle("process_start", 0.0)

  empty_rawx = build_rawx_frame(
    week=0,
    leap_seconds_valid=False,
    measurements=(),
  )
  diagnostics.note_rawx(empty_rawx, 1.0)
  assert len(milestone_logs("first_rawx_after_initialization")) == 1
  assert milestone_logs("first_nonempty_rawx") == []
  assert milestone_logs("first_gps_measurement") == []
  assert milestone_logs("first_glonass_measurement") == []

  gps_only_rawx = build_rawx_frame(
    week=0,
    leap_seconds_valid=False,
    measurements=((0, 32),),
  )
  diagnostics.note_rawx(gps_only_rawx, 2.0)
  diagnostics.note_rawx(gps_only_rawx, 3.0)
  assert len(milestone_logs("first_nonempty_rawx")) == 1
  assert len(milestone_logs("first_gps_measurement")) == 1
  assert milestone_logs("first_glonass_measurement") == []

  glonass_only_rawx = build_rawx_frame(
    week=0,
    leap_seconds_valid=False,
    measurements=((6, 29),),
  )
  diagnostics.note_rawx(glonass_only_rawx, 4.0)
  diagnostics.note_rawx(glonass_only_rawx, 5.0)
  assert len(milestone_logs("first_glonass_measurement")) == 1

  valid_week_rawx = build_rawx_frame(
    week=2427,
    leap_seconds_valid=False,
    measurements=(),
  )
  diagnostics.note_rawx(valid_week_rawx, 6.0)
  week_log = milestone_logs("first_valid_gps_week")
  assert len(week_log) == 1
  assert "gps_week_valid=True" in week_log[0]
  assert "leap_second_valid=False" in week_log[0]
  assert milestone_logs("first_valid_leap_second") == []

  valid_leap_rawx = build_rawx_frame(
    week=0,
    leap_seconds_valid=True,
    measurements=(),
  )
  diagnostics.note_rawx(valid_leap_rawx, 7.0)
  leap_log = milestone_logs("first_valid_leap_second")
  assert len(leap_log) == 1
  assert "gps_week_valid=False" in leap_log[0]
  assert "leap_second_valid=True" in leap_log[0]

  milestone_names = (
    "first_nonempty_rawx",
    "first_valid_gps_week",
    "first_valid_leap_second",
    "first_gps_measurement",
    "first_glonass_measurement",
  )
  for name in milestone_names:
    assert len(milestone_logs(name)) == 1
    assert "measurement_counts_by_gnss=" in milestone_logs(name)[0]
    assert "maximum_cno_by_gnss=" in milestone_logs(name)[0]

  diagnostics.start_cycle("all_zero_data", 10.0)
  mixed_rawx = build_rawx_frame(
    week=2427,
    leap_seconds_valid=True,
    measurements=((0, 34), (6, 31)),
  )
  diagnostics.note_rawx(mixed_rawx, 11.0)
  diagnostics.note_rawx(mixed_rawx, 12.0)

  for name in milestone_names:
    matching_logs = milestone_logs(name)
    assert len(matching_logs) == 2
    assert "cycle=2" in matching_logs[-1]
    assert "reason=all_zero_data" in matching_logs[-1]
    assert "measurement_count=2" in matching_logs[-1]
    assert "measurement_counts_by_gnss={0: 1, 6: 1}" in matching_logs[-1]
    assert "maximum_cno_by_gnss={0: 34, 6: 31}" in matching_logs[-1]


def test_time_assistance_rejection_and_later_acceptance_are_separate(
  monkeypatch,
):
  acknowledgments = iter(
    (
      SimpleNamespace(
        accepted=False,
        acknowledgment_type=0,
        version=0,
        info_code=5,
        message_id=0x40,
      ),
      SimpleNamespace(
        accepted=True,
        acknowledgment_type=1,
        version=0,
        info_code=0,
        message_id=0x40,
      ),
    )
  )
  sent_at = iter((10.1, 40.1))
  monotonic_values = iter((10.0, 10.2, 40.0, 40.2))
  attempts = []

  monkeypatch.setattr(
    pigeond,
    "_begin_response_transaction",
    lambda _pigeon, _message: SimpleNamespace(sent_at=next(sent_at)),
  )
  monkeypatch.setattr(
    pigeond,
    "wait_for_matching_mga_ack",
    lambda *_args, **_kwargs: next(acknowledgments),
  )

  first = pigeond.send_time_assistance(
    object(),
    assistance_time=datetime(2026, 7, 10, tzinfo=UTC),
    source="system_synchronized",
    diagnostic_context="cycle=1, reason=process_start",
    diagnostic_callback=attempts.append,
    monotonic=lambda: next(monotonic_values),
  )
  second = pigeond.send_time_assistance(
    object(),
    assistance_time=datetime(2026, 7, 10, tzinfo=UTC),
    source="system_synchronized",
    diagnostic_context="cycle=1, reason=runtime_retry",
    diagnostic_callback=attempts.append,
    monotonic=lambda: next(monotonic_values),
  )

  assert not first
  assert second
  assert len(attempts) == 2

  rejected, accepted = attempts
  assert rejected.attempted_at == 10.0
  assert rejected.written_at == 10.1
  assert rejected.ack_observed_at == 10.2
  assert rejected.write_status is pigeond.TimeAssistanceWriteStatus.SUCCEEDED
  assert rejected.ack_status is pigeond.TimeAssistanceAckStatus.REJECTED
  assert rejected.ack_info_code == 5
  assert rejected.accepted_at is None
  assert rejected.message_id == 0x40
  assert rejected.message_type == 0x10
  assert rejected.diagnostic_context == "cycle=1, reason=process_start"

  assert accepted.attempted_at == 40.0
  assert accepted.written_at == 40.1
  assert accepted.ack_observed_at == 40.2
  assert accepted.ack_status is pigeond.TimeAssistanceAckStatus.ACCEPTED
  assert accepted.ack_info_code == 0
  assert accepted.accepted_at == 40.2
  assert accepted.diagnostic_context == "cycle=1, reason=runtime_retry"


def test_time_assistance_diagnostic_callback_is_fail_open(
  monkeypatch,
):
  callback_errors = []
  monkeypatch.setattr(
    pigeond.cloudlog,
    "exception",
    callback_errors.append,
  )

  def fail_callback(_diagnostic):
    raise RuntimeError("diagnostic sink failed")

  assert pigeond.send_time_assistance(
    FakePigeon(),
    assistance_time=datetime(2026, 7, 10, tzinfo=UTC),
    source="system_synchronized",
    diagnostic_callback=fail_callback,
  )
  assert callback_errors == ["GPS time assistance diagnostic callback failed"]


def test_time_assistance_log_includes_cycle_context(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    network_host_observation,
  )

  receiver = FakePigeon()
  diagnostics = pigeond.GpsStartupDiagnostics(100.0)
  diagnostics.start_cycle("process_start", 101.0)

  assert pigeond.send_time_assistance(
    receiver,
    diagnostic_context=diagnostics.time_assistance_context(106.0),
  )
  time_log = next(message for message in logs if message.startswith("Time assistance"))
  assert "Time assistance written and accepted by ublox" in time_log
  assert "source=network_synchronized" in time_log
  assert "write_result=succeeded" in time_log
  assert "ack_result=accepted" in time_log
  assert "cycle=1" in time_log
  assert "reason=process_start" in time_log
  assert "process_elapsed_seconds=6.0" in time_log
  assert "cycle_elapsed_seconds=5.0" in time_log


def test_failed_time_assistance_log_includes_cycle_context(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "exception", logs.append)

  class FailingPigeon:
    def send(self, message: bytes) -> None:
      raise OSError("write failed")

  assert not pigeond.send_time_assistance(
    FailingPigeon(),
    assistance_time=datetime(2026, 7, 10, tzinfo=UTC),
    source="rtc_estimate",
    diagnostic_context="cycle=2, reason=all_zero_data, process_elapsed_seconds=40.0, cycle_elapsed_seconds=5.0",
  )
  assert "Time assistance serial write failed" in logs[0]
  assert "source=rtc_estimate" in logs[0]
  assert "write_result=failed" in logs[0]
  assert "ack_result=not_attempted" in logs[0]
  assert "cycle=2" in logs[0]
  assert "reason=all_zero_data" in logs[0]


def test_time_assistance_logs_exclude_time_and_position(monkeypatch):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  receiver = FakePigeon()

  assert pigeond.send_time_assistance(
    receiver,
    assistance_time=datetime(2026, 7, 10, 12, 34, 56, tzinfo=UTC),
    source="rtc_estimate",
    diagnostic_context=", ".join(
      (
        "cycle=3",
        "reason=all_zero_data",
        "process_elapsed_seconds=10.0",
        "cycle_elapsed_seconds=1.0",
      )
    ),
  )

  combined_logs = "\n".join(logs)
  assert "2026" not in combined_logs
  assert "12:34:56" not in combined_logs
  assert "latitude" not in combined_logs
  assert "longitude" not in combined_logs
  assert "source=rtc_estimate" in combined_logs
  assert "cycle=3" in combined_logs
  assert "reason=all_zero_data" in combined_logs


def test_cached_rtc_time_assistance_rejects_missing_anchor(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  monkeypatch.setattr(
    pigeond,
    "load_cache",
    lambda *args, **kwargs: SimpleNamespace(rtc_counter_seconds=None),
  )
  monkeypatch.setattr(
    pigeond,
    "read_rtc_counter_seconds",
    lambda: 100,
  )

  assert pigeond.cached_rtc_time_assistance("receiver") is None
  assert any("cache has no RTC anchor" in message for message in logs)


def test_cached_rtc_time_assistance_rejects_unavailable_current_rtc(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  monkeypatch.setattr(
    pigeond,
    "load_cache",
    lambda *args, **kwargs: SimpleNamespace(rtc_counter_seconds=100),
  )
  monkeypatch.setattr(
    pigeond,
    "read_rtc_counter_seconds",
    lambda: None,
  )

  assert pigeond.cached_rtc_time_assistance("receiver") is None
  assert any("current RTC unavailable" in message for message in logs)


def test_cached_rtc_time_assistance_rejects_rollback(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  monkeypatch.setattr(pigeond.cloudlog, "warning", logs.append)
  monkeypatch.setattr(
    pigeond,
    "load_cache",
    lambda *args, **kwargs: SimpleNamespace(rtc_counter_seconds=200),
  )
  monkeypatch.setattr(
    pigeond,
    "read_rtc_counter_seconds",
    lambda: 100,
  )

  assert pigeond.cached_rtc_time_assistance("receiver") is None
  assert any("RTC rollback detected" in message for message in logs)
  assert any("saved_rtc_seconds=200" in message for message in logs)
  assert any("current_rtc_seconds=100" in message for message in logs)


def test_cached_rtc_time_assistance_rejects_excessive_elapsed(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  monkeypatch.setattr(
    pigeond,
    "load_cache",
    lambda *args, **kwargs: SimpleNamespace(rtc_counter_seconds=100),
  )
  monkeypatch.setattr(
    pigeond,
    "read_rtc_counter_seconds",
    lambda: 200,
  )

  def reject_estimate(inventory, current_rtc_seconds):
    rejection = pigeond.RtcEstimateRejection(
      pigeond.RtcEstimateRejectionReason.ELAPSED_TIME_ABOVE_MAXIMUM,
      elapsed_seconds=999,
    )
    return None, ((inventory.primary, rejection),)

  monkeypatch.setattr(pigeond, "select_rtc_estimate", reject_estimate)

  assert pigeond.cached_rtc_time_assistance("receiver") is None
  assert any("elapsed_seconds=999" in message and "maximum_elapsed_seconds=" in message for message in logs)


def test_cached_rtc_time_assistance_uses_evaluator_reason_and_reads_once(
  monkeypatch,
):
  logs = []
  rtc_reads = 0

  def read_rtc():
    nonlocal rtc_reads
    rtc_reads += 1
    return 200

  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  monkeypatch.setattr(
    pigeond,
    "load_cache",
    lambda *args, **kwargs: SimpleNamespace(rtc_counter_seconds=100),
  )
  monkeypatch.setattr(pigeond, "read_rtc_counter_seconds", read_rtc)

  def reject_estimate(inventory, current_rtc_seconds):
    rejection = pigeond.RtcEstimateRejection(pigeond.RtcEstimateRejectionReason.CURRENT_RTC_UNAVAILABLE)
    return None, ((inventory.primary, rejection),)

  monkeypatch.setattr(pigeond, "select_rtc_estimate", reject_estimate)

  assert pigeond.cached_rtc_time_assistance("receiver") is None
  assert rtc_reads == 1
  assert any("current RTC unavailable" in message for message in logs)


def test_cross_boot_rtc_logging_is_explicitly_nonoperational(
  monkeypatch,
):
  messages = []
  monkeypatch.setattr(
    pigeond.cloudlog,
    "info",
    messages.append,
  )
  candidate = RtcObservationCandidate(
    candidate_utc=datetime(2026, 7, 22, 21, 1, 42, tzinfo=UTC),
    uncertainty_seconds=31.0,
    anchor_generation="previous",
    anchor_sequence=7,
    anchor_boot_id="12345678-1234-5678-9234-567812345678",
    current_boot_id="87654321-4321-6789-9234-567812345678",
    anchor_trusted_utc=datetime(2026, 7, 22, 21, tzinfo=UTC),
    anchor_rtc_epoch_seconds=1_000,
    current_rtc_epoch_seconds=1_102,
    rtc_elapsed_seconds=102,
    current_boottime_seconds=12.0,
    rtc_advanced=True,
    elapsed_covers_uptime=True,
    rtc_voltage_status_supported=False,
    rtc_voltage_status_flags=None,
  )
  observation = CrossBootRtcObservation(
    state=RtcObservationState.OBSERVED,
    reason=RtcObservationReason.CROSS_BOOT_CANDIDATE_OBSERVED,
    candidate=candidate,
    first_rtc_epoch_seconds=1_100,
    second_rtc_epoch_seconds=1_102,
    first_boottime_seconds=10.0,
    second_boottime_seconds=12.0,
    first_observed_at=50.0,
    second_observed_at=52.0,
    tick_elapsed_seconds=2.0,
    rtc_tick_delta_seconds=2,
    boottime_tick_delta_seconds=2.0,
    tick_consistent=True,
  )

  pigeond.log_cross_boot_rtc_observation(observation)

  assert len(messages) == 1
  assert "GPS cross-boot RTC observation" in messages[0]
  assert "state=observed" in messages[0]
  assert "authorized=false" in messages[0]
  assert "operational=false" in messages[0]
  assert "anchor_generation=previous" in messages[0]
  assert "rtc_tick_delta_seconds=2" in messages[0]
  assert "tick_consistent=true" in messages[0]


def test_independent_receiver_utc_is_aligned_to_current_boottime(
  monkeypatch,
):
  calls = []

  class Authority:
    def observe_independent_time(self, **kwargs):
      calls.append(kwargs)
      return SimpleNamespace(
        authorized_time=SimpleNamespace(
          utc=kwargs["utc"],
          uncertainty_seconds=kwargs["uncertainty_seconds"],
          source=kwargs["source"],
          provenance=kwargs["provenance"],
          independent=True,
          observed_boottime_seconds=(kwargs["observed_boottime_seconds"]),
        ),
        anchor_write_status=SimpleNamespace(value="saved"),
        anchor_write_reason=SimpleNamespace(value="anchor_missing_or_invalid"),
        anchor_comparison=None,
      )

  monkeypatch.setattr(
    pigeond,
    "read_boottime_seconds",
    lambda: 500.0,
  )
  observation = pigeond.ReceiverUtcObservation(
    classification=(pigeond.ReceiverUtcClassification.UNASSISTED_GNSS),
    reason="fresh_gnss_time_evidence",
    cycle_id=1,
    utc=datetime(2026, 7, 23, 12, tzinfo=UTC),
    observed_at=100.0,
    time_accuracy_ns=25_000_000,
    independent=True,
    time_assistance_written=False,
    time_assistance_source=None,
    rawx_observed_at=99.9,
    rawx_measurement_count=1,
    gps_week_valid=True,
    leap_second_valid=True,
  )

  result = pigeond.authorize_independent_receiver_utc(
    Authority(),
    observation,
    now=102.0,
  )

  assert result is not None
  assert len(calls) == 1
  assert calls[0]["utc"] == datetime(
    2026,
    7,
    23,
    12,
    0,
    2,
    tzinfo=UTC,
  )
  assert calls[0]["observed_boottime_seconds"] == 500.0
  assert calls[0]["uncertainty_seconds"] == 0.025


def test_receiver_correction_write_is_recorded_before_ack(
  monkeypatch,
):
  receiver = FakePigeon(auto_ack=False)
  provenance = pigeond.ReceiverTimeProvenanceTracker()
  provenance.start_cycle(1, 100.0)
  monkeypatch.setattr(
    pigeond,
    "read_boottime_seconds",
    lambda: 55.0,
  )

  assert not pigeond.send_time_assistance(
    receiver,
    assistance_time=datetime(2026, 7, 23, 12, tzinfo=UTC),
    source="system_synchronized",
    ack_timeout=0.0,
    time_provenance=provenance,
    assistance_boottime_seconds=55.0,
    independent=True,
    source_provenance=(pigeond.TimeProvenance.NETWORK_INDEPENDENT),
    correction=True,
  )
  observation = provenance.time_assistance_observation
  assert provenance.correction_written
  assert observation.correction_written
  assert observation.source == "system_synchronized"
  assert observation.written_boottime_seconds == 55.0
  assert observation.independent is True


def test_receiver_correction_is_sent_at_most_once_per_cycle():
  receiver = FakePigeon()
  provenance = pigeond.ReceiverTimeProvenanceTracker()
  provenance.start_cycle(1, 100.0)
  provenance.note_time_assistance_written(
    source="same_boot_boottime",
    assistance_utc=datetime(2026, 7, 23, 11, 58, 15, tzinfo=UTC),
    uncertainty_seconds=31.0,
    now=100.5,
    written_boottime_seconds=50.0,
    independent=False,
    provenance=pigeond.TimeProvenance.EXTERNAL_OR_UNKNOWN,
  )
  independent = pigeond.IndependentTimeObservation(
    utc=datetime(2026, 7, 23, 12, tzinfo=UTC),
    observed_boottime_seconds=150.0,
    uncertainty_seconds=30.0,
    source=pigeond.TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=pigeond.TimeProvenance.NETWORK_INDEPENDENT,
  )

  first, first_accepted = pigeond.maybe_send_receiver_time_correction(
    receiver,
    provenance,
    independent,
  )
  second, second_accepted = pigeond.maybe_send_receiver_time_correction(
    receiver,
    provenance,
    independent,
  )

  assert first.should_correct
  assert first_accepted
  assert len(receiver.sent) == 1
  assert not second.should_correct
  assert second.reason.value == "correction_already_written"
  assert not second_accepted
  assert len(receiver.sent) == 1


def test_receiver_independent_source_is_never_echoed_back():
  receiver = FakePigeon()
  provenance = pigeond.ReceiverTimeProvenanceTracker()
  provenance.start_cycle(1, 100.0)
  provenance.note_time_assistance_written(
    source="same_boot_boottime",
    assistance_utc=datetime(2026, 7, 23, 11, 58, 15, tzinfo=UTC),
    uncertainty_seconds=31.0,
    now=100.5,
    written_boottime_seconds=50.0,
    independent=False,
    provenance=pigeond.TimeProvenance.EXTERNAL_OR_UNKNOWN,
  )
  independent = pigeond.IndependentTimeObservation(
    utc=datetime(2026, 7, 23, 12, tzinfo=UTC),
    observed_boottime_seconds=150.0,
    uncertainty_seconds=0.025,
    source=(pigeond.TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS),
    provenance=pigeond.TimeProvenance.GNSS_INDEPENDENT,
  )

  decision, accepted = pigeond.maybe_send_receiver_time_correction(
    receiver,
    provenance,
    independent,
  )

  assert not decision.should_correct
  assert decision.reason.value == "receiver_self_source"
  assert not accepted
  assert receiver.sent == []


def receiver_host_observation(
  *,
  generation: str = "receiver:1",
):
  return pigeond.HostTimeObservation(
    utc=datetime(2026, 7, 23, 12, tzinfo=UTC),
    observed_boottime_seconds=100.0,
    uncertainty_seconds=30.0,
    source=pigeond.HostTimeSource.RECEIVER_DERIVED,
    independent=False,
    generation=generation,
  )


def test_receiver_derived_host_cannot_send_default_assistance(
  monkeypatch,
):
  receiver = FakePigeon()
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    receiver_host_observation,
  )

  assert not pigeond.send_time_assistance(receiver)
  assert receiver.sent == []


def test_receiver_derived_host_cannot_authorize_cache_promotion(
  monkeypatch,
):
  monkeypatch.setattr(
    pigeond,
    "read_host_time_observation",
    receiver_host_observation,
  )

  assert (
    pigeond.cache_promotion_trusted_now(
      None,
      None,
      None,
      receiver_utc_fresh=False,
      receiver_utc_independent=False,
    )
    is None
  )


def test_later_network_generation_is_processed():
  receiver = receiver_host_observation()
  receiver_state = pigeond.host_time_processing_state(
    receiver,
    None,
    now=10.0,
  )
  assert receiver_state.persistence_complete

  network = network_host_observation(generation="network:2")
  assert pigeond.host_time_requires_processing(
    receiver_state,
    network,
    now=11.0,
  )


def test_failed_network_anchor_write_is_retried():
  network = network_host_observation()
  failed = SimpleNamespace(
    anchor_write_status=pigeond.AnchorWriteStatus.FAILED,
  )
  state = pigeond.host_time_processing_state(
    network,
    failed,
    now=10.0,
  )

  assert not state.persistence_complete
  assert not pigeond.host_time_requires_processing(
    state,
    network,
    now=14.9,
  )
  assert pigeond.host_time_requires_processing(
    state,
    network,
    now=15.0,
  )


def test_saved_network_anchor_completes_processing():
  network = network_host_observation()
  saved = SimpleNamespace(
    anchor_write_status=pigeond.AnchorWriteStatus.SAVED,
  )
  state = pigeond.host_time_processing_state(
    network,
    saved,
    now=10.0,
  )

  assert state.persistence_complete
  assert not pigeond.host_time_requires_processing(
    state,
    network,
    now=100.0,
  )
