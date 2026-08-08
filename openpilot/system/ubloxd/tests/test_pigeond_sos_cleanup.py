from __future__ import annotations

import inspect

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.gps_assistance import NavigationCacheStore


def test_unsupported_sos_backup_creation_api_is_absent():
  removed_names = (
    "RECEIVER_BACKUP_RETRY_INTERVAL",
    "RECEIVER_BACKUP_REFRESH_INTERVAL",
    "ReceiverBackupResult",
    "ReceiverBackupAttemptReason",
    "ReceiverBackupScheduler",
    "save_almanac",
    "UBLOX_SOS_ACK",
    "UBLOX_SOS_NACK",
    "describe_upd_sos_response",
  )

  for name in removed_names:
    assert not hasattr(pigeond, name)


def test_sos_restore_and_navigation_cache_apis_remain_available():
  assert callable(pigeond.parse_upd_sos_response)
  assert callable(
    pigeond.TTYPigeon.wait_for_backup_restore_status
  )
  assert callable(
    pigeond.TTYPigeon.poll_backup_restore_status
  )
  assert callable(pigeond.TTYPigeon.reset_device)
  assert callable(pigeond.restore_navigation_assistance)


def test_select_inventory_requires_explicit_age_evidence():
  signature = inspect.signature(
    NavigationCacheStore.select_inventory
  )

  age_evidence = signature.parameters[
    "age_evidence"
  ]

  assert age_evidence.default is inspect.Parameter.empty
  assert age_evidence.kind is inspect.Parameter.KEYWORD_ONLY
