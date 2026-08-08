import datetime
import json
from types import SimpleNamespace

import pytest

from openpilot.system.manager import manager
from openpilot.system.ubloxd.gps_assistance import (
  NavPvtFix,
  add_ubx_checksum,
  create_cache,
  save_cache,
)


class FrozenDateTime(datetime.datetime):
  @classmethod
  def now(cls, tz=None):
    value = cls(
      2026,
      3,
      24,
      12,
      0,
      0,
      tzinfo=datetime.UTC,
    )
    return value if tz is not None else value.replace(tzinfo=None)


def configure_time_test(monkeypatch, cached_time):
  monkeypatch.setattr(manager, "PC", False)
  monkeypatch.setattr(manager.datetime, "datetime", FrozenDateTime)
  monkeypatch.setattr(
    manager,
    "min_date",
    lambda: datetime.datetime(2025, 1, 1),
  )
  monkeypatch.setattr(
    manager,
    "MAX_DATE",
    datetime.datetime(2035, 1, 1),
  )
  monkeypatch.setattr(
    manager,
    "load_cache",
    lambda _path, **_kwargs: SimpleNamespace(
      saved_at_utc=cached_time,
      rtc_counter_seconds=100,
    ),
  )
  monkeypatch.setattr(manager, "read_rtc_counter_seconds", lambda: 100)


def test_restore_cached_gps_time_advances_stale_clock(monkeypatch):
  cached_time = datetime.datetime(
    2026,
    7,
    3,
    12,
    47,
    8,
    tzinfo=datetime.UTC,
  )
  configure_time_test(monkeypatch, cached_time)

  calls = []
  monkeypatch.setattr(
    manager,
    "set_system_time",
    lambda value: calls.append(value) or True,
  )

  manager.restore_cached_gps_time()

  assert calls == [cached_time]


def test_restore_cached_gps_time_never_moves_clock_backward(monkeypatch):
  cached_time = datetime.datetime(
    2026,
    3,
    23,
    12,
    0,
    0,
    tzinfo=datetime.UTC,
  )
  configure_time_test(monkeypatch, cached_time)

  calls = []
  monkeypatch.setattr(
    manager,
    "set_system_time",
    lambda value: calls.append(value) or True,
  )

  manager.restore_cached_gps_time()

  assert calls == []


def test_restore_cached_gps_time_ignores_small_difference(monkeypatch):
  cached_time = datetime.datetime(
    2026,
    3,
    24,
    12,
    0,
    5,
    tzinfo=datetime.UTC,
  )
  configure_time_test(monkeypatch, cached_time)

  calls = []
  monkeypatch.setattr(
    manager,
    "set_system_time",
    lambda value: calls.append(value) or True,
  )

  manager.restore_cached_gps_time()

  assert calls == []


def write_cache(path, saved_at):
  frame = add_ubx_checksum(b"\xb5\x62\x13\x80\x01\x00\x01")
  value = create_cache(
    "receiver",
    NavPvtFix(True, 10, 1, 2, 3, 100, 100, saved_at),
    (frame,),
    saved_at,
    rtc_counter_seconds=100,
  )
  save_cache(path, value)


@pytest.mark.parametrize("primary_state", ["missing", "corrupt"])
def test_restore_cached_gps_time_uses_previous_fallback(monkeypatch, tmp_path, primary_state):
  primary = tmp_path / "navigation_cache.json"
  previous = tmp_path / "navigation_cache_previous.json"
  cached_time = datetime.datetime(2026, 7, 3, 12, 47, 8, tzinfo=datetime.UTC)
  if primary_state == "corrupt":
    primary.write_text("corrupt")
  write_cache(previous, cached_time)
  monkeypatch.setattr(manager, "PC", False)
  monkeypatch.setattr(manager, "GPS_ASSISTANCE_CACHE_PATH", primary)
  monkeypatch.setattr(manager.datetime, "datetime", FrozenDateTime)
  monkeypatch.setattr(manager, "read_rtc_counter_seconds", lambda: 100)
  monkeypatch.setattr(manager, "min_date", lambda: datetime.datetime(2025, 1, 1))
  calls = []
  monkeypatch.setattr(manager, "set_system_time", lambda value: calls.append(value) or True)

  manager.restore_cached_gps_time()

  assert calls == [cached_time]


@pytest.mark.parametrize("malformed", [[], "cache", 1, True, None, {"database": []}])
def test_malformed_cache_never_blocks_manager_init(monkeypatch, tmp_path, malformed):
  primary = tmp_path / "navigation_cache.json"
  primary.write_text(json.dumps(malformed))
  monkeypatch.setattr(manager, "PC", False)
  monkeypatch.setattr(manager, "GPS_ASSISTANCE_CACHE_PATH", primary)
  monkeypatch.setattr(manager, "read_rtc_counter_seconds", lambda: 100)

  class ReachedBootlog(Exception):
    pass

  monkeypatch.setattr(
    manager, "save_bootlog", lambda: (_ for _ in ()).throw(ReachedBootlog),
  )
  with pytest.raises(ReachedBootlog):
    manager.manager_init()


@pytest.mark.parametrize("case", [
  "position_list",
  "database_null",
  "quality_scalar",
  "complete_false",
  "boolean_count",
  "numeric_string",
  "non_string_hash",
  "non_string_fingerprint",
  "non_string_base64",
])
def test_malformed_nested_cache_never_blocks_manager_init(monkeypatch, tmp_path, case):
  primary = tmp_path / "navigation_cache.json"
  write_cache(primary, datetime.datetime(2026, 7, 3, 12, 47, 8, tzinfo=datetime.UTC))
  raw = json.loads(primary.read_text())
  if case == "position_list":
    raw["position"] = []
  elif case == "database_null":
    raw["database"] = None
  elif case == "quality_scalar":
    raw["quality"] = 1
  elif case == "complete_false":
    raw["database"]["complete"] = False
  elif case == "boolean_count":
    raw["database"]["message_count"] = True
  elif case == "numeric_string":
    raw["position"]["latitude_e7"] = "1"
  elif case == "non_string_hash":
    raw["database"]["sha256"] = 1
  elif case == "non_string_fingerprint":
    raw["receiver_fingerprint"] = 1
  elif case == "non_string_base64":
    raw["database"]["ubx_base64"] = 1
  primary.write_text(json.dumps(raw))
  monkeypatch.setattr(manager, "PC", False)
  monkeypatch.setattr(manager, "GPS_ASSISTANCE_CACHE_PATH", primary)
  monkeypatch.setattr(manager, "read_rtc_counter_seconds", lambda: 100)

  class ReachedBootlog(Exception):
    pass

  monkeypatch.setattr(
    manager, "save_bootlog", lambda: (_ for _ in ()).throw(ReachedBootlog),
  )
  with pytest.raises(ReachedBootlog):
    manager.manager_init()
