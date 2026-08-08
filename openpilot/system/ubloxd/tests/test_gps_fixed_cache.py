import json
import os
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path

import pytest

from openpilot.system.ubloxd import gps_assistance
from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import (
  CacheAgeEvidence,
  CacheFileState,
  CachePromotionStatus,
  CachePromotionStage,
  CacheValidationError,
  DEFAULT_MAX_CACHE_AGE_SECONDS,
  NavPvtFix,
  NavigationCacheStore,
  NavigationQuality,
  QUALITY_POLICY_VERSION,
  QUALITY_VERSION,
  create_cache,
  evaluate_utc_from_rtc,
  load_cache,
  navigation_quality_strictly_better,
  save_cache,
  RtcEstimateRejection,
  RtcEstimateRejectionReason,
)
from openpilot.system.ubloxd.tests.test_gps_assistance import (
  build_dbd_frame,
  build_mga_ack_frame,
  build_nav_pvt_frame,
)


NOW = datetime(2026, 7, 16, 12, tzinfo=UTC)
RECEIVER = "v1|receiver|sw=ext core 3.01|hw=00080000|prot=20.30|fw=hpg 1.40rov"
CYCLE = 7


def quality(
  gps=4,
  glo=6,
  used=8,
  gps_almanac=5,
  glo_almanac=5,
  offline=4,
  context="onroad",
):
  return NavigationQuality(
    QUALITY_VERSION,
    QUALITY_POLICY_VERSION,
    context,
    60,
    10,
    8,
    8,
    gps,
    glo,
    used,
    gps_almanac,
    glo_almanac,
    offline,
    {"ephemeris": 16},
  )


def usable_quality(**changes):
  return replace(
    quality(),
    continuous_reliable_fix_seconds=20,
    continuous_orbit_quality_seconds=0,
    gps_ephemeris_available=1,
    glonass_ephemeris_available=0,
    satellites_used=4,
    **changes,
  )


def cache(cache_quality=None, *, frame_id=1, saved_at=NOW, cycle=CYCLE):
  fix = NavPvtFix(True, 10, 1, 2, 3, 100, 100, saved_at)
  return create_cache(
    RECEIVER,
    fix,
    (build_dbd_frame(frame_id),),
    saved_at,
    rtc_counter_seconds=1000,
    quality=quality() if cache_quality is None else cache_quality,
    receiver_cycle=cycle,
  )


def store(tmp_path):
  return NavigationCacheStore(tmp_path / "navigation_cache.json")


def promote(cache_store, candidate):
  return cache_store.promote(candidate, RECEIVER, NOW, CYCLE)


def test_fixed_store_creates_only_primary_previous_and_candidate_name(tmp_path):
  cache_store = store(tmp_path)
  assert promote(cache_store, cache(frame_id=1)).status is CachePromotionStatus.SAVED
  assert promote(cache_store, cache(quality(5, 6, 8), frame_id=2)).status is CachePromotionStatus.SAVED

  assert {path.name for path in tmp_path.iterdir()} == {
    "navigation_cache.json",
    "navigation_cache_previous.json",
  }
  assert cache_store.candidate_path.name == "navigation_cache_candidate.tmp"
  assert not tuple(tmp_path.glob("*manifest*"))
  assert not tuple(path for path in tmp_path.iterdir() if path.is_dir())


def test_legacy_primary_without_new_optional_fields_remains_loadable(tmp_path):
  path = tmp_path / "navigation_cache.json"
  save_cache(path, cache(cycle=None))
  raw = json.loads(path.read_text())
  raw.pop("receiver_cycle", None)
  raw["database"].pop("complete", None)
  path.write_text(json.dumps(raw))

  loaded = load_cache(path, expected_receiver_fingerprint=RECEIVER)
  assert loaded.receiver_cycle is None
  assert loaded.database_frames


def write_raw_cache(path, value):
  path.write_text(json.dumps(value))


@pytest.mark.parametrize("root", [[], "cache", 1, True, None])
def test_non_object_cache_roots_fail_safely(tmp_path, root):
  path = tmp_path / "navigation_cache.json"
  write_raw_cache(path, root)
  with pytest.raises(CacheValidationError):
    load_cache(path)
  inspection = store(tmp_path).inspect(RECEIVER, None).primary
  assert inspection.state is CacheFileState.INVALID


@pytest.mark.parametrize("field", ["position", "database"])
@pytest.mark.parametrize("value", [[], "bad", 1, True, None])
def test_malformed_nested_cache_containers_fail_safely(tmp_path, field, value):
  path = tmp_path / "navigation_cache.json"
  save_cache(path, cache())
  raw = json.loads(path.read_text())
  raw[field] = value
  write_raw_cache(path, raw)
  with pytest.raises(CacheValidationError):
    load_cache(path)


@pytest.mark.parametrize(("container", "field", "value"), [
  ("root", "receiver_fingerprint", 7),
  ("root", "receiver_cycle", True),
  ("root", "receiver_cycle", -1),
  ("root", "receiver_cycle", None),
  ("position", "latitude_e7", True),
  ("position", "accuracy_cm", "100"),
  ("database", "message_count", True),
  ("database", "message_count", -1),
  ("database", "byte_count", "16"),
  ("database", "sha256", 1),
  ("database", "sha256", "A" * 64),
  ("database", "sha256", "0" * 63),
  ("database", "ubx_base64", 1),
  ("database", "ubx_base64", "not-valid-base64!"),
  ("database", "complete", False),
  ("database", "complete", None),
  ("database", "complete", 1),
])
def test_exact_cache_schema_types_are_required(tmp_path, container, field, value):
  path = tmp_path / "navigation_cache.json"
  save_cache(path, cache())
  raw = json.loads(path.read_text())
  target = raw if container == "root" else raw[container]
  target[field] = value
  write_raw_cache(path, raw)
  with pytest.raises(CacheValidationError):
    load_cache(path)


@pytest.mark.parametrize("quality_value", [None, [], "bad", 1, True])
def test_present_non_object_quality_is_rejected(tmp_path, quality_value):
  path = tmp_path / "navigation_cache.json"
  save_cache(path, cache())
  raw = json.loads(path.read_text())
  raw["quality"] = quality_value
  write_raw_cache(path, raw)
  with pytest.raises(CacheValidationError):
    load_cache(path)


def test_excessive_json_nesting_is_controlled(tmp_path):
  path = tmp_path / "navigation_cache.json"
  nested = "0"
  for _ in range(2_000):
    nested = f"[{nested}]"
  path.write_text(f'{{"nested":{nested}}}')
  with pytest.raises(CacheValidationError):
    load_cache(path)


def test_stale_regular_candidate_is_removed_and_symlink_is_rejected(tmp_path):
  cache_store = store(tmp_path)
  cache_store.candidate_path.write_text("stale")
  assert cache_store.remove_stale_candidate() is None
  assert not cache_store.candidate_path.exists()

  target = tmp_path / "outside"
  target.write_text("untouched")
  cache_store.candidate_path.symlink_to(target)
  failure = cache_store.remove_stale_candidate()
  assert failure is not None and "symbolic link" in failure
  assert target.read_text() == "untouched"


@pytest.mark.parametrize("generation", ["primary", "previous"])
def test_authoritative_symlink_is_rejected_without_following(tmp_path, generation):
  cache_store = store(tmp_path)
  target = tmp_path / "outside"
  save_cache(target, cache())
  path = cache_store.primary_path if generation == "primary" else cache_store.previous_path
  path.symlink_to(target)

  inventory = cache_store.inspect(RECEIVER, NOW)
  inspected = inventory.primary if generation == "primary" else inventory.previous
  assert inspected.state is CacheFileState.INVALID
  assert target.is_file()


@pytest.mark.parametrize("generation", ["primary", "previous", "candidate"])
def test_non_regular_fixed_file_is_rejected(tmp_path, generation):
  cache_store = store(tmp_path)
  path = {
    "primary": cache_store.primary_path,
    "previous": cache_store.previous_path,
    "candidate": cache_store.candidate_path,
  }[generation]
  path.mkdir()
  if generation == "candidate":
    assert cache_store.remove_stale_candidate() is not None
  else:
    inventory = cache_store.inspect(RECEIVER, NOW)
    inspected = inventory.primary if generation == "primary" else inventory.previous
    assert inspected.state is CacheFileState.INVALID


def test_inspection_remains_fail_safe_when_file_status_is_unavailable(monkeypatch, tmp_path):
  cache_store = NavigationCacheStore(
    tmp_path / "navigation_cache.json",
    loader=lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("load failed")),
  )
  real_lstat = Path.lstat

  def fail_primary_status(path):
    if path == cache_store.primary_path:
      raise PermissionError("status unavailable")
    return real_lstat(path)

  monkeypatch.setattr(Path, "lstat", fail_primary_status)
  inventory = cache_store.inspect(RECEIVER, NOW)

  assert inventory.primary.state is CacheFileState.INVALID
  assert inventory.primary.error == "load failed"


def test_quality_order_covers_all_reviewed_fields_and_is_pareto_conservative():
  baseline = quality()
  assert navigation_quality_strictly_better(quality(gps=5), baseline)
  assert navigation_quality_strictly_better(quality(used=9), baseline)
  assert navigation_quality_strictly_better(quality(gps_almanac=6), baseline)
  assert navigation_quality_strictly_better(quality(glo_almanac=6), baseline)
  assert navigation_quality_strictly_better(quality(offline=5), baseline)
  assert not navigation_quality_strictly_better(baseline, baseline)
  assert not navigation_quality_strictly_better(quality(gps=5, glo=5), baseline)


@pytest.mark.parametrize("candidate_quality", [
  quality(),
  quality(gps=4, glo=6, used=8, gps_almanac=4),
  quality(gps=5, glo=5),
])
def test_equal_weaker_or_incomparable_candidate_preserves_files(tmp_path, candidate_quality):
  cache_store = store(tmp_path)
  assert promote(cache_store, cache(quality(), frame_id=1)).status is CachePromotionStatus.SAVED
  before = cache_store.primary_path.read_bytes()

  result = promote(cache_store, cache(candidate_quality, frame_id=2))

  assert result.status is CachePromotionStatus.PRESERVED_EXISTING
  assert cache_store.primary_path.read_bytes() == before
  assert not cache_store.candidate_path.exists()


def test_better_candidate_rotates_primary_to_previous(tmp_path):
  cache_store = store(tmp_path)
  old = cache(quality(), frame_id=1)
  new = cache(quality(gps=5), frame_id=2)
  promote(cache_store, old)

  result = promote(cache_store, new)

  assert result.status is CachePromotionStatus.SAVED
  assert load_cache(cache_store.primary_path) == new
  assert load_cache(cache_store.previous_path) == old


def test_expired_stronger_primary_does_not_block_current_candidate(tmp_path):
  cache_store = store(tmp_path)
  expired = cache(
    quality(gps=6, glo=7, used=10),
    saved_at=NOW - timedelta(seconds=DEFAULT_MAX_CACHE_AGE_SECONDS + 1),
  )
  save_cache(cache_store.primary_path, expired)

  result = promote(cache_store, cache(quality(), frame_id=2))

  assert result.status is CachePromotionStatus.SAVED
  assert load_cache(cache_store.primary_path).database_frames == cache(quality(), frame_id=2).database_frames


def test_corrupt_primary_does_not_block_or_overwrite_best_previous(tmp_path):
  cache_store = store(tmp_path)
  previous = cache(quality(gps=5), frame_id=1)
  cache_store.primary_path.write_text("corrupt")
  save_cache(cache_store.previous_path, previous)

  selection, _ = cache_store.select_best(RECEIVER, NOW)
  assert selection is not None and selection.generation == "previous"
  result = promote(cache_store, cache(quality(gps=6), frame_id=2))
  assert result.status is CachePromotionStatus.SAVED
  assert load_cache(cache_store.previous_path) == previous


def test_startup_prefers_stronger_previous_but_primary_for_equal_or_incomparable(tmp_path):
  cache_store = store(tmp_path)
  save_cache(cache_store.primary_path, cache(quality(), frame_id=1))
  save_cache(cache_store.previous_path, cache(quality(gps=5), frame_id=2))
  selection, _ = cache_store.select_best(RECEIVER, NOW)
  assert selection is not None and selection.generation == "previous"

  save_cache(cache_store.previous_path, cache(quality(gps=5, glo=5), frame_id=3))
  selection, _ = cache_store.select_best(RECEIVER, NOW)
  assert selection is not None and selection.generation == "primary"


def test_usable_primary_never_overwrites_qualified_previous_on_failure(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  ineligible_primary = cache(quality(gps=3, glo=7), frame_id=1)
  eligible_previous = cache(quality(gps=4, glo=6), frame_id=2)
  save_cache(cache_store.primary_path, ineligible_primary)
  save_cache(cache_store.previous_path, eligible_previous)

  selection, _ = cache_store.select_best(RECEIVER, NOW)
  assert selection is not None
  assert selection.generation == "previous"
  assert selection.reason == "previous_higher_quality_tier"

  before = cache_store.previous_path.read_bytes()
  real_replace = os.replace

  def fail_candidate_replace(source, destination):
    if source == cache_store.candidate_path:
      raise OSError("candidate replacement failed")
    return real_replace(source, destination)

  monkeypatch.setattr(gps_assistance.os, "replace", fail_candidate_replace)
  result = promote(cache_store, cache(quality(gps=5, glo=6), frame_id=3))

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.CANDIDATE_TO_PRIMARY_REPLACE
  assert result.fallback_generation == "previous"
  assert cache_store.previous_path.read_bytes() == before
  assert load_cache(cache_store.previous_path) == eligible_previous


def test_promotion_accepts_usable_below_policy_same_cycle_candidate(tmp_path):
  cache_store = store(tmp_path)
  candidate = cache(usable_quality(), frame_id=1)

  result = promote(cache_store, candidate)

  assert result.status is CachePromotionStatus.SAVED
  assert load_cache(cache_store.primary_path) == candidate
  assert not candidate.quality.passes_policy
  assert candidate.quality.usable_for_capture


def test_fresh_usable_and_older_qualified_remain_recoverable(tmp_path):
  cache_store = store(tmp_path)
  qualified = cache(
    quality(gps=6, glo=7, used=10),
    frame_id=1,
    saved_at=NOW - timedelta(minutes=1),
  )
  usable = cache(usable_quality(), frame_id=2, saved_at=NOW)
  save_cache(cache_store.primary_path, qualified)

  result = promote(cache_store, usable)

  assert result.status is CachePromotionStatus.SAVED
  assert load_cache(cache_store.primary_path) == usable
  assert load_cache(cache_store.previous_path) == qualified
  assert result.selected is not None and result.selected.generation == "previous"


def test_saved_usable_uses_selected_qualified_generation_as_runtime_baseline(
  monkeypatch, tmp_path,
):
  cache_store = store(tmp_path)
  selected_quality = quality(gps=6, glo=7, used=10)
  usable = usable_quality()
  save_cache(cache_store.primary_path, cache(
    selected_quality,
    frame_id=1,
    saved_at=NOW - timedelta(minutes=1),
  ))
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", cache_store.primary_path)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(pigeond, "read_rtc_counter_seconds", lambda: 1_000)
  candidate_fix = NavPvtFix(True, 10, 1, 2, 3, 100, 100, NOW)

  class Tracker:
    def __init__(self, current):
      self.current = current
      self.latest_fix = candidate_fix

    def quality(self, _now, context):
      return replace(self.current, capture_context=context)

  state = pigeond.NavigationCaptureState()
  tracker = Tracker(usable)
  assert state.request(20, True, False, tracker, CYCLE, candidate_fix)
  result = pigeond.write_navigation_assistance_cache(
    RECEIVER,
    state.capture_fix,
    (build_dbd_frame(2),),
    usable,
    source="onroad_first",
    receiver_cycle=CYCLE,
    receiver_utc_now=NOW,
    active_receiver_cycle=CYCLE,
    receiver_utc_fresh=True,
    trusted_promotion_utc=NOW,
  )
  assert result is pigeond.NavigationAssistanceCacheResult.SAVED
  assert load_cache(cache_store.primary_path).quality == usable
  assert load_cache(cache_store.previous_path).quality == selected_quality
  selection, _ = cache_store.select_best(RECEIVER, NOW)
  assert selection is not None and selection.generation == "previous"

  durable_quality = pigeond.durable_quality_after_cache_result(
    result, RECEIVER, NOW,
  )
  assert durable_quality == selected_quality
  assert pigeond.durable_quality_after_cache_result(
    pigeond.NavigationAssistanceCacheResult.PRESERVED_EXISTING,
    RECEIVER,
    NOW,
  ) == selected_quality
  readiness_message = state.complete(result, 20, durable_quality, usable)
  assert state.durable_baseline_quality == selected_quality
  assert "candidate_quality_tier=usable" in readiness_message
  assert "selected_quality_tier=qualified" in readiness_message
  assert "action=candidate_saved_existing_selected" in readiness_message
  assert "context=onroad" in readiness_message
  assert "reason=qualified_cache_saved_onroad" not in readiness_message

  tracker.current = quality(gps=5, glo=6, used=9)
  assert navigation_quality_strictly_better(tracker.current, usable)
  assert not navigation_quality_strictly_better(
    tracker.current, selected_quality,
  )
  assert not state.request(21, True, False, tracker, CYCLE, candidate_fix)


def test_startup_selection_is_tier_and_freshness_aware(tmp_path):
  cache_store = store(tmp_path)
  usable = cache(
    usable_quality(),
    frame_id=1,
    saved_at=NOW,
  )
  qualified = cache(
    quality(),
    frame_id=2,
    saved_at=NOW - timedelta(hours=1),
  )

  save_cache(
    cache_store.primary_path,
    usable,
  )
  save_cache(
    cache_store.previous_path,
    qualified,
  )

  selection, _ = cache_store.select_best(
    RECEIVER,
    NOW,
  )

  assert selection is not None
  assert selection.generation == "previous"
  assert selection.reason == "previous_higher_quality_tier"

  qualified = replace(
    qualified,
    saved_at_utc=NOW - timedelta(hours=5),
  )
  save_cache(
    cache_store.previous_path,
    qualified,
  )

  for now_utc, age_evidence in (
    (
      NOW,
      CacheAgeEvidence.TRUSTED_UTC,
    ),
    (
      None,
      CacheAgeEvidence.UNVERIFIED,
    ),
  ):
    selection, _ = cache_store.select_best(
      RECEIVER,
      now_utc,
      age_evidence=age_evidence,
    )

    assert selection is not None
    assert selection.generation == "primary"
    assert selection.reason == "primary_materially_fresher"




def test_relative_freshness_applies_when_current_age_is_unverified(tmp_path):
  cache_store = store(tmp_path)

  older_qualified = cache(
    quality(),
    frame_id=1,
    saved_at=NOW - timedelta(hours=5),
  )
  newer_usable = cache(
    usable_quality(),
    frame_id=2,
    saved_at=NOW,
  )

  save_cache(
    cache_store.primary_path,
    older_qualified,
  )
  save_cache(
    cache_store.previous_path,
    newer_usable,
  )

  selection, _ = cache_store.select_best(
    RECEIVER,
    None,
    age_evidence=CacheAgeEvidence.UNVERIFIED,
  )

  assert selection is not None
  assert selection.generation == "previous"
  assert selection.reason == "previous_materially_fresher"




def test_cache_age_evidence_boundaries(tmp_path):
  cache_store = store(tmp_path)

  exactly_seven_days = cache(
    quality(),
    frame_id=1,
    saved_at=NOW - timedelta(days=7),
  )
  save_cache(
    cache_store.primary_path,
    exactly_seven_days,
  )

  for evidence in (
    CacheAgeEvidence.TRUSTED_UTC,
    CacheAgeEvidence.RTC_ESTIMATE,
  ):
    selection, _ = cache_store.select_best(
      RECEIVER,
      NOW,
      age_evidence=evidence,
    )
    assert selection is not None

  save_cache(
    cache_store.primary_path,
    replace(
      exactly_seven_days,
      saved_at_utc=(
        NOW - timedelta(days=7, seconds=1)
      ),
    ),
  )

  selection, _ = cache_store.select_best(
    RECEIVER,
    NOW,
    age_evidence=CacheAgeEvidence.TRUSTED_UTC,
  )
  assert selection is None

  # Immediate restoration remains intentionally available without age proof.
  selection, _ = cache_store.select_best(
    RECEIVER,
    None,
    age_evidence=CacheAgeEvidence.UNVERIFIED,
  )
  assert selection is not None


def test_verified_age_evidence_requires_current_utc(tmp_path):
  cache_store = store(tmp_path)

  with pytest.raises(
    ValueError,
    match="requires current UTC",
  ):
    cache_store.select_best(
      RECEIVER,
      None,
      age_evidence=CacheAgeEvidence.TRUSTED_UTC,
    )


def test_worse_or_incomparable_qualified_candidate_preserves_better_cache(tmp_path):
  cache_store = store(tmp_path)
  better = cache(quality(gps=6, glo=7, used=10), frame_id=1)
  save_cache(cache_store.primary_path, better)
  for candidate_quality in (quality(gps=5, glo=7, used=10), quality(gps=7, glo=6, used=10)):
    before = cache_store.primary_path.read_bytes()
    result = promote(cache_store, cache(candidate_quality, frame_id=2))
    assert result.status is CachePromotionStatus.PRESERVED_EXISTING
    assert cache_store.primary_path.read_bytes() == before


def test_usable_promotion_failure_preserves_qualified_generation(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  qualified = cache(quality(), frame_id=1, saved_at=NOW - timedelta(minutes=1))
  save_cache(cache_store.primary_path, qualified)
  real_replace = os.replace

  def fail_candidate_replace(source, destination):
    if source == cache_store.candidate_path:
      raise OSError("injected usable promotion failure")
    return real_replace(source, destination)

  monkeypatch.setattr(gps_assistance.os, "replace", fail_candidate_replace)
  result = promote(cache_store, cache(usable_quality(), frame_id=2))

  assert result.status is CachePromotionStatus.FAILED
  assert load_cache(cache_store.previous_path) == qualified


@pytest.mark.parametrize(("primary_state", "previous_state", "expected"), [
  ("absent", "absent", None),
  ("valid", "absent", "primary"),
  ("absent", "valid", "previous"),
  ("valid", "corrupt", "primary"),
  ("corrupt", "valid", "previous"),
  ("corrupt", "corrupt", None),
  ("expired", "valid", "previous"),
  ("valid", "expired", "primary"),
])
def test_startup_primary_previous_permutations(tmp_path, primary_state, previous_state, expected):
  cache_store = store(tmp_path)

  def write_state(path, state, frame_id):
    if state == "valid":
      save_cache(path, cache(frame_id=frame_id))
    elif state == "expired":
      save_cache(path, cache(
        frame_id=frame_id,
        saved_at=NOW - timedelta(seconds=DEFAULT_MAX_CACHE_AGE_SECONDS + 1),
      ))
    elif state == "corrupt":
      path.write_text("corrupt")

  write_state(cache_store.primary_path, primary_state, 1)
  write_state(cache_store.previous_path, previous_state, 2)
  selection, _ = cache_store.select_best(RECEIVER, NOW)
  assert (None if selection is None else selection.generation) == expected


def test_candidate_cycle_mismatch_never_changes_authoritative_files(tmp_path):
  cache_store = store(tmp_path)
  promote(cache_store, cache(frame_id=1))
  before = cache_store.primary_path.read_bytes()

  result = cache_store.promote(cache(quality(gps=5), frame_id=2, cycle=8), RECEIVER, NOW, CYCLE)

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.CANDIDATE_VALIDATION
  assert cache_store.primary_path.read_bytes() == before


@pytest.mark.parametrize("failure_stage", ["file_fsync", "candidate_dir_fsync"])
def test_candidate_durability_failure_preserves_authoritative_files(monkeypatch, tmp_path, failure_stage):
  cache_store = store(tmp_path)
  promote(cache_store, cache(frame_id=1))
  before = cache_store.primary_path.read_bytes()
  real_fsync = os.fsync
  directory_calls = 0

  def fail_fsync(descriptor):
    nonlocal directory_calls
    is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
    if failure_stage == "file_fsync" and not is_directory:
      raise OSError("file fsync")
    if is_directory:
      directory_calls += 1
      if failure_stage == "candidate_dir_fsync" and directory_calls == 1:
        raise OSError("candidate directory fsync")
    return real_fsync(descriptor)

  monkeypatch.setattr(gps_assistance.os, "fsync", fail_fsync)
  result = promote(cache_store, cache(quality(gps=5), frame_id=2))

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is {
    "file_fsync": CachePromotionStage.CANDIDATE_FILE_FSYNC,
    "candidate_dir_fsync": CachePromotionStage.CANDIDATE_DIRECTORY_FSYNC,
  }[failure_stage]
  assert cache_store.primary_path.read_bytes() == before


def test_failure_after_rotation_leaves_previous_valid(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  old = cache(frame_id=1)
  promote(cache_store, old)
  real_replace = os.replace
  calls = 0

  def fail_second_replace(source, destination):
    nonlocal calls
    calls += 1
    if calls == 2:
      raise OSError("candidate to primary")
    return real_replace(source, destination)

  monkeypatch.setattr(gps_assistance.os, "replace", fail_second_replace)
  result = promote(cache_store, cache(quality(gps=5), frame_id=2))

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.CANDIDATE_TO_PRIMARY_REPLACE
  assert not cache_store.primary_path.exists()
  assert load_cache(cache_store.previous_path) == old


def test_post_replace_directory_fsync_failure_is_retryable_preserve(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  candidate = cache(frame_id=1)
  real_fsync = os.fsync
  directory_calls = 0

  def fail_second_directory_fsync(descriptor):
    nonlocal directory_calls
    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
      directory_calls += 1
      if directory_calls == 2:
        raise OSError("post-replace directory fsync")
    return real_fsync(descriptor)

  monkeypatch.setattr(gps_assistance.os, "fsync", fail_second_directory_fsync)
  first = promote(cache_store, candidate)
  monkeypatch.setattr(gps_assistance.os, "fsync", real_fsync)
  retry = promote(cache_store, candidate)

  assert first.status is CachePromotionStatus.FAILED
  assert first.stage is CachePromotionStage.FINAL_DIRECTORY_FSYNC
  assert load_cache(cache_store.primary_path) == candidate
  assert retry.status is CachePromotionStatus.PRESERVED_EXISTING


def test_repeated_equal_capture_keeps_storage_bounded(tmp_path):
  cache_store = store(tmp_path)
  candidate = cache()
  assert promote(cache_store, candidate).status is CachePromotionStatus.SAVED
  for _ in range(5):
    assert promote(cache_store, candidate).status is CachePromotionStatus.PRESERVED_EXISTING
  assert {path.name for path in tmp_path.iterdir()} == {"navigation_cache.json"}


def test_missing_nofollow_fails_candidate_safely(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  monkeypatch.delattr(gps_assistance.os, "O_NOFOLLOW")

  result = promote(cache_store, cache())

  assert result.status is CachePromotionStatus.FAILED
  assert not cache_store.primary_path.exists()


def test_candidate_readback_mismatch_is_controlled(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  candidate = cache()
  real_write = cache_store._write_candidate

  def mismatched(value, stage_callback=None):
    real_write(value, stage_callback)
    return replace(value, receiver_cycle=CYCLE + 1)

  monkeypatch.setattr(cache_store, "_write_candidate", mismatched)
  result = promote(cache_store, candidate)

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.CANDIDATE_VALIDATION
  assert not cache_store.primary_path.exists()


def test_candidate_requires_completion_and_receiver_cycle_metadata(tmp_path):
  path = tmp_path / "navigation_cache_candidate.tmp"
  save_cache(path, cache())
  raw = json.loads(path.read_text())
  raw["database"].pop("complete")
  path.write_text(json.dumps(raw))
  with pytest.raises(CacheValidationError, match="completion metadata"):
    load_cache(path, require_complete=True, expected_receiver_cycle=CYCLE)

  save_cache(path, cache())
  with pytest.raises(CacheValidationError, match="different receiver cycle"):
    load_cache(path, require_complete=True, expected_receiver_cycle=CYCLE + 1)


def test_candidate_cleanup_failure_is_reported_separately(monkeypatch, tmp_path):
  cache_store = store(tmp_path)

  def failed_write(candidate, stage_callback=None):
    if stage_callback is not None:
      stage_callback(CachePromotionStage.CANDIDATE_WRITE)
    cache_store.candidate_path.write_text("partial")
    raise OSError("write failed")

  monkeypatch.setattr(cache_store, "_write_candidate", failed_write)
  monkeypatch.setattr(
    cache_store,
    "_remove_candidate",
    lambda **kwargs: (_ for _ in ()).throw(OSError("cleanup failed")),
  )
  result = promote(cache_store, cache())

  assert result.status is CachePromotionStatus.FAILED
  assert result.cleanup_failure is not None and "cleanup failed" in result.cleanup_failure


def test_primary_to_previous_replace_failure_is_staged_and_non_destructive(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  old = cache(frame_id=1)
  promote(cache_store, old)
  before = cache_store.primary_path.read_bytes()
  real_replace = os.replace

  def fail_rotation(source, destination):
    if source == cache_store.primary_path and destination == cache_store.previous_path:
      raise OSError("rotation failed")
    return real_replace(source, destination)

  monkeypatch.setattr(gps_assistance.os, "replace", fail_rotation)
  result = promote(cache_store, cache(quality(gps=5), frame_id=2))

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.PRIMARY_TO_PREVIOUS_REPLACE
  assert cache_store.primary_path.read_bytes() == before


def test_fallback_directory_fsync_failure_leaves_previous_usable(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  old = cache(frame_id=1)
  promote(cache_store, old)
  real_fsync = os.fsync
  directory_calls = 0

  def fail_fallback_fsync(descriptor):
    nonlocal directory_calls
    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
      directory_calls += 1
      if directory_calls == 2:
        raise OSError("fallback fsync failed")
    return real_fsync(descriptor)

  monkeypatch.setattr(gps_assistance.os, "fsync", fail_fallback_fsync)
  result = promote(cache_store, cache(quality(gps=5), frame_id=2))

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.FALLBACK_DIRECTORY_FSYNC
  assert load_cache(cache_store.previous_path) == old


@pytest.mark.parametrize("failure", ["readback", "mismatch"])
def test_primary_readback_failure_or_mismatch_is_controlled(monkeypatch, tmp_path, failure):
  cache_store = store(tmp_path)
  old = cache(frame_id=1)
  candidate = cache(quality(gps=5), frame_id=2)
  promote(cache_store, old)

  def load_with_failure(path, **kwargs):
    loaded = load_cache(path, **kwargs)
    if path == cache_store.primary_path and loaded.database_frames == candidate.database_frames:
      if failure == "readback":
        raise OSError("primary readback failed")
      return replace(loaded, latitude_e7=loaded.latitude_e7 + 1)
    return loaded

  cache_store._load_cache = load_with_failure
  result = promote(cache_store, candidate)

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is (
    CachePromotionStage.PRIMARY_READBACK
    if failure == "readback"
    else CachePromotionStage.PRIMARY_CANDIDATE_COMPARISON
  )
  assert load_cache(cache_store.previous_path) == old


@pytest.mark.parametrize("failure", ["unlink", "directory_fsync"])
def test_preserve_cleanup_failures_are_staged_and_retryable(monkeypatch, tmp_path, failure):
  cache_store = store(tmp_path)
  existing = cache(frame_id=1)
  promote(cache_store, existing)
  before = cache_store.primary_path.read_bytes()
  if failure == "unlink":
    real_unlink = Path.unlink

    def fail_unlink(path, *args, **kwargs):
      if path == cache_store.candidate_path:
        raise OSError("candidate unlink failed")
      return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_unlink)
  else:
    real_fsync = os.fsync
    directory_calls = 0

    def fail_preserve_fsync(descriptor):
      nonlocal directory_calls
      if stat.S_ISDIR(os.fstat(descriptor).st_mode):
        directory_calls += 1
        if directory_calls == 2:
          raise OSError("preserve fsync failed")
      return real_fsync(descriptor)

    monkeypatch.setattr(gps_assistance.os, "fsync", fail_preserve_fsync)

  result = promote(cache_store, existing)

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is (
    CachePromotionStage.PRESERVE_CANDIDATE_DELETE
    if failure == "unlink"
    else CachePromotionStage.PRESERVE_DIRECTORY_FSYNC
  )
  assert cache_store.primary_path.read_bytes() == before


def test_fdopen_failure_closes_candidate_descriptor(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  descriptor = None

  def fail_fdopen(value, *args, **kwargs):
    nonlocal descriptor
    descriptor = value
    raise OSError("fdopen failed")

  monkeypatch.setattr(gps_assistance.os, "fdopen", fail_fdopen)
  result = promote(cache_store, cache())

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.CANDIDATE_WRITE
  assert descriptor is not None
  with pytest.raises(OSError):
    os.fstat(descriptor)


def test_candidate_open_failure_is_staged_and_controlled(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  real_open = os.open

  def fail_candidate_open(path, flags, *args):
    if Path(path) == cache_store.candidate_path and flags & os.O_WRONLY:
      raise OSError("candidate open failed")
    return real_open(path, flags, *args)

  monkeypatch.setattr(gps_assistance.os, "open", fail_candidate_open)
  result = promote(cache_store, cache())

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.CANDIDATE_WRITE
  assert "candidate open failed" in result.reason
  assert not cache_store.primary_path.exists()


@pytest.mark.parametrize("failure", ["write", "flush", "close"])
def test_candidate_stream_failures_are_staged_and_controlled(monkeypatch, tmp_path, failure):
  cache_store = store(tmp_path)
  real_fdopen = os.fdopen

  class FailingStream:
    def __init__(self, descriptor):
      self.stream = real_fdopen(descriptor, "wb")

    def write(self, value):
      if failure == "write":
        raise OSError("candidate write failed")
      return self.stream.write(value)

    def flush(self):
      if failure == "flush":
        raise OSError("candidate flush failed")
      return self.stream.flush()

    def fileno(self):
      return self.stream.fileno()

    def close(self):
      self.stream.close()
      if failure == "close":
        raise OSError("candidate close failed")

  monkeypatch.setattr(
    gps_assistance.os,
    "fdopen",
    lambda descriptor, _mode: FailingStream(descriptor),
  )
  result = promote(cache_store, cache())

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.CANDIDATE_WRITE
  assert f"candidate {failure} failed" in result.reason
  assert not cache_store.primary_path.exists()


def test_original_candidate_failure_retains_secondary_close_detail(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  real_fdopen = os.fdopen

  class DoublyFailingStream:
    def __init__(self, descriptor):
      self.stream = real_fdopen(descriptor, "wb")

    def write(self, _value):
      raise OSError("original write failure")

    def flush(self):
      return self.stream.flush()

    def fileno(self):
      return self.stream.fileno()

    def close(self):
      self.stream.close()
      raise OSError("secondary close failure")

  monkeypatch.setattr(
    gps_assistance.os,
    "fdopen",
    lambda descriptor, _mode: DoublyFailingStream(descriptor),
  )
  result = promote(cache_store, cache())

  assert result.status is CachePromotionStatus.FAILED
  assert "original write failure" in result.reason
  assert "Candidate close also failed" in result.reason


def test_directory_fsync_failure_retains_secondary_close_detail(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  real_fsync = os.fsync
  real_close = os.close

  def fail_directory_fsync(descriptor):
    if stat.S_ISDIR(os.fstat(descriptor).st_mode):
      raise OSError("original directory fsync failure")
    return real_fsync(descriptor)

  def fail_directory_close(descriptor):
    is_directory = stat.S_ISDIR(os.fstat(descriptor).st_mode)
    real_close(descriptor)
    if is_directory:
      raise OSError("secondary directory close failure")

  monkeypatch.setattr(gps_assistance.os, "fsync", fail_directory_fsync)
  monkeypatch.setattr(gps_assistance.os, "close", fail_directory_close)
  result = promote(cache_store, cache())

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.CANDIDATE_DIRECTORY_FSYNC
  assert "original directory fsync failure" in result.reason
  assert "Cache directory descriptor close also failed" in result.reason


def test_candidate_reopen_failure_is_staged(monkeypatch, tmp_path):
  cache_store = store(tmp_path)

  def fail_candidate_load(path, **kwargs):
    if path == cache_store.candidate_path:
      raise OSError("candidate reopen failed")
    return load_cache(path, **kwargs)

  cache_store._load_cache = fail_candidate_load
  result = promote(cache_store, cache())

  assert result.status is CachePromotionStatus.FAILED
  assert result.stage is CachePromotionStage.CANDIDATE_READBACK
  assert not cache_store.primary_path.exists()


def test_rtc_and_database_generation_selection_are_independent(tmp_path):
  cache_store = store(tmp_path)
  primary = replace(cache(frame_id=1), saved_at_utc=NOW, rtc_counter_seconds=1_000)
  previous = replace(
    cache(frame_id=2),
    saved_at_utc=NOW + timedelta(seconds=120),
    rtc_counter_seconds=1_000,
  )
  save_cache(cache_store.primary_path, primary)
  save_cache(cache_store.previous_path, previous)

  database_selection, inventory = cache_store.select_best(RECEIVER, None)
  rtc_selection, _ = gps_assistance.select_rtc_estimate(inventory, 1_000)

  assert database_selection is not None and database_selection.generation == "primary"
  assert rtc_selection is not None and rtc_selection.generation == "previous"
  assert rtc_selection.estimate.estimated_utc == NOW + timedelta(seconds=120)


def test_exact_rtc_estimate_controls_cache_age_selection(tmp_path):
  cache_store = store(tmp_path)
  rtc_now = NOW + timedelta(days=8)
  save_cache(cache_store.primary_path, cache(frame_id=1, saved_at=NOW))
  save_cache(cache_store.previous_path, cache(frame_id=2, saved_at=rtc_now - timedelta(days=1)))

  selection, _ = cache_store.select_best(RECEIVER, rtc_now)

  assert selection is not None and selection.generation == "previous"


def test_process_collector_writer_and_primary_reload_end_to_end(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", cache_store.primary_path)
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  monkeypatch.setattr(pigeond, "read_rtc_counter_seconds", lambda: 1_000)
  database_frames = (build_dbd_frame(1), build_dbd_frame(2))
  terminal_ack = build_mga_ack_frame(
    True, 0, gps_assistance.UBX_ID_MGA_DBD,
    len(database_frames).to_bytes(4, "little"),
  )
  nav_pvt = build_nav_pvt_frame()
  raw_frames = [database_frames[0], nav_pvt, database_frames[1], terminal_ack]
  raw_observed = []
  fixes = []

  class StartupDiagnostics:
    def note_rawx(self, frame, _now):
      raw_observed.append(frame)

    def note_nav_pvt(self, fix, _now):
      fixes.append(fix)

  class FixTracker:
    def update(self, fix, _now):
      fixes.append(fix)

  class QualityTracker:
    def update_fix(self, _fix, _now):
      return None

    def orbit_eligible(self, _now):
      return False

    def update_nav_sat(self, _report, _now):
      return None

  class AutonomousDiagnostics:
    def note_nav_sat(self, _report):
      raise AssertionError("No NAV-SAT frame was supplied")

  collector = gps_assistance.NavigationDatabaseDumpCollector()
  collector.start(0)
  completed = pigeond.process_receiver_frames(
    raw_frames,
    1.0,
    StartupDiagnostics(),
    FixTracker(),
    QualityTracker(),
    AutonomousDiagnostics(),
    collector,
    pigeond.NavigationCaptureState(),
  )

  assert raw_observed == raw_frames
  assert len(fixes) == 2  # one startup diagnostic and one fix-tracker update
  assert completed == database_frames
  candidate_quality = usable_quality()
  parsed_fix = gps_assistance.parse_nav_pvt(nav_pvt)
  assert parsed_fix is not None and parsed_fix.utc_time is not None
  result = pigeond.write_navigation_assistance_cache(
    RECEIVER,
    parsed_fix,
    completed,
    candidate_quality,
    source="onroad_first",
    receiver_cycle=CYCLE,
    receiver_utc_now=parsed_fix.utc_time,
    active_receiver_cycle=CYCLE,
    receiver_utc_fresh=True,
    receiver_utc_independent=True,
  )

  assert result is pigeond.NavigationAssistanceCacheResult.SAVED
  reloaded = load_cache(
    cache_store.primary_path,
    expected_receiver_fingerprint=RECEIVER,
    require_complete=True,
    expected_receiver_cycle=CYCLE,
  )
  assert reloaded.database_frames == completed
  assert reloaded.quality == candidate_quality
  assert reloaded.quality.usable_for_capture
  assert not reloaded.quality.passes_policy
  assert reloaded.saved_at_utc == parsed_fix.utc_time


class _NoneOffset(tzinfo):
  def utcoffset(self, dt):
    return None


class _RaisingOffset(tzinfo):
  def utcoffset(self, dt):
    raise RuntimeError("broken timezone")


@pytest.mark.parametrize("zone", [_NoneOffset(), _RaisingOffset()])
def test_offline_promotion_rejects_effectively_naive_or_broken_timezone(monkeypatch, zone):
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  receiver_utc = datetime(2026, 7, 16, tzinfo=zone)

  assert pigeond.cache_promotion_trusted_now(
    receiver_utc, CYCLE, CYCLE, receiver_utc_fresh=True,
  ) is None


def test_offline_promotion_requires_fresh_same_cycle_receiver_utc(monkeypatch):
  monkeypatch.setattr(pigeond, "read_host_time_observation", lambda: None)
  assert pigeond.cache_promotion_trusted_now(
    NOW, CYCLE, CYCLE, receiver_utc_fresh=True,
    receiver_utc_independent=True,
  ) == NOW
  assert pigeond.cache_promotion_trusted_now(
    NOW, CYCLE, CYCLE + 1, receiver_utc_fresh=True,
    receiver_utc_independent=True,
  ) is None
  assert pigeond.cache_promotion_trusted_now(
    NOW, CYCLE, CYCLE, receiver_utc_fresh=False,
    receiver_utc_independent=True,
  ) is None


def test_rtc_selection_uses_valid_previous_when_primary_anchor_is_invalid(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  primary = replace(cache(frame_id=1), rtc_counter_seconds=2000)
  previous = replace(cache(frame_id=2), rtc_counter_seconds=1000)
  save_cache(cache_store.primary_path, primary)
  save_cache(cache_store.previous_path, previous)
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", cache_store.primary_path)
  monkeypatch.setattr(pigeond, "read_rtc_counter_seconds", lambda: 1100)

  result = pigeond.cached_rtc_time_assistance(RECEIVER)

  assert result is not None
  assert result[0] == previous.saved_at_utc + timedelta(seconds=100)


def test_rtc_selection_chooses_freshest_estimate_with_primary_tiebreak(monkeypatch, tmp_path):
  cache_store = store(tmp_path)
  primary = replace(cache(frame_id=1), saved_at_utc=NOW, rtc_counter_seconds=1000)
  previous = replace(
    cache(frame_id=2),
    saved_at_utc=NOW + timedelta(seconds=50),
    rtc_counter_seconds=1050,
  )
  save_cache(cache_store.primary_path, primary)
  save_cache(cache_store.previous_path, previous)
  monkeypatch.setattr(pigeond, "GPS_ASSISTANCE_CACHE_PATH", cache_store.primary_path)
  monkeypatch.setattr(pigeond, "read_rtc_counter_seconds", lambda: 1100)
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)

  result = pigeond.cached_rtc_time_assistance(RECEIVER)

  assert result is not None and result[0] == NOW + timedelta(seconds=100)
  assert any("generation=primary" in message for message in logs)


@pytest.mark.parametrize(("saved_at", "elapsed", "reason"), [
  (
    datetime(9999, 12, 31, 23, 59, 59, tzinfo=UTC),
    1,
    RtcEstimateRejectionReason.INVALID_RTC_ESTIMATE,
  ),
  (
    datetime(2025, 2, 21, tzinfo=UTC),
    0,
    RtcEstimateRejectionReason.UTC_BEFORE_SUPPORTED_MINIMUM,
  ),
  (
    # Beyond representable GPS week UInt16 ceiling (PR82 replaces 2035 product sunset).
    datetime(3236, 1, 6, tzinfo=UTC),
    0,
    RtcEstimateRejectionReason.UTC_AFTER_SUPPORTED_MAXIMUM,
  ),
])
def test_rtc_estimation_rejects_overflow_and_absolute_date_bounds(saved_at, elapsed, reason):
  candidate = replace(cache(saved_at=saved_at), rtc_counter_seconds=100)
  result = evaluate_utc_from_rtc(candidate, current_rtc_seconds=100 + elapsed)
  assert isinstance(result, RtcEstimateRejection)
  assert result.reason is reason
