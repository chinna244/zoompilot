from __future__ import annotations

from struct import pack
from unittest.mock import MagicMock

from openpilot.system.qcomgpsd.modemdiag import (
  DIAG_NV_READ_F,
  DIAG_NV_WRITE_F,
  NV_FULL_RESPONSE_LEN,
  DiagCommandError,
)
from openpilot.system.qcomgpsd.qcomgpsd import (
  NV_GNSS_OEM_FEATURE_MASK,
  _nv_item_match,
  ensure_gnss_oem_feature_mask,
  parse_nv_uint32_response,
)


def _short_nv(item: int, value: int) -> bytes:
  return pack("<HI", item, value)


def _full_nv(item: int, value: int, status: int = 0) -> bytes:
  data = bytearray(128)
  data[0:4] = pack("<I", value)
  return pack("<H", item) + bytes(data) + pack("<H", status)


def test_nv_match_requires_opcode_and_item():
  read_match = _nv_item_match(DIAG_NV_READ_F, 7165)
  write_match = _nv_item_match(DIAG_NV_WRITE_F, 7165)
  assert read_match(DIAG_NV_READ_F, _short_nv(7165, 1))
  assert not read_match(DIAG_NV_WRITE_F, _short_nv(7165, 1))
  assert write_match(DIAG_NV_WRITE_F, _short_nv(7165, 1))
  assert not write_match(DIAG_NV_READ_F, _short_nv(7165, 1))
  assert not read_match(DIAG_NV_READ_F, _short_nv(7166, 1))


def test_send_recv_read_ignores_stale_write(monkeypatch):
  from openpilot.system.qcomgpsd.modemdiag import ModemDiag, send_recv
  from openpilot.system.qcomgpsd.tests.helpers import FakeSerial, frame_diag

  serial = FakeSerial()
  diag = ModemDiag(serial)
  stale_write = frame_diag(DIAG_NV_WRITE_F, _full_nv(7165, 1))
  good_read = frame_diag(DIAG_NV_READ_F, _full_nv(7165, 1))
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  serial.extend_inbound(stale_write + good_read)
  opcode, payload = send_recv(
    diag,
    DIAG_NV_READ_F,
    pack("<H", 7165),
    timeout=1.0,
    match=_nv_item_match(DIAG_NV_READ_F, 7165),
  )
  assert (opcode, payload) == (DIAG_NV_READ_F, _full_nv(7165, 1))


def test_send_recv_write_ignores_stale_read(monkeypatch):
  from openpilot.system.qcomgpsd.modemdiag import ModemDiag, send_recv
  from openpilot.system.qcomgpsd.tests.helpers import FakeSerial, frame_diag

  serial = FakeSerial()
  diag = ModemDiag(serial)
  stale_read = frame_diag(DIAG_NV_READ_F, _full_nv(7165, 0))
  good_write = frame_diag(DIAG_NV_WRITE_F, _full_nv(7165, 1))
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  serial.extend_inbound(stale_read + good_write)
  opcode, payload = send_recv(
    diag,
    DIAG_NV_WRITE_F,
    _short_nv(7165, 1),
    timeout=1.0,
    match=_nv_item_match(DIAG_NV_WRITE_F, 7165),
  )
  assert (opcode, payload) == (DIAG_NV_WRITE_F, _full_nv(7165, 1))


def test_statusless_six_byte_response_rejected():
  payload = _short_nv(7165, 1)
  assert len(payload) == 6
  assert (
    parse_nv_uint32_response(
      DIAG_NV_READ_F,
      payload,
      expected_opcode=DIAG_NV_READ_F,
      item_id=7165,
    )
    is None
  )


def test_truncated_intermediate_rejected():
  payload = _short_nv(7165, 1) + b"\x00\x00"
  assert (
    parse_nv_uint32_response(
      DIAG_NV_READ_F,
      payload,
      expected_opcode=DIAG_NV_READ_F,
      item_id=7165,
    )
    is None
  )


def test_wrong_item_or_opcode_rejected():
  assert (
    parse_nv_uint32_response(
      DIAG_NV_WRITE_F,
      _full_nv(7165, 1),
      expected_opcode=DIAG_NV_READ_F,
      item_id=7165,
    )
    is None
  )
  assert (
    parse_nv_uint32_response(
      DIAG_NV_READ_F,
      _full_nv(7166, 1),
      expected_opcode=DIAG_NV_READ_F,
      item_id=7165,
    )
    is None
  )


def test_full_nv_status_nonzero_rejected():
  payload = _full_nv(7165, 1, status=5)
  assert len(payload) == NV_FULL_RESPONSE_LEN
  assert (
    parse_nv_uint32_response(
      DIAG_NV_READ_F,
      payload,
      expected_opcode=DIAG_NV_READ_F,
      item_id=7165,
    )
    is None
  )


def test_full_nv_status_ok_accepted():
  payload = _full_nv(7165, 1, status=0)
  assert (
    parse_nv_uint32_response(
      DIAG_NV_READ_F,
      payload,
      expected_opcode=DIAG_NV_READ_F,
      item_id=7165,
    )
    == 1
  )


def test_ensure_never_writes_when_mask_semantics_unproven(monkeypatch):
  calls: list[int] = []

  def fake_send_recv(diag, opcode, payload, **_kwargs):
    calls.append(opcode)
    if opcode == DIAG_NV_READ_F:
      return DIAG_NV_READ_F, _full_nv(NV_GNSS_OEM_FEATURE_MASK, 0)
    raise AssertionError("NV write must not happen when DRE bit unproven")

  monkeypatch.setattr("openpilot.system.qcomgpsd.qcomgpsd.send_recv", fake_send_recv)
  result = ensure_gnss_oem_feature_mask(MagicMock())
  assert result.wrote is False
  assert result.verified is False
  assert result.degraded is True
  assert result.value == 0
  assert DIAG_NV_WRITE_F not in calls


def test_ensure_never_writes_current_zero(monkeypatch):
  def fake_send_recv(diag, opcode, payload, **_kwargs):
    if opcode == DIAG_NV_READ_F:
      return DIAG_NV_READ_F, _full_nv(NV_GNSS_OEM_FEATURE_MASK, 0)
    raise AssertionError("write forbidden")

  monkeypatch.setattr("openpilot.system.qcomgpsd.qcomgpsd.send_recv", fake_send_recv)
  result = ensure_gnss_oem_feature_mask(MagicMock())
  assert result.wrote is False
  assert result.degraded is True


def test_ensure_never_writes_when_unrelated_bits_set(monkeypatch):
  unrelated = 0x0000_00A5

  def fake_send_recv(diag, opcode, payload, **_kwargs):
    if opcode == DIAG_NV_READ_F:
      return DIAG_NV_READ_F, _full_nv(NV_GNSS_OEM_FEATURE_MASK, unrelated)
    raise AssertionError("write would clobber unrelated bits")

  monkeypatch.setattr("openpilot.system.qcomgpsd.qcomgpsd.send_recv", fake_send_recv)
  result = ensure_gnss_oem_feature_mask(MagicMock())
  assert result.wrote is False
  assert result.value == unrelated
  assert result.degraded is True


def test_ensure_never_writes_on_short_or_malformed_read(monkeypatch):
  def fake_send_recv(diag, opcode, payload, **_kwargs):
    if opcode == DIAG_NV_READ_F:
      return DIAG_NV_READ_F, _short_nv(NV_GNSS_OEM_FEATURE_MASK, 1)
    raise AssertionError("write forbidden")

  monkeypatch.setattr("openpilot.system.qcomgpsd.qcomgpsd.send_recv", fake_send_recv)
  result = ensure_gnss_oem_feature_mask(MagicMock())
  assert result.wrote is False
  assert result.value is None
  assert result.degraded is True


def test_explicit_modem_error_never_writes(monkeypatch):
  def boom(*_a, **_k):
    raise DiagCommandError(19, b"")

  monkeypatch.setattr("openpilot.system.qcomgpsd.qcomgpsd.send_recv", boom)
  result = ensure_gnss_oem_feature_mask(MagicMock())
  assert result.wrote is False
  assert result.verified is False
  assert result.degraded is True
