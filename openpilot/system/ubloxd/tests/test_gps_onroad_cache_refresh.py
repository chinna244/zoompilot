from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from openpilot.system.ubloxd import gps_assistance, pigeond
from openpilot.system.ubloxd.gps_assistance import (
  CACHE_TIER_FRESHNESS_WINDOW_SECONDS,
  CacheQualityTier,
  CaptureQualityTracker,
  NavPvtFix,
  NavigationQuality,
  QUALITY_POLICY_VERSION,
  QUALITY_VERSION,
  ReliableFixTracker,
  compare_cache_quality,
  create_cache,
  load_cache,
  navigation_quality_strictly_better,
  navigation_quality_tier,
)
from openpilot.system.ubloxd.tests.test_gps_assistance import (
  build_dbd_frame,
  build_mga_ack_frame,
)


RECEIVER_CYCLE = 3


def quality(
  gps=4,
  glo=6,
  used=8,
  *,
  reliable_seconds=60,
  orbit_seconds=10,
  context="onroad",
):
  return NavigationQuality(
    QUALITY_VERSION,
    QUALITY_POLICY_VERSION,
    context,
    reliable_seconds,
    orbit_seconds,
    8,
    8,
    gps,
    glo,
    used,
    5,
    5,
    4,
    {"ephemeris": 16},
  )


def usable_quality(**kwargs):
  values = {
    "gps": 1,
    "glo": 0,
    "used": 4,
    "orbit_seconds": 0,
    "reliable_seconds": 20,
  }
  values.update(kwargs)
  return quality(**values)


class Tracker:
  def __init__(self, current=None, fix=None):
    self.current = usable_quality() if current is None else current
    self.latest_fix = fix or NavPvtFix(True, 10, 1, 1, 1, 100, 100)

  def quality(self, now, context):
    return replace(self.current, capture_context=context)


def request(state, now, tracker, *, started=True, collector=False, stable=True):
  return state.request(
    now,
    started,
    collector,
    tracker,
    RECEIVER_CYCLE,
    tracker.latest_fix if stable else None,
  )


def complete_initial(state, candidate=None, *, now=20, result=None, durable=None):
  candidate = usable_quality() if candidate is None else candidate
  tracker = Tracker(candidate)
  assert request(state, now, tracker)
  result = result or pigeond.NavigationAssistanceCacheResult.SAVED
  durable = candidate if durable is None else durable
  return state.complete(result, now, durable, candidate)


def test_first_stable_reliable_fix_starts_before_qualified_policy():
  state = pigeond.NavigationCaptureState()
  candidate = usable_quality()
  assert candidate.usable_for_capture
  assert not candidate.passes_policy
  assert request(state, 20, Tracker(candidate))
  assert state.capture_reason == "onroad"
  assert not state.capture_is_upgrade


def test_one_transient_fix_frame_cannot_start_capture():
  state = pigeond.NavigationCaptureState()
  transient = replace(usable_quality(), continuous_reliable_fix_seconds=0)
  assert not request(state, 0, Tracker(transient), stable=False)
  assert not request(state, 0, Tracker(transient), stable=True)


def test_real_trackers_start_at_stable_fix_without_waiting_for_full_policy():
  reliable_tracker = ReliableFixTracker()
  quality_tracker = CaptureQualityTracker()
  state = pigeond.NavigationCaptureState()
  fix = NavPvtFix(True, 10, 1, 1, 1, 100, 100)
  from openpilot.system.ubloxd.gps_assistance import NavSatQuality
  report = NavSatQuality(8, 8, 1, 0, 4, 5, 5, 4, {"ephemeris": 16})
  for second in range(21):
    reliable_tracker.update(fix, second)
    quality_tracker.update_fix(fix, second)
    quality_tracker.update_nav_sat(report, second)

  stable_fix = reliable_tracker.stable_fix(20)
  candidate = quality_tracker.quality(20, "onroad")
  assert candidate is not None and candidate.usable_for_capture
  assert not candidate.passes_policy
  assert state.request(20, True, False, quality_tracker, RECEIVER_CYCLE, stable_fix)


@pytest.mark.parametrize(("request_quality", "completion_quality"), [
  (quality(), usable_quality()),
  (quality(gps=6, glo=7, used=10), quality(gps=5, glo=6, used=9)),
  (usable_quality(), quality(gps=6, glo=7, used=10)),
])
def test_completion_quality_is_persisted_conservatively_end_to_end(
  monkeypatch, tmp_path, request_quality, completion_quality,
):
  path = tmp_path / "navigation_cache.json"
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", path)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(pigeond, "read_rtc_counter_seconds", lambda: 1_000)
  receiver_utc = datetime(2026, 7, 17, tzinfo=UTC)
  fix = NavPvtFix(True, 10, 1, 1, 1, 100, 100, receiver_utc)
  tracker = Tracker(request_quality, fix)
  state = pigeond.NavigationCaptureState()
  assert request(state, 60, tracker)
  assert state.capture_quality == request_quality

  tracker.current = completion_quality
  finalized_quality = pigeond.finalized_capture_quality(
    state, tracker, 61, RECEIVER_CYCLE, tracker.latest_fix,
  )
  expected_quality = gps_assistance.conservative_navigation_quality(
    request_quality, completion_quality,
  )
  assert finalized_quality == expected_quality
  assert finalized_quality is not None
  ordered_fields = (
    "continuous_reliable_fix_seconds",
    "continuous_orbit_quality_seconds",
    "gps_ephemeris_available",
    "glonass_ephemeris_available",
    "satellites_used",
    "gps_almanac_available",
    "glonass_almanac_available",
    "assistnow_offline_available",
  )
  for field in ordered_fields:
    persisted_value = getattr(finalized_quality, field)
    assert persisted_value <= getattr(request_quality, field)
    assert persisted_value <= getattr(completion_quality, field)

  collector = gps_assistance.NavigationDatabaseDumpCollector()
  database_frames = (build_dbd_frame(1), build_dbd_frame(2))
  collector.start(60)
  for frame in database_frames:
    assert collector.feed(frame) is None
  completed = collector.feed(build_mga_ack_frame(
    True,
    0,
    gps_assistance.UBX_ID_MGA_DBD,
    len(database_frames).to_bytes(4, "little"),
  ))
  assert completed == database_frames
  assert state.capture_fix is not None
  result = pigeond.write_navigation_assistance_cache(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    state.capture_fix,
    completed,
    finalized_quality,
    source="onroad_first",
    receiver_cycle=RECEIVER_CYCLE,
    receiver_utc_now=receiver_utc,
    active_receiver_cycle=RECEIVER_CYCLE,
    receiver_utc_fresh=True,
    trusted_promotion_utc=receiver_utc,
  )
  assert result is pigeond.NavigationAssistanceCacheResult.SAVED
  durable_quality = pigeond.durable_quality_after_cache_result(
    result, "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov", receiver_utc,
  )
  state.complete(result, 61, durable_quality, finalized_quality)

  saved = load_cache(
    path,
    expected_receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    require_complete=True,
    expected_receiver_cycle=RECEIVER_CYCLE,
  )
  assert saved.quality == expected_quality
  assert state.durable_baseline_quality == expected_quality


def test_crossed_endpoint_counts_do_not_synthesize_an_upgrade():
  baseline = quality(gps=5, glo=5)
  request_quality = quality(gps=6, glo=5)
  completion_quality = quality(gps=5, glo=6)
  conservative = gps_assistance.conservative_navigation_quality(
    request_quality, completion_quality,
  )
  assert conservative == baseline
  assert navigation_quality_strictly_better(request_quality, baseline)
  assert navigation_quality_strictly_better(completion_quality, baseline)
  assert not navigation_quality_strictly_better(conservative, baseline)

  state = pigeond.NavigationCaptureState()
  complete_initial(state, baseline)
  tracker = Tracker(request_quality)
  assert request(state, 61, tracker)
  tracker.current = completion_quality
  assert pigeond.finalized_capture_quality(
    state, tracker, 62, RECEIVER_CYCLE, tracker.latest_fix,
  ) is None


def test_conservative_qualified_improvement_still_saves(monkeypatch, tmp_path):
  path = tmp_path / "navigation_cache.json"
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", path)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(pigeond, "read_rtc_counter_seconds", lambda: 1_000)
  receiver_utc = datetime(2026, 7, 17, tzinfo=UTC)
  fix = NavPvtFix(True, 10, 1, 1, 1, 100, 100, receiver_utc)
  baseline = quality()
  gps_assistance.save_cache(path, create_cache(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    fix,
    (build_dbd_frame(1),),
    receiver_utc - timedelta(seconds=1),
    quality=baseline,
    receiver_cycle=RECEIVER_CYCLE,
  ))
  state = pigeond.NavigationCaptureState()
  complete_initial(state, baseline)
  request_quality = quality(gps=6, glo=7, used=10)
  completion_quality = quality(gps=5, glo=7, used=9)
  tracker = Tracker(request_quality, fix)
  assert request(state, 61, tracker)
  tracker.current = completion_quality
  finalized_quality = pigeond.finalized_capture_quality(
    state, tracker, 62, RECEIVER_CYCLE, tracker.latest_fix,
  )
  assert finalized_quality is not None
  assert finalized_quality.passes_policy
  assert navigation_quality_strictly_better(finalized_quality, baseline)

  result = pigeond.write_navigation_assistance_cache(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    state.capture_fix,
    (build_dbd_frame(2),),
    finalized_quality,
    source="onroad_refresh",
    receiver_cycle=RECEIVER_CYCLE,
    receiver_utc_now=receiver_utc,
    active_receiver_cycle=RECEIVER_CYCLE,
    receiver_utc_fresh=True,
    trusted_promotion_utc=receiver_utc,
  )
  assert result is pigeond.NavigationAssistanceCacheResult.SAVED
  assert load_cache(path).quality == finalized_quality


def test_complete_usable_cache_is_ready_with_explicit_tier():
  state = pigeond.NavigationCaptureState()
  message = complete_initial(state)
  assert state.drive_cache_saved
  assert state.durable_cache_ready
  assert state.durable_baseline_quality == usable_quality()
  assert "ready=True" in message
  assert "candidate_quality_tier=usable" in message
  assert "selected_quality_tier=usable" in message
  assert "action=candidate_saved_selected" in message
  assert "context=onroad" in message
  assert state.completion_readiness_message(
    "candidate_saved_selected", "onroad", usable_quality(), usable_quality(),
  ) is None
  assert state.drive_end_readiness_message() is None


def test_preserved_existing_cache_is_not_logged_as_an_upgrade():
  state = pigeond.NavigationCaptureState()
  tracker = Tracker()
  assert request(state, 20, tracker)
  message = state.complete(
    pigeond.NavigationAssistanceCacheResult.PRESERVED_EXISTING,
    20,
    usable_quality(),
  )
  assert "action=existing_cache_preserved" in message


def test_first_qualified_improvement_upgrades_immediately():
  state = pigeond.NavigationCaptureState()
  complete_initial(state)
  improved = quality()
  assert request(state, 21, Tracker(improved))
  assert state.capture_is_upgrade
  assert state.capture_reason == "onroad_refresh"
  message = state.complete(
    pigeond.NavigationAssistanceCacheResult.SAVED, 21, improved, improved,
  )
  assert "candidate_quality_tier=qualified" in message
  assert "selected_quality_tier=qualified" in message
  assert "action=candidate_saved_selected" in message
  assert "context=onroad_refresh" in message
  assert state.last_successful_qualified_upgrade == 21


def test_second_better_upgrade_is_allowed_after_five_minutes():
  state = pigeond.NavigationCaptureState()
  complete_initial(state)
  first = quality()
  assert request(state, 21, Tracker(first))
  first_message = state.complete(
    pigeond.NavigationAssistanceCacheResult.SAVED, 21, first, first,
  )
  assert first_message is not None

  second = quality(gps=5)
  assert not request(state, 320.9, Tracker(second))
  assert request(state, 321, Tracker(second))
  second_message = state.complete(
    pigeond.NavigationAssistanceCacheResult.SAVED, 321, second, second,
  )
  assert second_message is not None
  assert first_message != second_message

  third = quality(gps=6)
  assert not request(state, 620.9, Tracker(third))
  assert request(state, 621, Tracker(third))


def test_improvements_during_cooldown_coalesce_to_current_best():
  state = pigeond.NavigationCaptureState()
  complete_initial(state)
  first = quality()
  assert request(state, 21, Tracker(first))
  state.complete(
    pigeond.NavigationAssistanceCacheResult.SAVED, 21, first, first,
  )
  assert not request(state, 100, Tracker(quality(gps=5)))
  assert not request(state, 200, Tracker(quality(gps=6)))
  best = quality(gps=7)
  assert request(state, 321, Tracker(best))
  assert state.capture_quality == best


def test_failed_upgrade_retains_opportunity_and_does_not_advance_cooldown():
  state = pigeond.NavigationCaptureState()
  complete_initial(state)
  improved = quality()
  assert request(state, 21, Tracker(improved))
  state.complete(pigeond.NavigationAssistanceCacheResult.FAILED, 21)
  assert state.last_successful_qualified_upgrade is None
  assert not request(state, 80.9, Tracker(improved))
  assert request(state, 81, Tracker(improved))


@pytest.mark.parametrize("candidate", [quality(gps=3), quality(gps=5, glo=5)])
def test_worse_or_incomparable_quality_does_not_request_upgrade(candidate):
  state = pigeond.NavigationCaptureState()
  complete_initial(state, quality())
  assert not request(state, 21, Tracker(candidate))


def test_degradation_during_upgrade_does_not_change_durable_baseline():
  state = pigeond.NavigationCaptureState()
  baseline = quality()
  complete_initial(state, baseline)
  tracker = Tracker(quality(gps=5))
  assert request(state, 61, tracker)
  tracker.current = quality(gps=5, glo=5)
  assert pigeond.finalized_capture_quality(
    state, tracker, 62, RECEIVER_CYCLE, tracker.latest_fix,
  ) is None
  assert not pigeond.capture_quality_remains_valid(
    state, tracker, 62, RECEIVER_CYCLE, tracker.latest_fix,
  )
  state.complete(pigeond.NavigationAssistanceCacheResult.FAILED, 62)
  assert state.durable_baseline_quality == baseline
  assert state.next_capture_attempt == 122
  assert not request(state, 121.9, Tracker(quality(gps=6)))


def test_wrong_receiver_cycle_invalidates_frozen_capture():
  state = pigeond.NavigationCaptureState()
  tracker = Tracker()
  assert request(state, 20, tracker)
  assert not pigeond.capture_quality_remains_valid(
    state, tracker, 21, RECEIVER_CYCLE + 1, tracker.latest_fix,
  )


def test_receiver_recovery_preserves_progress_and_cooldown():
  state = pigeond.NavigationCaptureState()
  complete_initial(state)
  upgraded = quality()
  assert request(state, 21, Tracker(upgraded))
  state.complete(
    pigeond.NavigationAssistanceCacheResult.SAVED, 21, upgraded, upgraded,
  )
  state.reset_receiver_cycle()
  assert state.drive_cache_saved
  assert state.durable_baseline_quality == upgraded
  assert state.last_successful_qualified_upgrade == 21
  assert not state.frozen


def test_post_drive_is_optional_and_uses_same_progressive_rules():
  initial = pigeond.NavigationCaptureState()
  initial.road_state_changed(False)
  assert request(initial, 20, Tracker(), started=False)
  assert initial.capture_reason == "post_drive"

  upgrade = pigeond.NavigationCaptureState()
  complete_initial(upgrade)
  upgrade.road_state_changed(False)
  assert not request(upgrade, 21, Tracker(usable_quality(gps=2)), started=False)
  assert request(upgrade, 21, Tracker(quality()), started=False)


def test_new_drive_resets_progressive_state():
  state = pigeond.NavigationCaptureState()
  complete_initial(state)
  improved = quality()
  assert request(state, 21, Tracker(improved))
  state.complete(
    pigeond.NavigationAssistanceCacheResult.SAVED, 21, improved, improved,
  )
  state.road_state_changed(True)
  assert not state.drive_cache_saved
  assert state.durable_baseline_quality is None
  assert state.last_successful_qualified_upgrade is None
  assert request(state, 22, Tracker())


def test_route_6c_late_reliable_fix_saves_complete_usable_cache(monkeypatch, tmp_path):
  path = tmp_path / "navigation_cache.json"
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", path)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(pigeond, "read_rtc_counter_seconds", lambda: 1_000)
  reliable_tracker = ReliableFixTracker()
  quality_tracker = CaptureQualityTracker()
  state = pigeond.NavigationCaptureState()
  from openpilot.system.ubloxd.gps_assistance import NavSatQuality
  report = NavSatQuality(8, 8, 1, 0, 4, 5, 5, 4, {"ephemeris": 16})
  receiver_utc = datetime(2026, 7, 17, tzinfo=UTC)

  for second in range(396):
    no_fix = NavPvtFix(False, 0, 0, 0, 0, 100_000, 100_000)
    reliable_tracker.update(no_fix, second)
    quality_tracker.update_fix(no_fix, second)
    quality_tracker.update_nav_sat(report, second)
    assert not state.request(
      second, True, False, quality_tracker, RECEIVER_CYCLE,
      reliable_tracker.stable_fix(second),
    )

  for second in range(396, 417):
    fix = NavPvtFix(
      True, 10, 1, 1, 1, 100, 100,
      receiver_utc + timedelta(seconds=second),
    )
    reliable_tracker.update(fix, second)
    quality_tracker.update_fix(fix, second)
    quality_tracker.update_nav_sat(report, second)

  stable_fix = reliable_tracker.stable_fix(416)
  assert state.request(
    416, True, False, quality_tracker, RECEIVER_CYCLE, stable_fix,
  )
  candidate_quality = state.capture_quality
  assert candidate_quality is not None and candidate_quality.usable_for_capture
  assert not candidate_quality.passes_policy

  collector = gps_assistance.NavigationDatabaseDumpCollector()
  database_frames = (build_dbd_frame(1), build_dbd_frame(2))
  collector.start(416)
  assert collector.feed(database_frames[0]) is None
  assert collector.feed(database_frames[1]) is None
  completed = collector.feed(build_mga_ack_frame(
    True,
    0,
    gps_assistance.UBX_ID_MGA_DBD,
    len(database_frames).to_bytes(4, "little"),
  ))
  assert completed == database_frames

  latest_fix = quality_tracker.latest_fix
  assert latest_fix is not None and latest_fix.utc_time is not None
  result = pigeond.write_navigation_assistance_cache(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    state.capture_fix,
    completed,
    candidate_quality,
    source="onroad_first",
    receiver_cycle=RECEIVER_CYCLE,
    receiver_utc_now=latest_fix.utc_time,
    active_receiver_cycle=RECEIVER_CYCLE,
    receiver_utc_fresh=True,
    receiver_utc_independent=True,
  )
  assert result is pigeond.NavigationAssistanceCacheResult.SAVED
  saved = load_cache(
    path,
    expected_receiver_fingerprint="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov",
    require_complete=True,
    expected_receiver_cycle=RECEIVER_CYCLE,
  )
  assert saved.database_frames == database_frames
  assert saved.quality == candidate_quality


def test_route_6d_no_reliable_fix_leaves_state_and_cache_unchanged(tmp_path):
  path = tmp_path / "navigation_cache.json"
  path.write_bytes(b"existing")
  reliable_tracker = ReliableFixTracker()
  quality_tracker = CaptureQualityTracker()
  state = pigeond.NavigationCaptureState()
  from openpilot.system.ubloxd.gps_assistance import NavSatQuality
  report = NavSatQuality(8, 8, 1, 0, 0, 5, 5, 4, {"ephemeris": 16})
  for second in range(417):
    no_fix = NavPvtFix(False, 0, 0, 0, 0, 100_000, 100_000)
    reliable_tracker.update(no_fix, second)
    quality_tracker.update_fix(no_fix, second)
    quality_tracker.update_nav_sat(report, second)
    assert not state.request(
      second, True, False, quality_tracker, RECEIVER_CYCLE,
      reliable_tracker.stable_fix(second),
    )
  assert path.read_bytes() == b"existing"
  assert not state.drive_cache_saved


def test_readiness_failure_reason_is_usable_not_qualified_and_deduplicated():
  state = pigeond.NavigationCaptureState()
  state.road_state_changed(False)
  message = state.drive_end_readiness_message()
  assert "ready=False" in message
  assert "reason=no_usable_cache_completed" in message
  assert state.drive_end_readiness_message() is None


@pytest.mark.parametrize("result", [
  pigeond.NavigationAssistanceCacheResult.SAVED,
  pigeond.NavigationAssistanceCacheResult.PRESERVED_EXISTING,
])
def test_completed_result_without_selected_durable_cache_retries_safely(result):
  tracker = Tracker()
  state = pigeond.NavigationCaptureState()
  assert request(state, 20, tracker)
  assert state.complete(result, 20, None, usable_quality()) is None
  assert not state.drive_cache_saved
  assert not state.durable_cache_ready
  assert state.durable_baseline_quality is None
  assert state.next_capture_attempt == 80
  assert not request(state, 79.9, tracker)
  assert request(state, 80, tracker)

  post_drive = pigeond.NavigationCaptureState(post_drive_refresh_pending=True)
  assert request(post_drive, 20, tracker, started=False)
  assert post_drive.complete(result, 20, None, usable_quality()) is None
  assert post_drive.post_drive_refresh_pending
  assert post_drive.next_capture_attempt == 80


@pytest.mark.parametrize("result", [
  pigeond.NavigationAssistanceCacheResult.SAVED,
  pigeond.NavigationAssistanceCacheResult.PRESERVED_EXISTING,
])
def test_unresolved_completed_upgrade_retains_confirmed_durable_state(result):
  state = pigeond.NavigationCaptureState()
  baseline = usable_quality()
  complete_initial(state, baseline)
  improved = quality()
  assert request(state, 21, Tracker(improved))

  assert state.complete(result, 21, None, improved) is None
  assert state.drive_cache_saved
  assert state.durable_cache_ready
  assert state.durable_baseline_quality == baseline
  assert state.next_capture_attempt == 81
  assert not request(state, 21.1, Tracker(improved))


def test_shared_strictly_better_definition_remains_pareto_conservative():
  baseline = quality()
  assert navigation_quality_strictly_better(quality(gps=5), baseline)
  assert navigation_quality_strictly_better(quality(used=9), baseline)
  assert not navigation_quality_strictly_better(quality(gps=5, glo=5), baseline)
  assert not navigation_quality_strictly_better(baseline, baseline)


def test_quality_tiers_are_explicit_and_full_policy_is_unchanged():
  usable = usable_quality()
  qualified = quality()
  assert navigation_quality_tier(usable) is CacheQualityTier.USABLE
  assert navigation_quality_tier(qualified) is CacheQualityTier.QUALIFIED
  assert usable.usable_for_capture and not usable.passes_policy
  assert qualified.usable_for_capture and qualified.passes_policy


def test_cache_comparison_accepts_fresh_usable_but_keeps_old_recoverable():
  saved_at = datetime(2026, 7, 17, tzinfo=UTC)
  fix = NavPvtFix(True, 10, 1, 1, 1, 100, 100)

  def cache(candidate_quality, when):
    return create_cache(
      "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov", fix, (build_dbd_frame(1),), when, quality=candidate_quality,
    )

  replace_cache, reason = compare_cache_quality(
    cache(quality(gps=6), saved_at),
    cache(usable_quality(), saved_at + timedelta(seconds=1)),
    saved_at + timedelta(seconds=1),
  )
  assert replace_cache
  assert reason == "fresh_usable_candidate_preserves_existing_fallback"


def test_freshness_window_is_explicit_and_bounded():
  assert CACHE_TIER_FRESHNESS_WINDOW_SECONDS == 4 * 60 * 60
