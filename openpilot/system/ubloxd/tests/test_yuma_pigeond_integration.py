from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaDatabaseRestoreState,
  YumaSupplementationAction,
  YumaSupplementationPlan,
  YumaSupplementationReason,
  plan_yuma_supplementation,
)
from openpilot.system.ubloxd.yuma_almanac_runtime import (
  YumaSupplementationRuntimeOutcome,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  YumaAlmanacTransmitResult,
  YumaAlmanacTransmitStatus,
)


NOW = datetime(2026, 7, 21, 15, tzinfo=UTC)
SAVED_AT = datetime(2026, 7, 21, 10, tzinfo=UTC)


def initialization(status):
  restore_result = pigeond.NavigationAssistanceRestoreResult(
    status=status,
    total_frame_count=3,
    accepted_frame_count=(
      3
      if status is pigeond.NavigationAssistanceRestoreStatus.COMPLETE
      else 2
    ),
    cache_saved_at_utc=SAVED_AT,
    restored_cache_generation="previous",
    restored_cache_selection_reason="previous_gps_startup_ready",
    restored_gps_almanac_available=10,
    restored_glonass_almanac_available=9,
    restored_gps_ephemeris_available=0,
    restored_glonass_ephemeris_available=5,
    restored_satellites_used=5,
    restored_gps_startup_ready=False,
    restored_gps_almanac_satellite_ids=tuple(range(1, 11)),
  )
  return SimpleNamespace(
    navigation_assistance_restore_result=restore_result,
    completed_at=100.0,
    time_assistance_utc=NOW,
    time_assistance_source="rtc_estimate",
  )


def test_pigeond_maps_navigation_restore_state_for_yuma():
  assert (
    pigeond.yuma_database_restore_state(
      initialization(
        pigeond.NavigationAssistanceRestoreStatus.COMPLETE
      ).navigation_assistance_restore_result
    )
    is YumaDatabaseRestoreState.COMPLETE
  )
  assert (
    pigeond.yuma_database_restore_state(
      initialization(
        pigeond.NavigationAssistanceRestoreStatus.PARTIAL
      ).navigation_assistance_restore_result
    )
    is YumaDatabaseRestoreState.PARTIAL
  )
  assert (
    pigeond.yuma_database_restore_state(None)
    is YumaDatabaseRestoreState.FAILED
  )


@pytest.mark.parametrize(
  ("disposition", "expected"),
  (
    (
      pigeond.NavigationDatabaseRestoreDisposition.PENDING,
      YumaDatabaseRestoreState.PENDING,
    ),
    (
      pigeond.NavigationDatabaseRestoreDisposition.RESTORED,
      YumaDatabaseRestoreState.COMPLETE,
    ),
    (
      pigeond.NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
      YumaDatabaseRestoreState.PARTIAL,
    ),
    (
      pigeond.NavigationDatabaseRestoreDisposition.RESTORE_REJECTED,
      YumaDatabaseRestoreState.REJECTED,
    ),
    (
      pigeond.NavigationDatabaseRestoreDisposition.RESTORE_RESPONSE_TIMEOUT,
      YumaDatabaseRestoreState.RESPONSE_TIMEOUT,
    ),
    (
      pigeond.NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE,
      YumaDatabaseRestoreState.TRANSFER_DEADLINE,
    ),
    (
      pigeond.NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR,
      YumaDatabaseRestoreState.TRANSPORT_ERROR,
    ),
    (
      pigeond.NavigationDatabaseRestoreDisposition.RESTORE_CACHE_EXPIRED,
      YumaDatabaseRestoreState.EXPIRED,
    ),
    (
      pigeond.NavigationDatabaseRestoreDisposition.SKIPPED_WAIT_TIMEOUT,
      YumaDatabaseRestoreState.SKIPPED,
    ),
  ),
)
def test_pigeond_preserves_exact_database_outcome_for_yuma(
  disposition,
  expected,
):
  result = pigeond.NavigationAssistanceRestoreResult(
    status=pigeond.NavigationAssistanceRestoreStatus.FAILED,
    total_frame_count=69,
    accepted_frame_count=0,
    database_restore_disposition=disposition,
  )
  assert pigeond.yuma_database_restore_state(result) is expected


def test_d9_style_wait_timeout_reaches_exact_yuma_fallback() -> None:
  restore = pigeond.NavigationAssistanceRestoreResult(
    status=pigeond.NavigationAssistanceRestoreStatus.FAILED,
    total_frame_count=69,
    accepted_frame_count=0,
    database_restore_disposition=(
      pigeond.NavigationDatabaseRestoreDisposition.SKIPPED_WAIT_TIMEOUT
    ),
    database_trusted_time_wait_allowed=True,
    database_network_available=True,
    database_trusted_time_wait_elapsed_seconds=42.0,
  )
  database_state = pigeond.yuma_database_restore_state(restore)
  plan = plan_yuma_supplementation(
    database_state=database_state,
    database_age_seconds=None,
    yuma_reference_age_seconds=60.0,
    nav_sat=None,
    yuma_satellite_ids=frozenset(range(1, 33)),
    trusted_time_available=True,
    reliable_fix_available=False,
    trusted_time_wait_expired=True,
    cache_wait_expired=True,
    nav_sat_observation_expired=True,
  )

  assert database_state is YumaDatabaseRestoreState.SKIPPED
  assert plan.action is YumaSupplementationAction.SEND_ALL
  assert plan.reason is YumaSupplementationReason.DATABASE_RESTORE_SKIPPED


def test_pigeond_builds_runtime_from_cycle_initialization():
  runtime = pigeond.create_yuma_supplementation_runtime(
    initialization(
      pigeond.NavigationAssistanceRestoreStatus.COMPLETE
    )
  )

  assert runtime.controller.database_state is (
    YumaDatabaseRestoreState.COMPLETE
  )
  assert runtime.controller.database_saved_at_utc == SAVED_AT
  assert runtime.controller.restored_cache_generation == "previous"
  assert (
    runtime.controller.restored_cache_selection_reason
    == "previous_gps_startup_ready"
  )
  assert runtime.controller.restored_gps_almanac_available == 10
  assert runtime.controller.restored_glonass_almanac_available == 9
  assert runtime.controller.restored_gps_ephemeris_available == 0
  assert runtime.controller.restored_glonass_ephemeris_available == 5
  assert runtime.controller.restored_satellites_used == 5
  assert runtime.controller.restored_gps_startup_ready is False
  assert runtime.controller.restored_gps_almanac_satellite_ids == tuple(range(1, 11))
  assert runtime.trusted_now(100.0) == NOW
  assert runtime.time_anchor_source == "rtc_estimate"


def test_pigeond_logs_terminal_yuma_outcome(monkeypatch):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  outcome = YumaSupplementationRuntimeOutcome(
    plan=YumaSupplementationPlan(
      action=YumaSupplementationAction.SEND_MISSING,
      reason=(
        YumaSupplementationReason.MISSING_VISIBLE_GPS_ALMANAC
      ),
      satellite_ids=frozenset((2, 4)),
    ),
    restored_cache_generation="previous",
    restored_cache_selection_reason="previous_gps_startup_ready",
    restored_gps_almanac_available=10,
    restored_glonass_almanac_available=9,
    restored_gps_ephemeris_available=0,
    restored_glonass_ephemeris_available=5,
    restored_satellites_used=5,
    restored_gps_startup_ready=False,
    restored_gps_almanac_satellite_ids=tuple(range(1, 11)),
    runtime_elapsed_seconds=260.0,
    completion_elapsed_seconds=260.0,
    completion_utc=NOW,
    yuma_snapshot_sha256="b" * 64,
    terminal=True,
    receiver_cycle=7,
    feature_enabled=True,
    time_anchor_source="receiver_utc",
    trusted_now_utc=NOW,
    trusted_time_wait_expired=True,
    nav_sat_observation_expired=True,
    transmit_result=YumaAlmanacTransmitResult(
      status=YumaAlmanacTransmitStatus.COMPLETE,
      requested_satellite_ids=(2, 4),
      attempted_satellite_ids=(2, 4),
      accepted_satellite_ids=(2, 4),
    ),
  )

  pigeond.log_yuma_supplementation_outcome(outcome)

  assert len(logs) == 1
  assert "enabled=true" in logs[0]
  assert "terminal=true" in logs[0]
  assert "time_anchor_source=receiver_utc" in logs[0]
  assert "trusted_time_wait_expired=true" in logs[0]
  assert "action=send_missing" in logs[0]
  assert "reason=missing_visible_gps_almanac" in logs[0]
  assert "accepted_prns=2,4" in logs[0]
  assert "restored_cache_generation=previous" in logs[0]
  assert "restored_gps_almanac_available=10" in logs[0]
  assert "restored_gps_ephemeris_available=0" in logs[0]
  assert "restored_gps_startup_ready=False" in logs[0]
  assert "restored_gps_almanac_satellite_ids=(1, 2, 3, 4, 5, 6, 7, 8, 9, 10)" in logs[0]
  assert "completion_elapsed_seconds=260.0" in logs[0]
  assert "yuma_snapshot_sha256=" + "b" * 64 in logs[0]


def test_pigeond_persists_outcome_with_commit_and_cycle(monkeypatch):
  saved = []
  logs = []
  outcome = YumaSupplementationRuntimeOutcome(
    plan=YumaSupplementationPlan(
      YumaSupplementationAction.WAIT,
      YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME,
    ),
    terminal=False,
    receiver_cycle=7,
    feature_enabled=True,
  )
  params = SimpleNamespace(
    get=lambda key, encoding: (
      "8e5d28c4f48d049715b49d789b2e8e95ef286194"
    ),
  )

  monkeypatch.setattr(
    pigeond,
    "save_yuma_supplementation_outcome",
    lambda *args, **kwargs: saved.append((args, kwargs)),
  )
  monkeypatch.setattr(pigeond.cloudlog, "exception", logs.append)

  pigeond.persist_yuma_supplementation_outcome(
    outcome,
    params,
  )

  assert logs == []
  assert len(saved) == 1
  assert saved[0][0] == (pigeond.YUMA_LAST_OUTCOME_PATH, outcome)
  assert saved[0][1]["commit"] == (
    "8e5d28c4f48d049715b49d789b2e8e95ef286194"
  )
  assert saved[0][1]["receiver_cycle"] == 7
  assert saved[0][1]["recorded_at_utc"] is None


def test_pigeond_contains_outcome_persistence_failure(monkeypatch):
  logs = []
  outcome = YumaSupplementationRuntimeOutcome(
    plan=YumaSupplementationPlan(
      YumaSupplementationAction.SKIP,
      YumaSupplementationReason.FEATURE_DISABLED,
    ),
    terminal=True,
    receiver_cycle=1,
    feature_enabled=False,
  )
  params = SimpleNamespace(get=lambda key, encoding: "test")

  monkeypatch.setattr(
    pigeond,
    "save_yuma_supplementation_outcome",
    lambda *args, **kwargs: (_ for _ in ()).throw(
      OSError("injected persistence failure")
    ),
  )
  monkeypatch.setattr(pigeond.cloudlog, "exception", logs.append)

  pigeond.persist_yuma_supplementation_outcome(
    outcome,
    params,
  )

  assert logs == ["GPS public YUMA outcome persistence failed"]


def test_pigeond_logs_feature_disabled_state(monkeypatch):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  outcome = YumaSupplementationRuntimeOutcome(
    plan=YumaSupplementationPlan(
      YumaSupplementationAction.SKIP,
      YumaSupplementationReason.FEATURE_DISABLED,
    ),
    terminal=True,
    receiver_cycle=4,
    feature_enabled=False,
  )

  pigeond.log_yuma_supplementation_outcome(outcome)

  assert len(logs) == 1
  assert "enabled=false" in logs[0]
  assert "reason=feature_disabled" in logs[0]
