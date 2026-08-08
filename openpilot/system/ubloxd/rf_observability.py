"""Read-only u-blox RF/acquisition observability.

This module never writes receiver configuration or assistance. It summarizes
already-received UBX frames and framer health for bounded cloudlog telemetry.
"""

from __future__ import annotations

from collections import Counter
import math
import struct


DEFAULT_LOG_INTERVAL_SECONDS = 30.0
PARSER_ERROR_MESSAGE_MAX_LENGTH = 160

ANTENNA_STATUS_NAMES = {
  0: "init",
  1: "unknown",
  2: "ok",
  3: "short",
  4: "open",
}

ANTENNA_POWER_NAMES = {
  0: "off",
  1: "on",
  2: "unknown",
}


def _format_counts(values: dict[int, int]) -> str:
  if not values:
    return "none"
  return "|".join(f"{key}:{values[key]}" for key in sorted(values))


def _format_float(value: float) -> str:
  return f"{value:.1f}" if math.isfinite(value) else "none"


def _bounded_exception(exc: Exception) -> str:
  value = " ".join(str(exc).split())
  return value[:PARSER_ERROR_MESSAGE_MAX_LENGTH] or "<empty>"


def _message_identity(frame: bytes) -> tuple[int | None, int | None, int | None]:
  if len(frame) < 4:
    return None, None, None
  message_class = frame[2]
  message_id = frame[3]
  declared_payload_length = int.from_bytes(frame[4:6], "little") if len(frame) >= 6 else None
  return message_class, message_id, declared_payload_length


def _payload(frame: bytes) -> bytes:
  return frame[6:-2] if len(frame) >= 8 else b""


def _signed_byte(value: int) -> int:
  return value - 256 if value >= 128 else value


class UbloxRfObservability:
  """Rate-limited, side-effect-free summaries of already-received UBX data."""

  def __init__(self, log_interval_seconds: float = DEFAULT_LOG_INTERVAL_SECONDS) -> None:
    if log_interval_seconds <= 0.0:
      raise ValueError("log_interval_seconds must be positive")
    self.log_interval_seconds = float(log_interval_seconds)
    self.total_parser_errors = 0
    self.parser_error_counts: Counter[tuple[int | None, int | None]] = Counter()
    self._parser_error_last_log: dict[tuple[int | None, int | None], float] = {}
    self._last_log: dict[str, float] = {}
    self._highwater: dict[str, tuple[int, ...]] = {}
    self._antenna_signature: tuple[int, int] | None = None
    self._hw2_signature: tuple[int, int] | None = None
    self._framer_error_signature: tuple[int, int, int] | None = None
    self._sfrbx_count = 0
    self._sfrbx_gnss_counts: Counter[int] = Counter()
    self._sfrbx_satellites: set[tuple[int, int]] = set()

  def _interval_due(self, kind: str, now: float) -> bool:
    previous = self._last_log.get(kind)
    return previous is None or now < previous or now - previous >= self.log_interval_seconds

  def _mark_logged(self, kind: str, now: float) -> None:
    self._last_log[kind] = now

  def _progress_due(self, kind: str, progress: tuple[int, ...], now: float) -> bool:
    previous = self._highwater.get(kind)
    improved = previous is None or any(current > old for current, old in zip(progress, previous, strict=True))
    if improved:
      if previous is None:
        self._highwater[kind] = progress
      else:
        self._highwater[kind] = tuple(max(current, old) for current, old in zip(progress, previous, strict=True))
    return improved or self._interval_due(kind, now)

  def observe_parser_error(self, frame: bytes, exc: Exception, now: float) -> str | None:
    self.total_parser_errors += 1
    message_class, message_id, declared_payload_length = _message_identity(frame)
    key = (message_class, message_id)
    self.parser_error_counts[key] += 1
    message_error_count = self.parser_error_counts[key]

    previous = self._parser_error_last_log.get(key)
    if previous is not None and now >= previous and now - previous < self.log_interval_seconds:
      return None
    self._parser_error_last_log[key] = now

    class_text = "unknown" if message_class is None else f"0x{message_class:02x}"
    id_text = "unknown" if message_id is None else f"0x{message_id:02x}"
    payload_text = "unknown" if declared_payload_length is None else str(declared_payload_length)
    return ", ".join((
      "GPS ubloxd parser error",
      f"message_class={class_text}",
      f"message_id={id_text}",
      f"declared_payload_length={payload_text}",
      f"frame_length={len(frame)}",
      f"message_error_count={message_error_count}",
      f"total_error_count={self.total_parser_errors}",
      f"exception_type={type(exc).__name__}",
      f"exception={_bounded_exception(exc)}",
    ))

  def observe_framer_health(
    self,
    *,
    frames_valid: int,
    checksum_failures: int,
    discarded_prefix_bytes: int,
    resync_events: int,
    buffered_bytes: int,
    now: float,
  ) -> str | None:
    error_signature = (checksum_failures, discarded_prefix_bytes, resync_events)
    previous_signature = self._framer_error_signature
    first = previous_signature is None
    first_error = (
      previous_signature is not None
      and not any(previous_signature)
      and any(error_signature)
    )
    self._framer_error_signature = error_signature

    # Log the initial baseline and the first transition into a framing-error
    # condition immediately. After that, repeated counter growth is strictly
    # rate-limited so a corrupt serial stream cannot flood cloudlog or perturb
    # normal receiver parsing.
    if not first and not first_error and not self._interval_due("framer", now):
      return None
    self._mark_logged("framer", now)
    return ", ".join((
      "GPS RF observability kind=framer",
      f"frames_valid={frames_valid}",
      f"checksum_failures={checksum_failures}",
      f"discarded_prefix_bytes={discarded_prefix_bytes}",
      f"resync_events={resync_events}",
      f"buffered_bytes={buffered_bytes}",
    ))

  def observe_frame(self, frame: bytes, now: float) -> tuple[str, ...]:
    message_class, message_id, _ = _message_identity(frame)
    message_type = (message_class, message_id)

    if message_type == (0x02, 0x15):
      line = self._observe_rawx(_payload(frame), now)
    elif message_type == (0x01, 0x35):
      line = self._observe_nav_sat(_payload(frame), now)
    elif message_type == (0x0A, 0x09):
      line = self._observe_mon_hw(_payload(frame), now)
    elif message_type == (0x0A, 0x0B):
      line = self._observe_mon_hw2(_payload(frame), now)
    elif message_type == (0x02, 0x13):
      line = self._observe_sfrbx(_payload(frame), now)
    else:
      line = None

    return () if line is None else (line,)

  def _observe_rawx(self, payload: bytes, now: float) -> str | None:
    if len(payload) < 16:
      return None

    declared = payload[11]
    parsed = min(declared, max(0, (len(payload) - 16) // 32))
    gnss_counts: Counter[int] = Counter()
    signal_counts: Counter[int] = Counter()
    pseudorange_valid_counts: Counter[int] = Counter()
    max_cno_by_gnss: dict[int, int] = {}
    signal_cno_values: list[int] = []
    zero_cno_count = 0

    for index in range(parsed):
      offset = 16 + index * 32
      gnss_id = payload[offset + 20]
      cno = payload[offset + 26]
      tracking_status = payload[offset + 30]
      gnss_counts[gnss_id] += 1
      if cno > 0:
        signal_counts[gnss_id] += 1
        signal_cno_values.append(cno)
        max_cno_by_gnss[gnss_id] = max(max_cno_by_gnss.get(gnss_id, 0), cno)
      else:
        zero_cno_count += 1
      if tracking_status & 0x01:
        pseudorange_valid_counts[gnss_id] += 1

    max_cno = max(signal_cno_values, default=0)
    average_signal_cno = sum(signal_cno_values) / len(signal_cno_values) if signal_cno_values else 0.0
    pseudorange_valid = sum(pseudorange_valid_counts.values())
    signal_measurements = sum(signal_counts.values())
    progress = (parsed, signal_measurements, pseudorange_valid, max_cno)
    if not self._progress_due("rawx", progress, now):
      return None
    self._mark_logged("rawx", now)

    return ", ".join((
      "GPS RF observability kind=rawx",
      f"measurement_count={parsed}",
      f"declared_measurement_count={declared}",
      f"signal_measurements={signal_measurements}",
      f"zero_cno_count={zero_cno_count}",
      f"pseudorange_valid={pseudorange_valid}",
      f"max_cno_dbhz={max_cno}",
      f"average_signal_cno_dbhz={_format_float(average_signal_cno)}",
      f"measurements_by_gnss={_format_counts(dict(gnss_counts))}",
      f"signal_by_gnss={_format_counts(dict(signal_counts))}",
      f"pseudorange_valid_by_gnss={_format_counts(dict(pseudorange_valid_counts))}",
      f"max_cno_by_gnss={_format_counts(max_cno_by_gnss)}",
      f"gps_week={int.from_bytes(payload[8:10], 'little')}",
      f"leap_seconds_valid={bool(payload[12] & 0x01)}",
    ))

  def _observe_nav_sat(self, payload: bytes, now: float) -> str | None:
    if len(payload) < 8:
      return None

    declared = payload[5]
    parsed = min(declared, max(0, (len(payload) - 8) // 12))
    signal_by_gnss: Counter[int] = Counter()
    acquired_by_gnss: Counter[int] = Counter()
    code_locked_by_gnss: Counter[int] = Counter()
    used_by_gnss: Counter[int] = Counter()
    eph_by_gnss: Counter[int] = Counter()
    alm_by_gnss: Counter[int] = Counter()
    quality_counts: Counter[int] = Counter()
    health_counts: Counter[int] = Counter()
    max_cno_by_gnss: dict[int, int] = {}
    signal_cno_values: list[int] = []
    zero_cno_count = 0

    for index in range(parsed):
      offset = 8 + index * 12
      gnss_id = payload[offset]
      cno = payload[offset + 2]
      flags = struct.unpack_from("<I", payload, offset + 8)[0]
      quality = flags & 0x07
      health = (flags >> 4) & 0x03
      quality_counts[quality] += 1
      health_counts[health] += 1
      if cno > 0:
        signal_by_gnss[gnss_id] += 1
        signal_cno_values.append(cno)
        max_cno_by_gnss[gnss_id] = max(max_cno_by_gnss.get(gnss_id, 0), cno)
      else:
        zero_cno_count += 1
      if quality >= 2:
        acquired_by_gnss[gnss_id] += 1
      if quality >= 4:
        code_locked_by_gnss[gnss_id] += 1
      if flags & (1 << 3):
        used_by_gnss[gnss_id] += 1
      if flags & (1 << 11):
        eph_by_gnss[gnss_id] += 1
      if flags & (1 << 12):
        alm_by_gnss[gnss_id] += 1

    signal = sum(signal_by_gnss.values())
    acquired = sum(acquired_by_gnss.values())
    code_locked = sum(code_locked_by_gnss.values())
    used = sum(used_by_gnss.values())
    ephemeris = sum(eph_by_gnss.values())
    almanac = sum(alm_by_gnss.values())
    max_cno = max(signal_cno_values, default=0)
    average_signal_cno = sum(signal_cno_values) / len(signal_cno_values) if signal_cno_values else 0.0
    progress = (signal, acquired, code_locked, used, ephemeris, almanac, max_cno)
    if not self._progress_due("nav_sat", progress, now):
      return None
    self._mark_logged("nav_sat", now)

    return ", ".join((
      "GPS RF observability kind=nav_sat",
      f"satellite_count={parsed}",
      f"declared_satellite_count={declared}",
      f"signal_satellites={signal}",
      f"zero_cno_count={zero_cno_count}",
      f"acquired_satellites={acquired}",
      f"code_locked_satellites={code_locked}",
      f"used_satellites={used}",
      f"ephemeris_available={ephemeris}",
      f"almanac_available={almanac}",
      f"max_cno_dbhz={max_cno}",
      f"average_signal_cno_dbhz={_format_float(average_signal_cno)}",
      f"signal_by_gnss={_format_counts(dict(signal_by_gnss))}",
      f"acquired_by_gnss={_format_counts(dict(acquired_by_gnss))}",
      f"code_locked_by_gnss={_format_counts(dict(code_locked_by_gnss))}",
      f"used_by_gnss={_format_counts(dict(used_by_gnss))}",
      f"ephemeris_by_gnss={_format_counts(dict(eph_by_gnss))}",
      f"almanac_by_gnss={_format_counts(dict(alm_by_gnss))}",
      f"max_cno_by_gnss={_format_counts(max_cno_by_gnss)}",
      f"quality_counts={_format_counts(dict(quality_counts))}",
      f"health_counts={_format_counts(dict(health_counts))}",
    ))

  def _observe_mon_hw(self, payload: bytes, now: float) -> str | None:
    if len(payload) < 46:
      return None

    noise_per_ms = int.from_bytes(payload[16:18], "little")
    agc_count = int.from_bytes(payload[18:20], "little")
    antenna_status = payload[20]
    antenna_power = payload[21]
    flags = payload[22]
    jam_indicator = payload[45]
    signature = (antenna_status, antenna_power)
    changed = self._antenna_signature is not None and signature != self._antenna_signature
    first = self._antenna_signature is None
    self._antenna_signature = signature

    if not first and not changed and not self._interval_due("mon_hw", now):
      return None
    self._mark_logged("mon_hw", now)

    return ", ".join((
      "GPS RF observability kind=mon_hw",
      f"antenna_status={ANTENNA_STATUS_NAMES.get(antenna_status, str(antenna_status))}",
      f"antenna_status_code={antenna_status}",
      f"antenna_power={ANTENNA_POWER_NAMES.get(antenna_power, str(antenna_power))}",
      f"antenna_power_code={antenna_power}",
      f"agc_count={agc_count}",
      f"noise_per_ms={noise_per_ms}",
      f"jam_indicator={jam_indicator}",
      f"flags=0x{flags:02x}",
    ))

  def _observe_mon_hw2(self, payload: bytes, now: float) -> str | None:
    if len(payload) < 24:
      return None

    ofs_i = _signed_byte(payload[0])
    mag_i = payload[1]
    ofs_q = _signed_byte(payload[2])
    mag_q = payload[3]
    config_source = payload[4]
    low_level_config = int.from_bytes(payload[8:12], "little")
    post_status = int.from_bytes(payload[20:24], "little")
    signature = (config_source, post_status)
    changed = self._hw2_signature is not None and signature != self._hw2_signature
    first = self._hw2_signature is None
    self._hw2_signature = signature

    if not first and not changed and not self._interval_due("mon_hw2", now):
      return None
    self._mark_logged("mon_hw2", now)

    return ", ".join((
      "GPS RF observability kind=mon_hw2",
      f"ofs_i={ofs_i}",
      f"mag_i={mag_i}",
      f"ofs_q={ofs_q}",
      f"mag_q={mag_q}",
      f"config_source={config_source}",
      f"low_level_config=0x{low_level_config:08x}",
      f"post_status=0x{post_status:08x}",
    ))

  def _observe_sfrbx(self, payload: bytes, now: float) -> str | None:
    if len(payload) < 8:
      return None

    gnss_id = payload[0]
    sv_id = payload[1]
    num_words = payload[4]
    expected_length = 8 + 4 * num_words
    structurally_complete = len(payload) == expected_length
    self._sfrbx_count += 1
    self._sfrbx_gnss_counts[gnss_id] += 1
    before = len(self._sfrbx_satellites)
    self._sfrbx_satellites.add((gnss_id, sv_id))
    unique_increased = len(self._sfrbx_satellites) > before

    if self._sfrbx_count != 1 and not unique_increased and not self._interval_due("sfrbx", now):
      return None
    self._mark_logged("sfrbx", now)

    return ", ".join((
      "GPS RF observability kind=sfrbx",
      f"message_count={self._sfrbx_count}",
      f"latest_gnss_id={gnss_id}",
      f"latest_sv_id={sv_id}",
      f"latest_num_words={num_words}",
      f"latest_structurally_complete={structurally_complete}",
      f"unique_satellites={len(self._sfrbx_satellites)}",
      f"messages_by_gnss={_format_counts(dict(self._sfrbx_gnss_counts))}",
    ))
