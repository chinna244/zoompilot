import json
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from openpilot.system.ubloxd.yuma_almanac_outcome import (
  YumaOutcomeStoreError,
  save_yuma_supplementation_outcome,
  serialize_yuma_supplementation_outcome,
)
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaDatabaseRestoreState,
  YumaSupplementationAction,
  YumaSupplementationPlan,
  YumaSupplementationReason,
)
from openpilot.system.ubloxd.yuma_almanac_runtime import (
  YumaSupplementationRuntimeOutcome,
  YumaTransmissionAttemptOutcome,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  YumaAlmanacTransmitResult,
  YumaAlmanacTransmitStatus,
)


NOW = datetime(2026, 7, 22, 12, tzinfo=UTC)
REFERENCE_TIME = NOW - timedelta(hours=2)


def transmit_result(
  status: YumaAlmanacTransmitStatus,
  *,
  requested=(),
  attempted=(),
  accepted=(),
  failed=(),
  rejected=(),
  timed_out=(),
  deferred=(),
  unavailable=(),
) -> YumaAlmanacTransmitResult:
  return YumaAlmanacTransmitResult(
    status=status,
    requested_satellite_ids=requested,
    attempted_satellite_ids=attempted,
    accepted_satellite_ids=accepted,
    failed_satellite_ids=failed,
    rejected_satellite_ids=rejected,
    timed_out_satellite_ids=timed_out,
    deferred_satellite_ids=deferred,
    unavailable_satellite_ids=unavailable,
    reference_time_utc=REFERENCE_TIME,
    downloaded_at_utc=NOW - timedelta(minutes=5),
  )


def outcome() -> YumaSupplementationRuntimeOutcome:
  first = transmit_result(
    YumaAlmanacTransmitStatus.PARTIAL,
    requested=(1, 2, 3),
    attempted=(1, 2, 3),
    accepted=(1,),
    failed=(2,),
    rejected=(2,),
    timed_out=(3,),
    deferred=(3,),
  )
  second = transmit_result(
    YumaAlmanacTransmitStatus.COMPLETE,
    requested=(2, 3),
    attempted=(2, 3),
    accepted=(2, 3),
  )
  return YumaSupplementationRuntimeOutcome(
    plan=YumaSupplementationPlan(
      YumaSupplementationAction.SEND_ALL,
      YumaSupplementationReason.RESTORED_GPS_ALMANAC_INCOMPLETE,
      satellite_ids=frozenset((1, 2, 3)),
    ),
    transmit_result=second,
    database_state=YumaDatabaseRestoreState.COMPLETE,
    database_age_seconds=60.0,
    restored_cache_age_evidence="trusted_utc",
    restored_cache_age_verified=True,
    captured_gps_ephemeris_available=5,
    captured_glonass_ephemeris_available=6,
    captured_gps_startup_ready=True,
    restored_gps_ephemeris_fresh=False,
    restored_glonass_ephemeris_fresh=False,
    restored_quality_expiration_reasons=(
      "gps_ephemeris_expired",
      "glonass_ephemeris_expired",
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
    yuma_reference_utc=REFERENCE_TIME,
    yuma_snapshot_sha256="a" * 64,
    yuma_reference_age_seconds=7200.0,
    downloaded_at_utc=NOW - timedelta(minutes=5),
    transmission_attempt=2,
    transmission_elapsed_ms=125.0,
    attempt_history=(
      YumaTransmissionAttemptOutcome(
        attempt=1,
        elapsed_ms=750.0,
        transmit_result=first,
      ),
      YumaTransmissionAttemptOutcome(
        attempt=2,
        elapsed_ms=125.0,
        transmit_result=second,
      ),
    ),
    terminal=True,
    retry_pending=False,
    time_anchor_source="receiver_utc",
    time_anchor_utc=NOW,
    trusted_now_utc=NOW + timedelta(seconds=2),
    trusted_time_wait_expired=True,
    cache_wait_expired=False,
    nav_sat_observation_expired=True,
    runtime_elapsed_seconds=260.5,
    time_anchor_elapsed_seconds=257.0,
    decision_ready_elapsed_seconds=257.1,
    nav_sat_observed_elapsed_seconds=258.0,
    nav_sat_wait_seconds=0.9,
    completion_elapsed_seconds=260.5,
    completion_utc=NOW + timedelta(seconds=2),
  )


def test_serialize_preserves_complete_attempt_history():
  payload = json.loads(
    serialize_yuma_supplementation_outcome(
      outcome(),
      commit="8e5d28c4f48d049715b49d789b2e8e95ef286194",
      receiver_cycle=7,
      recorded_at_utc=NOW + timedelta(seconds=2),
    )
  )

  assert payload["version"] == 3
  assert payload["commit"] == "8e5d28c4f48d049715b49d789b2e8e95ef286194"
  assert payload["receiver_cycle"] == 7
  assert payload["feature_enabled"] is True
  assert payload["terminal"] is True
  assert payload["time"]["anchor_source"] == "receiver_utc"
  assert payload["time"]["trusted_time_wait_expired"] is True
  assert payload["database"]["age_evidence"] == "trusted_utc"
  assert payload["database"]["age_verified"] is True
  assert payload["database"]["captured_gps_ephemeris_available"] == 5
  assert payload["database"]["captured_glonass_ephemeris_available"] == 6
  assert payload["database"]["captured_gps_startup_ready"] is True
  assert payload["database"]["gps_ephemeris_fresh"] is False
  assert payload["database"]["glonass_ephemeris_fresh"] is False
  assert payload["database"]["quality_expiration_reasons"] == [
    "gps_ephemeris_expired",
    "glonass_ephemeris_expired",
  ]
  assert payload["database"]["restored_gps_almanac_available"] == 10
  assert payload["database"]["restored_gps_almanac_satellite_ids"] == list(range(1, 11))
  assert payload["time"]["anchor_elapsed_seconds"] == 257.0
  assert payload["time"]["decision_ready_elapsed_seconds"] == 257.1
  assert payload["time"]["nav_sat_wait_seconds"] == 0.9
  assert payload["completion_utc"] == (NOW + timedelta(seconds=2)).isoformat()
  assert payload["yuma"]["snapshot_sha256"] == "a" * 64
  assert payload["plan"]["action"] == "send_all"
  assert payload["plan"]["satellite_ids"] == [1, 2, 3]
  assert len(payload["attempts"]) == 2
  assert payload["attempts"][0]["transmit_result"]["status"] == "partial"
  assert payload["attempts"][0]["transmit_result"]["attempted_satellite_ids"] == [1, 2, 3]
  assert payload["attempts"][0]["transmit_result"]["rejected_satellite_ids"] == [2]
  assert payload["attempts"][0]["transmit_result"]["timed_out_satellite_ids"] == [3]
  assert payload["attempts"][1]["transmit_result"]["accepted_satellite_ids"] == [2, 3]
  assert payload["transmission_summary"]["attempted_satellite_ids"] == [1, 2, 3, 2, 3]
  assert payload["transmission_summary"]["per_prn_attempt_counts"] == {
    "1": 1,
    "2": 2,
    "3": 2,
  }
  assert payload["latest"]["transmission_attempt"] == 2


def test_save_is_atomic_and_private(tmp_path: Path):
  path = tmp_path / "public_yuma_last_outcome.json"

  save_yuma_supplementation_outcome(
    path,
    outcome(),
    commit="test-commit",
    receiver_cycle=3,
    recorded_at_utc=NOW,
  )

  payload = json.loads(path.read_text(encoding="utf-8"))
  assert payload["commit"] == "test-commit"
  assert payload["receiver_cycle"] == 3
  assert stat.S_IMODE(path.stat().st_mode) == 0o600
  assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_failed_replace_preserves_previous_outcome(
  tmp_path: Path,
  monkeypatch,
):
  path = tmp_path / "public_yuma_last_outcome.json"
  original = b'{"version":1,"preserved":true}\n'
  path.write_bytes(original)

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_outcome.os.replace",
    lambda source, destination: (_ for _ in ()).throw(
      OSError("injected replace failure")
    ),
  )

  with pytest.raises(OSError, match="injected replace failure"):
    save_yuma_supplementation_outcome(
      path,
      outcome(),
      commit="test-commit",
      receiver_cycle=3,
      recorded_at_utc=NOW,
    )

  assert path.read_bytes() == original
  assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


@pytest.mark.parametrize(
  ("receiver_cycle", "recorded_at_utc", "message"),
  (
    (-1, NOW, "receiver_cycle"),
    (True, NOW, "receiver_cycle"),
    (1, NOW.replace(tzinfo=None), "timezone-aware"),
  ),
)
def test_invalid_persistence_context_is_rejected(
  receiver_cycle,
  recorded_at_utc,
  message,
):
  with pytest.raises(YumaOutcomeStoreError, match=message):
    serialize_yuma_supplementation_outcome(
      outcome(),
      commit="test",
      receiver_cycle=receiver_cycle,
      recorded_at_utc=recorded_at_utc,
    )
