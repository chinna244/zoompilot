#!/usr/bin/env python3
"""Tests for applying the Firehose Model as the default driving model exactly once."""

from openpilot.sunnypilot.models.default_bootstrap import maybe_apply_default_model, DEFAULT_MODEL_BUNDLE_INDEX


class FakeBundle:
  def __init__(self, index):
    self.index = index


class FakeParams:
  """Minimal dict-backed Params stand-in supporting the get/put surface used by the bootstrap."""
  def __init__(self, initial=None):
    self._store = dict(initial or {})

  def get(self, key):
    return self._store.get(key)

  def put(self, key, val):
    self._store[key] = val

  def get_bool(self, key):
    return bool(self._store.get(key, False))

  def put_bool(self, key, val):
    self._store[key] = bool(val)


def _available_with_fm():
  return [FakeBundle(0), FakeBundle(DEFAULT_MODEL_BUNDLE_INDEX), FakeBundle(5)]


class TestDefaultModelBootstrap:
  def test_queues_firehose_when_nothing_active(self):
    params = FakeParams()
    maybe_apply_default_model(params, _available_with_fm())
    assert params.get("ModelManager_DownloadIndex") == DEFAULT_MODEL_BUNDLE_INDEX
    assert params.get_bool("DefaultModelApplied") is True

  def test_offline_is_noop_and_retries(self):
    # FM not yet in the available list (offline / not fetched) -> no marker, retried next loop.
    params = FakeParams()
    maybe_apply_default_model(params, [FakeBundle(0), FakeBundle(5)])
    assert params.get("ModelManager_DownloadIndex") is None
    assert params.get_bool("DefaultModelApplied") is False
    # once FM appears, it gets queued
    maybe_apply_default_model(params, _available_with_fm())
    assert params.get("ModelManager_DownloadIndex") == DEFAULT_MODEL_BUNDLE_INDEX
    assert params.get_bool("DefaultModelApplied") is True

  def test_existing_active_bundle_is_respected(self):
    params = FakeParams({"ModelManager_ActiveBundle": {"index": 7}})
    maybe_apply_default_model(params, _available_with_fm())
    assert params.get("ModelManager_DownloadIndex") is None  # not overridden
    assert params.get_bool("DefaultModelApplied") is True     # marked so we never touch it again

  def test_applied_once_then_left_alone(self):
    params = FakeParams({"DefaultModelApplied": True})
    maybe_apply_default_model(params, _available_with_fm())
    assert params.get("ModelManager_DownloadIndex") is None
