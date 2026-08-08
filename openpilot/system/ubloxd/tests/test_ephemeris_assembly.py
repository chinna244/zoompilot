from __future__ import annotations

from pathlib import Path

from openpilot.system.ubloxd import ubloxd
from openpilot.system.ubloxd.glonass import Glonass
from openpilot.system.ubloxd.gps import Gps
from openpilot.system.ubloxd.gps_assistance import add_ubx_checksum
from openpilot.system.ubloxd.ubloxd import (
  GLONASS_PARTIAL_EPHEMERIS_TTL_S,
  GPS_PARTIAL_EPHEMERIS_TTL_S,
  SFRBX_INTEGRITY_CONTRACT,
  UbloxMsgParser,
)


class BitWriter:
  def __init__(self) -> None:
    self.bits: list[int] = []

  def write(self, value: int, n: int) -> None:
    for i in range(n - 1, -1, -1):
      self.bits.append((value >> i) & 1)

  def write_bool(self, value: bool) -> None:
    self.write(1 if value else 0, 1)

  def to_bytes(self) -> bytes:
    out = bytearray()
    for i in range(0, len(self.bits), 8):
      byte = 0
      for j in range(8):
        bit = self.bits[i + j] if i + j < len(self.bits) else 0
        byte = (byte << 1) | bit
      out.append(byte)
    return bytes(out)


def gps_subframe_bytes(subframe_id: int, iod: int = 7, week: int = 100) -> bytes:
  writer = BitWriter()
  writer.write(0x8B, 8)
  writer.write(0, 14)
  writer.write_bool(False)
  writer.write_bool(False)
  writer.write(1000, 17)
  writer.write_bool(False)
  writer.write_bool(False)
  writer.write(subframe_id, 3)
  writer.write(0, 2)
  header = writer.to_bytes()
  if subframe_id == 1:
    body = BitWriter()
    body.write(week, 10)
    body.write(0, 2)
    body.write(0, 4)
    body.write(0, 6)
    body.write(0, 2)
    body.write_bool(False)
    body.write(0, 23)
    body.write(0, 24)
    body.write(0, 24)
    body.write(0, 16)
    body.write(0, 8)
    body.write(iod & 0xFF, 8)
    body.write(0, 16)
    body.write(0, 8)
    body.write(0, 16)
    body.write_bool(False)
    body.write(0, 21)
    body.write(0, 2)
    raw = header + body.to_bytes()
  elif subframe_id == 2:
    body = bytearray(24)
    body[0] = iod & 0xFF
    raw = header + bytes(body)
  else:
    body = bytearray(24)
    body[21] = iod & 0xFF
    raw = header + bytes(body)
  parsed = Gps.from_bytes(raw)
  assert parsed.how.subframe_id == subframe_id
  return raw


def glo_string_bytes(string_number: int, slot: int = 5, superframe: int = 3) -> bytes:
  writer = BitWriter()
  writer.write_bool(False)
  writer.write(string_number, 4)
  if string_number == 4:
    writer.write_bool(False)
    writer.write(0, 21)
    writer.write_bool(False)
    writer.write(0, 4)
    writer.write(0, 5)
    writer.write(0, 14)
    writer.write_bool(False)
    writer.write(0, 4)
    writer.write(0, 3)
    writer.write(1, 11)
    writer.write(slot, 5)
    writer.write(0, 2)
  else:
    writer.write(0, 72)
  writer.write(0, 8)
  writer.write(0, 11)
  writer.write(superframe, 16)
  writer.write(0, 8)
  writer.write(1, 8)
  raw = writer.to_bytes()
  parsed = Glonass.from_bytes(raw)
  assert parsed.string_number == string_number
  return raw


def subframe_to_words(subframe_30: bytes) -> list[int]:
  words: list[int] = []
  for offset in range(0, 30, 3):
    chunk = int.from_bytes(subframe_30[offset : offset + 3], "big")
    words.append(chunk << 6)
  return words


def string_to_words(string_16: bytes) -> list[int]:
  words: list[int] = []
  for offset in range(0, 16, 4):
    words.append(int.from_bytes(string_16[offset : offset + 4], "big"))
  return words


def make_sfrbx_frame(gnss_id: int, sv_id: int, freq_id: int, words: list[int]) -> bytes:
  payload = bytearray(8 + 4 * len(words))
  payload[0] = gnss_id
  payload[1] = sv_id
  payload[3] = freq_id
  payload[4] = len(words)
  for index, word in enumerate(words):
    payload[8 + 4 * index : 12 + 4 * index] = int(word).to_bytes(4, "little")
  return add_ubx_checksum(b"\xb5\x62\x02\x13" + len(payload).to_bytes(2, "little") + payload)


def _rawx_measurement(*, sv_id: int = 1, gnss_id: int = 0):
  from types import SimpleNamespace

  from openpilot.system.ubloxd.ubx import GnssType

  return SimpleNamespace(
    pr_mes=0.0,
    cp_mes=0.0,
    do_mes=0.0,
    gnss_id=GnssType(gnss_id),
    sv_id=sv_id,
    freq_id=0,
    lock_time=0,
    cno=30,
    pr_stdev=0,
    cp_stdev=0,
    do_stdev=0,
    trk_stat=0x01,
  )


def feed_rawx(
  parser: UbloxMsgParser,
  *,
  week: int,
  num_meas: int,
  mono_t: float,
) -> None:
  """Latch RAWX era evidence through the real _gen_rxm_rawx wiring path."""
  from types import SimpleNamespace
  from typing import cast

  from openpilot.system.ubloxd.ubx import Ubx

  parser.framer.last_log_time = mono_t
  meas = [_rawx_measurement(sv_id=i + 1) for i in range(max(0, num_meas))]
  msg = SimpleNamespace(
    rcv_tow=1000.0,
    week=week,
    leap_s=18,
    num_meas=num_meas,
    rec_stat=0x01,
    meas=meas,
  )
  parser._gen_rxm_rawx(cast(Ubx.RxmRawx, msg))


def feed_gps(parser: UbloxMsgParser, sv_id: int, subframe_id: int, iod: int, mono_t: float):
  parser.framer.last_log_time = mono_t
  # Real RAWX wiring supplies trusted era evidence (do not poke private state).
  if parser._latest_rawx_gps_week is None:
    feed_rawx(parser, week=2048 + 100, num_meas=1, mono_t=mono_t)
  frame = make_sfrbx_frame(0, sv_id, 0, subframe_to_words(gps_subframe_bytes(subframe_id, iod=iod)))
  return parser.parse_frame(frame)


def feed_glo(
  parser: UbloxMsgParser,
  sv_id: int,
  freq_id: int,
  string_number: int,
  mono_t: float,
  slot: int | None = None,
  superframe: int = 3,
):
  parser.framer.last_log_time = mono_t
  if slot is None:
    slot = sv_id if sv_id != 255 else 0
  frame = make_sfrbx_frame(
    6,
    sv_id,
    freq_id,
    string_to_words(glo_string_bytes(string_number, slot=slot, superframe=superframe)),
  )
  return parser.parse_frame(frame)


def test_sfrbx_integrity_contract_is_receiver_validated() -> None:
  assert SFRBX_INTEGRITY_CONTRACT == "receiver_validates_nav_words"
  # Documented reliance: host does not re-run GPS parity / GLONASS Hamming.
  source = Path(ubloxd.__file__).read_text(encoding="utf-8")
  assert "receiver_validates_nav_words" in source
  assert "UBX-13003221" in source


def test_gps_normal_complete_ephemeris_assembles() -> None:
  parser = UbloxMsgParser()
  assert feed_gps(parser, 7, 1, 9, 1.0) is None
  assert feed_gps(parser, 7, 2, 9, 2.0) is None
  result = feed_gps(parser, 7, 3, 9, 3.0)
  assert result is not None
  service, dat = result
  assert service == "ubloxGnss"
  assert dat.ubloxGnss.ephemeris.svId == 7
  assert parser.assembly_stats.gps_ephemeris_assembled == 1
  assert parser.assembly_stats.gps_subframes_received == 3


def test_gps_incomplete_does_not_assemble() -> None:
  parser = UbloxMsgParser()
  assert feed_gps(parser, 3, 1, 1, 1.0) is None
  assert feed_gps(parser, 3, 2, 1, 2.0) is None
  assert parser.assembly_stats.gps_ephemeris_assembled == 0
  assert len(parser.caches.gps_subframes[3]) == 2


def test_gps_ttl_expiration_prevents_stale_combine() -> None:
  parser = UbloxMsgParser()
  assert feed_gps(parser, 11, 1, 4, 1.0) is None
  # Beyond TTL before remaining subframes arrive.
  assert feed_gps(parser, 11, 2, 4, 1.0 + GPS_PARTIAL_EPHEMERIS_TTL_S + 1.0) is None
  assert parser.assembly_stats.gps_partial_expired == 1
  # Only subframe 2 remains after expiry+store.
  assert set(parser.caches.gps_subframes[11]) == {2}
  assert feed_gps(parser, 11, 3, 4, 1.0 + GPS_PARTIAL_EPHEMERIS_TTL_S + 2.0) is None
  assert parser.assembly_stats.gps_ephemeris_assembled == 0


def test_gps_assembles_after_ttl_with_fresh_sequence() -> None:
  parser = UbloxMsgParser()
  feed_gps(parser, 8, 1, 2, 1.0)
  feed_gps(parser, 8, 2, 2, 1.0 + GPS_PARTIAL_EPHEMERIS_TTL_S + 5.0)
  assert parser.assembly_stats.gps_partial_expired == 1
  t0 = 1.0 + GPS_PARTIAL_EPHEMERIS_TTL_S + 10.0
  assert feed_gps(parser, 8, 1, 2, t0) is None
  assert feed_gps(parser, 8, 2, 2, t0 + 1.0) is None
  result = feed_gps(parser, 8, 3, 2, t0 + 2.0)
  assert result is not None
  assert parser.assembly_stats.gps_ephemeris_assembled == 1


def test_gps_iod_mismatch_classified() -> None:
  parser = UbloxMsgParser()
  feed_gps(parser, 5, 1, 1, 1.0)
  feed_gps(parser, 5, 2, 1, 2.0)
  assert feed_gps(parser, 5, 3, 9, 3.0) is None
  assert parser.assembly_stats.gps_iod_mismatch == 1
  assert parser.assembly_stats.gps_ephemeris_assembled == 0


def test_gps_decode_failure_classified() -> None:
  parser = UbloxMsgParser()
  parser.framer.last_log_time = 1.0
  # Wrong word count -> decode failure
  frame = make_sfrbx_frame(0, 1, 0, [0, 0, 0])
  assert parser.parse_frame(frame) is None
  assert parser.assembly_stats.gps_decode_failure == 1


def test_glonass_normal_complete_ephemeris_assembles() -> None:
  parser = UbloxMsgParser()
  for string_number in range(1, 5):
    assert feed_glo(parser, 12, 7, string_number, float(string_number)) is None
  result = feed_glo(parser, 12, 7, 5, 5.0)
  assert result is not None
  _, dat = result
  assert dat.ubloxGnss.glonassEphemeris.svId == 12
  assert parser.assembly_stats.glonass_ephemeris_assembled == 1


def test_glonass_incomplete_does_not_assemble() -> None:
  parser = UbloxMsgParser()
  feed_glo(parser, 9, 4, 1, 1.0)
  feed_glo(parser, 9, 4, 2, 2.0)
  assert parser.assembly_stats.glonass_ephemeris_assembled == 0


def test_glonass_ttl_expiration_prevents_stale_combine() -> None:
  parser = UbloxMsgParser()
  feed_glo(parser, 6, 3, 1, 1.0)
  feed_glo(parser, 6, 3, 2, 1.0 + GLONASS_PARTIAL_EPHEMERIS_TTL_S + 1.0)
  assert parser.assembly_stats.glonass_partial_expired == 1
  assert set(parser.caches.glonass_strings[(6, 3)]) == {2}


def test_glonass_assembles_after_ttl_with_fresh_sequence() -> None:
  parser = UbloxMsgParser()
  feed_glo(parser, 6, 3, 1, 1.0)
  feed_glo(parser, 6, 3, 2, 1.0 + GLONASS_PARTIAL_EPHEMERIS_TTL_S + 1.0)
  t0 = 1.0 + GLONASS_PARTIAL_EPHEMERIS_TTL_S + 5.0
  for string_number in range(1, 5):
    assert feed_glo(parser, 6, 3, string_number, t0 + string_number) is None
  result = feed_glo(parser, 6, 3, 5, t0 + 5.0)
  assert result is not None
  assert parser.assembly_stats.glonass_ephemeris_assembled == 1


def test_glonass_interleaved_same_freq_different_sv_no_mix() -> None:
  parser = UbloxMsgParser()
  freq = 10
  # Interleave A and B on the same frequency channel.
  feed_glo(parser, 1, freq, 1, 1.0)
  feed_glo(parser, 2, freq, 1, 1.5)
  feed_glo(parser, 1, freq, 2, 2.0)
  feed_glo(parser, 2, freq, 2, 2.5)
  feed_glo(parser, 1, freq, 3, 3.0)
  feed_glo(parser, 2, freq, 3, 3.5)
  feed_glo(parser, 1, freq, 4, 4.0)
  feed_glo(parser, 2, freq, 4, 4.5)
  result_a = feed_glo(parser, 1, freq, 5, 5.0)
  result_b = feed_glo(parser, 2, freq, 5, 5.5)
  assert result_a is not None and result_a[1].ubloxGnss.glonassEphemeris.svId == 1
  assert result_b is not None and result_b[1].ubloxGnss.glonassEphemeris.svId == 2
  assert parser.assembly_stats.glonass_ephemeris_assembled == 2


def test_glonass_slot_mismatch_classified() -> None:
  parser = UbloxMsgParser()
  for string_number in (1, 2, 3):
    feed_glo(parser, 8, 5, string_number, float(string_number), slot=8)
  # String 4 reports a different slot than RXM-SFRBX sv_id; mismatch is checked on full assemble.
  feed_glo(parser, 8, 5, 4, 4.0, slot=15)
  assert feed_glo(parser, 8, 5, 5, 5.0, slot=8) is None
  assert parser.assembly_stats.glonass_slot_mismatch == 1
  assert parser.assembly_stats.glonass_ephemeris_assembled == 0
  assert parser.caches.glonass_strings[(8, 5)] == {}


def test_glonass_superframe_mismatch_clears_partials() -> None:
  parser = UbloxMsgParser()
  feed_glo(parser, 4, 2, 1, 1.0, superframe=1)
  feed_glo(parser, 4, 2, 2, 2.0, superframe=9)
  assert parser.assembly_stats.glonass_superframe_mismatch == 1
  assert parser.assembly_stats.glonass_timing_mismatch == 0
  assert set(parser.caches.glonass_strings[(4, 2)]) == {2}


def test_glonass_unknown_superframe_normal_sequence_assembles() -> None:
  parser = UbloxMsgParser()
  for string_number in range(1, 5):
    assert feed_glo(parser, 14, 6, string_number, 1.0 + 2.0 * (string_number - 1), superframe=0) is None
  result = feed_glo(parser, 14, 6, 5, 1.0 + 2.0 * 4, superframe=0)
  assert result is not None
  assert result[1].ubloxGnss.glonassEphemeris.svId == 14
  assert parser.assembly_stats.glonass_ephemeris_assembled == 1
  assert parser.assembly_stats.glonass_timing_mismatch == 0
  assert parser.assembly_stats.glonass_partial_expired == 0


def test_glonass_unknown_superframe_timing_clears_within_ttl() -> None:
  parser = UbloxMsgParser()
  # Stale string 1 at t=1; string 2 at t=20 is still inside the 30 s TTL.
  feed_glo(parser, 15, 6, 1, 1.0, superframe=0)
  feed_glo(parser, 15, 6, 2, 20.0, superframe=0)
  assert parser.assembly_stats.glonass_timing_mismatch == 1
  assert parser.assembly_stats.glonass_superframe_mismatch == 0
  assert parser.assembly_stats.glonass_partial_expired == 0
  assert set(parser.caches.glonass_strings[(15, 6)]) == {2}

  # Continuing 3/4/5 must not publish an ephemeris that included stale string 1.
  feed_glo(parser, 15, 6, 3, 22.0, superframe=0)
  feed_glo(parser, 15, 6, 4, 24.0, superframe=0)
  assert feed_glo(parser, 15, 6, 5, 26.0, superframe=0) is None
  assert parser.assembly_stats.glonass_ephemeris_assembled == 0
  assert 1 not in parser.caches.glonass_strings[(15, 6)]


def test_glonass_unknown_superframe_recovers_after_timing_reset() -> None:
  parser = UbloxMsgParser()
  feed_glo(parser, 16, 6, 1, 1.0, superframe=0)
  feed_glo(parser, 16, 6, 2, 20.0, superframe=0)
  assert parser.assembly_stats.glonass_timing_mismatch == 1

  t0 = 30.0
  for string_number in range(1, 5):
    assert feed_glo(parser, 16, 6, string_number, t0 + 2.0 * (string_number - 1), superframe=0) is None
  result = feed_glo(parser, 16, 6, 5, t0 + 8.0, superframe=0)
  assert result is not None
  assert parser.assembly_stats.glonass_ephemeris_assembled == 1


def test_gps_ephemeris_telemetry_fail_open(monkeypatch) -> None:
  parser = UbloxMsgParser()

  def boom(_msg: str) -> None:
    raise RuntimeError("cloudlog unavailable")

  monkeypatch.setattr(ubloxd.cloudlog, "info", boom)
  assert feed_gps(parser, 7, 1, 9, 1.0) is None
  assert feed_gps(parser, 7, 2, 9, 2.0) is None
  result = feed_gps(parser, 7, 3, 9, 3.0)
  assert result is not None
  assert result[1].ubloxGnss.ephemeris.svId == 7
  assert parser.assembly_stats.gps_ephemeris_assembled == 1
  assert parser.assembly_stats.gps_subframes_received == 3


def test_glonass_ephemeris_telemetry_fail_open(monkeypatch) -> None:
  parser = UbloxMsgParser()

  def boom(_msg: str) -> None:
    raise RuntimeError("cloudlog unavailable")

  monkeypatch.setattr(ubloxd.cloudlog, "info", boom)
  for string_number in range(1, 5):
    assert feed_glo(parser, 12, 7, string_number, float(string_number)) is None
  result = feed_glo(parser, 12, 7, 5, 5.0)
  assert result is not None
  assert result[1].ubloxGnss.glonassEphemeris.svId == 12
  assert parser.assembly_stats.glonass_ephemeris_assembled == 1
  assert parser.assembly_stats.glonass_strings_received == 5


def test_glonass_unknown_sv_does_not_publish_or_mix() -> None:
  parser = UbloxMsgParser()
  for string_number in range(1, 6):
    assert feed_glo(parser, 255, 8, string_number, float(string_number), slot=0) is None
  assert parser.assembly_stats.glonass_ephemeris_assembled == 0
  # Known SV on same freq remains independent.
  for string_number in range(1, 5):
    assert feed_glo(parser, 10, 8, string_number, 10.0 + string_number) is None
  result = feed_glo(parser, 10, 8, 5, 16.0)
  assert result is not None
  assert result[1].ubloxGnss.glonassEphemeris.svId == 10


def test_glonass_decode_failure_classified() -> None:
  parser = UbloxMsgParser()
  parser.framer.last_log_time = 1.0
  frame = make_sfrbx_frame(6, 3, 1, [0, 0])  # wrong word count
  assert parser.parse_frame(frame) is None
  assert parser.assembly_stats.glonass_decode_failure == 1


def test_rawx_nonempty_latches_trusted_week_for_ephemeris_era() -> None:
  parser = UbloxMsgParser()
  assert parser._latest_rawx_gps_week is None
  feed_rawx(parser, week=2411, num_meas=2, mono_t=1.0)
  assert parser._latest_rawx_gps_week == 2411
  week_mod = 2411 % 1024
  parser.framer.last_log_time = 2.0
  result = None
  for subframe_id in (1, 2, 3):
    frame = make_sfrbx_frame(
      0,
      5,
      0,
      subframe_to_words(gps_subframe_bytes(subframe_id, iod=7, week=week_mod)),
    )
    result = parser.parse_frame(frame)
  assert result is not None
  assert result[1].ubloxGnss.ephemeris.toeWeek == 2411


def test_live_ephemeris_far_era_from_rawx_rejected() -> None:
  parser = UbloxMsgParser()
  feed_rawx(parser, week=2411, num_meas=2, mono_t=1.0)
  assert parser._latest_rawx_gps_week == 2411
  # week_mod=100 nearest-resolves to 2148 (~5 years away) and must fail closed for LIVE ephemeris.
  parser.framer.last_log_time = 2.0
  result = None
  for subframe_id in (1, 2, 3):
    frame = make_sfrbx_frame(
      0,
      5,
      0,
      subframe_to_words(gps_subframe_bytes(subframe_id, iod=7, week=100)),
    )
    result = parser.parse_frame(frame)
  assert result is None
  assert parser.assembly_stats.gps_week_era_unresolved >= 1
  assert parser.assembly_stats.gps_week_current_mismatch >= 1


def test_live_ephemeris_week_boundary_plus_minus_one_accepted() -> None:
  parser = UbloxMsgParser()
  feed_rawx(parser, week=2411, num_meas=2, mono_t=1.0)
  for offset, sv_id in ((-1, 6), (1, 7)):
    target_week = 2411 + offset
    week_mod = target_week % 1024
    parser.framer.last_log_time = float(10 + sv_id)
    result = None
    for subframe_id in (1, 2, 3):
      frame = make_sfrbx_frame(
        0,
        sv_id,
        0,
        subframe_to_words(gps_subframe_bytes(subframe_id, iod=sv_id, week=week_mod)),
      )
      result = parser.parse_frame(frame)
    assert result is not None
    assert result[1].ubloxGnss.ephemeris.toeWeek == target_week


def test_rawx_empty_nonzero_week_does_not_establish_era() -> None:
  parser = UbloxMsgParser()
  feed_rawx(parser, week=2411, num_meas=0, mono_t=1.0)
  assert parser._latest_rawx_gps_week is None
  # Attempt assembly without feed_gps auto-RAWX latch.
  parser.framer.last_log_time = 2.0
  result = None
  for subframe_id in (1, 2, 3):
    frame = make_sfrbx_frame(0, 9, 0, subframe_to_words(gps_subframe_bytes(subframe_id, iod=7)))
    result = parser.parse_frame(frame)
  assert result is None
  assert parser.assembly_stats.gps_week_era_unresolved >= 1
  assert parser._latest_rawx_gps_week is None


def test_rawx_zero_week_does_not_establish_era() -> None:
  parser = UbloxMsgParser()
  feed_rawx(parser, week=0, num_meas=3, mono_t=1.0)
  assert parser._latest_rawx_gps_week is None


def test_ephemeris_next_era_rollover_resolves_from_rawx() -> None:
  parser = UbloxMsgParser()
  # Trusted week in 2900 era; subframe week_mod=853 → absolute 2901.
  feed_rawx(parser, week=2900, num_meas=1, mono_t=1.0)
  parser.framer.last_log_time = 2.0
  result = None
  for subframe_id in (1, 2, 3):
    frame = make_sfrbx_frame(
      0,
      11,
      0,
      subframe_to_words(gps_subframe_bytes(subframe_id, iod=4, week=853)),
    )
    result = parser.parse_frame(frame)
  assert result is not None
  assert result[1].ubloxGnss.ephemeris.toeWeek == 2901
