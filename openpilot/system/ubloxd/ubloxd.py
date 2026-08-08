#!/usr/bin/env python3
import math
import capnp
import calendar
import numpy as np
from collections import defaultdict
from dataclasses import dataclass

from openpilot.cereal import log
from openpilot.cereal import messaging
from openpilot.common.swaglog import cloudlog
from openpilot.system.ubloxd.rf_observability import UbloxRfObservability
from openpilot.common.gps_time import (
  encode_ublox_gps_flags,
  live_ephemeris_week_matches_rawx_current,
  rawx_full_week_is_trusted_era_evidence,
  resolve_gps_week_mod_1024,
  ublox_nav_pvt_has_fix,
)
from openpilot.system.ubloxd.ubx import Ubx
from openpilot.system.ubloxd.gps import Gps
from openpilot.system.ubloxd.glonass import Glonass


SECS_IN_MIN = 60
SECS_IN_HR = 60 * SECS_IN_MIN
SECS_IN_DAY = 24 * SECS_IN_HR
SECS_IN_WEEK = 7 * SECS_IN_DAY

# Live-stream UBX payload ceiling. Messages we parse top out well below this
# (RXM-RAWX ~2KiB at 64 meas, NAV-SAT ~3KiB at 255 SVs). A corrupt 16-bit length
# must not stall resync by waiting for up to ~65KiB.
MAX_UBX_PAYLOAD_BYTES = 4096

# GPS L1 C/A subframe period is 6s; pages 1-3 that form an ephemeris repeat about
# every 30s. Allow ~3 full cycles before discarding incomplete SV state.
GPS_PARTIAL_EPHEMERIS_TTL_S = 90.0

# GLONASS strings arrive every 2s; strings 1-5 for one SV complete in ~10s.
# Allow ~3 full cycles of partial assembly before expiry.
GLONASS_PARTIAL_EPHEMERIS_TTL_S = 30.0

# When superframe_number is unavailable (0), correlate partials by the expected
# 2 s string cadence using normalized monotonic arrival times. Same 10 s gate as
# the pre-PR76 C++/Python correlation path.
GLONASS_UNKNOWN_SUPERFRAME_TIMING_TOLERANCE_S = 10.0

# Ephemeris-assembly diagnostic summary interval (passive, rate-limited).
EPHEMERIS_ASSEMBLY_DIAG_INTERVAL_S = 10.0

# u-blox M8 UBX-13003221: RXM-SFRBX is emitted only after the receiver detects a
# complete subframe and all appropriate over-the-air parity checks have passed;
# GPS words are inverted by the receiver when required. Host code therefore relies
# on receiver_validates_nav_words for GPS parity / GLONASS Hamming and treats the
# UBX transport checksum as the host-side integrity check for SFRBX payloads.
SFRBX_INTEGRITY_CONTRACT = "receiver_validates_nav_words"


class UbxFramer:
  PREAMBLE1 = 0xB5
  PREAMBLE2 = 0x62
  HEADER_SIZE = 6
  CHECKSUM_SIZE = 2
  MAX_PAYLOAD_BYTES = MAX_UBX_PAYLOAD_BYTES

  def __init__(self) -> None:
    self.buf = bytearray()
    self.last_log_time = 0.0
    self.frames_valid = 0
    self.checksum_failures = 0
    self.discarded_prefix_bytes = 0
    self.resync_events = 0
    self.oversized_frame_rejects = 0

  def reset(self) -> None:
    self.buf.clear()

  @staticmethod
  def _checksum_ok(frame: bytes) -> bool:
    ck_a = 0
    ck_b = 0
    for b in frame[2:-2]:
      ck_a = (ck_a + b) & 0xFF
      ck_b = (ck_b + ck_a) & 0xFF
    return ck_a == frame[-2] and ck_b == frame[-1]

  def add_data(self, log_time: float, incoming: bytes) -> list[bytes]:
    self.last_log_time = log_time
    out: list[bytes] = []
    if not incoming:
      return out
    self.buf += incoming

    while True:
      # Need at least sync1+sync2 to search; a lone trailing 0xB5 is retained.
      if len(self.buf) < 2:
        break
      start = self.buf.find(b"\xb5\x62")
      if start < 0:
        # No complete preamble. Keep a trailing sync1 so the next chunk's 0x62
        # can complete B5 62 across serial read boundaries.
        if self.buf[-1] == self.PREAMBLE1:
          discarded = len(self.buf) - 1
          if discarded:
            self.discarded_prefix_bytes += discarded
            self.resync_events += 1
          del self.buf[:-1]
        else:
          if self.buf:
            self.discarded_prefix_bytes += len(self.buf)
            self.resync_events += 1
          self.buf.clear()
        break
      if start > 0:
        # drop garbage before preamble
        self.discarded_prefix_bytes += start
        self.resync_events += 1
        self.buf = self.buf[start:]

      if len(self.buf) < self.HEADER_SIZE:
        break

      length_le = int.from_bytes(self.buf[4:6], 'little', signed=False)
      if length_le > self.MAX_PAYLOAD_BYTES:
        # Reject oversized advertised length immediately and resync; do not wait
        # for the bogus payload to arrive.
        self.oversized_frame_rejects += 1
        self.resync_events += 1
        self.buf = self.buf[1:]
        continue

      total_len = self.HEADER_SIZE + length_le + self.CHECKSUM_SIZE
      if len(self.buf) < total_len:
        break

      candidate = bytes(self.buf[:total_len])
      if self._checksum_ok(candidate):
        self.frames_valid += 1
        out.append(candidate)
        # consume this frame
        self.buf = self.buf[total_len:]
      else:
        self.checksum_failures += 1
        self.resync_events += 1
        # drop first byte and retry
        self.buf = self.buf[1:]

    return out


def _bit(b: int, shift: int) -> bool:
  return (b & (1 << shift)) != 0


@dataclass
class EphemerisCaches:
  gps_subframes: defaultdict[int, dict[int, bytes]]
  gps_subframe_times: defaultdict[int, dict[int, float]]
  # Keyed by (sv_id, freq_id). Frequency alone is insufficient when channels are reused.
  glonass_strings: defaultdict[tuple[int, int], dict[int, bytes]]
  glonass_string_times: defaultdict[tuple[int, int], dict[int, float]]
  glonass_string_superframes: defaultdict[tuple[int, int], dict[int, int]]


@dataclass
class EphemerisAssemblyStats:
  gps_subframes_received: int = 0
  gps_ephemeris_assembled: int = 0
  gps_partial_expired: int = 0
  gps_iod_mismatch: int = 0
  gps_decode_failure: int = 0
  gps_week_era_unresolved: int = 0
  gps_week_current_mismatch: int = 0
  glonass_strings_received: int = 0
  glonass_ephemeris_assembled: int = 0
  glonass_partial_expired: int = 0
  glonass_decode_failure: int = 0
  glonass_slot_mismatch: int = 0
  glonass_superframe_mismatch: int = 0
  glonass_timing_mismatch: int = 0
  last_diag_log_t: float = float("nan")


class UbloxMsgParser:
  gpsPi = 3.1415926535898

  # user range accuracy in meters
  glonass_URA_lookup: dict[int, float] = {
    0: 1,
    1: 2,
    2: 2.5,
    3: 4,
    4: 5,
    5: 7,
    6: 10,
    7: 12,
    8: 14,
    9: 16,
    10: 32,
    11: 64,
    12: 128,
    13: 256,
    14: 512,
    15: 1024,
  }

  def __init__(self) -> None:
    self.framer = UbxFramer()
    self.caches = EphemerisCaches(
      gps_subframes=defaultdict(dict),
      gps_subframe_times=defaultdict(dict),
      glonass_strings=defaultdict(dict),
      glonass_string_times=defaultdict(dict),
      glonass_string_superframes=defaultdict(dict),
    )
    self.assembly_stats = EphemerisAssemblyStats()
    # Latest full (UInt16) RAWX GPS week — era evidence for 10-bit ephemeris weeks.
    self._latest_rawx_gps_week: int | None = None

  # Message generation entry point
  def parse_frame(
    self,
    frame: bytes,
    *,
    measurement_mono_ns: int | None = None,
  ) -> tuple[str, capnp.lib.capnp._DynamicStructBuilder] | None:
    # Quick header parse
    msg_type = int.from_bytes(frame[2:4], 'big')
    payload = frame[6:-2]
    if msg_type == 0x0107:
      body = Ubx.NavPvt.from_bytes(payload)
      mono_ns = int(measurement_mono_ns) if measurement_mono_ns is not None else 0
      return self._gen_nav_pvt(body, measurement_mono_ns=mono_ns)
    if msg_type == 0x0213:
      # Manually parse RXM-SFRBX to avoid EOF on some frames
      if len(payload) < 8:
        return None
      gnss_id = payload[0]
      sv_id = payload[1]
      freq_id = payload[3]
      num_words = payload[4]
      exp = 8 + 4 * num_words
      if exp != len(payload):
        return None
      words: list[int] = []
      off = 8
      for _ in range(num_words):
        words.append(int.from_bytes(payload[off : off + 4], 'little'))
        off += 4

      class _SfrbxView:
        def __init__(self, gid: int, sid: int, fid: int, body: list[int]):
          self.gnss_id = Ubx.GnssType(gid)
          self.sv_id = sid
          self.freq_id = fid
          self.body = body

      view = _SfrbxView(gnss_id, sv_id, freq_id, words)
      return self._gen_rxm_sfrbx(view)
    if msg_type == 0x0215:
      body = Ubx.RxmRawx.from_bytes(payload)
      return self._gen_rxm_rawx(body)
    if msg_type == 0x0A09:
      body = Ubx.MonHw.from_bytes(payload)
      return self._gen_mon_hw(body)
    if msg_type == 0x0A0B:
      body = Ubx.MonHw2.from_bytes(payload)
      return self._gen_mon_hw2(body)
    if msg_type == 0x0135:
      body = Ubx.NavSat.from_bytes(payload)
      return self._gen_nav_sat(body)
    return None

  # NAV-PVT -> gpsLocationExternal
  def _gen_nav_pvt(
    self,
    msg: Ubx.NavPvt,
    *,
    measurement_mono_ns: int,
  ) -> tuple[str, capnp.lib.capnp._DynamicStructBuilder]:
    dat = messaging.new_message('gpsLocationExternal', valid=True)
    gps = dat.gpsLocationExternal
    gps.source = log.GpsLocationData.SensorSource.ublox
    # Preserve the existing fix flags in the lower byte and expose the
    # NAV-PVT validDate/validTime bits in the upper byte.
    gps.flags = encode_ublox_gps_flags(msg.flags, msg.valid)
    # hasFix: gnssFixOk and fixType in {3D, GNSS+DR}. See ublox_nav_pvt_has_fix.
    gps.hasFix = ublox_nav_pvt_has_fix(msg.flags, msg.fix_type)
    gps.latitude = msg.lat * 1e-07
    gps.longitude = msg.lon * 1e-07
    gps.altitude = msg.height * 1e-03
    gps.speed = msg.g_speed * 1e-03
    gps.bearingDeg = msg.head_mot * 1e-5
    gps.horizontalAccuracy = msg.h_acc * 1e-03
    gps.satelliteCount = msg.num_sv

    # build UTC timestamp millis (NAV-PVT is in UTC)
    # tolerate invalid or unset date values like C++ timegm
    try:
      utc_tt = calendar.timegm((msg.year, msg.month, msg.day, msg.hour, msg.min, msg.sec, 0, 0, 0))
    except Exception:
      utc_tt = 0
    gps.unixTimestampMillis = int(utc_tt * 1e3 + (msg.nano * 1e-6))

    # Host mono at ubloxRaw framing completion — not publication time.
    gps.measurementMonoNs = int(measurement_mono_ns)

    # match C++ float32 rounding semantics exactly
    gps.vNED = [
      float(np.float32(msg.vel_n) * np.float32(1e-03)),
      float(np.float32(msg.vel_e) * np.float32(1e-03)),
      float(np.float32(msg.vel_d) * np.float32(1e-03)),
    ]
    gps.verticalAccuracy = msg.v_acc * 1e-03
    gps.speedAccuracy = msg.s_acc * 1e-03
    gps.bearingAccuracyDeg = msg.head_acc * 1e-05
    return ('gpsLocationExternal', dat)

  # RXM-SFRBX dispatch to GPS or GLONASS ephemeris.
  # Integrity: SFRBX_INTEGRITY_CONTRACT == receiver_validates_nav_words (u-blox M8).
  def _gen_rxm_sfrbx(self, msg) -> tuple[str, capnp.lib.capnp._DynamicStructBuilder] | None:
    if msg.gnss_id == Ubx.GnssType.gps:
      return self._parse_gps_ephemeris(msg)
    if msg.gnss_id == Ubx.GnssType.glonass:
      return self._parse_glonass_ephemeris(msg)
    return None

  def _maybe_log_ephemeris_assembly_stats(self, now: float) -> None:
    stats = self.assembly_stats
    if math.isfinite(stats.last_diag_log_t) and (now - stats.last_diag_log_t) < EPHEMERIS_ASSEMBLY_DIAG_INTERVAL_S:
      return
    stats.last_diag_log_t = now
    try:
      cloudlog.info(
        "ubloxd ephemeris assembly: "
        + f"gps(recv={stats.gps_subframes_received} assembled={stats.gps_ephemeris_assembled} "
        + f"expired={stats.gps_partial_expired} iod={stats.gps_iod_mismatch} decode={stats.gps_decode_failure} "
        + f"week_era={stats.gps_week_era_unresolved} week_mismatch={stats.gps_week_current_mismatch}) "
        + f"glo(recv={stats.glonass_strings_received} assembled={stats.glonass_ephemeris_assembled} "
        + f"expired={stats.glonass_partial_expired} decode={stats.glonass_decode_failure} "
        + f"slot={stats.glonass_slot_mismatch} superframe={stats.glonass_superframe_mismatch} "
        + f"timing={stats.glonass_timing_mismatch})"
      )
    except Exception:
      # Observability must never interfere with ephemeris assembly or publication.
      pass

  def _expire_gps_partials(self, sv_id: int, now: float) -> None:
    times = self.caches.gps_subframe_times[sv_id]
    if not times:
      return
    if (now - min(times.values())) > GPS_PARTIAL_EPHEMERIS_TTL_S:
      self.assembly_stats.gps_partial_expired += 1
      self.caches.gps_subframes[sv_id].clear()
      times.clear()

  def _clear_glonass_partials(self, key: tuple[int, int]) -> None:
    self.caches.glonass_strings[key].clear()
    self.caches.glonass_string_times[key].clear()
    self.caches.glonass_string_superframes[key].clear()

  def _expire_glonass_partials(self, key: tuple[int, int], now: float) -> None:
    times = self.caches.glonass_string_times[key]
    if not times:
      return
    if (now - min(times.values())) > GLONASS_PARTIAL_EPHEMERIS_TTL_S:
      self.assembly_stats.glonass_partial_expired += 1
      self._clear_glonass_partials(key)

  def _parse_gps_ephemeris(self, msg: Ubx.RxmSfrbx) -> tuple[str, capnp.lib.capnp._DynamicStructBuilder] | None:
    # body is list of 10 words; convert to 30-byte subframe (strip receiver-validated parity/padding)
    now = self.framer.last_log_time
    body = msg.body
    if len(body) != 10:
      self.assembly_stats.gps_decode_failure += 1
      self._maybe_log_ephemeris_assembly_stats(now)
      return None
    subframe_data = bytearray()
    for word in body:
      word >>= 6
      subframe_data.append((word >> 16) & 0xFF)
      subframe_data.append((word >> 8) & 0xFF)
      subframe_data.append(word & 0xFF)

    try:
      sf = Gps.from_bytes(bytes(subframe_data))
      subframe_id = sf.how.subframe_id
    except Exception:
      self.assembly_stats.gps_decode_failure += 1
      self._maybe_log_ephemeris_assembly_stats(now)
      return None
    if subframe_id < 1 or subframe_id > 3:
      return None

    self._expire_gps_partials(msg.sv_id, now)
    self.caches.gps_subframes[msg.sv_id][subframe_id] = bytes(subframe_data)
    self.caches.gps_subframe_times[msg.sv_id][subframe_id] = now
    self.assembly_stats.gps_subframes_received += 1

    if len(self.caches.gps_subframes[msg.sv_id]) != 3:
      self._maybe_log_ephemeris_assembly_stats(now)
      return None

    dat = messaging.new_message('ubloxGnss', valid=True)
    eph = dat.ubloxGnss.init('ephemeris')
    eph.svId = msg.sv_id

    iode_s2 = 0
    iode_s3 = 0
    iodc_lsb = 0
    week = 0

    try:
      # Subframe 1
      sf1 = Gps.from_bytes(self.caches.gps_subframes[msg.sv_id][1])
      s1 = sf1.body
      assert isinstance(s1, Gps.Subframe1)
      week = s1.week_no
      # 10-bit GPS week requires trustworthy era evidence (RAWX full week preferred).
      # Do not hard-code +1024/+2048 current-era magic (fails ~2035-08).
      # Live ephemeris must also match RAWX current week within ±1 (week-boundary race).
      try:
        week = resolve_gps_week_mod_1024(
          int(week),
          trusted_full_week=self._latest_rawx_gps_week,
        )
      except ValueError:
        self.assembly_stats.gps_week_era_unresolved += 1
        self.caches.gps_subframes[msg.sv_id].clear()
        self.caches.gps_subframe_times[msg.sv_id].clear()
        self._maybe_log_ephemeris_assembly_stats(now)
        return None
      eph.tgd = s1.t_gd * math.pow(2, -31)
      eph.toc = s1.t_oc * math.pow(2, 4)
      eph.af2 = s1.af_2 * math.pow(2, -55)
      eph.af1 = s1.af_1 * math.pow(2, -43)
      eph.af0 = s1.af_0 * math.pow(2, -31)
      eph.svHealth = s1.sv_health
      eph.towCount = sf1.how.tow_count
      iodc_lsb = s1.iodc_lsb

      # Subframe 2
      sf2 = Gps.from_bytes(self.caches.gps_subframes[msg.sv_id][2])
      s2 = sf2.body
      assert isinstance(s2, Gps.Subframe2)
      if s2.t_oe == 0 and sf2.how.tow_count * 6 >= (SECS_IN_WEEK - 2 * SECS_IN_HR):
        week += 1
      if self._latest_rawx_gps_week is None or not live_ephemeris_week_matches_rawx_current(
        week,
        self._latest_rawx_gps_week,
      ):
        self.assembly_stats.gps_week_era_unresolved += 1
        self.assembly_stats.gps_week_current_mismatch += 1
        cloudlog.info(
          "ubloxd ephemeris gps_week_era_unresolved/current-week-mismatch: " + f"sv={msg.sv_id} resolved_week={week} rawx_week={self._latest_rawx_gps_week}"
        )
        self.caches.gps_subframes[msg.sv_id].clear()
        self.caches.gps_subframe_times[msg.sv_id].clear()
        self._maybe_log_ephemeris_assembly_stats(now)
        return None
      eph.crs = s2.c_rs * math.pow(2, -5)
      eph.deltaN = s2.delta_n * math.pow(2, -43) * self.gpsPi
      eph.m0 = s2.m_0 * math.pow(2, -31) * self.gpsPi
      eph.cuc = s2.c_uc * math.pow(2, -29)
      eph.ecc = s2.e * math.pow(2, -33)
      eph.cus = s2.c_us * math.pow(2, -29)
      eph.a = math.pow(s2.sqrt_a * math.pow(2, -19), 2.0)
      eph.toe = s2.t_oe * math.pow(2, 4)
      iode_s2 = s2.iode

      # Subframe 3
      sf3 = Gps.from_bytes(self.caches.gps_subframes[msg.sv_id][3])
      s3 = sf3.body
      assert isinstance(s3, Gps.Subframe3)
      eph.cic = s3.c_ic * math.pow(2, -29)
      eph.omega0 = s3.omega_0 * math.pow(2, -31) * self.gpsPi
      eph.cis = s3.c_is * math.pow(2, -29)
      eph.i0 = s3.i_0 * math.pow(2, -31) * self.gpsPi
      eph.crc = s3.c_rc * math.pow(2, -5)
      eph.omega = s3.omega * math.pow(2, -31) * self.gpsPi
      eph.omegaDot = s3.omega_dot * math.pow(2, -43) * self.gpsPi
      eph.iode = s3.iode
      eph.iDot = s3.idot * math.pow(2, -43) * self.gpsPi
      iode_s3 = s3.iode
    except Exception:
      self.caches.gps_subframes[msg.sv_id].clear()
      self.caches.gps_subframe_times[msg.sv_id].clear()
      self.assembly_stats.gps_decode_failure += 1
      self._maybe_log_ephemeris_assembly_stats(now)
      return None

    eph.toeWeek = week
    eph.tocWeek = week

    # clear cache for this SV
    self.caches.gps_subframes[msg.sv_id].clear()
    self.caches.gps_subframe_times[msg.sv_id].clear()
    if not (iodc_lsb == iode_s2 == iode_s3):
      self.assembly_stats.gps_iod_mismatch += 1
      self._maybe_log_ephemeris_assembly_stats(now)
      return None
    self.assembly_stats.gps_ephemeris_assembled += 1
    self._maybe_log_ephemeris_assembly_stats(now)
    return ('ubloxGnss', dat)

  def _parse_glonass_ephemeris(self, msg: Ubx.RxmSfrbx) -> tuple[str, capnp.lib.capnp._DynamicStructBuilder] | None:
    # words are 4 bytes each; Glonass parser expects 16 bytes (string)
    now = self.framer.last_log_time
    body = msg.body
    if len(body) != 4:
      self.assembly_stats.glonass_decode_failure += 1
      self._maybe_log_ephemeris_assembly_stats(now)
      return None
    string_bytes = bytearray()
    for word in body:
      for i in (3, 2, 1, 0):
        string_bytes.append((word >> (8 * i)) & 0xFF)

    try:
      gl = Glonass.from_bytes(bytes(string_bytes))
      string_number = gl.string_number
    except Exception:
      self.assembly_stats.glonass_decode_failure += 1
      self._maybe_log_ephemeris_assembly_stats(now)
      return None
    if string_number < 1 or string_number > 5 or gl.idle_chip:
      return None

    # Identity: (sv_id, freq_id). Unknown SV (255) stays in its own bucket and never publishes.
    key = (msg.sv_id, msg.freq_id)
    self._expire_glonass_partials(key, now)

    # Correlate partials: known nonzero superframes must match; when either side
    # lacks a reliable superframe (0), fall back to normalized 2 s timing.
    superframe_unknown = False
    needs_clear = False
    known_superframe_mismatch = False
    timing_mismatch = False
    for i in range(1, 6):
      if i not in self.caches.glonass_strings[key]:
        continue
      sf_prev = self.caches.glonass_string_superframes[key].get(i, 0)
      if sf_prev == 0 or gl.superframe_number == 0:
        superframe_unknown = True
      elif sf_prev != gl.superframe_number:
        needs_clear = True
        known_superframe_mismatch = True
      if superframe_unknown:
        prev_time = self.caches.glonass_string_times[key].get(i, 0.0)
        if abs((prev_time - 2.0 * i) - (now - 2.0 * string_number)) > GLONASS_UNKNOWN_SUPERFRAME_TIMING_TOLERANCE_S:
          needs_clear = True
          timing_mismatch = True

    if needs_clear:
      # One clear event: classify known-superframe mismatch ahead of timing fallback.
      if known_superframe_mismatch:
        self.assembly_stats.glonass_superframe_mismatch += 1
      elif timing_mismatch:
        self.assembly_stats.glonass_timing_mismatch += 1
      self._clear_glonass_partials(key)

    self.caches.glonass_strings[key][string_number] = bytes(string_bytes)
    self.caches.glonass_string_superframes[key][string_number] = gl.superframe_number
    self.caches.glonass_string_times[key][string_number] = now
    self.assembly_stats.glonass_strings_received += 1

    if msg.sv_id == 255:
      # Unknown SV id: retain isolated partials but do not fabricate identity or publish.
      self._maybe_log_ephemeris_assembly_stats(now)
      return None
    if len(self.caches.glonass_strings[key]) != 5:
      self._maybe_log_ephemeris_assembly_stats(now)
      return None

    dat = messaging.new_message('ubloxGnss', valid=True)
    eph = dat.ubloxGnss.init('glonassEphemeris')
    eph.svId = msg.sv_id
    eph.freqNum = msg.freq_id - 7

    current_day = 0
    tk = 0

    try:
      # string 1
      s1 = Glonass.from_bytes(self.caches.glonass_strings[key][1]).data
      assert isinstance(s1, Glonass.String1)
      eph.p1 = int(s1.p1)
      tk = int(s1.t_k)
      eph.deprecated.tk = tk
      eph.xVel = float(s1.x_vel) * math.pow(2, -20)
      eph.xAccel = float(s1.x_accel) * math.pow(2, -30)
      eph.x = float(s1.x) * math.pow(2, -11)

      # string 2
      s2 = Glonass.from_bytes(self.caches.glonass_strings[key][2]).data
      assert isinstance(s2, Glonass.String2)
      eph.svHealth = int(s2.b_n >> 2)
      eph.p2 = int(s2.p2)
      eph.tb = int(s2.t_b)
      eph.yVel = float(s2.y_vel) * math.pow(2, -20)
      eph.yAccel = float(s2.y_accel) * math.pow(2, -30)
      eph.y = float(s2.y) * math.pow(2, -11)

      # string 3
      s3 = Glonass.from_bytes(self.caches.glonass_strings[key][3]).data
      assert isinstance(s3, Glonass.String3)
      eph.p3 = int(s3.p3)
      eph.gammaN = float(s3.gamma_n) * math.pow(2, -40)
      eph.svHealth = int(eph.svHealth | (1 if s3.l_n else 0))
      eph.zVel = float(s3.z_vel) * math.pow(2, -20)
      eph.zAccel = float(s3.z_accel) * math.pow(2, -30)
      eph.z = float(s3.z) * math.pow(2, -11)

      # string 4
      s4 = Glonass.from_bytes(self.caches.glonass_strings[key][4]).data
      assert isinstance(s4, Glonass.String4)
      current_day = int(s4.n_t)
      eph.nt = current_day
      eph.tauN = float(s4.tau_n) * math.pow(2, -30)
      eph.deltaTauN = float(s4.delta_tau_n) * math.pow(2, -30)
      eph.age = int(s4.e_n)
      eph.p4 = int(s4.p4)
      eph.svURA = float(self.glonass_URA_lookup.get(int(s4.f_t), 0.0))
      eph.svType = int(s4.m)
      # Slot number from String 4 must match RXM-SFRBX sv_id when both are known.
      slot = int(s4.n)
      if slot != 0 and slot != msg.sv_id:
        self._clear_glonass_partials(key)
        self.assembly_stats.glonass_slot_mismatch += 1
        self._maybe_log_ephemeris_assembly_stats(now)
        return None

      # string 5
      s5 = Glonass.from_bytes(self.caches.glonass_strings[key][5]).data
      assert isinstance(s5, Glonass.String5)
      eph.n4 = int(s5.n_4)
      tk_seconds = int(SECS_IN_HR * ((tk >> 7) & 0x1F) + SECS_IN_MIN * ((tk >> 1) & 0x3F) + (tk & 0x1) * 30)
      eph.tkSeconds = tk_seconds
    except Exception:
      self._clear_glonass_partials(key)
      self.assembly_stats.glonass_decode_failure += 1
      self._maybe_log_ephemeris_assembly_stats(now)
      return None

    self._clear_glonass_partials(key)
    self.assembly_stats.glonass_ephemeris_assembled += 1
    self._maybe_log_ephemeris_assembly_stats(now)
    return ('ubloxGnss', dat)

  def _gen_rxm_rawx(self, msg: Ubx.RxmRawx) -> tuple[str, capnp.lib.capnp._DynamicStructBuilder]:
    dat = messaging.new_message('ubloxGnss', valid=True)
    mr = dat.ubloxGnss.init('measurementReport')
    mr.rcvTow = msg.rcv_tow
    mr.gpsWeek = msg.week
    mr.leapSeconds = msg.leap_s
    if rawx_full_week_is_trusted_era_evidence(week=int(msg.week), num_meas=int(msg.num_meas)):
      self._latest_rawx_gps_week = int(msg.week)

    mb = mr.init('measurements', msg.num_meas)
    for i, m in enumerate(msg.meas):
      mb[i].svId = m.sv_id
      mb[i].pseudorange = m.pr_mes
      mb[i].carrierCycles = m.cp_mes
      mb[i].doppler = m.do_mes
      mb[i].gnssId = int(m.gnss_id.value)
      mb[i].glonassFrequencyIndex = m.freq_id
      mb[i].locktime = m.lock_time
      mb[i].cno = m.cno
      mb[i].pseudorangeStdev = 0.01 * (math.pow(2, (m.pr_stdev & 15)))
      mb[i].carrierPhaseStdev = 0.004 * (m.cp_stdev & 15)
      mb[i].dopplerStdev = 0.002 * (math.pow(2, (m.do_stdev & 15)))

      ts = mb[i].init('trackingStatus')
      trk = m.trk_stat
      ts.pseudorangeValid = _bit(trk, 0)
      ts.carrierPhaseValid = _bit(trk, 1)
      ts.halfCycleValid = _bit(trk, 2)
      ts.halfCycleSubtracted = _bit(trk, 3)

    mr.numMeas = msg.num_meas
    rs = mr.init('receiverStatus')
    rs.leapSecValid = _bit(msg.rec_stat, 0)
    rs.clkReset = _bit(msg.rec_stat, 2)
    return ('ubloxGnss', dat)

  def _gen_nav_sat(self, msg: Ubx.NavSat) -> tuple[str, capnp.lib.capnp._DynamicStructBuilder]:
    dat = messaging.new_message('ubloxGnss', valid=True)
    sr = dat.ubloxGnss.init('satReport')
    sr.iTow = msg.itow
    svs = sr.init('svs', msg.num_svs)
    for i, s in enumerate(msg.svs):
      svs[i].svId = s.sv_id
      svs[i].gnssId = int(s.gnss_id.value)
      svs[i].flagsBitfield = s.flags
      svs[i].cno = s.cno
      svs[i].elevationDeg = s.elev
      svs[i].azimuthDeg = s.azim
      svs[i].pseudorangeResidual = s.pr_res * 0.1
    return ('ubloxGnss', dat)

  def _gen_mon_hw(self, msg: Ubx.MonHw) -> tuple[str, capnp.lib.capnp._DynamicStructBuilder]:
    dat = messaging.new_message('ubloxGnss', valid=True)
    hw = dat.ubloxGnss.init('hwStatus')
    hw.noisePerMS = msg.noise_per_ms
    hw.flags = msg.flags
    hw.agcCnt = msg.agc_cnt
    hw.aStatus = int(msg.a_status.value)
    hw.aPower = int(msg.a_power.value)
    hw.jamInd = msg.jam_ind
    return ('ubloxGnss', dat)

  def _gen_mon_hw2(self, msg: Ubx.MonHw2) -> tuple[str, capnp.lib.capnp._DynamicStructBuilder]:
    dat = messaging.new_message('ubloxGnss', valid=True)
    hw = dat.ubloxGnss.init('hwStatus2')
    hw.ofsI = msg.ofs_i
    hw.magI = msg.mag_i
    hw.ofsQ = msg.ofs_q
    hw.magQ = msg.mag_q
    # Map Ubx enum to cereal enum {undefined=0, rom=1, otp=2, configpins=3, flash=4}
    cfg_map = {
      Ubx.MonHw2.ConfigSource.rom: 1,
      Ubx.MonHw2.ConfigSource.otp: 2,
      Ubx.MonHw2.ConfigSource.config_pins: 3,
      Ubx.MonHw2.ConfigSource.flash: 4,
    }
    hw.cfgSource = cfg_map.get(msg.cfg_source, 0)
    hw.lowLevCfg = msg.low_lev_cfg
    hw.postStatus = msg.post_status
    return ('ubloxGnss', dat)


def main():
  parser = UbloxMsgParser()
  observability = UbloxRfObservability()
  pm = messaging.PubMaster(['ubloxGnss', 'gpsLocationExternal'])
  sock = messaging.sub_sock('ubloxRaw', timeout=100, conflate=False)

  while True:
    msg = messaging.recv_one(sock)
    if msg is None:
      continue

    data = bytes(msg.ubloxRaw)
    log_time = msg.logMonoTime * 1e-9
    frames = parser.framer.add_data(log_time, data)
    try:
      framer_line = observability.observe_framer_health(
        frames_valid=parser.framer.frames_valid,
        checksum_failures=parser.framer.checksum_failures,
        discarded_prefix_bytes=parser.framer.discarded_prefix_bytes,
        resync_events=parser.framer.resync_events,
        buffered_bytes=len(parser.framer.buf),
        now=log_time,
      )
      if framer_line is not None:
        cloudlog.info(framer_line)
    except Exception:
      # Observability must never interfere with UBX framing or publication.
      pass

    for frame in frames:
      try:
        for telemetry_line in observability.observe_frame(frame, log_time):
          cloudlog.info(telemetry_line)
      except Exception:
        # RF diagnostics are read-only and must never interfere with parsing.
        pass
      try:
        # Stamp measurement epoch at ubloxRaw receive/framing completion time.
        res = parser.parse_frame(frame, measurement_mono_ns=int(msg.logMonoTime))
      except Exception as exc:
        try:
          parser_error = observability.observe_parser_error(frame, exc, log_time)
          if parser_error is not None:
            cloudlog.error(parser_error)
        except Exception:
          # Observability must never turn a nonfatal parser error into a daemon failure.
          pass
        continue
      if not res:
        continue
      service, dat = res
      pm.send(service, dat)


if __name__ == '__main__':
  main()
