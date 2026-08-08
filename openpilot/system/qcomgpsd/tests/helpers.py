"""Helpers for deterministic QCOM DIAG/AT unit tests."""

from __future__ import annotations

from collections import deque
from typing import Any

from openpilot.system.qcomgpsd.modemdiag import ModemDiag
from openpilot.system.qcomgpsd.qcom_position import (
  POS_SOURCE_KALMAN,
  RELIABILITY_MEDIUM,
)


class FakeSerial:
  """Minimal serial stand-in used by ModemDiag unit tests."""

  def __init__(self, inbound: bytes = b"") -> None:
    self._inbound = deque(inbound)
    self.written = bytearray()
    self.fd = 3

  def extend_inbound(self, data: bytes) -> None:
    self._inbound.extend(data)

  def write(self, data: bytes) -> int:
    self.written.extend(data)
    return len(data)

  def read(self, size: int = 1) -> bytes:
    out = bytearray()
    while self._inbound and len(out) < size:
      out.append(self._inbound.popleft())
    return bytes(out)

  def flush(self) -> None:
    return None

  def reset_input_buffer(self) -> None:
    self._inbound.clear()

  def reset_output_buffer(self) -> None:
    self.written.clear()


def frame_diag(opcode: int, payload: bytes = b"") -> bytes:
  body = bytes([opcode]) + payload
  return ModemDiag(FakeSerial()).hdlc_encapsulate(body)


def corrupt_crc(frame: bytes) -> bytes:
  mutated = bytearray(frame)
  if len(mutated) > 3:
    mutated[1] ^= 0xFF
  return bytes(mutated)


def valid_position_report(**overrides: Any) -> dict[str, Any]:
  report: dict[str, Any] = {
    "u_Version": 0,
    "u_PosSource": POS_SOURCE_KALMAN,
    "u_FailureCode": 0,
    "w_GpsWeekNumber": 2300,
    "q_GpsFixTimeMs": 123456.0,
    "t_DblFinalPosLatLon[0]": 0.65,
    "t_DblFinalPosLatLon[1]": -2.1,
    "q_FltFinalPosAlt": 120.5,
    "q_FltHeadingRad": 0.25,
    "q_FltHeadingUncRad": 0.05,
    "q_FltVelEnuMps[0]": 1.0,
    "q_FltVelEnuMps[1]": 2.0,
    "q_FltVelEnuMps[2]": 0.1,
    "q_FltVelSigmaMps[0]": 0.2,
    "q_FltVelSigmaMps[1]": 0.3,
    "q_FltVelSigmaMps[2]": 0.4,
    "q_FltVdop": 1.5,
    "q_FltEllipseSemimajorAxis": 4.0,
    "q_FltEllipseSemiminorAxis": 2.0,
    "q_FltPosSigmaVertical": 3.5,
    "u_HorizontalReliability": RELIABILITY_MEDIUM,
    "u_VerticalReliability": RELIABILITY_MEDIUM,
    "u_NumGpsSvsUsed": 6,
    "u_NumGloSvsUsed": 2,
    "u_NumBdsSvsUsed": 0,
  }
  report.update(overrides)
  return report
