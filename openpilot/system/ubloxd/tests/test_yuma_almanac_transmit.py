from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.yuma_almanac import YumaAlmanacError
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  YUMA_ALMANAC_RETRY_DELAY_SECONDS,
  MgaReceiverNackError,
  MgaTransactionError,
  MgaWriteError,
  YumaAlmanacTransmitStatus,
  transmit_public_yuma_almanac,
)


NOW = datetime(2026, 7, 21, 15, tzinfo=UTC)
REFERENCE_TIME = datetime(2026, 7, 21, 12, tzinfo=UTC)


def frame(satellite_id: int) -> bytes:
  data = bytearray(16)
  data[8] = satellite_id
  return bytes(data)


def stored_almanac(*satellite_ids: int):
  return SimpleNamespace(
    downloaded_at_utc=NOW,
    almanac=SimpleNamespace(
      frames=tuple(frame(value) for value in satellite_ids),
    ),
  )


def prepare_cache(tmp_path, monkeypatch, *satellite_ids):
  path = tmp_path / "public_yuma_almanac.json"
  path.write_text("present", encoding="utf-8")
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_transmit.load_yuma_almanac",
    lambda cache_path: stored_almanac(*satellite_ids),
  )
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_transmit.validate_yuma_reference_time",
    lambda almanac, trusted_now: REFERENCE_TIME,
  )
  return path


def test_transmit_requires_trusted_time(tmp_path, monkeypatch):
  def unexpected_load(path):
    raise AssertionError("cache must not be loaded")

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_transmit.load_yuma_almanac",
    unexpected_load,
  )

  result = transmit_public_yuma_almanac(
    lambda message: None,
    trusted_now=None,
    satellite_ids=frozenset((1, 2)),
    path=tmp_path / "cache.json",
  )

  assert result.status is YumaAlmanacTransmitStatus.TIME_UNAVAILABLE
  assert not result.receiver_write_attempted


def test_transmit_empty_selection_is_noop(tmp_path):
  result = transmit_public_yuma_almanac(
    lambda message: None,
    trusted_now=NOW,
    satellite_ids=frozenset(),
    path=tmp_path / "cache.json",
  )

  assert result.status is YumaAlmanacTransmitStatus.NO_SATELLITES
  assert not result.receiver_write_attempted


def test_transmit_missing_cache_is_retryable(tmp_path):
  result = transmit_public_yuma_almanac(
    lambda message: None,
    trusted_now=NOW,
    satellite_ids=frozenset((1,)),
    path=tmp_path / "missing.json",
  )

  assert result.status is YumaAlmanacTransmitStatus.MISSING
  assert not result.receiver_write_attempted


def test_transmit_rejects_invalid_cache(tmp_path, monkeypatch):
  path = tmp_path / "public_yuma_almanac.json"
  path.write_text("present", encoding="utf-8")
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_transmit.load_yuma_almanac",
    lambda cache_path: (_ for _ in ()).throw(
      YumaAlmanacError("invalid cache")
    ),
  )

  result = transmit_public_yuma_almanac(
    lambda message: None,
    trusted_now=NOW,
    satellite_ids=frozenset((1,)),
    path=path,
  )

  assert result.status is YumaAlmanacTransmitStatus.UNAVAILABLE
  assert not result.receiver_write_attempted


def test_transmit_selects_only_requested_prns(tmp_path, monkeypatch):
  path = prepare_cache(tmp_path, monkeypatch, 1, 2, 3, 4)
  sent: list[int] = []

  result = transmit_public_yuma_almanac(
    lambda message: sent.append(message[8]),
    trusted_now=NOW,
    satellite_ids=frozenset((2, 4)),
    path=path,
  )

  assert result.status is YumaAlmanacTransmitStatus.COMPLETE
  assert sent == [2, 4]
  assert result.attempted_satellite_ids == (2, 4)
  assert result.accepted_satellite_ids == (2, 4)
  assert result.reference_time_utc == REFERENCE_TIME
  assert result.downloaded_at_utc == NOW


def test_transmit_retries_only_failed_prns_once(tmp_path, monkeypatch):
  path = prepare_cache(tmp_path, monkeypatch, 1, 2, 3)
  attempts: list[int] = []
  failures_remaining = {2: 1, 3: 2}

  def send(message: bytes) -> None:
    satellite_id = message[8]
    attempts.append(satellite_id)
    if failures_remaining.get(satellite_id, 0):
      failures_remaining[satellite_id] -= 1
      raise TimeoutError("injected timeout")

  delays = []
  result = transmit_public_yuma_almanac(
    send,
    trusted_now=NOW,
    satellite_ids=frozenset((1, 2, 3)),
    path=path,
    sleep=delays.append,
  )

  assert result.status is YumaAlmanacTransmitStatus.PARTIAL
  assert attempts == [1, 2, 3, 2, 3]
  assert delays == [YUMA_ALMANAC_RETRY_DELAY_SECONDS]
  assert result.accepted_satellite_ids == (1, 2)
  assert result.failed_satellite_ids == (3,)


@pytest.mark.parametrize(
  "error_type",
  (
    TimeoutError,
    MgaReceiverNackError,
    MgaWriteError,
    MgaTransactionError,
  ),
)
def test_transmit_retries_only_typed_receiver_failures_once(
  tmp_path,
  monkeypatch,
  error_type,
):
  path = prepare_cache(tmp_path, monkeypatch, 1)
  attempts = []

  def send(message: bytes) -> None:
    attempts.append(message[8])
    if len(attempts) == 1:
      raise error_type("injected receiver failure")

  result = transmit_public_yuma_almanac(
    send,
    trusted_now=NOW,
    satellite_ids=frozenset((1,)),
    path=path,
    sleep=lambda delay: None,
  )

  assert result.status is YumaAlmanacTransmitStatus.COMPLETE
  assert attempts == [1, 1]
  assert result.attempted_satellite_ids == (1, 1)
  assert result.accepted_satellite_ids == (1,)
  assert result.failed_satellite_ids == ()


def test_transmit_uses_frozen_snapshot_without_reopening_path(
  tmp_path,
  monkeypatch,
):
  frozen = stored_almanac(1, 2)
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_transmit.load_yuma_almanac",
    lambda path: (_ for _ in ()).throw(
      AssertionError("frozen snapshot must avoid path reload")
    ),
  )
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_transmit.validate_yuma_reference_time",
    lambda almanac, trusted_now: REFERENCE_TIME,
  )
  sent = []

  result = transmit_public_yuma_almanac(
    lambda message: sent.append(message[8]),
    trusted_now=NOW,
    satellite_ids=frozenset((2,)),
    path=tmp_path / "replaced-after-planning.json",
    stored_almanac=frozen,
  )

  assert result.status is YumaAlmanacTransmitStatus.COMPLETE
  assert sent == [2]
  assert result.accepted_satellite_ids == (2,)


def test_transmit_reports_prns_missing_from_cache(tmp_path, monkeypatch):
  path = prepare_cache(tmp_path, monkeypatch, 1, 2)
  sent: list[int] = []

  result = transmit_public_yuma_almanac(
    lambda message: sent.append(message[8]),
    trusted_now=NOW,
    satellite_ids=frozenset((1, 2, 3)),
    path=path,
  )

  assert result.status is YumaAlmanacTransmitStatus.PARTIAL
  assert sent == [1, 2]
  assert result.unavailable_satellite_ids == (3,)


def test_transmit_all_requested_prns_missing_is_noop(
  tmp_path,
  monkeypatch,
):
  path = prepare_cache(tmp_path, monkeypatch, 1, 2)

  result = transmit_public_yuma_almanac(
    lambda message: None,
    trusted_now=NOW,
    satellite_ids=frozenset((31, 32)),
    path=path,
  )

  assert result.status is YumaAlmanacTransmitStatus.NO_SATELLITES
  assert result.unavailable_satellite_ids == (31, 32)
  assert not result.receiver_write_attempted


def test_transmit_stops_at_budget_before_next_frame(
  tmp_path,
  monkeypatch,
):
  path = prepare_cache(tmp_path, monkeypatch, 1, 2, 3)
  times = iter((0.0, 0.0, 0.5, 1.0))
  sent: list[int] = []

  result = transmit_public_yuma_almanac(
    lambda message: sent.append(message[8]),
    trusted_now=NOW,
    satellite_ids=frozenset((1, 2, 3)),
    path=path,
    max_duration_seconds=1.0,
    monotonic=lambda: next(times),
  )

  assert result.status is YumaAlmanacTransmitStatus.PARTIAL
  assert sent == [1, 2]
  assert result.accepted_satellite_ids == (1, 2)
  assert result.deferred_satellite_ids == (3,)


def test_transmit_budget_can_expire_before_first_write(
  tmp_path,
  monkeypatch,
):
  path = prepare_cache(tmp_path, monkeypatch, 1)
  times = iter((0.0, 1.0))

  result = transmit_public_yuma_almanac(
    lambda message: None,
    trusted_now=NOW,
    satellite_ids=frozenset((1,)),
    path=path,
    max_duration_seconds=1.0,
    monotonic=lambda: next(times),
  )

  assert result.status is YumaAlmanacTransmitStatus.BUDGET_EXPIRED
  assert result.deferred_satellite_ids == (1,)
  assert not result.receiver_write_attempted


@pytest.mark.parametrize(
  "value",
  (0, -1, float("inf"), float("nan"), True),
)
def test_transmit_rejects_invalid_duration(value):
  with pytest.raises(ValueError, match="positive finite"):
    transmit_public_yuma_almanac(
      lambda message: None,
      trusted_now=NOW,
      satellite_ids=frozenset(),
      max_duration_seconds=value,
    )


def test_transmit_rejects_invalid_satellite_ids():
  with pytest.raises(ValueError, match="1 through 32"):
    transmit_public_yuma_almanac(
      lambda message: None,
      trusted_now=NOW,
      satellite_ids=frozenset((0, 1)),
    )


def test_unexpected_cache_load_failure_is_logged(
  tmp_path,
  monkeypatch,
):
  path = tmp_path / "public_yuma_almanac.json"
  path.write_text("present", encoding="utf-8")
  logs = []
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_transmit.cloudlog.exception",
    logs.append,
  )
  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_transmit.load_yuma_almanac",
    lambda cache_path: (_ for _ in ()).throw(
      RuntimeError("injected cache bug")
    ),
  )

  result = transmit_public_yuma_almanac(
    lambda message: None,
    trusted_now=NOW,
    satellite_ids=frozenset((1,)),
    path=path,
  )

  assert result.status is YumaAlmanacTransmitStatus.UNAVAILABLE
  assert logs == ["Unexpected public YUMA cache load failure"]


def test_transmit_requires_full_write_margin_before_starting_frame(
  tmp_path,
  monkeypatch,
):
  path = prepare_cache(tmp_path, monkeypatch, 1, 2)
  times = iter((0.0, 0.0, 0.251))
  sent = []

  result = transmit_public_yuma_almanac(
    lambda message: sent.append(message[8]),
    trusted_now=NOW,
    satellite_ids=frozenset((1, 2)),
    path=path,
    max_duration_seconds=1.0,
    minimum_remaining_seconds=0.75,
    monotonic=lambda: next(times),
  )

  assert result.status is YumaAlmanacTransmitStatus.PARTIAL
  assert sent == [1]
  assert result.accepted_satellite_ids == (1,)
  assert result.deferred_satellite_ids == (2,)


def test_transmit_allows_write_at_exact_margin_boundary(
  tmp_path,
  monkeypatch,
):
  path = prepare_cache(tmp_path, monkeypatch, 1, 2)
  times = iter((0.0, 0.0, 0.25))
  sent = []

  result = transmit_public_yuma_almanac(
    lambda message: sent.append(message[8]),
    trusted_now=NOW,
    satellite_ids=frozenset((1, 2)),
    path=path,
    max_duration_seconds=1.0,
    minimum_remaining_seconds=0.75,
    monotonic=lambda: next(times),
  )

  assert result.status is YumaAlmanacTransmitStatus.COMPLETE
  assert sent == [1, 2]


@pytest.mark.parametrize(
  "value",
  (-1, float("inf"), float("nan"), True),
)
def test_transmit_rejects_invalid_minimum_remaining_seconds(value):
  with pytest.raises(ValueError, match="non-negative finite"):
    transmit_public_yuma_almanac(
      lambda message: None,
      trusted_now=NOW,
      satellite_ids=frozenset(),
      minimum_remaining_seconds=value,
    )

def test_unexpected_receiver_write_failure_is_propagated(
  tmp_path,
  monkeypatch,
):
  path = prepare_cache(tmp_path, monkeypatch, 1)
  logs = []

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_transmit.cloudlog.exception",
    logs.append,
  )

  def send(message: bytes) -> None:
    raise TypeError("injected programming failure")

  with pytest.raises(
    TypeError,
    match="injected programming failure",
  ):
    transmit_public_yuma_almanac(
      send,
      trusted_now=NOW,
      satellite_ids=frozenset((1,)),
      path=path,
    )

  assert logs == [
    "Unexpected public YUMA receiver write failure, satellite_id=1"
  ]


@pytest.mark.parametrize(
  ("error_type", "field"),
  (
    (TimeoutError, "timed_out_satellite_ids"),
    (MgaReceiverNackError, "rejected_satellite_ids"),
  ),
)
def test_transmit_records_retry_failure_category(
  tmp_path,
  monkeypatch,
  error_type,
  field,
):
  path = prepare_cache(tmp_path, monkeypatch, 1)
  calls = 0

  def send(message: bytes) -> None:
    nonlocal calls
    calls += 1
    if calls == 1:
      raise error_type("injected")

  result = transmit_public_yuma_almanac(
    send,
    trusted_now=NOW,
    satellite_ids=frozenset((1,)),
    path=path,
    sleep=lambda delay: None,
  )

  assert result.status is YumaAlmanacTransmitStatus.COMPLETE
  assert getattr(result, field) == (1,)
  assert result.attempted_satellite_ids == (1, 1)


def test_assistance_state_unavailable_is_not_reported_as_yuma_success(
  tmp_path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  path = prepare_cache(tmp_path, monkeypatch, 1, 2, 3)
  claim_attempts = 0
  receiver_writes: list[bytes] = []
  error_logs: list[str] = []
  retry_sleeps: list[float] = []

  class RejectingRuntime:
    def claim_yuma_transmission(self) -> bool:
      nonlocal claim_attempts
      claim_attempts += 1
      return False

  runtime = RejectingRuntime()
  monkeypatch.setattr(pigeond.cloudlog, "error", error_logs.append)

  result = transmit_public_yuma_almanac(
    lambda message: pigeond.send_yuma_with_durable_claim(
      runtime,  # type: ignore[arg-type, ty:invalid-argument-type]
      receiver_writes.append,
      message,
    ),
    trusted_now=NOW,
    satellite_ids=frozenset((1, 2, 3)),
    path=path,
    sleep=retry_sleeps.append,
  )

  assert claim_attempts == 1
  assert receiver_writes == []
  assert result.status is YumaAlmanacTransmitStatus.UNAVAILABLE
  assert result.attempted_satellite_ids == ()
  assert result.accepted_satellite_ids == ()
  assert result.failed_satellite_ids == ()
  assert result.unavailable_satellite_ids == (1, 2, 3)
  assert result.assistance_state_unavailable
  assert not result.receiver_write_attempted
  assert retry_sleeps == []
  assert len(error_logs) == 1


def test_assistance_state_unavailable_after_first_accept_preserves_partial_history(
  tmp_path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  path = prepare_cache(tmp_path, monkeypatch, 1, 2, 3)
  claim_attempts = 0
  receiver_writes: list[bytes] = []
  error_logs: list[str] = []
  retry_sleeps: list[float] = []

  class FailingAfterFirstClaimRuntime:
    def claim_yuma_transmission(self) -> bool:
      nonlocal claim_attempts
      claim_attempts += 1
      return claim_attempts == 1

  runtime = FailingAfterFirstClaimRuntime()
  monkeypatch.setattr(pigeond.cloudlog, "error", error_logs.append)

  result = transmit_public_yuma_almanac(
    lambda message: pigeond.send_yuma_with_durable_claim(
      runtime,  # type: ignore[arg-type, ty:invalid-argument-type]
      receiver_writes.append,
      message,
    ),
    trusted_now=NOW,
    satellite_ids=frozenset((1, 2, 3)),
    path=path,
    sleep=retry_sleeps.append,
  )

  assert claim_attempts == 2
  assert [message[8] for message in receiver_writes] == [1]
  assert result.status is YumaAlmanacTransmitStatus.PARTIAL
  assert result.attempted_satellite_ids == (1,)
  assert result.accepted_satellite_ids == (1,)
  assert result.failed_satellite_ids == ()
  assert result.rejected_satellite_ids == ()
  assert result.timed_out_satellite_ids == ()
  assert result.deferred_satellite_ids == ()
  assert result.unavailable_satellite_ids == (2, 3)
  assert result.assistance_state_unavailable
  assert result.receiver_write_attempted
  assert retry_sleeps == []
  assert len(error_logs) == 1
