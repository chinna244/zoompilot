from datetime import UTC, datetime
from types import SimpleNamespace

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.provisional_yuma_reference import (
  PROVISIONAL_YUMA_DISABLE_REASON_VALIDATION_DISAGREES,
  ProvisionalYumaBootDisableState,
  ProvisionalYumaReferenceTime,
  ProvisionalYumaTransmissionOutcome,
  load_provisional_yuma_boot_disable_state,
  store_provisional_yuma_boot_disable_state,
)
from openpilot.system.ubloxd.trusted_time_anchor import TimeProvenance, TrustedTimeSource
from openpilot.system.ubloxd.trusted_time_validation import (
  CrossBootRtcValidation,
  CrossBootRtcValidationStatus,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  YumaAlmanacTransmitResult,
  YumaAlmanacTransmitStatus,
)


UTC_NOW = datetime(2026, 7, 26, 2, 0, tzinfo=UTC)
CURRENT_BOOT = "22222222-2222-2222-2222-222222222222"
NEXT_BOOT = "33333333-3333-3333-3333-333333333333"


class FakeRuntime:
  def __init__(self):
    self.evaluate_calls = 0
    self.anchor = None

  def cancel(self, *, now, reason):
    return None

  def set_time_anchor(self, utc, monotonic, source):
    self.anchor = (utc, monotonic, source)

  def evaluate(self, send_message, **kwargs):
    self.evaluate_calls += 1
    return None


def initialization(completed_at=0.0):
  return SimpleNamespace(
    completed_at=completed_at,
    yuma_time_anchor_utc=None,
    yuma_time_anchor_source=None,
    yuma_time_anchor_monotonic=completed_at,
  )


def reference(cycle=0, current_boot_id=CURRENT_BOOT):
  return ProvisionalYumaReferenceTime(
    utc=UTC_NOW,
    observed_at=2.0,
    uncertainty_seconds=3.0,
    receiver_cycle=cycle,
    anchor_generation="primary",
    anchor_sequence=17,
    anchor_source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    anchor_provenance=TimeProvenance.NETWORK_INDEPENDENT,
    anchor_boot_id="11111111-1111-1111-1111-111111111111",
    current_boot_id=current_boot_id,
    rtc_elapsed_seconds=14_400,
  )


def transmitted_outcome(ref):
  result = YumaAlmanacTransmitResult(
    status=YumaAlmanacTransmitStatus.COMPLETE,
    requested_satellite_ids=(1, 2),
    attempted_satellite_ids=(1, 2),
    accepted_satellite_ids=(1, 2),
    reference_time_utc=ref.utc,
  )
  return ProvisionalYumaTransmissionOutcome(
    reference=ref,
    attempted_at=2.0,
    elapsed_ms=20.0,
    satellite_ids=(1, 2),
    snapshot_sha256="abc",
    validated_reference_utc=ref.utc,
    receiver_write_attempted=True,
    transmit_result=result,
    error=None,
  )


def make_feature(
  monkeypatch,
  cycle=0,
  *,
  current_boot_id=CURRENT_BOOT,
  disabled=False,
):
  runtime = FakeRuntime()
  monkeypatch.setattr(pigeond, "public_yuma_almanac_enabled", lambda _params: True)
  monkeypatch.setattr(pigeond, "create_yuma_supplementation_runtime", lambda *args, **kwargs: runtime)
  monkeypatch.setattr(pigeond, "read_boot_id", lambda: current_boot_id)
  monkeypatch.setattr(
    pigeond,
    "load_provisional_yuma_boot_disable_state",
    lambda boot_id: ProvisionalYumaBootDisableState(
      disabled, boot_id, boot_id if disabled else None,
      PROVISIONAL_YUMA_DISABLE_REASON_VALIDATION_DISAGREES if disabled else None,
      None,
    ),
  )
  monkeypatch.setattr(
    pigeond,
    "store_provisional_yuma_boot_disable_state",
    lambda _boot_id, _reason: None,
  )
  feature = pigeond.YumaSupplementationFeature(SimpleNamespace(), initialization(), cycle)
  return feature, runtime


def test_provisional_transmission_consumes_cycle_and_blocks_duplicate(monkeypatch):
  feature, runtime = make_feature(monkeypatch)
  ref = reference()
  monkeypatch.setattr(pigeond, "transmit_provisional_yuma_reference", lambda _ref, _send: transmitted_outcome(ref))

  assert feature.set_provisional_reference(ref)
  outcome = feature.evaluate_provisional(lambda _message: True, now=2.1, reliable_fix_available=False)

  assert outcome is not None
  assert outcome.receiver_write_attempted
  assert feature.cycle_injection_consumed
  assert feature.evaluate_provisional(lambda _message: True, now=2.2, reliable_fix_available=False) is None
  assert feature.evaluate(lambda _message: True, now=2.2, nav_sat=None, nav_sat_time=None, reliable_fix_available=False) is None
  assert runtime.evaluate_calls == 0


def test_pending_database_restore_blocks_provisional_transmission(monkeypatch):
  feature, runtime = make_feature(monkeypatch)
  ref = reference()
  calls = []
  monkeypatch.setattr(
    pigeond,
    "transmit_provisional_yuma_reference",
    lambda *args, **kwargs: calls.append(True),
  )

  assert feature.set_provisional_reference(ref)
  assert (
    feature.evaluate_provisional(
      lambda _message: True,
      now=2.1,
      reliable_fix_available=False,
      database_restore_pending=True,
    )
    is None
  )
  assert calls == []
  assert not feature.cycle_injection_consumed
  assert runtime.evaluate_calls == 0


def test_authorized_time_overrides_pending_provisional_reference(monkeypatch):
  feature, _runtime = make_feature(monkeypatch)
  ref = reference()
  calls = []
  monkeypatch.setattr(pigeond, "transmit_provisional_yuma_reference", lambda *args, **kwargs: calls.append(True))

  assert feature.set_provisional_reference(ref)
  feature.set_time_anchor(UTC_NOW, 3.0, "system_synchronized")

  assert feature.evaluate_provisional(lambda _message: True, now=3.1, reliable_fix_available=False) is None
  assert calls == []


def test_reliable_fix_permanently_suppresses_cycle_attempt(monkeypatch):
  feature, _runtime = make_feature(monkeypatch)
  ref = reference()
  calls = []
  monkeypatch.setattr(pigeond, "transmit_provisional_yuma_reference", lambda *args, **kwargs: calls.append(True))

  assert feature.set_provisional_reference(ref)
  assert feature.evaluate_provisional(lambda _message: True, now=2.1, reliable_fix_available=True) is None
  assert feature.evaluate_provisional(lambda _message: True, now=2.2, reliable_fix_available=False) is None
  assert calls == []


def test_failure_without_receiver_write_leaves_regular_yuma_available(monkeypatch):
  feature, runtime = make_feature(monkeypatch)
  ref = reference()
  failed = ProvisionalYumaTransmissionOutcome(
    reference=ref,
    attempted_at=2.0,
    elapsed_ms=1.0,
    satellite_ids=(),
    snapshot_sha256=None,
    validated_reference_utc=None,
    receiver_write_attempted=False,
    transmit_result=None,
    error="YumaAlmanacError: unavailable",
  )
  monkeypatch.setattr(pigeond, "transmit_provisional_yuma_reference", lambda _ref, _send: failed)

  assert feature.set_provisional_reference(ref)
  outcome = feature.evaluate_provisional(lambda _message: True, now=2.1, reliable_fix_available=False)
  assert outcome is failed
  assert not feature.cycle_injection_consumed

  assert feature.evaluate(lambda _message: True, now=2.2, nav_sat=None, nav_sat_time=None, reliable_fix_available=False) is None
  assert runtime.evaluate_calls == 1


def test_disagreement_disables_provisional_path_for_later_receiver_cycles(monkeypatch):
  feature, _runtime = make_feature(monkeypatch)
  ref = reference()
  monkeypatch.setattr(pigeond, "transmit_provisional_yuma_reference", lambda _ref, _send: transmitted_outcome(ref))
  assert feature.set_provisional_reference(ref)
  assert feature.evaluate_provisional(lambda _message: True, now=2.1, reliable_fix_available=False) is not None

  validation = CrossBootRtcValidation(
    status=CrossBootRtcValidationStatus.DISAGREES,
    reason="cross_boot_candidate_exceeds_allowed_error",
    validation_source=TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS,
    validation_provenance=TimeProvenance.GNSS_INDEPENDENT,
    validation_utc=UTC_NOW,
    validation_boottime_seconds=100.0,
    validation_uncertainty_seconds=1.0,
    candidate_utc_at_validation=UTC_NOW,
    candidate_error_seconds=40.0,
    allowed_error_seconds=10.0,
    anchor_generation="primary",
    anchor_sequence=17,
    anchor_boot_id=ref.anchor_boot_id,
    current_boot_id=ref.current_boot_id,
    rtc_elapsed_seconds=ref.rtc_elapsed_seconds,
    current_uptime_seconds=2.0,
    rtc_tick_delta_seconds=2,
    boottime_tick_delta_seconds=2.0,
    tick_consistent=True,
  )
  feature.note_cross_boot_validation(validation)
  feature.reset_receiver_cycle(initialization(4.0), 1)

  assert not feature.set_provisional_reference(reference(1))


def test_unexpected_write_exception_consumes_cycle_and_blocks_regular_yuma(monkeypatch):
  feature, runtime = make_feature(monkeypatch)
  ref = reference()
  failed_after_write = ProvisionalYumaTransmissionOutcome(
    reference=ref,
    attempted_at=2.0,
    elapsed_ms=1.0,
    satellite_ids=(1, 2),
    snapshot_sha256="abc",
    validated_reference_utc=ref.utc,
    receiver_write_attempted=True,
    transmit_result=None,
    error="RuntimeError: unexpected sender failure",
  )
  monkeypatch.setattr(
    pigeond,
    "transmit_provisional_yuma_reference",
    lambda _ref, _send: failed_after_write,
  )

  assert feature.set_provisional_reference(ref)
  outcome = feature.evaluate_provisional(
    lambda _message: None, now=2.1, reliable_fix_available=False
  )

  assert outcome is failed_after_write
  assert feature.cycle_injection_consumed
  assert feature.evaluate(
    lambda _message: True,
    now=2.2,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  ) is None
  assert runtime.evaluate_calls == 0


def test_disagreement_persists_across_pigeond_restart_for_same_boot(
  monkeypatch, tmp_path
):
  marker = tmp_path / "provisional_yuma_disabled.json"
  monkeypatch.setattr(pigeond, "public_yuma_almanac_enabled", lambda _params: True)
  monkeypatch.setattr(
    pigeond,
    "create_yuma_supplementation_runtime",
    lambda *args, **kwargs: FakeRuntime(),
  )
  monkeypatch.setattr(pigeond, "read_boot_id", lambda: CURRENT_BOOT)
  monkeypatch.setattr(
    pigeond,
    "load_provisional_yuma_boot_disable_state",
    lambda boot_id: load_provisional_yuma_boot_disable_state(boot_id, path=marker),
  )
  monkeypatch.setattr(
    pigeond,
    "store_provisional_yuma_boot_disable_state",
    lambda boot_id, reason: store_provisional_yuma_boot_disable_state(
      boot_id, reason, path=marker
    ),
  )

  first = pigeond.YumaSupplementationFeature(
    SimpleNamespace(), initialization(), 0
  )
  ref = reference()
  monkeypatch.setattr(
    pigeond,
    "transmit_provisional_yuma_reference",
    lambda _ref, _send: transmitted_outcome(ref),
  )
  assert first.set_provisional_reference(ref)
  assert first.evaluate_provisional(
    lambda _message: None, now=2.1, reliable_fix_available=False
  ) is not None

  validation = CrossBootRtcValidation(
    status=CrossBootRtcValidationStatus.DISAGREES,
    reason="cross_boot_candidate_exceeds_allowed_error",
    validation_source=TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS,
    validation_provenance=TimeProvenance.GNSS_INDEPENDENT,
    validation_utc=UTC_NOW,
    validation_boottime_seconds=100.0,
    validation_uncertainty_seconds=1.0,
    candidate_utc_at_validation=UTC_NOW,
    candidate_error_seconds=40.0,
    allowed_error_seconds=10.0,
    anchor_generation="primary",
    anchor_sequence=17,
    anchor_boot_id=ref.anchor_boot_id,
    current_boot_id=ref.current_boot_id,
    rtc_elapsed_seconds=ref.rtc_elapsed_seconds,
    current_uptime_seconds=2.0,
    rtc_tick_delta_seconds=2,
    boottime_tick_delta_seconds=2.0,
    tick_consistent=True,
  )
  first.note_cross_boot_validation(validation)

  restarted_same_boot = pigeond.YumaSupplementationFeature(
    SimpleNamespace(), initialization(4.0), 0
  )
  assert not restarted_same_boot.set_provisional_reference(reference())

  monkeypatch.setattr(pigeond, "read_boot_id", lambda: NEXT_BOOT)
  restarted_new_boot = pigeond.YumaSupplementationFeature(
    SimpleNamespace(), initialization(5.0), 0
  )
  assert restarted_new_boot.set_provisional_reference(
    reference(current_boot_id=NEXT_BOOT)
  )


def test_corrupt_boot_disable_marker_fails_closed(monkeypatch, tmp_path):
  marker = tmp_path / "provisional_yuma_disabled.json"
  marker.write_text("not-json", encoding="utf-8")
  monkeypatch.setattr(pigeond, "public_yuma_almanac_enabled", lambda _params: True)
  monkeypatch.setattr(
    pigeond,
    "create_yuma_supplementation_runtime",
    lambda *args, **kwargs: FakeRuntime(),
  )
  monkeypatch.setattr(pigeond, "read_boot_id", lambda: CURRENT_BOOT)
  monkeypatch.setattr(
    pigeond,
    "load_provisional_yuma_boot_disable_state",
    lambda boot_id: load_provisional_yuma_boot_disable_state(boot_id, path=marker),
  )

  feature = pigeond.YumaSupplementationFeature(
    SimpleNamespace(), initialization(), 0
  )
  assert not feature.set_provisional_reference(reference())
def test_feature_telemetry_is_fail_soft(monkeypatch):
  calls = []

  def fail_store(*args, **kwargs):
    calls.append((args, kwargs))
    raise OSError("injected telemetry failure")

  monkeypatch.setattr(
    pigeond,
    "store_provisional_yuma_decision_event",
    fail_store,
  )
  monkeypatch.setattr(pigeond, "read_boot_id", lambda: CURRENT_BOOT)
  monkeypatch.setattr(
    pigeond,
    "load_provisional_yuma_boot_disable_state",
    lambda *_args, **_kwargs: ProvisionalYumaBootDisableState(
      False, CURRENT_BOOT, None, None, None
    ),
  )
  monkeypatch.setattr(
    pigeond,
    "public_yuma_almanac_enabled",
    lambda _params: True,
  )
  monkeypatch.setattr(
    pigeond,
    "create_yuma_supplementation_runtime",
    lambda *_args, **_kwargs: FakeRuntime(),
  )

  feature = pigeond.YumaSupplementationFeature(
    SimpleNamespace(),
    initialization(),
    4,
  )
  feature.persist_provisional_telemetry(
    "rtc_observation",
    now=2.0,
    observation=SimpleNamespace(
      state="rejected",
      reason="rtc_not_advanced",
    ),
  )

  assert len(calls) == 1
  assert calls[0][0] == ("rtc_observation",)
  assert calls[0][1]["current_boot_id"] == CURRENT_BOOT
  assert calls[0][1]["receiver_cycle"] == 4
