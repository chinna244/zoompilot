from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from openpilot.system.ubloxd.gps_assistance import NavSatQuality
from openpilot.system.ubloxd.yuma_almanac_controller import (
  YUMA_CACHE_RETRY_SECONDS,
  YUMA_CACHE_SLOW_RETRY_SECONDS,
  YUMA_CACHE_WAIT_SECONDS,
  YUMA_NAV_SAT_OBSERVATION_SECONDS,
  YUMA_TRUSTED_TIME_WAIT_SECONDS,
  YumaSupplementationController,
)
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaDatabaseRestoreState,
  YumaSupplementationAction,
  YumaSupplementationReason,
)


NOW = datetime(2026, 7, 21, 15, tzinfo=UTC)
REFERENCE_TIME = NOW - timedelta(hours=2)
YUMA_PRNS = frozenset((*range(1, 13), *range(14, 33)))
YUMA_PRN_IDS = tuple(sorted(YUMA_PRNS))
YUMA_PRN_COUNT = len(YUMA_PRNS)


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


def controller(
  *,
  database_state=YumaDatabaseRestoreState.COMPLETE,
  database_saved_at_utc=NOW - timedelta(hours=1),
  restored_gps_almanac_available=YUMA_PRN_COUNT,
  restored_gps_startup_ready=True,
  restored_gps_almanac_satellite_ids=YUMA_PRN_IDS,
  cache_loader=lambda path: stored(*YUMA_PRNS),
  **overrides,
):
  return YumaSupplementationController(
    database_state=database_state,
    database_saved_at_utc=database_saved_at_utc,
    restored_gps_almanac_available=(
      restored_gps_almanac_available
    ),
    restored_gps_startup_ready=restored_gps_startup_ready,
    restored_gps_almanac_satellite_ids=(
      restored_gps_almanac_satellite_ids
    ),
    started_at=100.0,
    cache_loader=cache_loader,
    reference_validator=(
      lambda almanac, trusted_now: REFERENCE_TIME
    ),
    **overrides,
  )


def report(
  *,
  healthy=frozenset(),
  almanac=frozenset(),
):
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


def evaluate(
  state,
  *,
  now=100.0,
  trusted_now=NOW,
  nav_sat=None,
  nav_sat_time=None,
  reliable_fix_available=False,
):
  return state.evaluate(
    now=now,
    trusted_now=trusted_now,
    nav_sat=nav_sat,
    nav_sat_time=nav_sat_time,
    reliable_fix_available=reliable_fix_available,
  )


def test_failed_database_selects_full_yuma_immediately():
  state = controller(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
    restored_gps_almanac_available=None,
    restored_gps_startup_ready=None,
    restored_gps_almanac_satellite_ids=None,
  )

  plan = evaluate(state)

  assert plan.action is YumaSupplementationAction.SEND_ALL
  assert plan.satellite_ids == YUMA_PRNS
  assert state.terminal_plan is plan


def test_route_88_exact_ten_almanacs_sends_missing():
  restored = tuple(range(1, 11))
  state = controller(
    restored_gps_almanac_available=10,
    restored_gps_startup_ready=False,
    restored_gps_almanac_satellite_ids=restored,
  )

  plan = evaluate(state)

  assert plan.action is YumaSupplementationAction.SEND_MISSING
  assert plan.reason is YumaSupplementationReason.RESTORED_GPS_ALMANAC_PRNS_MISSING
  assert plan.satellite_ids == YUMA_PRNS - frozenset(restored)


def test_count_complete_but_not_startup_ready_sends_all_immediately():
  state = controller(
    restored_gps_startup_ready=False,
    restored_gps_almanac_satellite_ids=None,
  )

  plan = evaluate(state)

  assert plan.action is YumaSupplementationAction.SEND_ALL
  assert plan.reason is YumaSupplementationReason.RESTORED_CACHE_NOT_STARTUP_READY
  assert state.decision_ready_at == 100.0
  assert state.last_decision_nav_sat_time is None


def test_complete_database_targets_fresh_visible_missing_prns():
  state = controller()
  nav_sat = report(
    healthy=frozenset((1, 2, 3, 4)),
    almanac=frozenset((1, 3)),
  )

  plan = evaluate(
    state,
    nav_sat=nav_sat,
    nav_sat_time=100.0,
  )

  assert plan.action is YumaSupplementationAction.SEND_MISSING
  assert plan.satellite_ids == frozenset((2, 4))


def test_stale_nav_sat_is_ignored():
  state = controller()
  nav_sat = report(
    healthy=frozenset((1, 2)),
    almanac=frozenset((1,)),
  )

  plan = evaluate(
    state,
    nav_sat=nav_sat,
    nav_sat_time=90.0,
  )

  assert plan.action is YumaSupplementationAction.WAIT
  assert plan.reason is YumaSupplementationReason.WAITING_FOR_NAV_SAT


def test_recent_complete_database_skips_after_deadline():
  state = controller(
    database_saved_at_utc=NOW - timedelta(hours=1),
  )

  waiting = evaluate(state, now=100.0)
  plan = evaluate(
    state,
    now=100.0 + YUMA_NAV_SAT_OBSERVATION_SECONDS,
  )

  assert waiting.action is YumaSupplementationAction.WAIT
  assert plan.action is YumaSupplementationAction.SKIP
  assert plan.reason is YumaSupplementationReason.COMPLETE_DATABASE_IS_RECENT


def test_five_hour_database_uses_newer_yuma_immediately():
  state = controller(
    database_saved_at_utc=NOW - timedelta(hours=5),
  )

  plan = evaluate(state, now=100.0)

  assert plan.action is YumaSupplementationAction.SEND_ALL
  assert plan.reason is YumaSupplementationReason.STALE_DATABASE_WITH_NEWER_YUMA
  assert state.decision_ready_at == 100.0
  assert state.last_decision_nav_sat_time is None


def test_partial_database_uses_full_yuma_immediately():
  state = controller(
    database_state=YumaDatabaseRestoreState.PARTIAL,
  )

  plan = evaluate(state)

  assert plan.action is YumaSupplementationAction.SEND_ALL
  assert plan.reason is YumaSupplementationReason.DATABASE_RESTORE_PARTIAL


def test_missing_cache_retries_and_uses_file_when_it_appears():
  calls = []

  def cache_loader(path):
    calls.append(path)
    if len(calls) == 1:
      raise FileNotFoundError(path)
    return stored(*YUMA_PRNS)

  state = controller(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
    restored_gps_almanac_available=None,
    restored_gps_startup_ready=None,
    cache_loader=cache_loader,
  )

  first = evaluate(state, now=100.0)
  throttled = evaluate(
    state,
    now=100.0 + YUMA_CACHE_RETRY_SECONDS / 2,
  )
  recovered = evaluate(
    state,
    now=100.0 + YUMA_CACHE_RETRY_SECONDS,
  )

  assert first.action is YumaSupplementationAction.WAIT
  assert throttled.action is YumaSupplementationAction.WAIT
  assert len(calls) == 2
  assert recovered.action is YumaSupplementationAction.SEND_ALL
  assert state.last_cache_error is None


def test_missing_cache_remains_nonterminal_after_deadline():
  state = controller(
    cache_loader=lambda path: (_ for _ in ()).throw(
      FileNotFoundError(path)
    ),
  )

  waiting = evaluate(state, now=100.0)
  after_deadline = evaluate(
    state,
    now=100.0 + YUMA_CACHE_WAIT_SECONDS,
  )

  assert waiting.action is YumaSupplementationAction.WAIT
  assert after_deadline.action is YumaSupplementationAction.WAIT
  assert after_deadline.reason is YumaSupplementationReason.WAITING_FOR_YUMA_CACHE
  assert state.terminal_plan is None
  assert "FileNotFoundError" in state.last_cache_error


def test_reliable_fix_stops_without_loading_cache():
  def unexpected_load(path):
    raise AssertionError("cache must not be loaded")

  state = controller(cache_loader=unexpected_load)

  plan = evaluate(
    state,
    trusted_now=None,
    reliable_fix_available=True,
  )

  assert plan.action is YumaSupplementationAction.SKIP
  assert plan.reason is YumaSupplementationReason.RELIABLE_FIX_AVAILABLE


def test_terminal_plan_is_sticky():
  state = controller()
  first = evaluate(
    state,
    reliable_fix_available=True,
  )
  second = evaluate(
    state,
    now=200.0,
    reliable_fix_available=False,
  )

  assert second is first


@pytest.mark.parametrize(
  "elapsed_seconds",
  (179.0, 180.0, 257.0, 400.0),
)
def test_trusted_time_milestones_remain_nonterminal(elapsed_seconds):
  state = controller()

  plan = evaluate(
    state,
    now=100.0 + elapsed_seconds,
    trusted_now=None,
  )

  assert plan.action is YumaSupplementationAction.WAIT
  assert plan.reason is YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME
  assert state.terminal_plan is None


def test_reliable_fix_wins_when_time_arrives_in_same_evaluation():
  state = controller()

  plan = evaluate(
    state,
    now=330.0,
    trusted_now=NOW,
    reliable_fix_available=True,
  )

  assert plan.action is YumaSupplementationAction.SKIP
  assert plan.reason is YumaSupplementationReason.RELIABLE_FIX_AVAILABLE


def test_route_8b_waits_past_180_seconds_then_activates_at_257_seconds():
  state = controller(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
    restored_gps_almanac_available=None,
    restored_gps_startup_ready=None,
  )

  expired_wait = evaluate(
    state,
    now=100.0 + YUMA_TRUSTED_TIME_WAIT_SECONDS,
    trusted_now=None,
  )
  activated = evaluate(
    state,
    now=100.0 + 257.0,
    trusted_now=NOW,
  )

  assert expired_wait.action is YumaSupplementationAction.WAIT
  assert expired_wait.reason is YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME
  assert state.terminal_plan is activated
  assert activated.action is YumaSupplementationAction.SEND_ALL
  assert activated.satellite_ids == YUMA_PRNS


def test_late_time_starts_fresh_nav_sat_observation_window():
  state = controller()
  late_time = 100.0 + 257.0

  before_time = evaluate(
    state,
    now=late_time - 1.0,
    trusted_now=None,
  )
  decision_ready = evaluate(
    state,
    now=late_time,
    trusted_now=NOW,
    nav_sat=report(
      healthy=frozenset((1, 2, 3, 4)),
      almanac=frozenset((1, 3)),
    ),
    nav_sat_time=late_time - 0.1,
  )
  fresh_nav_sat = evaluate(
    state,
    now=late_time + 1.0,
    trusted_now=NOW + timedelta(seconds=1),
    nav_sat=report(
      healthy=frozenset((1, 2, 3, 4)),
      almanac=frozenset((1, 3)),
    ),
    nav_sat_time=late_time + 1.0,
  )

  assert before_time.action is YumaSupplementationAction.WAIT
  assert decision_ready.action is YumaSupplementationAction.WAIT
  assert decision_ready.reason is YumaSupplementationReason.WAITING_FOR_NAV_SAT
  assert state.decision_ready_at == late_time
  assert state.nav_sat_deadline == late_time + YUMA_NAV_SAT_OBSERVATION_SECONDS
  assert fresh_nav_sat.action is YumaSupplementationAction.SEND_MISSING
  assert fresh_nav_sat.satellite_ids == frozenset((2, 4))


def test_late_time_without_post_ready_nav_sat_uses_fallback_after_window():
  state = controller()
  late_time = 100.0 + 257.0

  waiting = evaluate(state, now=late_time, trusted_now=NOW)
  before_deadline = evaluate(
    state,
    now=late_time + YUMA_NAV_SAT_OBSERVATION_SECONDS - 0.001,
    trusted_now=NOW + timedelta(
      seconds=YUMA_NAV_SAT_OBSERVATION_SECONDS - 0.001,
    ),
  )
  terminal = evaluate(
    state,
    now=late_time + YUMA_NAV_SAT_OBSERVATION_SECONDS,
    trusted_now=NOW + timedelta(
      seconds=YUMA_NAV_SAT_OBSERVATION_SECONDS,
    ),
  )

  assert waiting.action is YumaSupplementationAction.WAIT
  assert before_deadline.action is YumaSupplementationAction.WAIT
  assert terminal.action is YumaSupplementationAction.SKIP
  assert terminal.reason is YumaSupplementationReason.COMPLETE_DATABASE_IS_RECENT


def test_future_database_timestamp_is_not_treated_as_old():
  state = controller(
    database_saved_at_utc=NOW + timedelta(minutes=1),
  )

  waiting = evaluate(state, now=100.0)
  plan = evaluate(
    state,
    now=100.0 + YUMA_NAV_SAT_OBSERVATION_SECONDS,
  )

  assert waiting.action is YumaSupplementationAction.WAIT
  assert plan.action is YumaSupplementationAction.SKIP
  assert plan.reason is YumaSupplementationReason.DATABASE_AGE_UNVERIFIED


@pytest.mark.parametrize(
  ("argument", "value"),
  (
    ("started_at", -1.0),
    ("nav_sat_observation_seconds", 0.0),
    ("cache_wait_seconds", float("inf")),
    ("trusted_time_wait_seconds", -1.0),
    ("cache_retry_seconds", float("inf")),
    ("cache_slow_retry_seconds", 0.0),
    ("restored_gps_almanac_available", 33),
    ("restored_gps_startup_ready", 1),
  ),
)
def test_invalid_controller_inputs_are_rejected(argument, value):
  kwargs = {
    "database_state": YumaDatabaseRestoreState.COMPLETE,
    "database_saved_at_utc": NOW,
    "started_at": 100.0,
    "nav_sat_observation_seconds": 15.0,
    "cache_wait_seconds": 30.0,
    "trusted_time_wait_seconds": 180.0,
    "cache_retry_seconds": 1.0,
    "cache_slow_retry_seconds": 30.0,
  }
  kwargs[argument] = value

  with pytest.raises(ValueError):
    YumaSupplementationController(**kwargs)


def test_naive_trusted_time_is_rejected():
  state = controller()

  with pytest.raises(ValueError, match="timezone-aware"):
    evaluate(
      state,
      trusted_now=datetime(2026, 7, 21, 15),
    )


def test_cache_wait_starts_when_trusted_time_arrives_and_can_recover_late():
  calls = []

  def cache_loader(path):
    calls.append(path)
    if len(calls) < 3:
      raise FileNotFoundError(path)
    return stored(*YUMA_PRNS)

  state = controller(
    database_state=YumaDatabaseRestoreState.FAILED,
    database_saved_at_utc=None,
    restored_gps_almanac_available=None,
    restored_gps_startup_ready=None,
    cache_loader=cache_loader,
  )

  before_time = evaluate(
    state,
    now=300.0,
    trusted_now=None,
  )
  first_cache_attempt = evaluate(
    state,
    now=301.0,
    trusted_now=NOW,
  )
  after_cache_deadline = evaluate(
    state,
    now=301.0 + YUMA_CACHE_WAIT_SECONDS,
    trusted_now=NOW,
  )
  before_slow_retry = evaluate(
    state,
    now=(
      301.0
      + YUMA_CACHE_WAIT_SECONDS
      + YUMA_CACHE_SLOW_RETRY_SECONDS
      - 0.001
    ),
    trusted_now=NOW,
  )
  recovered = evaluate(
    state,
    now=(
      301.0
      + YUMA_CACHE_WAIT_SECONDS
      + YUMA_CACHE_SLOW_RETRY_SECONDS
    ),
    trusted_now=NOW,
  )

  assert before_time.action is YumaSupplementationAction.WAIT
  assert first_cache_attempt.action is YumaSupplementationAction.WAIT
  assert after_cache_deadline.action is YumaSupplementationAction.WAIT
  assert before_slow_retry.action is YumaSupplementationAction.WAIT
  assert recovered.action is YumaSupplementationAction.SEND_ALL
  assert len(calls) == 3


def test_unknown_coverage_sends_all_immediately():
  state = controller(
    restored_gps_almanac_available=None,
    restored_gps_startup_ready=None,
    restored_gps_almanac_satellite_ids=None,
  )

  plan = evaluate(state, now=100.0)

  assert plan.action is YumaSupplementationAction.SEND_ALL
  assert plan.reason is YumaSupplementationReason.RESTORED_GPS_ALMANAC_UNKNOWN
  assert state.decision_ready_at == 100.0
  assert state.last_decision_nav_sat_time is None


def test_unexpected_cache_failure_is_logged(monkeypatch):
  logs = []
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_controller.cloudlog.exception",
    logs.append,
  )
  state = controller(
    cache_loader=lambda path: (_ for _ in ()).throw(
      RuntimeError("injected cache bug")
    ),
  )

  plan = evaluate(state)

  assert plan.action is YumaSupplementationAction.WAIT
  assert state.last_cache_error == "RuntimeError: injected cache bug"
  assert logs == ["Unexpected public YUMA cache observation failure"]
