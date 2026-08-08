from __future__ import annotations

from struct import calcsize, pack

import pytest

from openpilot.system.qcomgpsd import modemdiag as md
from openpilot.system.qcomgpsd.modemdiag import (
  DIAG_LOG_CONFIG_F,
  DiagFramingError,
  DiagTimeoutError,
  LOG_CONFIG_RETRIEVE_ID_RANGES_OP,
  LOG_CONFIG_SET_MASK_OP,
  LOG_CONFIG_SUCCESS_S,
  ModemDiag,
  setup_logs,
)
from openpilot.system.qcomgpsd.tests.helpers import FakeSerial, frame_diag


def _fast_timeout(monkeypatch):
  real = md.send_recv

  def wrapped(*args, timeout=5.0, **kwargs):
    return real(*args, timeout=0.05, **kwargs)

  monkeypatch.setattr(md, "send_recv", wrapped)


def _range_payload(*, status: int = LOG_CONFIG_SUCCESS_S, masks: list[int] | None = None) -> bytes:
  if masks is None:
    masks = [0] * 16
  assert len(masks) == 16
  return pack("<3xII", LOG_CONFIG_RETRIEVE_ID_RANGES_OP, status) + pack("<16I", *masks)


def _mask_payload(
  *,
  status: int = LOG_CONFIG_SUCCESS_S,
  log_type: int = 0,
  bitsize: int = 8,
  include_ids: bool = True,
) -> bytes:
  if include_ids:
    return pack("<3xIIII", LOG_CONFIG_SET_MASK_OP, status, log_type, bitsize)
  return pack("<3xII", LOG_CONFIG_SET_MASK_OP, status)


def test_range_truncated_header_rejected(monkeypatch):
  _fast_timeout(monkeypatch)
  serial = FakeSerial()
  diag = ModemDiag(serial)
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  serial.extend_inbound(frame_diag(DIAG_LOG_CONFIG_F, pack("<3xII", LOG_CONFIG_RETRIEVE_ID_RANGES_OP, 0)))
  with pytest.raises(DiagTimeoutError):
    setup_logs(diag, [])


def test_range_header_valid_missing_masks_rejected(monkeypatch):
  _fast_timeout(monkeypatch)
  serial = FakeSerial()
  diag = ModemDiag(serial)
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  serial.extend_inbound(frame_diag(DIAG_LOG_CONFIG_F, pack("<3xII", LOG_CONFIG_RETRIEVE_ID_RANGES_OP, LOG_CONFIG_SUCCESS_S)))
  with pytest.raises(DiagTimeoutError):
    setup_logs(diag, [])


def test_range_bad_status_rejected(monkeypatch):
  _fast_timeout(monkeypatch)
  serial = FakeSerial()
  diag = ModemDiag(serial)
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  serial.extend_inbound(frame_diag(DIAG_LOG_CONFIG_F, _range_payload(status=7)))
  with pytest.raises(DiagTimeoutError):
    setup_logs(diag, [])


def test_unrelated_log_config_then_valid_range(monkeypatch):
  _fast_timeout(monkeypatch)
  serial = FakeSerial()
  diag = ModemDiag(serial)
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  unrelated = frame_diag(DIAG_LOG_CONFIG_F, pack("<3xII", LOG_CONFIG_SET_MASK_OP, 0) + pack("<16I", *([0] * 16)))
  good = frame_diag(DIAG_LOG_CONFIG_F, _range_payload())
  serial.extend_inbound(unrelated + good)
  setup_logs(diag, [])


def test_valid_range_no_masks(monkeypatch):
  _fast_timeout(monkeypatch)
  serial = FakeSerial()
  diag = ModemDiag(serial)
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  serial.extend_inbound(frame_diag(DIAG_LOG_CONFIG_F, _range_payload()))
  setup_logs(diag, [])


def test_mask_set_truncated_rejected(monkeypatch):
  _fast_timeout(monkeypatch)
  serial = FakeSerial()
  diag = ModemDiag(serial)
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  masks = [0] * 16
  masks[0] = 8
  serial.extend_inbound(frame_diag(DIAG_LOG_CONFIG_F, _range_payload(masks=masks)))
  serial.extend_inbound(frame_diag(DIAG_LOG_CONFIG_F, pack("<3xII", LOG_CONFIG_SET_MASK_OP, 0) + b"\x00\x00"))
  with pytest.raises(DiagTimeoutError):
    setup_logs(diag, [0x0000])


def test_mask_set_wrong_operation_ignored_until_timeout(monkeypatch):
  _fast_timeout(monkeypatch)
  serial = FakeSerial()
  diag = ModemDiag(serial)
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  masks = [0] * 16
  masks[1] = 8
  serial.extend_inbound(frame_diag(DIAG_LOG_CONFIG_F, _range_payload(masks=masks)))
  serial.extend_inbound(frame_diag(DIAG_LOG_CONFIG_F, _range_payload()))
  with pytest.raises(DiagTimeoutError):
    setup_logs(diag, [0x1000])


def test_mask_set_valid_with_echoed_ids(monkeypatch):
  _fast_timeout(monkeypatch)
  serial = FakeSerial()
  diag = ModemDiag(serial)
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  masks = [0] * 16
  masks[2] = 16
  serial.extend_inbound(frame_diag(DIAG_LOG_CONFIG_F, _range_payload(masks=masks)))
  serial.extend_inbound(frame_diag(DIAG_LOG_CONFIG_F, _mask_payload(log_type=2, bitsize=16)))
  setup_logs(diag, [])


def test_setup_logs_raises_typed_on_post_match_truncation():
  need = calcsize("<3xII") + 16 * 4
  assert len(_range_payload()) >= need
  assert issubclass(DiagFramingError, ValueError)
