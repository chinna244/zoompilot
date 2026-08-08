from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from openpilot.common.time_helpers import HostTimeObservation, HostTimeSource
from openpilot.system.qcomgpsd.qcomgpsd import (
  AtCommandError,
  AtCommandTimeout,
  QGPSXTRATIME_ASSISTANCE_BUDGET_SECONDS,
  QGPSXTRATIME_MIN_UNCERTAINTY_MS,
  QGPSXTRATIME_PROVEN_UNCERTAINTY_MS,
  maybe_inject_host_time,
  qgpsxtratime_uncertainty_ms,
)
from openpilot.system.qcomgpsd.qcom_position import host_time_safe_for_qcom_injection


def _obs(
  *,
  source: HostTimeSource,
  independent: bool,
  uncertainty_seconds: float = 1.0,
  boottime: float = 100.0,
  utc: datetime | None = None,
) -> HostTimeObservation:
  return HostTimeObservation(
    utc=utc or datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
    observed_boottime_seconds=boottime,
    uncertainty_seconds=uncertainty_seconds,
    source=source,
    independent=independent,
    generation=f"{source.value}:test",
  )


def test_network_synchronized_authorized():
  assert host_time_safe_for_qcom_injection(_obs(source=HostTimeSource.NETWORK_SYNCHRONIZED, independent=True))


def test_receiver_derived_blocked():
  assert not host_time_safe_for_qcom_injection(_obs(source=HostTimeSource.RECEIVER_DERIVED, independent=False))


def test_unknown_blocked():
  assert not host_time_safe_for_qcom_injection(_obs(source=HostTimeSource.UNKNOWN, independent=False))


def test_none_blocked():
  assert not host_time_safe_for_qcom_injection(None)


def test_fractional_below_proven_raises_to_3500():
  assert qgpsxtratime_uncertainty_ms(0.0011, 0.0) == QGPSXTRATIME_PROVEN_UNCERTAINTY_MS
  assert qgpsxtratime_uncertainty_ms(0.0, 0.0) == QGPSXTRATIME_MIN_UNCERTAINTY_MS
  assert qgpsxtratime_uncertainty_ms(1.0, 0.0) == QGPSXTRATIME_PROVEN_UNCERTAINTY_MS
  assert qgpsxtratime_uncertainty_ms(3.5, 0.0) == QGPSXTRATIME_PROVEN_UNCERTAINTY_MS


def test_exactly_proven_3500_accepted():
  assert qgpsxtratime_uncertainty_ms(3.5, 0.0) == 3500


def test_above_proven_3500_fail_closed():
  # EG25-G docs do not establish a max > 3500; larger required unc must skip.
  assert qgpsxtratime_uncertainty_ms(3.5001, 0.0) is None
  assert qgpsxtratime_uncertainty_ms(30.0, 0.0) is None
  assert qgpsxtratime_uncertainty_ms(1.0, 3.0) is None  # 4000 ms after ceil


def test_never_downward_clamp_means_skip_not_reduce():
  # Required 10000 ms cannot be reduced to 3500 — skip instead.
  assert qgpsxtratime_uncertainty_ms(10.0, 0.0) is None


def test_production_30s_host_uncertainty_skips_injection():
  calls: list[str] = []
  assert (
    maybe_inject_host_time(
      read_observation=lambda: _obs(
        source=HostTimeSource.NETWORK_SYNCHRONIZED,
        independent=True,
        uncertainty_seconds=30.0,
      ),
      read_boottime=lambda: 100.0,
      at_command=lambda cmd, **_k: calls.append(cmd) or "",
    )
    is False
  )
  assert calls == []


def test_observation_age_included_when_still_representable():
  calls: list[str] = []
  obs = _obs(
    source=HostTimeSource.NETWORK_SYNCHRONIZED,
    independent=True,
    uncertainty_seconds=1.0,
    boottime=100.0,
  )
  assert (
    maybe_inject_host_time(
      read_observation=lambda: obs,
      read_boottime=lambda: 102.0,  # age 2s → total 3s → raise to 3500
      at_command=lambda cmd, **_k: calls.append(cmd) or "",
    )
    is True
  )
  assert len(calls) == 1
  assert calls[0].endswith(",3500")
  assert "2026/08/01,12:00:02" in calls[0]


def test_age_that_exceeds_proven_skips():
  calls: list[str] = []
  assert (
    maybe_inject_host_time(
      read_observation=lambda: _obs(
        source=HostTimeSource.NETWORK_SYNCHRONIZED,
        independent=True,
        uncertainty_seconds=1.0,
        boottime=100.0,
      ),
      read_boottime=lambda: 104.0,  # 5s age → 6000 ms required
      at_command=lambda cmd, **_k: calls.append(cmd) or "",
    )
    is False
  )
  assert calls == []


def test_unrepresentable_uncertainty_skips():
  assert qgpsxtratime_uncertainty_ms(float("nan"), 0.0) is None
  assert qgpsxtratime_uncertainty_ms(-1.0, 0.0) is None
  assert qgpsxtratime_uncertainty_ms(1e12, 0.0) is None


def test_maybe_inject_skips_without_authority():
  calls: list[str] = []
  assert (
    maybe_inject_host_time(
      read_observation=lambda: None,
      at_command=lambda cmd, **_k: calls.append(cmd) or "",
    )
    is False
  )
  assert calls == []


def test_maybe_inject_skips_receiver_derived():
  calls: list[str] = []
  assert (
    maybe_inject_host_time(
      read_observation=lambda: _obs(
        source=HostTimeSource.RECEIVER_DERIVED,
        independent=False,
      ),
      at_command=lambda cmd, **_k: calls.append(cmd) or "",
    )
    is False
  )
  assert calls == []


def test_maybe_inject_authorized_small_uncertainty():
  calls: list[str] = []
  assert (
    maybe_inject_host_time(
      read_observation=lambda: _obs(
        source=HostTimeSource.NETWORK_SYNCHRONIZED,
        independent=True,
        uncertainty_seconds=1.0,
        boottime=10.0,
      ),
      read_boottime=lambda: 10.0,
      at_command=lambda cmd, **_k: calls.append(cmd) or "",
    )
    is True
  )
  assert len(calls) == 1
  assert calls[0].startswith("AT+QGPSXTRATIME=")
  assert ",3500" in calls[0]


@pytest.mark.parametrize(
  "exc",
  [
    AtCommandError("AT+QGPSXTRATIME", "ERROR"),
    AtCommandError("AT+QGPSXTRATIME", "+CME ERROR: 100"),
    AtCommandTimeout("AT+QGPSXTRATIME"),
    OSError("transport"),
  ],
)
def test_xtratime_failure_still_sends_qgps(monkeypatch, exc):
  from openpilot.system.qcomgpsd import qcomgpsd as m

  monkeypatch.setattr(m, "ensure_gnss_oem_feature_mask", lambda *_a, **_k: None)
  monkeypatch.setattr(m, "try_setup_logs", lambda *_a, **_k: None)
  monkeypatch.setattr(m, "gps_enabled", lambda: False)
  monkeypatch.setattr(m, "send_recv", lambda *_a, **_k: (75, b"\r\x00d\xca\x00\x01\x00\x00\x00\x01\x00\x00\x00"))
  cmds: list[str] = []

  def fake_at(cmd: str, **_kwargs):
    cmds.append(cmd)
    return ""

  monkeypatch.setattr(m, "at_cmd", fake_at)
  obs = _obs(source=HostTimeSource.NETWORK_SYNCHRONIZED, independent=True, boottime=10.0, uncertainty_seconds=1.0)

  def inject():
    return maybe_inject_host_time(
      read_observation=lambda: obs,
      read_boottime=lambda: 10.0,
      at_command=lambda cmd, **_kw: (_ for _ in ()).throw(exc),
    )

  monkeypatch.setattr(m, "maybe_inject_host_time", inject)
  m.setup_quectel.__wrapped__(MagicMock())
  assert "AT+QGPS=1" in cmds


def test_xtratime_logging_raise_still_sends_qgps(monkeypatch):
  from openpilot.system.qcomgpsd import qcomgpsd as mod

  monkeypatch.setattr(mod, "ensure_gnss_oem_feature_mask", lambda *_a, **_k: None)
  monkeypatch.setattr(mod, "try_setup_logs", lambda *_a, **_k: None)
  monkeypatch.setattr(mod, "gps_enabled", lambda: False)
  monkeypatch.setattr(mod, "send_recv", lambda *_a, **_k: (75, b"\r\x00d\xca\x00\x01\x00\x00\x00\x01\x00\x00\x00"))
  monkeypatch.setattr(
    mod.cloudlog,
    "info",
    lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("log boom")),
  )
  cmds: list[str] = []

  def fake_at(cmd: str, **_kwargs):
    cmds.append(cmd)
    return ""

  monkeypatch.setattr(mod, "at_cmd", fake_at)
  obs = _obs(source=HostTimeSource.NETWORK_SYNCHRONIZED, independent=True, boottime=10.0, uncertainty_seconds=1.0)

  def inject():
    return maybe_inject_host_time(
      read_observation=lambda: obs,
      read_boottime=lambda: 10.0,
      at_command=lambda cmd, **_k: (_ for _ in ()).throw(AtCommandError(cmd, "ERROR")),
    )

  monkeypatch.setattr(mod, "maybe_inject_host_time", inject)
  mod.setup_quectel.__wrapped__(MagicMock())
  assert "AT+QGPS=1" in cmds


def test_xtratime_failure_does_not_trigger_setup_retry(monkeypatch):
  from openpilot.system.qcomgpsd import qcomgpsd as mod

  monkeypatch.setattr(mod, "ensure_gnss_oem_feature_mask", lambda *_a, **_k: None)
  monkeypatch.setattr(mod, "try_setup_logs", lambda *_a, **_k: None)
  monkeypatch.setattr(mod, "gps_enabled", lambda: False)
  monkeypatch.setattr(mod, "send_recv", lambda *_a, **_k: (75, b"\r\x00d\xca\x00\x01\x00\x00\x00\x01\x00\x00\x00"))
  cmds: list[str] = []
  setup_calls = {"n": 0}

  def fake_at(cmd: str, **_kwargs):
    cmds.append(cmd)
    return ""

  monkeypatch.setattr(mod, "at_cmd", fake_at)
  obs = _obs(source=HostTimeSource.NETWORK_SYNCHRONIZED, independent=True, boottime=10.0, uncertainty_seconds=1.0)

  def inject():
    return maybe_inject_host_time(
      read_observation=lambda: obs,
      read_boottime=lambda: 10.0,
      at_command=lambda cmd, **_k: (_ for _ in ()).throw(AtCommandError(cmd, "ERROR")),
    )

  monkeypatch.setattr(mod, "maybe_inject_host_time", inject)
  setup_calls["n"] += 1
  mod.setup_quectel.__wrapped__(MagicMock())
  assert setup_calls["n"] == 1
  assert "AT+QGPS=1" in cmds


def test_held_lock_xtratime_budget_expires_quickly_qgps_continues(monkeypatch):
  """Held AT lock must not burn the 90s modem-startup deadline before QGPS=1."""
  import fcntl

  from openpilot.system.qcomgpsd import qcomgpsd as mod

  clock = {"t": 0.0}
  max_t = {"t": 0.0}

  def mono():
    return clock["t"]

  def sleep(dt):
    clock["t"] += dt
    max_t["t"] = max(max_t["t"], clock["t"])

  def flock(_fd, flags):
    assert flags == (fcntl.LOCK_EX | fcntl.LOCK_NB)
    raise BlockingIOError()

  class LockFile:
    def __enter__(self):
      return self

    def __exit__(self, *a):
      return False

    def fileno(self):
      return 3

  monkeypatch.setattr(mod.os, "open", lambda *_a, **_k: 3)
  monkeypatch.setattr(mod.os, "fdopen", lambda *_a, **_k: LockFile())

  monkeypatch.setattr(mod, "ensure_gnss_oem_feature_mask", lambda *_a, **_k: None)
  monkeypatch.setattr(mod, "try_setup_logs", lambda *_a, **_k: None)
  monkeypatch.setattr(mod, "gps_enabled", lambda: False)
  monkeypatch.setattr(mod, "send_recv", lambda *_a, **_k: (75, b"\r\x00d\xca\x00\x01\x00\x00\x00\x01\x00\x00\x00"))

  cmds: list[str] = []

  def fake_at(cmd: str, **_kwargs):
    cmds.append(cmd)
    return ""

  monkeypatch.setattr(mod, "at_cmd", fake_at)

  obs = _obs(
    source=HostTimeSource.NETWORK_SYNCHRONIZED,
    independent=True,
    uncertainty_seconds=1.0,
    boottime=10.0,
  )

  def inject():
    return maybe_inject_host_time(
      read_observation=lambda: obs,
      read_boottime=lambda: 10.0 + mono(),
      monotonic=mono,
      sleeper=sleep,
      flock=flock,
    )

  monkeypatch.setattr(mod, "maybe_inject_host_time", inject)
  mod.setup_quectel.__wrapped__(MagicMock())
  assert "AT+QGPS=1" in cmds
  assert max_t["t"] <= QGPSXTRATIME_ASSISTANCE_BUDGET_SECONDS + 0.1
  assert max_t["t"] < 10.0  # nowhere near 90s


def test_lock_wait_elapsed_included_in_sent_timestamp(monkeypatch):
  """Age must be recomputed after lock acquisition (timestamp reflects lock wait)."""
  import fcntl

  from openpilot.system.qcomgpsd import qcomgpsd as mod

  clock = {"t": 0.0}
  boot0 = 100.0
  sent: list[str] = []
  lock_attempts = {"n": 0}

  def mono():
    return clock["t"]

  def sleep(dt):
    # One poll sleep advances past a full second so projected UTC changes.
    clock["t"] += 1.05

  def flock(_fd, flags):
    assert flags == (fcntl.LOCK_EX | fcntl.LOCK_NB)
    lock_attempts["n"] += 1
    if lock_attempts["n"] < 2:
      raise BlockingIOError()
    return None

  class FakeSer:
    def __init__(self, *a, **k):
      pass

    def __enter__(self):
      return self

    def __exit__(self, *a):
      return False

    def reset_input_buffer(self):
      return None

    def write(self, data: bytes):
      sent.append(data.decode())
      return len(data)

    def readline(self):
      return b"OK\r\n"

  class LockFile:
    def __enter__(self):
      return self

    def __exit__(self, *a):
      return False

    def fileno(self):
      return 3

  monkeypatch.setattr(mod.os, "open", lambda *_a, **_k: 3)
  monkeypatch.setattr(mod.os, "fdopen", lambda *_a, **_k: LockFile())
  monkeypatch.setattr(mod, "Serial", FakeSer)

  obs = _obs(
    source=HostTimeSource.NETWORK_SYNCHRONIZED,
    independent=True,
    uncertainty_seconds=1.0,
    boottime=boot0,
    utc=datetime(2026, 8, 1, 12, 0, tzinfo=UTC),
  )
  assert maybe_inject_host_time(
    read_observation=lambda: obs,
    read_boottime=lambda: boot0 + mono(),
    monotonic=mono,
    sleeper=sleep,
    flock=flock,
  )
  assert len(sent) == 1
  assert ",3500" in sent[0]
  # Lock wait (~1.05s) must appear in projected UTC sent after lock.
  assert "2026/08/01,12:00:01" in sent[0]
  assert lock_attempts["n"] == 2


def test_logging_failure_on_skip_does_not_block_qgps_start(monkeypatch):
  from openpilot.system.qcomgpsd import qcomgpsd as mod

  monkeypatch.setattr(mod, "ensure_gnss_oem_feature_mask", lambda *_a, **_k: None)
  monkeypatch.setattr(mod, "try_setup_logs", lambda *_a, **_k: None)
  monkeypatch.setattr(mod, "gps_enabled", lambda: False)
  monkeypatch.setattr(mod, "send_recv", lambda *_a, **_k: (75, b"\r\x00d\xca\x00\x01\x00\x00\x00\x01\x00\x00\x00"))
  monkeypatch.setattr(
    mod.cloudlog,
    "info",
    lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("log boom")),
  )

  cmds: list[str] = []

  def fake_at(cmd: str, **_kwargs):
    cmds.append(cmd)
    return ""

  monkeypatch.setattr(mod, "at_cmd", fake_at)
  monkeypatch.setattr(mod, "read_host_time_observation", lambda: None)
  mod.setup_quectel.__wrapped__(MagicMock())
  assert "AT+QGPS=1" in cmds
  assert not any("QGPSXTRATIME" in c for c in cmds)


def test_skip_does_not_block_qgps_start(monkeypatch):
  from openpilot.system.qcomgpsd import qcomgpsd as mod

  monkeypatch.setattr(mod, "ensure_gnss_oem_feature_mask", lambda *_a, **_k: None)
  monkeypatch.setattr(mod, "try_setup_logs", lambda *_a, **_k: None)
  monkeypatch.setattr(mod, "gps_enabled", lambda: False)
  monkeypatch.setattr(mod, "maybe_inject_host_time", lambda: False)
  monkeypatch.setattr(mod, "send_recv", lambda *_a, **_k: (75, b"\r\x00d\xca\x00\x01\x00\x00\x00\x01\x00\x00\x00"))

  cmds: list[str] = []

  def fake_at(cmd: str, **_kwargs):
    cmds.append(cmd)
    return ""

  monkeypatch.setattr(mod, "at_cmd", fake_at)
  mod.setup_quectel.__wrapped__(MagicMock())
  assert "AT+QGPS=1" in cmds
  assert not any("QGPSXTRATIME" in c for c in cmds)
