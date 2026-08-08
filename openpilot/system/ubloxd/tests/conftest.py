"""Shared ubloxd test isolation."""

from pathlib import Path

import pytest

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.navigation_database_restore_runtime import (
  NavigationDatabaseRestoreRuntime,
)


@pytest.fixture(autouse=True)
def isolate_implicit_dbd_runtime_state(
  tmp_path: Path,
  monkeypatch: pytest.MonkeyPatch,
) -> None:
  """Keep implicit test runtimes off the device-only /data state path."""

  class TestNavigationDatabaseRestoreRuntime(
    NavigationDatabaseRestoreRuntime
  ):
    def __init__(
      self,
      receiver_fingerprint: str,
      **kwargs,
    ) -> None:
      kwargs.setdefault(
        "state_path",
        tmp_path / "navigation_database_restore_state.json",
      )
      super().__init__(receiver_fingerprint, **kwargs)

  monkeypatch.setattr(
    pigeond,
    "NavigationDatabaseRestoreRuntime",
    TestNavigationDatabaseRestoreRuntime,
  )
