import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import (
  CacheValidationError,
  CaptureQualityTracker,
  MAXIMUM_NAV_PVT_GAP_SECONDS,
  MAXIMUM_NAV_SAT_AGE_SECONDS,
  NavPvtFix,
  NavigationQuality,
  NavSatQuality,
  QUALITY_POLICY_VERSION,
  QUALITY_VERSION,
  add_ubx_checksum,
  compare_cache_quality,
  create_cache,
  load_cache,
  parse_nav_sat,
  save_cache,
)
from openpilot.system.ubloxd.tests.test_gps_assistance import build_dbd_frame, build_mga_ack_frame


def fix(reliable=True):
  return NavPvtFix(reliable, 10, 1, 1, 1, 100, 100)


def report(gps=4, glo=6, used=8):
  return NavSatQuality(8, 8, gps, glo, used, 5, 5, 4, {"ephemeris": 16})


def quality(gps=4, glo=6, used=8, context="onroad"):
  return NavigationQuality(
    QUALITY_VERSION, QUALITY_POLICY_VERSION, context, 60, 10,
    8, 8, gps, glo, used, 5, 5, 4, {"ephemeris": 16},
  )


def mature_tracker() -> CaptureQualityTracker:
  tracker = CaptureQualityTracker()
  for second in range(61):
    tracker.update_fix(fix(), second)
    if second >= 50:
      tracker.update_nav_sat(report(), second)
  return tracker


def cache(tmp_quality, saved_at=datetime(2026, 7, 13, tzinfo=UTC), receiver="v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov"):
  return create_cache(receiver, fix(), (build_dbd_frame(1),), saved_at, quality=tmp_quality)


def nav_sat_frame(satellite_ids):
  flags = (1 << 3) | (1 << 8) | (1 << 11) | (1 << 12) | (1 << 13)
  blocks = [
    bytes((gnss_id, sv_id, 30, 10)) + b"\x00" * 4 + flags.to_bytes(4, "little")
    for gnss_id, sv_id in satellite_ids
  ]
  payload = b"\x00" * 4 + bytes((1, len(blocks))) + b"\x00\x00" + b"".join(blocks)
  return add_ubx_checksum(b"\xb5\x62\x01\x35" + len(payload).to_bytes(2, "little") + payload)


def test_route_55_state_does_not_become_eligible_then_quality_matures():
  tracker = CaptureQualityTracker()
  for second in range(61):
    tracker.update_fix(fix(), second)
    tracker.update_nav_sat(report(3, 5, 7), second)
  assert not tracker.eligible(60)

  for second in range(61, 72):
    tracker.update_fix(fix(), second)
    tracker.update_nav_sat(report(4, 6, 8), second)
  assert tracker.eligible(71)


def test_nav_sat_parser_tracks_orbits_and_excludes_only_explicitly_unhealthy_ephemeris():
  blocks = []
  for sv_id, (gnss_id, health) in enumerate(((0, 0), (0, 2), (6, 1)), start=1):
    flags = (1 << 3) | (health << 4) | (1 << 8) | (1 << 11) | (1 << 12) | (1 << 13)
    blocks.append(bytes((gnss_id, sv_id, 30, 10)) + b"\x00" * 4 + flags.to_bytes(4, "little"))
  payload = b"\x00" * 4 + bytes((1, len(blocks))) + b"\x00\x00" + b"".join(blocks)
  frame = add_ubx_checksum(b"\xb5\x62\x01\x35" + len(payload).to_bytes(2, "little") + payload)

  parsed = parse_nav_sat(frame)
  assert parsed is not None
  assert (parsed.gps_satellites_known, parsed.glonass_satellites_known) == (2, 1)
  assert (parsed.gps_ephemeris_available, parsed.glonass_ephemeris_available) == (1, 1)
  assert parsed.satellites_used == 3
  assert parsed.assistnow_offline_available == 3
  assert parsed.orbit_source_counts == {"ephemeris": 3}


@pytest.mark.parametrize("duplicate_gnss_id", [0, 6, 2, 3])
def test_duplicate_nav_sat_snapshot_is_rejected_and_next_valid_snapshot_recovers(duplicate_gnss_id):
  tracker = mature_tracker()
  before = tracker.quality(60, "onroad")
  duplicate = nav_sat_frame(((duplicate_gnss_id, 1), (duplicate_gnss_id, 1), (0, 2), (6, 2)))
  assert parse_nav_sat(duplicate) is None
  assert tracker.quality(60, "onroad") == before

  valid_ids = tuple((0, sv_id) for sv_id in range(1, 9)) + tuple((6, sv_id) for sv_id in range(1, 9))
  recovered = parse_nav_sat(nav_sat_frame(valid_ids))
  assert recovered is not None
  tracker.update_fix(fix(), 61)
  tracker.update_nav_sat(recovered, 61)
  assert tracker.quality(61, "onroad") is not None


def test_process_receiver_frames_does_not_apply_duplicate_nav_sat_snapshot():
  tracker = mature_tracker()
  before = tracker.quality(60, "onroad")
  duplicate = nav_sat_frame(((0, 1), (0, 1), (6, 1)))
  valid_ids = tuple((0, sv_id) for sv_id in range(1, 9)) + tuple((6, sv_id) for sv_id in range(1, 9))
  valid = nav_sat_frame(valid_ids)
  autonomous_reports = []

  class StartupDiagnostics:
    def note_rawx(self, _frame, _now):
      pass

    def note_nav_pvt(self, _fix, _now):
      raise AssertionError("No NAV-PVT frame was supplied")

  class FixTracker:
    def update(self, _fix, _now):
      raise AssertionError("No NAV-PVT frame was supplied")

  class AutonomousDiagnostics:
    def note_nav_sat(self, report):
      autonomous_reports.append(report)

  arguments = (
    StartupDiagnostics(),
    FixTracker(),
    tracker,
    AutonomousDiagnostics(),
    pigeond.NavigationDatabaseDumpCollector(),
    pigeond.NavigationCaptureState(),
  )
  pigeond.process_receiver_frames([duplicate], 60.5, *arguments)
  assert tracker.quality(60, "onroad") == before
  assert autonomous_reports == []

  tracker.update_fix(fix(), 61)
  pigeond.process_receiver_frames([valid], 61, *arguments)
  assert len(autonomous_reports) == 1
  assert tracker.quality(61, "onroad") is not None


def test_continuous_fix_and_orbit_periods_are_both_required():
  tracker = CaptureQualityTracker()
  for second in range(61):
    tracker.update_fix(fix(), second)
  for second in range(51, 61):
    tracker.update_nav_sat(report(), second)
  assert not tracker.eligible(60)
  tracker.update_fix(fix(), 61)
  tracker.update_nav_sat(report(), 61)
  assert tracker.eligible(61)


@pytest.mark.parametrize("bad_report", [
  report(3, 7, 8),
  report(5, 4, 8),
  report(4, 5, 8),
  report(5, 5, 7),
])
def test_each_orbit_boundary_below_threshold_fails(bad_report):
  tracker = mature_tracker()
  tracker.update_nav_sat(bad_report, 61)
  assert not tracker.eligible(61)


def test_unreliable_fix_threshold_failure_and_nav_sat_gap_reset_timing():
  tracker = mature_tracker()
  assert tracker.eligible(60)
  tracker.update_fix(fix(False), 61)
  assert not tracker.eligible(61)
  tracker = mature_tracker()
  tracker.update_nav_sat(report(3, 6, 8), 61)
  assert not tracker.eligible(61)
  tracker = mature_tracker()
  tracker.update_nav_sat(report(), 63.1)
  assert not tracker.eligible(63.1)


def test_receiver_restart_resets_all_quality_state():
  tracker = mature_tracker()
  tracker.reset()
  assert tracker.quality(60, "onroad") is None


def test_quality_degradation_during_dump_invalidates_live_gate():
  tracker = mature_tracker()
  frozen = tracker.quality(60, "onroad")
  assert frozen is not None and frozen.passes_policy
  tracker.update_nav_sat(report(3, 6, 8), 61)
  live = tracker.quality(61, "onroad")
  assert live is not None
  assert live.usable_for_capture
  assert not live.passes_policy


def test_quality_metadata_round_trip_and_malformed_is_rejected(tmp_path):
  path = tmp_path / "cache.json"
  save_cache(path, cache(quality()))
  assert load_cache(path).quality == quality()
  raw = json.loads(path.read_text())
  raw["quality"] = {"version": QUALITY_VERSION, "broken": True}
  path.write_text(json.dumps(raw))
  with pytest.raises(CacheValidationError):
    load_cache(path)
  save_cache(path, cache(quality()))
  raw = json.loads(path.read_text())
  raw.pop("quality")
  path.write_text(json.dumps(raw))
  assert load_cache(path).quality is None


def test_unknown_quality_and_policy_versions_are_rejected(tmp_path):
  path = tmp_path / "cache.json"
  save_cache(path, cache(quality()))
  for field in ("version", "policy_version"):
    raw = json.loads(path.read_text())
    raw["quality"][field] = 999
    path.write_text(json.dumps(raw))
    with pytest.raises(CacheValidationError):
      load_cache(path)
    save_cache(path, cache(quality()))


@pytest.mark.parametrize(("field", "value"), [
  ("gps_satellites_known", True),
  ("gps_satellites_known", 8.5),
  ("gps_satellites_known", -1),
  ("continuous_reliable_fix_seconds", float("nan")),
  ("continuous_orbit_quality_seconds", float("inf")),
])
def test_wrong_or_nonfinite_quality_values_are_rejected(tmp_path, field, value):
  path = tmp_path / "cache.json"
  save_cache(path, cache(quality()))
  raw = json.loads(path.read_text())
  raw["quality"][field] = value
  path.write_text(json.dumps(raw))
  with pytest.raises(CacheValidationError):
    load_cache(path)


@pytest.mark.parametrize(("field", "value"), [
  ("gps_ephemeris_available", 9),
  ("gps_almanac_available", 9),
  ("glonass_ephemeris_available", 9),
  ("glonass_almanac_available", 9),
  ("satellites_used", 17),
  ("assistnow_offline_available", 17),
  ("total_ephemeris_available", 99),
  ("orbit_source_counts", {"ephemeris": 15}),
])
def test_impossible_quality_counts_are_rejected(tmp_path, field, value):
  path = tmp_path / "cache.json"
  save_cache(path, cache(quality()))
  raw = json.loads(path.read_text())
  raw["quality"][field] = value
  path.write_text(json.dumps(raw))
  with pytest.raises(CacheValidationError):
    load_cache(path)


def test_huge_quality_integer_cannot_escape_as_overflow(tmp_path):
  path = tmp_path / "cache.json"
  save_cache(path, cache(quality()))
  raw = json.loads(path.read_text())
  raw["quality"]["continuous_reliable_fix_seconds"] = 10 ** 1000
  path.write_text(json.dumps(raw))
  with pytest.raises(CacheValidationError):
    load_cache(path)


def test_directly_constructed_malformed_quality_is_not_serialized(tmp_path):
  malformed = replace(quality(), satellites_used=17)
  with pytest.raises(CacheValidationError, match="quality metadata is invalid"):
    save_cache(tmp_path / "cache.json", cache(malformed))


@pytest.mark.parametrize(("existing_quality", "candidate_quality", "replace_expected"), [
  (quality(5, 6, 9), quality(4, 6, 8), False),
  (quality(4, 5, 8), quality(4, 6, 8), True),
  (quality(4, 6, 8), quality(5, 5, 8), False),
  (quality(4, 6, 8), quality(4, 6, 9), True),
])
def test_recent_cache_pareto_comparison(existing_quality, candidate_quality, replace_expected):
  now = datetime(2026, 7, 13, tzinfo=UTC)
  replace_cache, _ = compare_cache_quality(cache(existing_quality, now), cache(candidate_quality, now), now)
  assert replace_cache is replace_expected


def test_timestamp_age_alone_never_replaces_stronger_eligible_cache():
  saved = datetime(2026, 7, 13, tzinfo=UTC)
  existing = cache(quality(5, 6, 9), saved)
  candidate = cache(quality(4, 6, 8), saved)
  replace_cache, _ = compare_cache_quality(existing, candidate, saved + timedelta(hours=4))
  assert not replace_cache
  assert not compare_cache_quality(existing, candidate, None)[0]
  assert not compare_cache_quality(existing, candidate, saved - timedelta(seconds=1))[0]


def test_usable_below_policy_candidate_replaces_legacy_cache():
  candidate = replace(quality(), satellites_used=7)
  assert candidate.usable_for_capture and not candidate.passes_policy
  assert compare_cache_quality(cache(None), cache(candidate), None)[0]


def test_post_drive_quality_is_qualified():
  tracker = mature_tracker()
  frozen = tracker.quality(60, "post_drive")
  assert frozen is not None and frozen.passes_policy
  assert frozen.capture_context == "post_drive"


def test_preserved_existing_completes_capture_phase_without_retry():
  assert pigeond.navigation_cache_phase_completed(
    pigeond.NavigationAssistanceCacheResult.PRESERVED_EXISTING
  )
  assert not pigeond.navigation_cache_phase_completed(
    pigeond.NavigationAssistanceCacheResult.FAILED
  )




def test_production_capture_request_starts_once_when_usable_quality_exists():
  tracker = CaptureQualityTracker()
  state = pigeond.NavigationCaptureState()
  sent = []
  for second in range(61):
    tracker.update_fix(fix(), second)
    if second >= 50:
      tracker.update_nav_sat(report(), second)
    if state.request(second, True, bool(sent), tracker, 1, tracker.latest_fix):
      sent.append(pigeond.build_database_poll_message())
  assert sent == [pigeond.build_database_poll_message()]
  assert not state.request(120, True, True, tracker)


def test_below_qualified_route_still_requests_one_usable_database_poll():
  tracker = CaptureQualityTracker()
  state = pigeond.NavigationCaptureState()
  requests = 0
  for second in range(121):
    tracker.update_fix(fix(), second)
    tracker.update_nav_sat(report(3, 5, 7), second)
    if state.request(second, True, False, tracker, 1, tracker.latest_fix):
      requests += 1
  assert requests == 1


def test_nav_pvt_and_nav_sat_gap_policies_reset_only_their_own_interval():
  assert MAXIMUM_NAV_PVT_GAP_SECONDS == MAXIMUM_NAV_SAT_AGE_SECONDS == 2
  tracker = mature_tracker()
  tracker.update_nav_sat(report(), 61)
  tracker.update_nav_sat(report(), 62)
  tracker.update_fix(fix(), 63.1)
  after_pvt_gap = tracker.quality(63.1, "onroad")
  assert after_pvt_gap is not None
  assert after_pvt_gap.continuous_reliable_fix_seconds == 0
  assert after_pvt_gap.continuous_orbit_quality_seconds == pytest.approx(13.1)

  tracker = mature_tracker()
  tracker.update_fix(fix(), 61)
  tracker.update_fix(fix(), 62)
  tracker.update_fix(fix(), 63.1)
  tracker.update_nav_sat(report(), 63.1)
  after_nav_sat_gap = tracker.quality(63.1, "onroad")
  assert after_nav_sat_gap is not None
  assert after_nav_sat_gap.continuous_reliable_fix_seconds == pytest.approx(63.1)
  assert after_nav_sat_gap.continuous_orbit_quality_seconds == 0


def test_receiver_recovery_resets_intervals_and_frozen_capture():
  tracker = mature_tracker()
  state = pigeond.NavigationCaptureState()
  collector = pigeond.NavigationDatabaseDumpCollector()
  assert state.request(60, True, False, tracker, 1, tracker.latest_fix)
  collector.start(60)
  tracker.reset()
  collector.cancel()
  state.reset_receiver_cycle()
  assert tracker.quality(60, "onroad") is None
  assert not state.frozen
  assert not collector.active


def test_below_policy_quality_during_initial_dump_remains_usable(tmp_path):
  path = tmp_path / "cache.json"
  save_cache(path, cache(quality(5, 6, 9)))
  before = path.read_bytes()
  tracker = mature_tracker()
  state = pigeond.NavigationCaptureState()
  assert state.request(60, True, False, tracker, 1, tracker.latest_fix)
  tracker.update_nav_sat(report(3, 6, 8), 61)
  assert pigeond.capture_quality_remains_valid(
    state, tracker, 61, 1, tracker.latest_fix,
  )
  state.complete(
    pigeond.NavigationAssistanceCacheResult.SAVED,
    61,
    tracker.quality(61, "onroad"),
  )
  assert path.read_bytes() == before


def test_preserved_result_completes_onroad_and_post_drive_without_later_poll():
  tracker = mature_tracker()
  onroad = pigeond.NavigationCaptureState()
  assert onroad.request(60, True, False, tracker, 1, tracker.latest_fix)
  onroad.complete(
    pigeond.NavigationAssistanceCacheResult.PRESERVED_EXISTING, 60, quality(),
  )
  assert onroad.drive_cache_saved
  assert not onroad.request(120, True, False, tracker, 1, tracker.latest_fix)

  post_drive = pigeond.NavigationCaptureState(post_drive_refresh_pending=True)
  assert post_drive.request(60, False, False, tracker, 1, tracker.latest_fix)
  post_drive.complete(
    pigeond.NavigationAssistanceCacheResult.PRESERVED_EXISTING, 60, quality(),
  )
  assert not post_drive.post_drive_refresh_pending
  assert not post_drive.request(120, False, False, tracker, 1, tracker.latest_fix)


def _write_candidate(monkeypatch, path, candidate_quality):
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", path)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  candidate_fix = replace(fix(), utc_time=datetime(2026, 7, 13, 1, tzinfo=UTC))
  return pigeond.write_navigation_assistance_cache(
    "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov", candidate_fix, (build_dbd_frame(2),), candidate_quality,
    receiver_utc_independent=True,
  )


def test_writer_preserves_better_existing_cache_byte_for_byte(monkeypatch, tmp_path):
  path = tmp_path / "cache.json"
  save_cache(path, cache(quality(5, 6, 9)))
  before = path.read_bytes()
  result = _write_candidate(monkeypatch, path, quality(4, 6, 8))
  assert result is pigeond.NavigationAssistanceCacheResult.PRESERVED_EXISTING
  assert path.read_bytes() == before


def test_post_drive_candidate_replaces_inferior_onroad_cache(monkeypatch, tmp_path):
  path = tmp_path / "cache.json"
  save_cache(path, cache(quality(4, 6, 8)))
  result = _write_candidate(monkeypatch, path, quality(5, 6, 8, "post_drive"))
  assert result is pigeond.NavigationAssistanceCacheResult.SAVED
  assert load_cache(path).quality == quality(5, 6, 8, "post_drive")


def test_usable_candidate_replaces_legacy_but_unstable_candidate_never_does(monkeypatch, tmp_path):
  path = tmp_path / "cache.json"
  save_cache(path, cache(None))
  legacy = path.read_bytes()
  unstable = replace(quality(), continuous_reliable_fix_seconds=19)
  assert _write_candidate(monkeypatch, path, unstable) is pigeond.NavigationAssistanceCacheResult.FAILED
  assert path.read_bytes() == legacy
  usable = replace(quality(), satellites_used=7)
  assert _write_candidate(monkeypatch, path, usable) is pigeond.NavigationAssistanceCacheResult.SAVED
  assert load_cache(path).quality == usable


def test_dump_timeout_rejection_and_save_failure_preserve_existing(monkeypatch, tmp_path):
  path = tmp_path / "cache.json"
  save_cache(path, cache(quality(4, 6, 8)))
  before = path.read_bytes()
  collector = pigeond.NavigationDatabaseDumpCollector(timeout_seconds=1)
  collector.start(0)
  assert collector.expired(1)
  collector.cancel()
  assert path.read_bytes() == before

  collector.start(0)
  rejection = build_mga_ack_frame(False, 2, 0x80, b"\0\0\0\0")
  with pytest.raises(CacheValidationError):
    collector.feed(rejection)
  assert path.read_bytes() == before

  monkeypatch.setattr(
    pigeond.NavigationCacheStore,
    "_write_candidate",
    lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk")),
  )
  result = _write_candidate(monkeypatch, path, quality(5, 6, 8))
  assert result is pigeond.NavigationAssistanceCacheResult.FAILED
  assert path.read_bytes() == before
