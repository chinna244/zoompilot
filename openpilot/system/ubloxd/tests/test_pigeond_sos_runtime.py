from __future__ import annotations

from openpilot.system.ubloxd import pigeond


def test_sos_restore_status_poll_sends_expected_message(
  monkeypatch,
):
  pigeon = object.__new__(pigeond.TTYPigeon)
  transaction = object()

  begin_calls: list[tuple[bytes, str]] = []
  wait_calls: list[tuple[object, float]] = []

  def begin_response_transaction(
    message: bytes,
    operation: str,
  ) -> object:
    begin_calls.append(
      (
        message,
        operation,
      )
    )
    return transaction

  def wait_for_backup_restore_status(
    actual_transaction: object,
    timeout: float,
  ) -> int:
    wait_calls.append(
      (
        actual_transaction,
        timeout,
      )
    )
    return 3

  monkeypatch.setattr(
    pigeon,
    "begin_response_transaction",
    begin_response_transaction,
  )
  monkeypatch.setattr(
    pigeon,
    "wait_for_backup_restore_status",
    wait_for_backup_restore_status,
  )

  assert (
    pigeon.poll_backup_restore_status(
      timeout=0.25
    )
    == 3
  )

  assert begin_calls == [(
    b"\xB5\x62\x09\x14\x00\x00\x1D\x60",
    "upd_sos_restore_status_poll",
  )]

  assert wait_calls == [(
    transaction,
    0.25,
  )]
