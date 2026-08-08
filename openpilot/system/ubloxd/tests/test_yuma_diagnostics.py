from datetime import UTC, datetime

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaDatabaseRestoreState,
  YumaSupplementationAction,
  YumaSupplementationPlan,
  YumaSupplementationReason,
)
from openpilot.system.ubloxd.yuma_almanac_runtime import (
  YumaSupplementationRuntimeOutcome,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  YumaAlmanacTransmitResult,
  YumaAlmanacTransmitStatus,
)


REFERENCE_TIME = datetime(2026, 7, 21, 12, tzinfo=UTC)
DOWNLOADED_AT = datetime(2026, 7, 21, 15, tzinfo=UTC)


def test_pigeond_logs_complete_yuma_diagnostics(monkeypatch):
  logs = []
  monkeypatch.setattr(pigeond.cloudlog, "info", logs.append)
  plan = YumaSupplementationPlan(
    YumaSupplementationAction.SEND_MISSING,
    YumaSupplementationReason.MISSING_VISIBLE_GPS_ALMANAC,
    satellite_ids=frozenset((2, 4)),
  )
  result = YumaAlmanacTransmitResult(
    YumaAlmanacTransmitStatus.PARTIAL,
    requested_satellite_ids=(2, 4),
    attempted_satellite_ids=(2, 4),
    accepted_satellite_ids=(2,),
    failed_satellite_ids=(4,),
    reference_time_utc=REFERENCE_TIME,
    downloaded_at_utc=DOWNLOADED_AT,
  )
  outcome = YumaSupplementationRuntimeOutcome(
    plan=plan,
    transmit_result=result,
    database_state=YumaDatabaseRestoreState.PARTIAL,
    database_age_seconds=18000.0,
    yuma_reference_utc=REFERENCE_TIME,
    yuma_reference_age_seconds=10800.0,
    downloaded_at_utc=DOWNLOADED_AT,
    cache_error="FileNotFoundError: old cache",
    transmission_attempt=1,
    transmission_elapsed_ms=275.0,
  )

  pigeond.log_yuma_supplementation_outcome(outcome)

  assert len(logs) == 1
  message = logs[0]
  expected = (
    "enabled=true",
    "dbd_state=partial",
    "dbd_age_seconds=18000.0",
    "action=send_missing",
    "reason=missing_visible_gps_almanac",
    "yuma_reference_utc=2026-07-21T12:00:00+00:00",
    "yuma_reference_age_seconds=10800.0",
    "downloaded_at_utc=2026-07-21T15:00:00+00:00",
    "cache_error=FileNotFoundError: old cache",
    "transmission_attempt=1",
    "transmission_elapsed_ms=275.0",
    "requested_prns=2,4",
    "attempted_prns=2,4",
    "accepted_prns=2",
    "failed_prns=4",
  )
  assert all(value in message for value in expected)
