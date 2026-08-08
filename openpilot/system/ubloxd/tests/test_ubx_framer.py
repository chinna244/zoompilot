from __future__ import annotations

import pytest

from openpilot.system.ubloxd.gps_assistance import add_ubx_checksum
from openpilot.system.ubloxd.ubloxd import MAX_UBX_PAYLOAD_BYTES, UbxFramer


def valid_frame(message_class: int, message_id: int, payload: bytes) -> bytes:
  return add_ubx_checksum(b"\xb5\x62" + bytes((message_class, message_id)) + len(payload).to_bytes(2, "little") + payload)


def corrupt_checksum(frame: bytes) -> bytes:
  bad = bytearray(frame)
  bad[-1] ^= 0xFF
  return bytes(bad)


@pytest.fixture
def sample_frame() -> bytes:
  return valid_frame(0x01, 0x07, bytes(range(32)))


def test_full_frame_one_chunk(sample_frame: bytes) -> None:
  framer = UbxFramer()
  assert framer.add_data(1.0, sample_frame) == [sample_frame]
  assert framer.frames_valid == 1
  assert framer.buf == b""


@pytest.mark.parametrize("split_at", range(40))
def test_every_two_chunk_byte_boundary(sample_frame: bytes, split_at: int) -> None:
  if split_at > len(sample_frame):
    pytest.skip("split beyond frame")
  framer = UbxFramer()
  first = framer.add_data(1.0, sample_frame[:split_at])
  second = framer.add_data(2.0, sample_frame[split_at:])
  assert first + second == [sample_frame]
  assert framer.frames_valid == 1


def test_byte_by_byte_feed(sample_frame: bytes) -> None:
  framer = UbxFramer()
  out: list[bytes] = []
  for index, byte in enumerate(sample_frame):
    out.extend(framer.add_data(float(index), bytes((byte,))))
  assert out == [sample_frame]


def test_b5_62_split_across_reads(sample_frame: bytes) -> None:
  framer = UbxFramer()
  assert sample_frame[0] == 0xB5
  assert sample_frame[1] == 0x62
  assert framer.add_data(1.0, sample_frame[:1]) == []
  assert framer.buf == b"\xb5"
  assert framer.add_data(2.0, sample_frame[1:]) == [sample_frame]


def test_trailing_b5_after_garbage_then_62(sample_frame: bytes) -> None:
  framer = UbxFramer()
  assert framer.add_data(1.0, b"\x00\x01\x02\xb5") == []
  assert framer.buf == b"\xb5"
  assert framer.add_data(2.0, sample_frame[1:]) == [sample_frame]


def test_b5_b5_62_recovers_valid_frame(sample_frame: bytes) -> None:
  framer = UbxFramer()
  assert framer.add_data(1.0, b"\xb5" + sample_frame) == [sample_frame]


def test_garbage_before_frame(sample_frame: bytes) -> None:
  framer = UbxFramer()
  assert framer.add_data(1.0, b"noise!!" + sample_frame) == [sample_frame]
  assert framer.discarded_prefix_bytes == len(b"noise!!")


def test_partial_header_then_rest(sample_frame: bytes) -> None:
  framer = UbxFramer()
  assert framer.add_data(1.0, sample_frame[:4]) == []
  assert framer.add_data(2.0, sample_frame[4:]) == [sample_frame]


def test_partial_payload_then_rest(sample_frame: bytes) -> None:
  framer = UbxFramer()
  assert framer.add_data(1.0, sample_frame[:10]) == []
  assert framer.add_data(2.0, sample_frame[10:]) == [sample_frame]


def test_partial_checksum_then_rest(sample_frame: bytes) -> None:
  framer = UbxFramer()
  assert framer.add_data(1.0, sample_frame[:-1]) == []
  assert framer.add_data(2.0, sample_frame[-1:]) == [sample_frame]


def test_checksum_bad_then_valid(sample_frame: bytes) -> None:
  framer = UbxFramer()
  assert framer.add_data(1.0, corrupt_checksum(sample_frame) + sample_frame) == [sample_frame]
  assert framer.checksum_failures >= 1
  assert framer.frames_valid == 1


def test_huge_advertised_length_then_valid_resyncs_promptly(sample_frame: bytes) -> None:
  # Corrupt header advertising payload far above MAX_UBX_PAYLOAD_BYTES, immediately
  # followed by a valid frame. Framer must not wait for the bogus payload.
  huge = bytearray(b"\xb5\x62\x01\x07\xff\xff")  # length 65535
  framer = UbxFramer()
  out = framer.add_data(1.0, bytes(huge) + sample_frame)
  assert out == [sample_frame]
  assert framer.oversized_frame_rejects >= 1
  assert framer.frames_valid == 1


def test_length_at_max_payload_accepted() -> None:
  payload = b"\x00" * MAX_UBX_PAYLOAD_BYTES
  frame = valid_frame(0x01, 0x07, payload)
  framer = UbxFramer()
  assert framer.add_data(1.0, frame) == [frame]
  assert framer.oversized_frame_rejects == 0


def test_length_just_over_max_rejected_without_emit() -> None:
  # Craft header-only oversized candidate; do not wait for payload.
  header = b"\xb5\x62\x01\x07" + (MAX_UBX_PAYLOAD_BYTES + 1).to_bytes(2, "little")
  framer = UbxFramer()
  assert framer.add_data(1.0, header) == []
  assert framer.oversized_frame_rejects == 1
  # Buffer should not retain the full oversized wait; sync1 dropped for resync.
  assert len(framer.buf) < 6 or framer.buf[:2] != b"\xb5\x62"


def test_multiple_frames_one_chunk(sample_frame: bytes) -> None:
  other = valid_frame(0x0A, 0x09, b"\x11" * 8)
  framer = UbxFramer()
  assert framer.add_data(1.0, sample_frame + other) == [sample_frame, other]


def test_back_to_back_split_at_awkward_boundary(sample_frame: bytes) -> None:
  other = valid_frame(0x0A, 0x0B, b"\x22" * 12)
  stream = sample_frame + other
  split = len(sample_frame) - 3
  framer = UbxFramer()
  first = framer.add_data(1.0, stream[:split])
  second = framer.add_data(2.0, stream[split:])
  assert first + second == [sample_frame, other]


def test_empty_incoming_noop(sample_frame: bytes) -> None:
  framer = UbxFramer()
  assert framer.add_data(1.0, b"") == []
  assert framer.add_data(2.0, sample_frame) == [sample_frame]
