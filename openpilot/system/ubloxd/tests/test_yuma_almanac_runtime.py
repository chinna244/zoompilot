from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from openpilot.system.ubloxd.gps_assistance import (
  CacheAgeEvidence,
  NavSatQuality,
  NavigationQuality,
  effective_restored_navigation_quality,
)
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaDatabaseRestoreState,
  YumaSupplementationAction,
  YumaSupplementationReason,
)
from openpilot.system.ubloxd.yuma_almanac_runtime import (
  YUMA_PIGEOND_RETRY_DELAY_SECONDS,
  YUMA_PIGEOND_TRANSMIT_BUDGET_SECONDS,
  YUMA_PIGEOND_TRANSMIT_MARGIN_SECONDS,
  YumaSupplementationRuntime,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  YumaAlmanacTransmitStatus,
)


NOW = datetime(2026, 7, 21, 15, tzinfo=UTC)
REFERENCE_TIME = NOW - timedelta(hours=2)
YUMA_PRNS = frozenset((*range(1, 13), *range(14, 33)))
YUMA_PRN_IDS = tuple(sorted(YUMA_PRNS))
YUMA_PRN_COUNT = len(YUMA_PRNS)


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


def restored_quality(
  database_saved_at_utc: datetime,
):
  return effective_restored_navigation_quality(
    startup_ready_quality(),
    database_saved_at_utc,
    None,
    CacheAgeEvidence.UNVERIFIED,
  )


def frame(satellite_id: int) -> bytes:
  data = bytearray(16)
  data[8] = satellite_id
  return bytes(data)


def stored(*satellite_ids: int):
  return SimpleNamespace(
    downloaded_at_utc=NOW,
    almanac=SimpleNamespace(
      frames=tuple(frame(value) for value in satellite_ids),
      ubx_data=b"".join(frame(value) for value in satellite_ids),
    ),
  )


def runtime(
  *,
  database_state=YumaDatabaseRestoreState.COMPLETE,
  database_saved_at_utc=NOW - timedelta(hours=1),
  time_anchor_utc=NOW,
  time_anchor_source="test",
  restored_gps_almanac_available=YUMA_PRN_COUNT,
  restored_gps_ephemeris_available=None,
  restored_glonass_ephemeris_available=None,
  restored_gps_startup_ready=True,
  restored_gps_almanac_satellite_ids=YUMA_PRN_IDS,
  restored_navigation_quality=None,
  cache_loader=lambda path: stored(*YUMA_PRNS),
  monotonic=lambda: 100.0,
):
  return YumaSupplementationRuntime(
    database_state=database_state,
    database_saved_at_utc=database_saved_at_utc,
    started_at=100.0,
    time_anchor_utc=time_anchor_utc,
    time_anchor_source=time_anchor_source,
    restored_gps_almanac_available=(
      restored_gps_almanac_available
    ),
    restored_gps_ephemeris_available=(
      restored_gps_ephemeris_available
    ),
    restored_glonass_ephemeris_available=(
      restored_glonass_ephemeris_available
    ),
    restored_gps_startup_ready=restored_gps_startup_ready,
    restored_gps_almanac_satellite_ids=(
      restored_gps_almanac_satellite_ids
    ),
    restored_navigation_quality=restored_navigation_quality,
    cache_loader=cache_loader,
    reference_validator=(
      lambda almanac, trusted_now: REFERENCE_TIME
    ),
    monotonic=monotonic,
  )


def report(*, healthy, almanac):
  return NavSatQuality(
    len(healthy),
    0,
    0,
    0,
    0,
    len(almanac),
    0,
    0,
    {},
    gps_satellite_ids=healthy,
    gps_healthy_satellite_ids=healthy,
    gps_almanac_satellite_ids=almanac,
  )


def test_time_anchor_advances_with_monotonic_time():
  state = runtime()

  assert state.trusted_now(100.0) == NOW
  assert state.trusted_now(112.5) == NOW + timedelta(
    seconds=12.5,
  )


def test_time_anchor_can_be_updated_before_completion():
  state = runtime(time_anchor_utc=None)
  synchronized = NOW + timedelta(minutes=1)

  assert state.trusted_now(100.0) is None

  state.set_time_anchor(synchronized, 105.0, "synchronized")

  assert state.trusted_now(108.0) == synchronized + timedelta(
    seconds=3,
  )
  assert state.time_anchor_source == "synchronized"


def test_late_trusted_time_recomputes_recent_cache_before_planning():
  saved_at = NOW - timedelta(minutes=30)
  captured = restored_quality(saved_at)
  state = runtime(
    database_saved_at_utc=saved_at,
    time_anchor_utc=None,
    time_anchor_source=None,
    restored_gps_ephemeris_available=0,
    restored_glonass_ephemeris_available=0,
    restored_gps_startup_ready=False,
    restored_navigation_quality=captured,
  )

  before_time = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  state.set_time_anchor(NOW, 105.0, "receiver_utc")
  after_time = state.evaluate(
    lambda message: None,
    now=105.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  refreshed = state.restored_navigation_quality
  assert before_time is not None
  assert (
    before_time.plan.reason
    is YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME
  )
  assert after_time is not None
  assert after_time.plan.action is YumaSupplementationAction.WAIT
  assert (
    after_time.plan.reason
    is YumaSupplementationReason.WAITING_FOR_NAV_SAT
  )
  assert refreshed is not None
  assert refreshed.cache_age_seconds == 30 * 60
  assert refreshed.age_verified
  assert refreshed.effective_gps_ephemeris_available == 5
  assert refreshed.effective_glonass_ephemeris_available == 6
  assert refreshed.effective_gps_startup_ready
  assert refreshed.expiration_reasons == ()
  assert state.controller.restored_gps_ephemeris_available == 5
  assert state.controller.restored_glonass_ephemeris_available == 6
  assert state.controller.restored_gps_startup_ready is True
  assert not state.completed


def test_late_trusted_time_recomputes_stale_cache_and_sends_immediately(
  monkeypatch,
):
  saved_at = NOW - timedelta(hours=17)
  captured = restored_quality(saved_at)
  state = runtime(
    database_saved_at_utc=saved_at,
    time_anchor_utc=None,
    time_anchor_source=None,
    restored_gps_ephemeris_available=0,
    restored_glonass_ephemeris_available=0,
    restored_gps_startup_ready=False,
    restored_navigation_quality=captured,
  )
  calls = []
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    lambda *args, **kwargs: (
      calls.append(kwargs["satellite_ids"])
      or transmit_result(
        YumaAlmanacTransmitStatus.COMPLETE,
        accepted=tuple(sorted(kwargs["satellite_ids"])),
      )
    ),
  )

  state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  state.set_time_anchor(NOW, 105.0, "receiver_utc")
  outcome = state.evaluate(
    lambda message: None,
    now=105.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert outcome.plan.action is YumaSupplementationAction.SEND_ALL
  assert (
    outcome.plan.reason
    is YumaSupplementationReason.STALE_DATABASE_WITH_NEWER_YUMA
  )
  assert outcome.nav_sat_wait_seconds == 0.0
  assert not outcome.nav_sat_observation_expired
  assert outcome.restored_cache_age_verified
  assert outcome.captured_gps_ephemeris_available == 5
  assert outcome.captured_glonass_ephemeris_available == 6
  assert outcome.captured_gps_startup_ready
  assert outcome.restored_gps_ephemeris_available == 0
  assert outcome.restored_glonass_ephemeris_available == 0
  assert outcome.restored_gps_startup_ready is False
  assert outcome.restored_gps_ephemeris_fresh is False
  assert outcome.restored_glonass_ephemeris_fresh is False
  assert outcome.restored_quality_expiration_reasons == (
    "gps_ephemeris_expired",
    "glonass_ephemeris_expired",
  )
  assert calls == [YUMA_PRNS]
  assert outcome.terminal


def test_late_trusted_time_future_cache_remains_fail_closed():
  saved_at = NOW + timedelta(minutes=1)
  captured = restored_quality(saved_at)
  state = runtime(
    database_saved_at_utc=saved_at,
    time_anchor_utc=None,
    time_anchor_source=None,
    restored_gps_ephemeris_available=0,
    restored_glonass_ephemeris_available=0,
    restored_gps_startup_ready=False,
    restored_navigation_quality=captured,
  )

  state.set_time_anchor(NOW, 105.0, "receiver_utc")
  outcome = state.evaluate(
    lambda message: None,
    now=105.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  refreshed = state.restored_navigation_quality
  assert outcome is not None
  assert refreshed is not None
  assert refreshed.cache_age_seconds is None
  assert not refreshed.age_verified
  assert refreshed.effective_gps_ephemeris_available == 0
  assert refreshed.effective_glonass_ephemeris_available == 0
  assert not refreshed.effective_gps_startup_ready
  assert refreshed.expiration_reasons == (
    "cache_timestamp_in_future",
  )


def test_reliable_fix_skips_yuma_loading_after_late_quality_refresh():
  saved_at = NOW - timedelta(minutes=30)
  captured = restored_quality(saved_at)
  state = runtime(
    database_saved_at_utc=saved_at,
    time_anchor_utc=None,
    time_anchor_source=None,
    restored_gps_ephemeris_available=0,
    restored_glonass_ephemeris_available=0,
    restored_gps_startup_ready=False,
    restored_navigation_quality=captured,
    cache_loader=lambda path: (_ for _ in ()).throw(
      AssertionError("cache must not be loaded")
    ),
  )
  state.set_time_anchor(NOW, 105.0, "receiver_utc")

  outcome = state.evaluate(
    lambda message: None,
    now=105.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=True,
  )

  assert outcome is not None
  assert outcome.plan.action is YumaSupplementationAction.SKIP
  assert (
    outcome.plan.reason
    is YumaSupplementationReason.RELIABLE_FIX_AVAILABLE
  )
  refreshed = state.restored_navigation_quality
  assert refreshed is not None
  assert refreshed.age_verified
  assert refreshed.effective_gps_ephemeris_available == 5
  assert refreshed.effective_glonass_ephemeris_available == 6
  assert state.completed


def test_runtime_reports_wait_state_once_without_completing():
  state = runtime(
    time_anchor_utc=None,
    time_anchor_source=None,
  )

  outcome = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  repeated = state.evaluate(
    lambda message: None,
    now=101.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert outcome.plan.reason is YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME
  assert not outcome.terminal
  assert not outcome.retry_pending
  assert repeated is None
  assert not state.completed


def test_runtime_skips_once_reliable_fix_exists_without_loading_cache():
  def unexpected_load(path):
    raise AssertionError("cache must not be loaded")

  state = runtime(cache_loader=unexpected_load)

  outcome = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=True,
  )

  assert outcome is not None
  assert outcome.plan.action is YumaSupplementationAction.SKIP
  assert (
    outcome.plan.reason
    is YumaSupplementationReason.RELIABLE_FIX_AVAILABLE
  )
  assert state.completed


def test_runtime_transmits_targeted_missing_prns_once(
  monkeypatch,
):
  state = runtime()
  sent = []
  calls = []

  def fake_transmit(
    send_message,
    *,
    trusted_now,
    satellite_ids,
    path,
    stored_almanac,
    max_duration_seconds,
    minimum_remaining_seconds,
  ):
    calls.append((
      trusted_now,
      satellite_ids,
      path,
      stored_almanac,
      max_duration_seconds,
      minimum_remaining_seconds,
    ))
    for satellite_id in sorted(satellite_ids):
      send_message(frame(satellite_id))
    return SimpleNamespace(
      status=YumaAlmanacTransmitStatus.COMPLETE,
      accepted_satellite_ids=tuple(sorted(satellite_ids)),
      failed_satellite_ids=(),
      deferred_satellite_ids=(),
      unavailable_satellite_ids=(),
    )

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    fake_transmit,
  )
  nav_sat = report(
    healthy=frozenset((1, 2, 3, 4)),
    almanac=frozenset((1, 3)),
  )

  outcome = state.evaluate(
    lambda message: sent.append(message[8]),
    now=100.0,
    nav_sat=nav_sat,
    nav_sat_time=100.0,
    reliable_fix_available=False,
  )
  repeated = state.evaluate(
    lambda message: sent.append(message[8]),
    now=101.0,
    nav_sat=nav_sat,
    nav_sat_time=101.0,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert outcome.plan.action is (
    YumaSupplementationAction.SEND_MISSING
  )
  assert outcome.plan.satellite_ids == frozenset((2, 4))
  assert sent == [2, 4]
  assert calls[0][3] is state.controller.cache_observation.stored
  assert calls[0][4] == YUMA_PIGEOND_TRANSMIT_BUDGET_SECONDS
  assert calls[0][5] == YUMA_PIGEOND_TRANSMIT_MARGIN_SECONDS
  assert repeated is None


def test_runtime_contains_unexpected_transmitter_failure(
  monkeypatch,
):
  logs = []
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.cloudlog.exception",
    logs.append,
  )
  state = runtime(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
  )

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    lambda *args, **kwargs: (_ for _ in ()).throw(
      RuntimeError("injected failure")
    ),
  )

  outcome = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert outcome.plan.action is YumaSupplementationAction.SEND_ALL
  assert outcome.transmit_result is None
  assert outcome.error == "RuntimeError: injected failure"
  assert outcome.terminal
  assert len(outcome.attempt_history) == 1
  assert outcome.attempt_history[0].error == outcome.error
  assert logs == ["Unexpected public YUMA transmission failure"]
  assert state.completed


def transmit_result(
  status,
  *,
  accepted=(),
  failed=(),
  deferred=(),
  unavailable=(),
):
  return SimpleNamespace(
    status=status,
    accepted_satellite_ids=accepted,
    failed_satellite_ids=failed,
    deferred_satellite_ids=deferred,
    unavailable_satellite_ids=unavailable,
  )


def test_runtime_retries_only_failed_and_deferred_prns_once(
  monkeypatch,
):
  clock = [100.5]
  state = runtime(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
    monotonic=lambda: clock[0],
  )
  calls = []
  results = iter((
    transmit_result(
      YumaAlmanacTransmitStatus.PARTIAL,
      accepted=(1,),
      failed=(2,),
      deferred=(3,),
      unavailable=(4,),
    ),
    transmit_result(
      YumaAlmanacTransmitStatus.COMPLETE,
      accepted=(2, 3),
    ),
  ))

  def fake_transmit(
    send_message,
    *,
    trusted_now,
    satellite_ids,
    path,
    stored_almanac,
    max_duration_seconds,
    minimum_remaining_seconds,
  ):
    calls.append(satellite_ids)
    assert (
      minimum_remaining_seconds
      == YUMA_PIGEOND_TRANSMIT_MARGIN_SECONDS
    )
    return next(results)

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    fake_transmit,
  )

  first = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  retry_at = 100.5 + YUMA_PIGEOND_RETRY_DELAY_SECONDS
  early = state.evaluate(
    lambda message: None,
    now=retry_at - 0.001,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  second = state.evaluate(
    lambda message: None,
    now=retry_at,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert first is not None
  assert first.transmit_result.status is YumaAlmanacTransmitStatus.PARTIAL
  assert not first.terminal
  assert first.retry_pending
  assert len(first.attempt_history) == 1
  assert early is None
  assert second is not None
  assert second.terminal
  assert not second.retry_pending
  assert len(second.attempt_history) == 2
  assert calls == [
    YUMA_PRNS,
    frozenset((2, 3)),
  ]
  assert state.transmission_attempts == 2
  assert state.completed
  assert not state.retry_pending


def test_runtime_cancels_pending_retry_after_reliable_fix(
  monkeypatch,
):
  state = runtime(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
  )
  calls = []

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    lambda *args, **kwargs: (
      calls.append(kwargs["satellite_ids"])
      or transmit_result(
        YumaAlmanacTransmitStatus.FAILED,
        failed=(1, 2),
      )
    ),
  )

  first = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  cancelled = state.evaluate(
    lambda message: None,
    now=102.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=True,
  )

  assert first is not None
  assert cancelled is not None
  assert cancelled.terminal
  assert cancelled.plan.reason is YumaSupplementationReason.RELIABLE_FIX_AVAILABLE
  assert len(cancelled.attempt_history) == 1
  assert len(calls) == 1
  assert state.completed
  assert not state.retry_pending


def test_runtime_does_not_retry_unavailable_only_partial(
  monkeypatch,
):
  state = runtime(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
  )
  calls = []

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    lambda *args, **kwargs: (
      calls.append(kwargs["satellite_ids"])
      or transmit_result(
        YumaAlmanacTransmitStatus.PARTIAL,
        accepted=(1,),
        unavailable=(2,),
      )
    ),
  )

  outcome = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert len(calls) == 1
  assert state.completed
  assert not state.retry_pending


def test_runtime_second_failure_is_terminal(
  monkeypatch,
):
  state = runtime(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
  )
  calls = []
  results = iter((
    transmit_result(
      YumaAlmanacTransmitStatus.BUDGET_EXPIRED,
      deferred=(1, 2),
    ),
    transmit_result(
      YumaAlmanacTransmitStatus.FAILED,
      failed=(1, 2),
    ),
  ))

  def fake_transmit(*args, **kwargs):
    calls.append(kwargs["satellite_ids"])
    return next(results)

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    fake_transmit,
  )

  first = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  second = state.evaluate(
    lambda message: None,
    now=102.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  repeated = state.evaluate(
    lambda message: None,
    now=104.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert first is not None
  assert second is not None
  assert repeated is None
  assert calls == [
    YUMA_PRNS,
    frozenset((1, 2)),
  ]
  assert state.transmission_attempts == 2
  assert state.completed


def test_runtime_outcome_contains_startup_diagnostics(
  monkeypatch,
):
  times = iter((100.0, 100.25))
  state = runtime(monotonic=lambda: next(times))

  result = SimpleNamespace(
    status=YumaAlmanacTransmitStatus.COMPLETE,
    requested_satellite_ids=(1, 2),
    attempted_satellite_ids=(1, 2),
    accepted_satellite_ids=(1, 2),
    failed_satellite_ids=(),
    deferred_satellite_ids=(),
    unavailable_satellite_ids=(),
    reference_time_utc=REFERENCE_TIME,
    downloaded_at_utc=NOW,
  )
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    lambda *args, **kwargs: result,
  )

  outcome = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=report(
      healthy=frozenset((1, 2)),
      almanac=frozenset(),
    ),
    nav_sat_time=100.0,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert outcome.database_state is YumaDatabaseRestoreState.COMPLETE
  assert outcome.database_age_seconds == 3600.25
  assert outcome.yuma_reference_utc == REFERENCE_TIME
  assert outcome.yuma_reference_age_seconds == 7200.25
  assert outcome.completion_utc == NOW + timedelta(seconds=0.25)
  assert outcome.downloaded_at_utc == NOW
  assert outcome.cache_error is None
  assert outcome.transmission_attempt == 1
  assert outcome.transmission_elapsed_ms == 250.0

def test_runtime_transmits_the_frozen_validated_cache_snapshot(
  monkeypatch,
):
  frozen = stored(*YUMA_PRNS)
  loads = []
  transmitted = []

  def cache_loader(path):
    loads.append(path)
    return frozen

  state = runtime(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
    cache_loader=cache_loader,
  )

  def fake_transmit(*args, **kwargs):
    transmitted.append(kwargs["stored_almanac"])
    return transmit_result(
      YumaAlmanacTransmitStatus.COMPLETE,
      accepted=tuple(sorted(kwargs["satellite_ids"])),
    )

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    fake_transmit,
  )

  outcome = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert loads == [state.controller.path]
  assert transmitted == [frozen]


def test_runtime_transmit_margin_includes_overhead():
  assert YUMA_PIGEOND_TRANSMIT_MARGIN_SECONDS == 1.0


def test_route_8f_stale_unknown_cache_transmits_without_nav_sat_wait(
  monkeypatch,
):
  state = runtime(
    database_saved_at_utc=NOW - timedelta(hours=16, minutes=47),
    restored_gps_almanac_available=14,
    restored_gps_startup_ready=False,
    restored_gps_almanac_satellite_ids=None,
  )
  calls = []

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    lambda *args, **kwargs: (
      calls.append(kwargs["satellite_ids"])
      or transmit_result(
        YumaAlmanacTransmitStatus.COMPLETE,
        accepted=tuple(sorted(kwargs["satellite_ids"])),
      )
    ),
  )

  outcome = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert outcome.plan.action is YumaSupplementationAction.SEND_ALL
  assert outcome.plan.reason is YumaSupplementationReason.STALE_DATABASE_WITH_NEWER_YUMA
  assert outcome.nav_sat_wait_seconds == 0.0
  assert not outcome.nav_sat_observation_expired
  assert calls == [YUMA_PRNS]
  assert outcome.terminal
  assert state.completed


def test_route_8b_runtime_recovers_when_receiver_time_arrives_after_180_seconds(
  monkeypatch,
):
  state = runtime(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
    time_anchor_utc=None,
    restored_gps_almanac_available=None,
    restored_gps_startup_ready=None,
    restored_gps_almanac_satellite_ids=None,
  )
  calls = []

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    lambda *args, **kwargs: (
      calls.append((
        kwargs["trusted_now"],
        kwargs["satellite_ids"],
      ))
      or transmit_result(
        YumaAlmanacTransmitStatus.COMPLETE,
        accepted=tuple(sorted(kwargs["satellite_ids"])),
      )
    ),
  )

  before_time = state.evaluate(
    lambda message: None,
    now=280.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  state.set_time_anchor(NOW, 357.0, "receiver_utc")

  outcome = state.evaluate(
    lambda message: None,
    now=357.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert before_time is not None
  assert before_time.plan.reason is YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME
  assert before_time.trusted_time_wait_expired
  assert not before_time.terminal
  assert outcome is not None
  assert outcome.plan.action is YumaSupplementationAction.SEND_ALL
  assert calls == [(NOW, YUMA_PRNS)]
  assert outcome.time_anchor_source == "receiver_utc"
  assert outcome.terminal
  assert state.completed


def test_runtime_outcome_contains_restored_cache_snapshot(
  monkeypatch,
):
  state = YumaSupplementationRuntime(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=NOW - timedelta(hours=1),
    started_at=100.0,
    time_anchor_utc=NOW,
    time_anchor_source="rtc_estimate",
    restored_cache_generation="previous",
    restored_cache_selection_reason="previous_gps_startup_ready",
    restored_gps_almanac_available=10,
    restored_glonass_almanac_available=9,
    restored_gps_ephemeris_available=0,
    restored_glonass_ephemeris_available=5,
    restored_satellites_used=5,
    restored_gps_startup_ready=False,
    restored_gps_almanac_satellite_ids=tuple(range(1, 11)),
    cache_loader=lambda path: stored(*YUMA_PRNS),
    reference_validator=lambda almanac, trusted_now: REFERENCE_TIME,
  )

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    lambda *args, **kwargs: transmit_result(
      YumaAlmanacTransmitStatus.COMPLETE,
      accepted=tuple(sorted(kwargs["satellite_ids"])),
    ),
  )

  outcome = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert outcome.restored_cache_generation == "previous"
  assert outcome.restored_cache_selection_reason == "previous_gps_startup_ready"
  assert outcome.restored_gps_almanac_available == 10
  assert outcome.restored_glonass_almanac_available == 9
  assert outcome.restored_gps_ephemeris_available == 0
  assert outcome.restored_glonass_ephemeris_available == 5
  assert outcome.restored_satellites_used == 5
  assert outcome.restored_gps_startup_ready is False
  assert outcome.restored_gps_almanac_satellite_ids == tuple(range(1, 11))


def test_runtime_reports_cache_deadline_without_completing():
  state = runtime(
    cache_loader=lambda path: (_ for _ in ()).throw(
      FileNotFoundError(path)
    ),
  )

  first = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  repeated = state.evaluate(
    lambda message: None,
    now=101.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  deadline = state.evaluate(
    lambda message: None,
    now=130.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert first is not None
  assert first.plan.reason is YumaSupplementationReason.WAITING_FOR_YUMA_CACHE
  assert not first.cache_wait_expired
  assert repeated is None
  assert deadline is not None
  assert deadline.cache_wait_expired
  assert not deadline.terminal
  assert not state.completed


def test_runtime_uses_post_transmission_completion_clock(monkeypatch):
  clock = iter((100.0, 103.5))
  state = runtime(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
    monotonic=lambda: next(clock),
  )
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    lambda *args, **kwargs: transmit_result(
      YumaAlmanacTransmitStatus.COMPLETE,
      accepted=tuple(sorted(kwargs["satellite_ids"])),
    ),
  )

  outcome = state.evaluate(
    lambda message: None,
    now=100.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )

  assert outcome is not None
  assert outcome.completion_elapsed_seconds == 3.5
  assert outcome.completion_monotonic == 103.5
  assert outcome.completion_utc == NOW + timedelta(seconds=3.5)
  assert outcome.runtime_elapsed_seconds == 3.5
  assert outcome.yuma_snapshot_sha256 is not None


def test_runtime_records_decision_ready_and_nav_sat_wait(monkeypatch):
  state = runtime(time_anchor_utc=None)
  state.set_time_anchor(NOW, 357.0, "receiver_utc")
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_runtime.transmit_public_yuma_almanac",
    lambda *args, **kwargs: transmit_result(
      YumaAlmanacTransmitStatus.COMPLETE,
      accepted=tuple(sorted(kwargs["satellite_ids"])),
    ),
  )

  waiting = state.evaluate(
    lambda message: None,
    now=357.0,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )
  outcome = state.evaluate(
    lambda message: None,
    now=358.0,
    nav_sat=report(
      healthy=frozenset((1, 2)),
      almanac=frozenset((1,)),
    ),
    nav_sat_time=358.0,
    reliable_fix_available=False,
  )

  assert waiting is not None
  assert waiting.decision_ready_elapsed_seconds == 257.0
  assert outcome is not None
  assert outcome.nav_sat_observed_elapsed_seconds == 258.0
  assert outcome.nav_sat_wait_seconds == 1.0
