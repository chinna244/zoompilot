from __future__ import annotations

from struct import pack

import pytest

from openpilot.system.qcomgpsd.modemdiag import (
  DIAG_BAD_CMD_F,
  DIAG_BAD_LEN_F,
  DIAG_BAD_PARM_F,
  DIAG_LOG_F,
  DIAG_NV_READ_F,
  DIAG_SUBSYS_CMD_F,
  DiagCommandError,
  DiagFramingError,
  DiagTimeoutError,
  ModemDiag,
  send_recv,
)
from openpilot.system.qcomgpsd.qcomgpsd import (
  CGPS_DIAG_PDAPI_CMD,
  CGPS_OEM_CONTROL,
  DIAG_SUBSYS_GPS,
  _gps_oem_control_match,
)
from openpilot.system.qcomgpsd.tests.helpers import FakeSerial, corrupt_crc, frame_diag


def test_full_valid_packet():
  frame = frame_diag(DIAG_NV_READ_F, b"\x01\x02")
  diag = ModemDiag(FakeSerial())
  packets = diag.feed(frame)
  assert packets == [(DIAG_NV_READ_F, b"\x01\x02")]


@pytest.mark.parametrize("split_at", range(1, 8))
def test_split_at_boundaries(split_at: int):
  frame = frame_diag(0x42, b"abcd")
  diag = ModemDiag(FakeSerial())
  assert diag.feed(frame[:split_at]) == []
  packets = diag.feed(frame[split_at:])
  assert packets == [(0x42, b"abcd")]


def test_byte_by_byte():
  frame = frame_diag(0x11, b"zz")
  diag = ModemDiag(FakeSerial())
  got = []
  for i in range(len(frame)):
    got.extend(diag.feed(frame[i : i + 1]))
  assert got == [(0x11, b"zz")]


def test_corrupted_crc_then_valid_packet():
  bad = corrupt_crc(frame_diag(0x10, b"bad"))
  good = frame_diag(DIAG_NV_READ_F, b"ok")
  diag = ModemDiag(FakeSerial())
  packets = diag.feed(bad + good)
  assert packets == [(DIAG_NV_READ_F, b"ok")]


def test_invalid_trailer_garbage_then_valid():
  garbage = b"\x00\x01\x02\x7e"
  good = frame_diag(0x55, b"hi")
  diag = ModemDiag(FakeSerial())
  packets = diag.feed(garbage + good)
  assert packets == [(0x55, b"hi")]


def test_multiple_valid_packets():
  frames = frame_diag(1, b"a") + frame_diag(2, b"b") + frame_diag(3, b"c")
  diag = ModemDiag(FakeSerial())
  packets = diag.feed(frames)
  assert packets == [(1, b"a"), (2, b"b"), (3, b"c")]


def test_partial_retained():
  frame = frame_diag(9, b"partial")
  diag = ModemDiag(FakeSerial())
  assert diag.feed(frame[:-1]) == []
  assert diag.pend == frame[:-1]
  assert diag.feed(frame[-1:]) == [(9, b"partial")]


def test_oversized_bogus_packet_bounded():
  diag = ModemDiag(FakeSerial())
  diag.feed(b"\x00" * (ModemDiag.MAX_FRAME_BYTES + 10))
  assert len(diag.pend) <= ModemDiag.MAX_FRAME_BYTES
  good = frame_diag(7, b"x")
  assert diag.feed(good) == [(7, b"x")]


def test_escaped_payload_roundtrip():
  payload = b"\x7d\x7e\x00"
  frame = frame_diag(0x20, payload)
  assert b"\x7d" in frame
  diag = ModemDiag(FakeSerial())
  assert diag.feed(frame) == [(0x20, payload)]


def test_hdlc_decapsulate_rejects_bad_crc():
  diag = ModemDiag(FakeSerial())
  with pytest.raises(DiagFramingError):
    diag.hdlc_decapsulate(corrupt_crc(frame_diag(1, b"z")))


@pytest.mark.parametrize("bad_op", [DIAG_BAD_CMD_F, DIAG_BAD_PARM_F, DIAG_BAD_LEN_F])
def test_explicit_diag_errors_fail_immediately(monkeypatch, bad_op: int):
  serial = FakeSerial()
  diag = ModemDiag(serial)
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  serial.extend_inbound(frame_diag(bad_op, b""))
  with pytest.raises(DiagCommandError) as exc:
    send_recv(diag, DIAG_NV_READ_F, b"", timeout=1.0)
  assert exc.value.opcode == bad_op


def test_unrelated_benign_then_match(monkeypatch):
  serial = FakeSerial()
  diag = ModemDiag(serial)
  unrelated = frame_diag(99, b"nope")
  match = frame_diag(DIAG_NV_READ_F, b"yes")
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  serial.extend_inbound(unrelated + match)
  opcode, payload = send_recv(
    diag,
    DIAG_NV_READ_F,
    b"",
    timeout=1.0,
    match=lambda op, pl: op == DIAG_NV_READ_F and pl == b"yes",
  )
  assert (opcode, payload) == (DIAG_NV_READ_F, b"yes")


def test_send_recv_skips_log_packets(monkeypatch):
  serial = FakeSerial()
  diag = ModemDiag(serial)
  log_pkt = frame_diag(DIAG_LOG_F, b"log")
  match = frame_diag(DIAG_NV_READ_F, b"ok")
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  serial.extend_inbound(log_pkt + match)
  opcode, payload = send_recv(diag, DIAG_NV_READ_F, b"", timeout=1.0)
  assert (opcode, payload) == (DIAG_NV_READ_F, b"ok")


def test_send_recv_timeout(monkeypatch):
  serial = FakeSerial()
  diag = ModemDiag(serial)
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([], [], []),
  )
  with pytest.raises(DiagTimeoutError):
    send_recv(diag, DIAG_NV_READ_F, b"", timeout=0.05)


def test_subsystem_match_ignores_unrelated_then_accepts(monkeypatch):
  serial = FakeSerial()
  diag = ModemDiag(serial)
  unrelated = frame_diag(
    DIAG_SUBSYS_CMD_F,
    pack("<BHB", DIAG_SUBSYS_GPS, 0x1111, 0x00),
  )
  stale_same_header_wrong_op = frame_diag(
    DIAG_SUBSYS_CMD_F,
    pack(
      "<BHBBII",
      DIAG_SUBSYS_GPS,
      CGPS_DIAG_PDAPI_CMD,
      CGPS_OEM_CONTROL,
      0,
      1,
      0,  # wrong state
    ),
  )
  good = frame_diag(
    DIAG_SUBSYS_CMD_F,
    pack(
      "<BHBBII",
      DIAG_SUBSYS_GPS,
      CGPS_DIAG_PDAPI_CMD,
      CGPS_OEM_CONTROL,
      0,
      1,
      1,
    ),
  )
  monkeypatch.setattr(
    "openpilot.system.qcomgpsd.modemdiag.select.select",
    lambda *_a, **_k: ([serial.fd], [], []),
  )
  serial.extend_inbound(unrelated + stale_same_header_wrong_op + good)
  opcode, payload = send_recv(
    diag,
    DIAG_SUBSYS_CMD_F,
    pack("<BHBBIIII", DIAG_SUBSYS_GPS, CGPS_DIAG_PDAPI_CMD, CGPS_OEM_CONTROL, 0, 1, 1, 0, 0),
    timeout=1.0,
    match=_gps_oem_control_match,
  )
  assert opcode == DIAG_SUBSYS_CMD_F
  assert _gps_oem_control_match(opcode, payload)


def test_oem_match_rejects_stale_version_or_feature():
  good = pack("<BHBBII", DIAG_SUBSYS_GPS, CGPS_DIAG_PDAPI_CMD, CGPS_OEM_CONTROL, 0, 1, 1)
  assert _gps_oem_control_match(DIAG_SUBSYS_CMD_F, good)
  wrong_ver = pack("<BHBBII", DIAG_SUBSYS_GPS, CGPS_DIAG_PDAPI_CMD, CGPS_OEM_CONTROL, 9, 1, 1)
  assert not _gps_oem_control_match(DIAG_SUBSYS_CMD_F, wrong_ver)
  wrong_feat = pack("<BHBBII", DIAG_SUBSYS_GPS, CGPS_DIAG_PDAPI_CMD, CGPS_OEM_CONTROL, 0, 2, 1)
  assert not _gps_oem_control_match(DIAG_SUBSYS_CMD_F, wrong_feat)
  truncated = pack("<BHB", DIAG_SUBSYS_GPS, CGPS_DIAG_PDAPI_CMD, CGPS_OEM_CONTROL)
  assert not _gps_oem_control_match(DIAG_SUBSYS_CMD_F, truncated)
