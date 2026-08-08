from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from openpilot.system.ubloxd import provisional_yuma_reference
from openpilot.system.ubloxd.provisional_yuma_reference import (
  PROVISIONAL_YUMA_MAX_RTC_ELAPSED_SECONDS,
  PROVISIONAL_YUMA_DISABLE_REASON_VALIDATION_DISAGREES,
  PROVISIONAL_YUMA_MAX_UNCERTAINTY_SECONDS,
  ProvisionalYumaRejection,
  evaluate_provisional_yuma_reference,
  load_provisional_yuma_boot_disable_state,
  store_provisional_yuma_boot_disable_state,
  transmit_provisional_yuma_reference,
)
from openpilot.system.ubloxd.rtc_time_observation import (
  CrossBootRtcObservation,
  RtcObservationCandidate,
  RtcObservationReason,
  RtcObservationState,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
  TrustedTimeSource,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AnchorWriteStatus,
  AuthorizedTime,
  TimeAuthorizationEvidence,
  TimeAuthorityEvaluation,
  TimeAuthorityRejectionReason,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  YumaAlmanacTransmitResult,
  YumaAlmanacTransmitStatus,
)


UTC_NOW = datetime(2026, 7, 26, 2, 0, tzinfo=UTC)
ANCHOR_BOOT = "11111111-1111-1111-1111-111111111111"
CURRENT_BOOT = "22222222-2222-2222-2222-222222222222"


def candidate(**overrides):
  values = {
    "candidate_utc": UTC_NOW,
    "uncertainty_seconds": 3.0,
    "anchor_generation": "primary",
    "anchor_sequence": 17,
    "anchor_boot_id": ANCHOR_BOOT,
    "current_boot_id": CURRENT_BOOT,
    "anchor_trusted_utc": UTC_NOW,
    "anchor_rtc_epoch_seconds": 1_000,
    "current_rtc_epoch_seconds": 15_400,
    "rtc_elapsed_seconds": 14_400,
    "current_boottime_seconds": 2.0,
    "rtc_advanced": True,
    "elapsed_covers_uptime": True,
    "rtc_voltage_status_supported": True,
    "rtc_voltage_status_flags": 0,
    "anchor_source": TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    "anchor_provenance": TimeProvenance.NETWORK_INDEPENDENT,
    "anchor_authorized": True,
    "anchor_independent": True,
  }
  values.update(overrides)
  return RtcObservationCandidate(**values)


def observation(**candidate_overrides):
  return CrossBootRtcObservation(
    state=RtcObservationState.OBSERVED,
    reason=RtcObservationReason.CROSS_BOOT_CANDIDATE_OBSERVED,
    candidate=candidate(**candidate_overrides),
    first_rtc_epoch_seconds=15_398,
    second_rtc_epoch_seconds=15_400,
    first_boottime_seconds=0.0,
    second_boottime_seconds=2.0,
    first_observed_at=0.0,
    second_observed_at=2.0,
    tick_elapsed_seconds=2.0,
    rtc_tick_delta_seconds=2,
    boottime_tick_delta_seconds=2.0,
    tick_consistent=True,
  )


def cross_boot_authority():
  return TimeAuthorityEvaluation(
    authorized_time=None,
    rejection_reason=(
      TimeAuthorityRejectionReason.CROSS_BOOT_CONTINUITY_UNPROVABLE
    ),
    anchor_write_status=AnchorWriteStatus.NOT_REQUIRED,
    selected_anchor_generation="primary",
    selected_anchor_sequence=17,
  )


def test_reference_is_eligible_only_for_strict_independent_anchor():
  decision = evaluate_provisional_yuma_reference(
    observation(),
    cross_boot_authority(),
    receiver_cycle=4,
  )

  assert decision.eligible
  assert decision.rejection is None
  assert decision.reference is not None
  assert decision.reference.receiver_cycle == 4
  assert decision.reference.anchor_source is TrustedTimeSource.SYSTEM_SYNCHRONIZED
  assert decision.reference.anchor_provenance is TimeProvenance.NETWORK_INDEPENDENT


def test_authorized_time_suppresses_provisional_reference():
  authority = TimeAuthorityEvaluation(
    authorized_time=AuthorizedTime(
      utc=UTC_NOW,
      uncertainty_seconds=1.0,
      source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
      provenance=TimeProvenance.NETWORK_INDEPENDENT,
      independent=True,
      evidence=TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED,
    ),
    rejection_reason=None,
    anchor_write_status=AnchorWriteStatus.NOT_REQUIRED,
  )

  decision = evaluate_provisional_yuma_reference(
    observation(),
    authority,
    receiver_cycle=0,
  )

  assert not decision.eligible
  assert decision.rejection is ProvisionalYumaRejection.AUTHORIZED_TIME_AVAILABLE


def test_reference_rejects_selection_source_age_and_uncertainty():
  mismatch = evaluate_provisional_yuma_reference(
    observation(),
    TimeAuthorityEvaluation(
      authorized_time=None,
      rejection_reason=TimeAuthorityRejectionReason.CROSS_BOOT_CONTINUITY_UNPROVABLE,
      anchor_write_status=AnchorWriteStatus.NOT_REQUIRED,
      selected_anchor_generation="previous",
      selected_anchor_sequence=17,
    ),
    receiver_cycle=0,
  )
  assert mismatch.rejection is ProvisionalYumaRejection.ANCHOR_SELECTION_MISMATCH

  source = evaluate_provisional_yuma_reference(
    observation(anchor_provenance=TimeProvenance.EXTERNAL_OR_UNKNOWN),
    cross_boot_authority(),
    receiver_cycle=0,
  )
  assert source.rejection is ProvisionalYumaRejection.ANCHOR_SOURCE_NOT_INDEPENDENT

  age = evaluate_provisional_yuma_reference(
    observation(rtc_elapsed_seconds=PROVISIONAL_YUMA_MAX_RTC_ELAPSED_SECONDS + 1),
    cross_boot_authority(),
    receiver_cycle=0,
  )
  assert age.rejection is ProvisionalYumaRejection.RTC_ELAPSED_ABOVE_MAXIMUM

  uncertainty = evaluate_provisional_yuma_reference(
    observation(uncertainty_seconds=PROVISIONAL_YUMA_MAX_UNCERTAINTY_SECONDS + 0.1),
    cross_boot_authority(),
    receiver_cycle=0,
  )
  assert uncertainty.rejection is ProvisionalYumaRejection.UNCERTAINTY_ABOVE_MAXIMUM


def test_reference_rejects_supported_rtc_voltage_fault():
  fault = evaluate_provisional_yuma_reference(
    observation(rtc_voltage_status_supported=True, rtc_voltage_status_flags=1),
    cross_boot_authority(),
    receiver_cycle=0,
  )
  assert fault.rejection is ProvisionalYumaRejection.RTC_VOLTAGE_FAULT

  missing_flags = evaluate_provisional_yuma_reference(
    observation(rtc_voltage_status_supported=True, rtc_voltage_status_flags=None),
    cross_boot_authority(),
    receiver_cycle=0,
  )
  assert missing_flags.rejection is ProvisionalYumaRejection.RTC_VOLTAGE_FAULT

  unsupported = evaluate_provisional_yuma_reference(
    observation(rtc_voltage_status_supported=False, rtc_voltage_status_flags=7),
    cross_boot_authority(),
    receiver_cycle=0,
  )
  assert unsupported.eligible


def test_transmission_sends_all_snapshot_prns_without_time_assistance(monkeypatch):
  reference = evaluate_provisional_yuma_reference(
    observation(),
    cross_boot_authority(),
    receiver_cycle=2,
  ).reference
  assert reference is not None

  frames = tuple(
    bytes((0xB5, 0x62, 0x13, 0x00, 0, 0, 0, 0, prn, 0))
    for prn in (1, 7, 20, 32)
  )
  stored = SimpleNamespace(
    almanac=SimpleNamespace(
      frames=frames,
      ubx_data=b"".join(frames),
    ),
    downloaded_at_utc=UTC_NOW,
  )
  captured = {}

  def fake_transmit(send_message, **kwargs):
    captured.update(kwargs)
    for frame in frames:
      send_message(frame)
    return YumaAlmanacTransmitResult(
      status=YumaAlmanacTransmitStatus.COMPLETE,
      requested_satellite_ids=tuple(sorted(kwargs["satellite_ids"])),
      attempted_satellite_ids=(1, 7, 20, 32),
      accepted_satellite_ids=(1, 7, 20, 32),
      reference_time_utc=reference.utc,
      downloaded_at_utc=UTC_NOW,
    )

  monkeypatch.setattr(
    "openpilot.system.ubloxd.provisional_yuma_reference.transmit_public_yuma_almanac",
    fake_transmit,
  )
  sent = []
  monotonic_values = iter((5.0, 5.25))
  outcome = transmit_provisional_yuma_reference(
    reference,
    sent.append,
    path=Path("unused"),
    cache_loader=lambda _path: stored,
    reference_validator=lambda _almanac, now: now,
    monotonic=lambda: next(monotonic_values),
  )

  assert outcome.error is None
  assert outcome.satellite_ids == (1, 7, 20, 32)
  assert sent == list(frames)
  assert captured["trusted_now"] == reference.utc
  assert captured["satellite_ids"] == frozenset((1, 7, 20, 32))
  assert outcome.receiver_write_attempted
  assert not outcome.time_assistance_written
  assert not outcome.cache_quality_changed
  assert not outcome.anchor_written
  assert not outcome.system_clock_changed
  assert not outcome.receiver_reset


def test_module_has_static_isolation_boundary():
  source = Path(
    provisional_yuma_reference.__file__
  ).read_text(encoding="utf-8")
  forbidden = (
    "build_time_assistance_message",
    "send_time_assistance",
    "set_time_anchor",
    "_refresh_restored_quality",
    "refresh_restored_navigation_quality",
    "CacheAgeEvidence",
    "write_navigation_assistance_cache",
    "observe_independent_time",
    "set_system_time",
    "set_power",
    "initialize_receiver_cycle",
  )
  for token in forbidden:
    assert token not in source


def test_unexpected_transmit_exception_preserves_receiver_write_attempt(monkeypatch):
  reference = evaluate_provisional_yuma_reference(
    observation(),
    cross_boot_authority(),
    receiver_cycle=2,
  ).reference
  assert reference is not None
  frames = tuple(
    bytes((0xB5, 0x62, 0x13, 0x00, 0, 0, 0, 0, prn, 0))
    for prn in (1, 2)
  )
  stored = SimpleNamespace(
    almanac=SimpleNamespace(frames=frames, ubx_data=b"".join(frames)),
    downloaded_at_utc=UTC_NOW,
  )

  def unexpected_failure(send_message, **_kwargs):
    send_message(frames[0])
    send_message(frames[1])
    raise RuntimeError("unexpected sender failure")

  monkeypatch.setattr(
    provisional_yuma_reference,
    "transmit_public_yuma_almanac",
    unexpected_failure,
  )
  sent = []
  monotonic_values = iter((5.0, 5.25))
  outcome = transmit_provisional_yuma_reference(
    reference,
    sent.append,
    path=Path("unused"),
    cache_loader=lambda _path: stored,
    reference_validator=lambda _almanac, now: now,
    monotonic=lambda: next(monotonic_values),
  )

  assert sent == list(frames)
  assert outcome.receiver_write_attempted
  assert outcome.transmit_result is None
  assert outcome.error == "RuntimeError: unexpected sender failure"


def test_prewrite_failure_does_not_report_receiver_write():
  reference = evaluate_provisional_yuma_reference(
    observation(),
    cross_boot_authority(),
    receiver_cycle=2,
  ).reference
  assert reference is not None
  monotonic_values = iter((5.0, 5.25))
  outcome = transmit_provisional_yuma_reference(
    reference,
    lambda _message: None,
    path=Path("unused"),
    cache_loader=lambda _path: (_ for _ in ()).throw(OSError("missing")),
    monotonic=lambda: next(monotonic_values),
  )

  assert not outcome.receiver_write_attempted
  assert outcome.transmit_result is None
  assert outcome.error == "OSError: missing"


def test_boot_disable_state_is_atomic_scoped_and_fail_closed(tmp_path):
  path = tmp_path / "provisional_yuma_disabled.json"
  store_provisional_yuma_boot_disable_state(
    CURRENT_BOOT,
    PROVISIONAL_YUMA_DISABLE_REASON_VALIDATION_DISAGREES,
    path=path,
  )

  same_boot = load_provisional_yuma_boot_disable_state(CURRENT_BOOT, path=path)
  assert same_boot.disabled
  assert same_boot.reason == PROVISIONAL_YUMA_DISABLE_REASON_VALIDATION_DISAGREES
  assert path.stat().st_mode & 0o777 == 0o600

  new_boot = load_provisional_yuma_boot_disable_state(ANCHOR_BOOT, path=path)
  assert not new_boot.disabled
  assert new_boot.stored_boot_id == CURRENT_BOOT

  path.write_text("not-json", encoding="utf-8")
  corrupt = load_provisional_yuma_boot_disable_state(CURRENT_BOOT, path=path)
  assert corrupt.disabled
  assert corrupt.error is not None

  invalid_boot = load_provisional_yuma_boot_disable_state(None, path=path)
  assert invalid_boot.disabled
  assert invalid_boot.error == "invalid_current_boot_id"
def test_decision_telemetry_is_atomic_private_and_merges_events(tmp_path):
  import json
  import stat

  path = tmp_path / "provisional_yuma_last_decision.json"
  observation_value = observation()
  authority_value = cross_boot_authority()

  provisional_yuma_reference.store_provisional_yuma_decision_event(
    "rtc_observation",
    current_boot_id=CURRENT_BOOT,
    receiver_cycle=7,
    observed_at=2.0,
    observation=observation_value,
    authority=authority_value,
    path=path,
  )
  provisional_yuma_reference.store_provisional_yuma_decision_event(
    "reference_decision",
    current_boot_id=CURRENT_BOOT,
    receiver_cycle=7,
    observed_at=2.1,
    decision=provisional_yuma_reference.evaluate_provisional_yuma_reference(
      observation_value,
      authority_value,
      receiver_cycle=7,
    ),
    accepted=True,
    path=path,
  )

  payload = json.loads(path.read_text(encoding="utf-8"))
  assert payload["version"] == 1
  assert payload["boot_id"] == CURRENT_BOOT
  assert payload["receiver_cycle"] == 7
  assert payload["updated_event"] == "reference_decision"
  assert set(payload["events"]) == {
    "rtc_observation",
    "reference_decision",
  }
  assert (
    payload["events"]["rtc_observation"]["observation"]["state"]
    == "observed"
  )
  assert (
    payload["events"]["reference_decision"]["decision"]["reference"]
    ["anchor_sequence"]
    == 17
  )
  assert stat.S_IMODE(path.stat().st_mode) == 0o600
  assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_decision_telemetry_new_boot_replaces_old_events(tmp_path):
  import json

  path = tmp_path / "provisional_yuma_last_decision.json"
  provisional_yuma_reference.store_provisional_yuma_decision_event(
    "rtc_observation",
    current_boot_id=CURRENT_BOOT,
    receiver_cycle=1,
    observed_at=1.0,
    observation=observation(),
    path=path,
  )
  next_boot = "33333333-3333-3333-3333-333333333333"
  provisional_yuma_reference.store_provisional_yuma_decision_event(
    "rtc_observation",
    current_boot_id=next_boot,
    receiver_cycle=1,
    observed_at=1.0,
    observation=observation(),
    path=path,
  )

  payload = json.loads(path.read_text(encoding="utf-8"))
  assert payload["boot_id"] == next_boot
  assert list(payload["events"]) == ["rtc_observation"]
