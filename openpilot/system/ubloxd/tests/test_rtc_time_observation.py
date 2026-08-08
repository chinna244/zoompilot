import ast
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openpilot.system.ubloxd.rtc_time_observation import (
  CrossBootRtcObserver,
  RtcObservationCandidate,
  RtcObservationReason,
  RtcObservationState,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  RtcVoltageStatus,
  TimeProvenance,
  TrustedTimeAnchor,
  TrustedTimeAnchorStore,
  TrustedTimeSource,
)

ANCHOR_BOOT_ID = "12345678-1234-5678-9234-567812345678"
CURRENT_BOOT_ID = "87654321-4321-6789-9234-567812345678"
ANCHOR_UTC = datetime(2026, 7, 22, 21, tzinfo=UTC)


def anchor(
  *,
  boot_id: str = ANCHOR_BOOT_ID,
  rtc_epoch_seconds: int | None = 1_000,
  sequence: int = 1,
) -> TrustedTimeAnchor:
  return TrustedTimeAnchor(
    version=1,
    trusted_utc=ANCHOR_UTC,
    source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
    provenance=TimeProvenance.NETWORK_INDEPENDENT,
    authorized=True,
    independent=True,
    uncertainty_seconds=30.0,
    boot_id=boot_id,
    boottime_seconds=100.0,
    rtc_epoch_seconds=rtc_epoch_seconds,
    rtc_voltage_status_supported=False,
    rtc_voltage_status_flags=None,
    sequence=sequence,
  )


def sequence_reader(values):
  values = iter(values)
  return lambda: next(values)


def stored_anchor(tmp_path, value: TrustedTimeAnchor):
  store = TrustedTimeAnchorStore(
    tmp_path / "trusted_time_anchor.json"
  )
  store.save(value)
  return store


def observer(
  store,
  *,
  rtc_values=(1_100, 1_102),
  boottime_values=(10.0, 12.0),
  current_boot_id=CURRENT_BOOT_ID,
  tick_interval=2.0,
  tick_tolerance=0.25,
  max_elapsed=30 * 24 * 60 * 60,
):
  return CrossBootRtcObserver(
    store,
    boot_id_reader=lambda: current_boot_id,
    rtc_epoch_reader=sequence_reader(rtc_values),
    boottime_reader=sequence_reader(boottime_values),
    rtc_voltage_reader=lambda: RtcVoltageStatus(
      False,
      None,
    ),
    tick_interval_seconds=tick_interval,
    tick_tolerance_seconds=tick_tolerance,
    max_elapsed_seconds=max_elapsed,
  )


def test_cross_boot_candidate_is_pending_then_observed_without_sleep(
  tmp_path,
):
  store = stored_anchor(tmp_path, anchor())
  value = observer(store)

  pending = value.current_observation(50.0)
  before_deadline = value.current_observation(51.999)
  observed = value.current_observation(52.0)

  assert pending.state is RtcObservationState.PENDING_TICK
  assert pending.reason is (
    RtcObservationReason.CROSS_BOOT_CANDIDATE_PENDING_TICK
  )
  assert before_deadline is pending
  assert pending.candidate is not None
  assert pending.candidate.candidate_utc == datetime(
    2026,
    7,
    22,
    21,
    1,
    40,
    tzinfo=UTC,
  )
  assert not pending.authorized
  assert not pending.operational
  assert not pending.candidate.authorized
  assert not pending.candidate.operational

  assert observed.state is RtcObservationState.OBSERVED
  assert observed.reason is (
    RtcObservationReason.CROSS_BOOT_CANDIDATE_OBSERVED
  )
  assert observed.rtc_tick_delta_seconds == 2
  assert observed.boottime_tick_delta_seconds == 2.0
  assert observed.tick_consistent is True
  assert observed.candidate is not None
  assert observed.candidate.candidate_utc == datetime(
    2026,
    7,
    22,
    21,
    1,
    42,
    tzinfo=UTC,
  )


def test_pending_poll_does_not_take_second_rtc_sample(
  tmp_path,
):
  store = stored_anchor(tmp_path, anchor())
  reads = []

  def read_rtc():
    reads.append(len(reads))
    return (1_100, 1_102)[len(reads) - 1]

  value = CrossBootRtcObserver(
    store,
    boot_id_reader=lambda: CURRENT_BOOT_ID,
    rtc_epoch_reader=read_rtc,
    boottime_reader=sequence_reader((10.0, 12.0)),
    rtc_voltage_reader=lambda: RtcVoltageStatus(False, None),
    tick_interval_seconds=2.0,
  )

  value.current_observation(50.0)
  value.current_observation(50.5)
  value.current_observation(51.9)
  assert len(reads) == 1

  value.current_observation(52.0)
  assert len(reads) == 2


def test_previous_cross_boot_anchor_survives_current_boot_anchor_write(
  tmp_path,
):
  store = stored_anchor(tmp_path, anchor(sequence=1))
  store.save(anchor(
    boot_id=CURRENT_BOOT_ID,
    rtc_epoch_seconds=1_090,
    sequence=2,
  ))
  value = observer(store)

  pending = value.current_observation(50.0)

  assert pending.candidate is not None
  assert pending.candidate.anchor_generation == "previous"
  assert pending.candidate.anchor_sequence == 1
  assert pending.candidate.anchor_boot_id == ANCHOR_BOOT_ID


def test_same_boot_only_is_not_applicable(tmp_path):
  store = stored_anchor(tmp_path, anchor(
    boot_id=CURRENT_BOOT_ID,
  ))
  value = observer(
    store,
    current_boot_id=CURRENT_BOOT_ID,
  )

  observation = value.current_observation(50.0)

  assert observation.state is RtcObservationState.NOT_APPLICABLE
  assert observation.reason is RtcObservationReason.SAME_BOOT_ONLY
  assert observation.candidate is None


@pytest.mark.parametrize(
  ("stored_value", "rtc_value", "boottime", "max_elapsed", "reason"),
  (
    (
      anchor(rtc_epoch_seconds=None),
      1_100,
      10.0,
      1_000,
      RtcObservationReason.ANCHOR_RTC_UNAVAILABLE,
    ),
    (
      anchor(),
      None,
      10.0,
      1_000,
      RtcObservationReason.CURRENT_RTC_UNAVAILABLE,
    ),
    (
      anchor(),
      999,
      10.0,
      1_000,
      RtcObservationReason.RTC_ROLLBACK,
    ),
    (
      anchor(),
      1_000,
      10.0,
      1_000,
      RtcObservationReason.RTC_NOT_ADVANCED,
    ),
    (
      anchor(),
      2_001,
      10.0,
      1_000,
      RtcObservationReason.RTC_ELAPSED_ABOVE_MAXIMUM,
    ),
    (
      anchor(),
      1_050,
      100.0,
      1_000,
      RtcObservationReason.RTC_ELAPSED_BELOW_UPTIME,
    ),
  ),
)
def test_cross_boot_candidate_rejections_are_explicit(
  tmp_path,
  stored_value,
  rtc_value,
  boottime,
  max_elapsed,
  reason,
):
  store = stored_anchor(tmp_path, stored_value)
  value = CrossBootRtcObserver(
    store,
    boot_id_reader=lambda: CURRENT_BOOT_ID,
    rtc_epoch_reader=lambda: rtc_value,
    boottime_reader=lambda: boottime,
    rtc_voltage_reader=lambda: RtcVoltageStatus(False, None),
    max_elapsed_seconds=max_elapsed,
  )

  observation = value.current_observation(50.0)

  assert observation.state is RtcObservationState.REJECTED
  assert observation.reason is reason
  assert observation.candidate is None
  assert not observation.authorized
  assert not observation.operational


@pytest.mark.parametrize(
  ("second_rtc", "second_boottime", "reason"),
  (
    (
      1_100,
      12.0,
      RtcObservationReason.RTC_TICK_NOT_ADVANCED,
    ),
    (
      1_099,
      12.0,
      RtcObservationReason.RTC_TICK_ROLLBACK,
    ),
    (
      1_102,
      9.0,
      RtcObservationReason.BOOTTIME_TICK_ROLLBACK,
    ),
    (
      1_110,
      12.0,
      RtcObservationReason.RTC_TICK_RATE_INCONSISTENT,
    ),
  ),
)
def test_tick_failures_reject_candidate(
  tmp_path,
  second_rtc,
  second_boottime,
  reason,
):
  store = stored_anchor(tmp_path, anchor())
  value = observer(
    store,
    rtc_values=(1_100, second_rtc),
    boottime_values=(10.0, second_boottime),
  )

  assert (
    value.current_observation(50.0).state
    is RtcObservationState.PENDING_TICK
  )
  result = value.current_observation(52.0)

  assert result.state is RtcObservationState.REJECTED
  assert result.reason is reason
  assert result.tick_consistent is False


def test_changed_observation_reports_only_transitions(
  tmp_path,
):
  store = stored_anchor(tmp_path, anchor())
  value = observer(store)

  first = value.changed_observation(50.0)
  repeated = value.changed_observation(51.0)
  final = value.changed_observation(52.0)
  final_repeated = value.changed_observation(53.0)

  assert first is not None
  assert first.state is RtcObservationState.PENDING_TICK
  assert repeated is None
  assert final is not None
  assert final.state is RtcObservationState.OBSERVED
  assert final_repeated is None


def test_observation_never_writes_or_rotates_anchor_store(
  tmp_path,
):
  store = stored_anchor(tmp_path, anchor())
  primary_before = store.primary_path.read_bytes()
  previous_before = store.previous_path.exists()
  value = observer(store)

  value.current_observation(50.0)
  value.current_observation(52.0)

  assert store.primary_path.read_bytes() == primary_before
  assert store.previous_path.exists() is previous_before


def test_authorization_fields_cannot_be_overridden():
  with pytest.raises(TypeError):
    RtcObservationCandidate(
      candidate_utc=ANCHOR_UTC,
      uncertainty_seconds=30.0,
      anchor_generation="primary",
      anchor_sequence=1,
      anchor_boot_id=ANCHOR_BOOT_ID,
      current_boot_id=CURRENT_BOOT_ID,
      anchor_trusted_utc=ANCHOR_UTC,
      anchor_rtc_epoch_seconds=1_000,
      current_rtc_epoch_seconds=1_100,
      rtc_elapsed_seconds=100,
      current_boottime_seconds=10.0,
      rtc_advanced=True,
      elapsed_covers_uptime=True,
      rtc_voltage_status_supported=False,
      rtc_voltage_status_flags=None,
      authorized=True,  # ty: ignore[unknown-argument]
    )



def test_pigeond_cross_boot_observation_dataflow_is_validation_only():
  source = (
    Path(__file__).resolve().parents[1] / "pigeond.py"
  ).read_text(encoding="utf-8")
  tree = ast.parse(source)
  run_receiving = next(
    node
    for node in tree.body
    if (
      isinstance(node, ast.FunctionDef)
      and node.name == "run_receiving"
    )
  )
  forbidden = {
    "send_time_assistance",
    "restore_navigation_assistance",
    "cache_promotion_trusted_now",
    "write_navigation_assistance_cache",
    "set_system_time",
    "mark_time_synced",
    "set_time_anchor",
    "set_receiver_time_anchor",
    "save",
  }

  def callee_name(call):
    if isinstance(call.func, ast.Name):
      return call.func.id
    if isinstance(call.func, ast.Attribute):
      return call.func.attr
    return ""

  for call in (
    node
    for node in ast.walk(run_receiving)
    if isinstance(node, ast.Call)
  ):
    names = {
      child.id
      for child in ast.walk(call)
      if (
        isinstance(child, ast.Name)
        and child.id in {
          "changed_rtc_observation",
          "latest_cross_boot_rtc_observation",
        }
      )
    }
    if names:
      assert callee_name(call) not in forbidden

  validation_calls = [
    node
    for node in ast.walk(run_receiving)
    if (
      isinstance(node, ast.Call)
      and callee_name(node)
      == "validate_observed_cross_boot_rtc"
    )
  ]
  assert len(validation_calls) == 3

  for function_name in (
    "log_cross_boot_rtc_observation",
    "validate_observed_cross_boot_rtc",
    "log_cross_boot_rtc_validation",
  ):
    function = next(
      node
      for node in tree.body
      if (
        isinstance(node, ast.FunctionDef)
        and node.name == function_name
      )
    )
    called = {
      callee_name(node)
      for node in ast.walk(function)
      if isinstance(node, ast.Call)
    }
    assert called.isdisjoint(forbidden)
