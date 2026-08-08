from __future__ import annotations

import pytest

from openpilot.system.qcomgpsd.qcomgpsd import (
  AtCommandError,
  AtCommandTimeout,
  ModemStartupTimeout,
  consume_at_response,
  wait_for_modem,
)


def test_at_ok():
  assert consume_at_response("AT", ["OK"]) == ""


def test_at_multiline_ok():
  assert consume_at_response("AT+QGPS?", ["+QGPS: 1", "OK"]) == "+QGPS: 1"


def test_at_error():
  with pytest.raises(AtCommandError) as exc:
    consume_at_response("AT+BAD", ["ERROR"])
  assert exc.value.terminal == "ERROR"


def test_at_cme_error():
  with pytest.raises(AtCommandError) as exc:
    consume_at_response("AT+BAD", ["+CME ERROR: 100"])
  assert exc.value.terminal.startswith("+CME ERROR")


def test_at_timeout_empty():
  with pytest.raises(AtCommandTimeout):
    consume_at_response("AT", [None])


def test_at_timeout_no_terminal():
  with pytest.raises(AtCommandTimeout):
    consume_at_response("AT", ["+URC: 1"])


def test_at_unsolicited_before_ok():
  body = consume_at_response("AT+QGPS?", ["^SYSSTART", "+QGPS: 0", "OK"])
  assert body == "^SYSSTART\n+QGPS: 0"


def test_at_echo_ignored():
  assert consume_at_response("ATI", ["ATI", "Quectel", "OK"]) == "Quectel"


def test_wait_for_modem_immediately_ready():
  clock = {"t": 0.0}

  wait_for_modem(
    deadline_seconds=5.0,
    sleep_seconds=0.1,
    monotonic=lambda: clock["t"],
    sleeper=lambda _s: None,
    path_exists=lambda _p: True,
    at_command=lambda _c, **_k: "+QGPS: 0",
  )


def test_wait_for_modem_ready_after_retries():
  clock = {"t": 0.0}
  calls = {"n": 0}

  def sleep(dt):
    clock["t"] += dt

  def at_command(_cmd: str, **_kwargs) -> str:
    calls["n"] += 1
    if calls["n"] < 3:
      raise RuntimeError("transient")
    return "+QGPS: 0"

  wait_for_modem(
    deadline_seconds=10.0,
    sleep_seconds=0.5,
    monotonic=lambda: clock["t"],
    sleeper=sleep,
    path_exists=lambda _p: True,
    at_command=at_command,
  )
  assert calls["n"] == 3
  assert clock["t"] < 10.0


def test_wait_for_modem_never_ready_bounded():
  clock = {"t": 0.0}

  def sleep(dt):
    clock["t"] += dt

  with pytest.raises(ModemStartupTimeout):
    wait_for_modem(
      deadline_seconds=2.0,
      sleep_seconds=0.5,
      monotonic=lambda: clock["t"],
      sleeper=sleep,
      path_exists=lambda _p: True,
      at_command=lambda _c, **_k: "NOPE",
    )
  assert clock["t"] >= 2.0
  assert clock["t"] <= 3.0


def test_wait_for_modem_port_never_appears():
  clock = {"t": 0.0}

  def sleep(dt):
    clock["t"] += dt

  with pytest.raises(ModemStartupTimeout):
    wait_for_modem(
      deadline_seconds=1.5,
      sleep_seconds=0.5,
      monotonic=lambda: clock["t"],
      sleeper=sleep,
      path_exists=lambda _p: False,
      at_command=lambda _c, **_k: "+QGPS: 0",
    )


def test_wait_for_modem_transient_at_errors():
  clock = {"t": 0.0}
  n = {"i": 0}

  def sleep(dt):
    clock["t"] += dt

  def at_command(_cmd: str, **_kwargs) -> str:
    n["i"] += 1
    if n["i"] < 4:
      raise AtCommandError("AT+QGPS?", "ERROR")
    return "+QGPS: 1"

  wait_for_modem(
    deadline_seconds=5.0,
    sleep_seconds=0.2,
    monotonic=lambda: clock["t"],
    sleeper=sleep,
    path_exists=lambda _p: True,
    at_command=at_command,
  )


def test_wait_for_modem_deadline_bounds_blocking_at():
  """AT that would otherwise retry for many seconds must respect remaining budget."""
  clock = {"t": 0.0}
  max_seen = {"t": 0.0}

  def mono():
    return clock["t"]

  def sleep(dt):
    clock["t"] += dt
    max_seen["t"] = max(max_seen["t"], clock["t"])

  def blocking_at(_cmd: str, deadline=None, **_kwargs):
    # Simulate a long AT/retry without jumping past the remaining budget.
    while deadline is None or mono() < deadline:
      sleep(0.05)
    raise AtCommandTimeout("AT+QGPS?")

  with pytest.raises(ModemStartupTimeout):
    wait_for_modem(
      deadline_seconds=0.2,
      sleep_seconds=0.05,
      monotonic=mono,
      sleeper=sleep,
      path_exists=lambda _p: True,
      at_command=blocking_at,
    )
  assert max_seen["t"] <= 0.35


def test_acquire_at_lock_available_immediately():
  from openpilot.system.qcomgpsd.qcomgpsd import acquire_at_lock

  clock = {"t": 0.0}
  calls = {"n": 0}

  def flock(_fd, flags):
    calls["n"] += 1
    assert flags == (__import__("fcntl").LOCK_EX | __import__("fcntl").LOCK_NB)

  acquire_at_lock(
    3,
    deadline=1.0,
    monotonic=lambda: clock["t"],
    sleeper=lambda _s: None,
    flock=flock,
  )
  assert calls["n"] == 1


def test_acquire_at_lock_temporary_contention_then_acquire():
  from openpilot.system.qcomgpsd.qcomgpsd import acquire_at_lock

  clock = {"t": 0.0}
  attempts = {"n": 0}

  def flock(_fd, _flags):
    attempts["n"] += 1
    if attempts["n"] < 3:
      raise BlockingIOError()

  def sleep(dt):
    clock["t"] += dt

  acquire_at_lock(
    3,
    deadline=1.0,
    monotonic=lambda: clock["t"],
    sleeper=sleep,
    flock=flock,
    poll_seconds=0.1,
  )
  assert attempts["n"] == 3
  assert clock["t"] < 1.0


def test_acquire_at_lock_held_forever_times_out():
  from openpilot.system.qcomgpsd.qcomgpsd import AtCommandTimeout, acquire_at_lock

  clock = {"t": 0.0}

  def flock(_fd, _flags):
    raise BlockingIOError()

  def sleep(dt):
    clock["t"] += dt

  with pytest.raises(AtCommandTimeout) as exc:
    acquire_at_lock(
      3,
      deadline=0.25,
      monotonic=lambda: clock["t"],
      sleeper=sleep,
      flock=flock,
      poll_seconds=0.05,
    )
  assert exc.value.terminal == "TIMEOUT"
  assert clock["t"] >= 0.25


def test_at_cmd_once_deadline_expires_during_lock_no_serial(monkeypatch):
  """Lock wait exhausting the deadline must not open the serial port."""
  import fcntl

  from openpilot.system.qcomgpsd import qcomgpsd as mod

  clock = {"t": 0.0}
  serial_opened = {"n": 0}

  class BoomSerial:
    def __init__(self, *a, **k):
      serial_opened["n"] += 1
      raise AssertionError("serial must not open after lock deadline")

  def flock(_fd, flags):
    assert flags == (fcntl.LOCK_EX | fcntl.LOCK_NB)
    raise BlockingIOError()

  def sleep(dt):
    clock["t"] += dt

  monkeypatch.setattr(mod.os, "open", lambda *_a, **_k: 3)
  monkeypatch.setattr(mod.os, "fdopen", lambda *_a, **_k: MagicMockFd())
  monkeypatch.setattr(mod, "Serial", BoomSerial)

  class MagicMockFd:
    def __enter__(self):
      return self

    def __exit__(self, *a):
      return False

    def fileno(self):
      return 3

  with pytest.raises(mod.AtCommandTimeout):
    mod._at_cmd_once(
      "AT",
      serial_timeout=5.0,
      deadline=0.2,
      monotonic=lambda: clock["t"],
      sleeper=sleep,
      flock=flock,
    )
  assert serial_opened["n"] == 0
  assert clock["t"] >= 0.2


def test_lock_plus_serial_cannot_exceed_overall_deadline(monkeypatch):
  import fcntl

  from openpilot.system.qcomgpsd import qcomgpsd as mod

  clock = {"t": 0.0}
  state = {"locked": False}

  class FakeSer:
    def __init__(self, *a, **k):
      self.timeout = k.get("timeout", 5.0)

    def __enter__(self):
      return self

    def __exit__(self, *a):
      return False

    def reset_input_buffer(self):
      return None

    def write(self, _data):
      # Consume remaining budget during serial I/O.
      remaining = 1.0 - clock["t"]
      clock["t"] += max(0.0, remaining)
      return None

    def readline(self):
      return b""

  def flock(_fd, flags):
    assert flags == (fcntl.LOCK_EX | fcntl.LOCK_NB)
    if not state["locked"]:
      state["locked"] = True
      raise BlockingIOError()
    return None

  def sleep(dt):
    clock["t"] += dt

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

  with pytest.raises(mod.AtCommandTimeout):
    mod._at_cmd_once(
      "AT",
      serial_timeout=5.0,
      deadline=1.0,
      monotonic=lambda: clock["t"],
      sleeper=sleep,
      flock=flock,
    )
  assert clock["t"] <= 1.05
