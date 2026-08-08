from __future__ import annotations

import select
import struct
import time
from collections import deque
from collections.abc import Callable
from struct import calcsize, pack, unpack_from

from openpilot.common.serial import Serial


def _gen_crc16_reflected_table(poly: int) -> list[int]:
  # poly is the reflected form (e.g. 0x8408 for CRC-16 poly 0x1021)
  table = []
  for i in range(256):
    crc = i
    for _ in range(8):
      if crc & 1:
        crc = (crc >> 1) ^ poly
      else:
        crc >>= 1
    table.append(crc)
  return table


# CRC-16/IBM-SDLC (aka X-25 / ISO-HDLC): poly 0x1021, refin/refout, init/xor 0xFFFF
_CRC16_X25_TABLE = _gen_crc16_reflected_table(0x8408)


def crc16_x25(data: bytes) -> int:
  crc = 0xFFFF
  for b in data:
    crc = _CRC16_X25_TABLE[(crc ^ b) & 0xFF] ^ (crc >> 8)
  return crc ^ 0xFFFF


class DiagFramingError(ValueError):
  pass


class DiagTimeoutError(TimeoutError):
  pass


class DiagCommandError(RuntimeError):
  """Modem returned an explicit DIAG rejection opcode."""

  def __init__(self, opcode: int, payload: bytes = b"") -> None:
    self.opcode = opcode
    self.payload = payload
    super().__init__(f"DIAG command rejected with opcode={opcode}")


class ModemDiag:
  ESCAPE_CHAR = b"\x7d"
  TRAILER_CHAR = b"\x7e"
  MAX_RX_BUFFER = 256 * 1024
  MAX_FRAME_BYTES = 64 * 1024

  def __init__(self, serial=None):
    self.serial = serial if serial is not None else self.open_serial()
    self.pend = b""
    self._queue: deque[tuple[int, bytes]] = deque()

  def open_serial(self):
    serial = Serial(
      "/dev/ttyUSB0",
      baudrate=115200,
      rtscts=True,
      dsrdtr=True,
      timeout=0,
      exclusive=True,
    )
    serial.flush()
    serial.reset_input_buffer()
    serial.reset_output_buffer()
    return serial

  def hdlc_encapsulate(self, payload: bytes) -> bytes:
    payload = payload + pack("<H", crc16_x25(payload))
    payload = payload.replace(
      self.ESCAPE_CHAR,
      bytes([self.ESCAPE_CHAR[0], self.ESCAPE_CHAR[0] ^ 0x20]),
    )
    payload = payload.replace(
      self.TRAILER_CHAR,
      bytes([self.ESCAPE_CHAR[0], self.TRAILER_CHAR[0] ^ 0x20]),
    )
    return payload + self.TRAILER_CHAR

  def hdlc_decapsulate(self, framed: bytes) -> bytes:
    if len(framed) < 3 or framed[-1:] != self.TRAILER_CHAR:
      raise DiagFramingError("incomplete or missing DIAG trailer")
    payload = framed[:-1]
    payload = payload.replace(
      bytes([self.ESCAPE_CHAR[0], self.TRAILER_CHAR[0] ^ 0x20]),
      self.TRAILER_CHAR,
    )
    payload = payload.replace(
      bytes([self.ESCAPE_CHAR[0], self.ESCAPE_CHAR[0] ^ 0x20]),
      self.ESCAPE_CHAR,
    )
    if len(payload) < 2:
      raise DiagFramingError("DIAG frame too short for CRC")
    expected = pack("<H", crc16_x25(payload[:-2]))
    if payload[-2:] != expected:
      raise DiagFramingError("DIAG CRC mismatch")
    return payload[:-2]

  def feed(self, data: bytes = b"") -> list[tuple[int, bytes]]:
    """Append raw serial bytes and return newly validated opcode/payload pairs."""
    if data:
      self.pend += data
    if len(self.pend) > self.MAX_RX_BUFFER:
      self.pend = self.pend[-self.MAX_RX_BUFFER :]

    packets: list[tuple[int, bytes]] = []
    while True:
      trailer = self.pend.find(self.TRAILER_CHAR)
      if trailer < 0:
        if len(self.pend) > self.MAX_FRAME_BYTES:
          self.pend = b""
        break
      raw_frame = self.pend[: trailer + 1]
      self.pend = self.pend[trailer + 1 :]
      if len(raw_frame) > self.MAX_FRAME_BYTES:
        continue
      try:
        unframed = self.hdlc_decapsulate(raw_frame)
      except DiagFramingError:
        continue
      if not unframed:
        continue
      packets.append((unframed[0], unframed[1:]))
    return packets

  def recv(self, timeout: float | None = None) -> tuple[int, bytes]:
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
      if self._queue:
        return self._queue.popleft()
      for packet in self.feed():
        self._queue.append(packet)
      if self._queue:
        return self._queue.popleft()

      remaining = None
      if deadline is not None:
        remaining = deadline - time.monotonic()
        if remaining <= 0.0:
          raise DiagTimeoutError("DIAG receive timeout")
      ready, _, _ = select.select(
        [self.serial.fd],
        [],
        [],
        None if remaining is None else max(0.0, remaining),
      )
      if not ready:
        raise DiagTimeoutError("DIAG receive timeout")
      raw = self.serial.read(0x10000)
      for packet in self.feed(raw):
        self._queue.append(packet)

  def send(self, packet_type: int, packet_payload: bytes) -> None:
    self.serial.write(self.hdlc_encapsulate(bytes([packet_type]) + packet_payload))


DIAG_LOG_F = 16
DIAG_BAD_CMD_F = 19
DIAG_BAD_PARM_F = 20
DIAG_BAD_LEN_F = 21
DIAG_BAD_DEVICE_F = 22
DIAG_BAD_MODE_F = 24
DIAG_NV_READ_F = 38
DIAG_NV_WRITE_F = 39
DIAG_SUBSYS_CMD_F = 75
DIAG_LOG_CONFIG_F = 115
LOG_CONFIG_RETRIEVE_ID_RANGES_OP = 1
LOG_CONFIG_SET_MASK_OP = 3
LOG_CONFIG_SUCCESS_S = 0

DIAG_EXPLICIT_ERROR_OPCODES = frozenset(
  {
    DIAG_BAD_CMD_F,
    DIAG_BAD_PARM_F,
    DIAG_BAD_LEN_F,
    DIAG_BAD_DEVICE_F,
    DIAG_BAD_MODE_F,
  }
)

# Classic Qualcomm nv_item_type payload after opcode: item(2) + data(128) + nv_stat(2)
NV_ITEM_DATA_SIZE = 128
NV_FULL_RESPONSE_LEN = 2 + NV_ITEM_DATA_SIZE + 2
NV_STATUS_OK = 0


def send_recv(
  diag: ModemDiag,
  packet_type: int,
  packet_payload: bytes,
  *,
  timeout: float = 5.0,
  match: Callable[[int, bytes], bool] | None = None,
) -> tuple[int, bytes]:
  """Send a DIAG command and wait for a correlated response.

  Ignores unsolicited DIAG_LOG_F messages. Explicit BAD_* rejection opcodes
  raise DiagCommandError immediately. By default matches response opcode to the
  request packet_type. Optional match() can require NV item / op fields.
  """
  if match is None:

    def default_match(opcode: int, _payload: bytes) -> bool:
      return opcode == packet_type

    match = default_match

  diag.send(packet_type, packet_payload)
  deadline = time.monotonic() + timeout
  while True:
    remaining = deadline - time.monotonic()
    if remaining <= 0.0:
      raise DiagTimeoutError(f"DIAG response timeout waiting for opcode={packet_type}")
    opcode, payload = diag.recv(timeout=remaining)
    if opcode == DIAG_LOG_F:
      continue
    if opcode in DIAG_EXPLICIT_ERROR_OPCODES:
      raise DiagCommandError(opcode, payload)
    if match(opcode, payload):
      return opcode, payload
    # Unrelated non-log responses are ignored until timeout.


def setup_logs(diag: ModemDiag, types_to_log) -> None:
  range_header = "<3xII"
  range_need = calcsize(range_header) + 16 * 4

  def _range_match(op: int, pl: bytes) -> bool:
    if op != DIAG_LOG_CONFIG_F or len(pl) < range_need:
      return False
    operation, status = unpack_from(range_header, pl)
    return operation == LOG_CONFIG_RETRIEVE_ID_RANGES_OP and status == LOG_CONFIG_SUCCESS_S

  opcode, payload = send_recv(
    diag,
    DIAG_LOG_CONFIG_F,
    pack("<3xI", LOG_CONFIG_RETRIEVE_ID_RANGES_OP),
    match=_range_match,
  )
  if len(payload) < range_need:
    raise DiagFramingError("DIAG log-config range response truncated")
  try:
    operation, status = unpack_from(range_header, payload)
    log_masks = unpack_from("<16I", payload, calcsize(range_header))
  except struct.error as exc:
    raise DiagFramingError("DIAG log-config range response malformed") from exc
  if operation != LOG_CONFIG_RETRIEVE_ID_RANGES_OP or status != LOG_CONFIG_SUCCESS_S:
    raise DiagFramingError("DIAG log-config range query failed")

  mask_header = "<3xIIII"
  mask_min = calcsize("<3xII")
  mask_full = calcsize(mask_header)

  for log_type, log_mask_bitsize in enumerate(log_masks):
    if log_mask_bitsize:
      log_mask = [0] * ((log_mask_bitsize + 7) // 8)
      for i in range(log_mask_bitsize):
        if ((log_type << 12) | i) in types_to_log:
          log_mask[i // 8] |= 1 << (i % 8)

      def _mask_match(
        op: int,
        pl: bytes,
        expected_type: int = log_type,
        expected_bitsize: int = log_mask_bitsize,
      ) -> bool:
        if op != DIAG_LOG_CONFIG_F:
          return False
        if len(pl) < mask_min:
          return False
        # Reject intermediate truncation between op/status and type/bitsize.
        if mask_min < len(pl) < mask_full:
          return False
        operation, status = unpack_from("<3xII", pl)
        if operation != LOG_CONFIG_SET_MASK_OP or status != LOG_CONFIG_SUCCESS_S:
          return False
        if len(pl) >= mask_full:
          _op, _st, echoed_type, echoed_bitsize = unpack_from(mask_header, pl)
          return echoed_type == expected_type and echoed_bitsize == expected_bitsize
        return len(pl) == mask_min

      opcode, payload = send_recv(
        diag,
        DIAG_LOG_CONFIG_F,
        pack(
          "<3xIII",
          LOG_CONFIG_SET_MASK_OP,
          log_type,
          log_mask_bitsize,
        )
        + bytes(log_mask),
        match=_mask_match,
      )
      if len(payload) < mask_min or (mask_min < len(payload) < mask_full):
        raise DiagFramingError("DIAG log-config mask-set response truncated")
      try:
        operation, status = unpack_from("<3xII", payload)
      except struct.error as exc:
        raise DiagFramingError("DIAG log-config mask-set response malformed") from exc
      if operation != LOG_CONFIG_SET_MASK_OP or status != LOG_CONFIG_SUCCESS_S:
        raise DiagFramingError("DIAG log-config mask set failed")
      if len(payload) >= mask_full:
        _op, _st, echoed_type, echoed_bitsize = unpack_from(mask_header, payload)
        if echoed_type != log_type or echoed_bitsize != log_mask_bitsize:
          raise DiagFramingError("DIAG log-config mask-set response mismatch")
