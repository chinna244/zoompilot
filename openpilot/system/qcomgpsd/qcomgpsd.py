#!/usr/bin/env python3
from __future__ import annotations

import fcntl
import itertools
import math
import os
import signal
import struct
import sys
import time
from dataclasses import dataclass
from datetime import timedelta
from struct import calcsize, pack, unpack_from
from typing import NoReturn

from openpilot.cereal import log
import openpilot.cereal.messaging as messaging
from openpilot.common.gpio import gpio_init, gpio_set
from openpilot.common.gps import ublox_hardware_available
from openpilot.common.hardware.tici.pins import GPIO
from openpilot.common.serial import Serial
from openpilot.common.swaglog import cloudlog
from openpilot.common.time_helpers import HostTimeSource, read_host_time_observation
from openpilot.common.utils import retry
from openpilot.system.qcomgpsd.modemdiag import (
  DIAG_LOG_F,
  DIAG_NV_READ_F,
  DIAG_SUBSYS_CMD_F,
  NV_FULL_RESPONSE_LEN,
  NV_STATUS_OK,
  DiagCommandError,
  DiagFramingError,
  DiagTimeoutError,
  ModemDiag,
  send_recv,
  setup_logs,
)
from openpilot.system.qcomgpsd.qcom_position import (
  POS_SOURCE_KALMAN,
  host_time_safe_for_qcom_injection,
  qcom_position_fields,
)
from openpilot.system.qcomgpsd.structs import (
  LOG_GNSS_GLONASS_MEASUREMENT_REPORT,
  LOG_GNSS_GPS_MEASUREMENT_REPORT,
  LOG_GNSS_OEMDRE_MEASUREMENT_REPORT,
  LOG_GNSS_OEMDRE_SVPOLY_REPORT,
  LOG_GNSS_POSITION_REPORT,
  dict_unpacker,
  glonass_measurement_report,
  glonass_measurement_report_sv,
  gps_measurement_report,
  gps_measurement_report_sv,
  oemdre_measurement_report,
  oemdre_measurement_report_sv,
  oemdre_svpoly_report,
  position_report,
  relist,
)

DEBUG = int(os.getenv("DEBUG", "0")) == 1

LOG_TYPES = [
  LOG_GNSS_GPS_MEASUREMENT_REPORT,
  LOG_GNSS_GLONASS_MEASUREMENT_REPORT,
  LOG_GNSS_OEMDRE_MEASUREMENT_REPORT,
  LOG_GNSS_POSITION_REPORT,
  LOG_GNSS_OEMDRE_SVPOLY_REPORT,
]

MODEM_STARTUP_DEADLINE_SECONDS = 90.0
AT_LOCK_POLL_SECONDS = 0.05
# Optional QGPSXTRATIME assistance budget (lock + serial); not modem-startup 90s.
QGPSXTRATIME_ASSISTANCE_BUDGET_SECONDS = 5.0
NV_GNSS_OEM_FEATURE_MASK = 7165
# Historical openpilot desired whole-mask write. Not used for writes: NV bit
# semantics for OEMDRE are unverified (see ensure_gnss_oem_feature_mask).
NV_GNSS_OEM_FEATURE_MASK_DESIRED = 1
# Quectel EG25-G (qcomgpsd target): LTE Standard GNSS Application Note V1.2
# documents AT+QGPSXTRATIME and uncertainty default 3500 ms. No authoritative
# EG25-G max range is established; do not borrow BG95/BG77 limits. Fail closed:
# only the proven 3500 ms value may be sent (raise-only from below; never larger).
QGPSXTRATIME_PROVEN_UNCERTAINTY_MS = 3500
QGPSXTRATIME_MIN_UNCERTAINTY_MS = QGPSXTRATIME_PROVEN_UNCERTAINTY_MS
MAX_SV_COUNT = 64

DIAG_SUBSYS_GPS = 13
CGPS_DIAG_PDAPI_CMD = 0x64
CGPS_OEM_CONTROL = 202
GPSDIAG_OEMFEATURE_DRE = 1
GPSDIAG_OEM_DRE_ON = 1
# Request layout <BHBBIIII: version=0, feature=DRE, state=ON. Match echoed fields.
GPSDIAG_OEM_CONTROL_VERSION = 0
# Only version 0 is proven against openpilot's fixed 291-byte 0x1476 layout
# (structs.py + unit helpers). Other version numbers are not established.
POSITION_REPORT_SUPPORTED_VERSIONS = frozenset({0})

miscStatusFields = {
  "multipathEstimateIsValid": 0,
  "directionIsValid": 1,
}

measurementStatusFields = {
  "subMillisecondIsValid": 0,
  "subBitTimeIsKnown": 1,
  "satelliteTimeIsKnown": 2,
  "bitEdgeConfirmedFromSignal": 3,
  "measuredVelocity": 4,
  "fineOrCoarseVelocity": 5,
  "lockPointValid": 6,
  "lockPointPositive": 7,
  "lastUpdateFromDifference": 9,
  "lastUpdateFromVelocityDifference": 10,
  "strongIndicationOfCrossCorelation": 11,
  "tentativeMeasurement": 12,
  "measurementNotUsable": 13,
  "sirCheckIsNeeded": 14,
  "probationMode": 15,
  "multipathIndicator": 24,
  "imdJammingIndicator": 25,
  "lteB13TxJammingIndicator": 26,
  "freshMeasurementIndicator": 27,
}

measurementStatusGPSFields = {
  "gpsRoundRobinRxDiversity": 18,
  "gpsRxDiversity": 19,
  "gpsLowBandwidthRxDiversityCombined": 20,
  "gpsHighBandwidthNu4": 21,
  "gpsHighBandwidthNu8": 22,
  "gpsHighBandwidthUniform": 23,
}

measurementStatusGlonassFields = {
  "glonassMeanderBitEdgeValid": 16,
  "glonassTimeMarkValid": 17,
}


class AtCommandError(RuntimeError):
  def __init__(self, cmd: str, terminal: str, body: str = "") -> None:
    self.cmd = cmd
    self.terminal = terminal
    self.body = body
    super().__init__(f"AT command failed ({terminal}): {cmd}")


class AtCommandTimeout(AtCommandError):
  def __init__(self, cmd: str) -> None:
    super().__init__(cmd, "TIMEOUT")


class ModemStartupTimeout(TimeoutError):
  pass


@retry(attempts=10, delay=1.0)
def try_setup_logs(diag, logs):
  return setup_logs(diag, logs)


AT_PORT = "/dev/modem_at0"
AT_LOCK = "/dev/shm/modem.lock"  # shared with modem.py and LPA


def at_cmd(
  cmd: str,
  *,
  attempts: int = 5,
  deadline: float | None = None,
  monotonic=time.monotonic,
  sleeper=time.sleep,
  serial_timeout: float = 5.0,
  flock=fcntl.flock,
) -> str:
  """Run an AT command and return response body lines on OK only.

  When deadline is set (monotonic clock), lock wait, nested retries, and serial
  waits all consume the same absolute budget and cannot extend past it.
  """
  effective_deadline = deadline if deadline is not None else monotonic() + MODEM_STARTUP_DEADLINE_SECONDS
  last_error: Exception | None = None
  for attempt in range(attempts):
    remaining = effective_deadline - monotonic()
    if remaining <= 0.0:
      raise AtCommandTimeout(cmd) if last_error is None else last_error
    try:
      return _at_cmd_once(
        cmd,
        serial_timeout=min(serial_timeout, remaining),
        deadline=effective_deadline,
        monotonic=monotonic,
        sleeper=sleeper,
        flock=flock,
      )
    except (AtCommandError, AtCommandTimeout, OSError) as exc:
      last_error = exc
      if attempt + 1 >= attempts:
        break
      remaining = effective_deadline - monotonic()
      if remaining <= 0.0:
        break
      sleeper(min(1.0, remaining))
  assert last_error is not None
  raise last_error


def consume_at_response(cmd: str, lines: list[str | None]) -> str:
  """Interpret modem AT response lines. None/empty line means timeout."""
  body: list[str] = []
  for line in lines:
    if line is None or line == "":
      raise AtCommandTimeout(cmd)
    text = line.strip()
    if text == "OK":
      return "\n".join(body)
    if text == "ERROR" or text.startswith("+CME ERROR"):
      raise AtCommandError(cmd, text, "\n".join(body))
    if text and text != cmd:
      body.append(text)
  raise AtCommandTimeout(cmd)


def acquire_at_lock(
  lock_fd: int,
  *,
  deadline: float,
  monotonic=time.monotonic,
  sleeper=time.sleep,
  flock=fcntl.flock,
  poll_seconds: float = AT_LOCK_POLL_SECONDS,
) -> None:
  """Acquire /dev/shm/modem.lock with LOCK_NB against an absolute deadline."""
  while True:
    try:
      flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
      return
    except BlockingIOError:
      remaining = deadline - monotonic()
      if remaining <= 0.0:
        raise AtCommandTimeout("AT_LOCK") from None
      sleeper(min(poll_seconds, remaining))


def _at_cmd_once(
  cmd: str,
  *,
  serial_timeout: float = 5.0,
  deadline: float | None = None,
  monotonic=time.monotonic,
  sleeper=time.sleep,
  flock=fcntl.flock,
  build_cmd=None,
) -> str:
  """Run one AT exchange. Optional build_cmd() runs after the lock is held."""
  effective_deadline = deadline if deadline is not None else monotonic() + MODEM_STARTUP_DEADLINE_SECONDS
  with os.fdopen(os.open(AT_LOCK, os.O_CREAT | os.O_RDWR, 0o666), "r+") as lock:
    acquire_at_lock(
      lock.fileno(),
      deadline=effective_deadline,
      monotonic=monotonic,
      sleeper=sleeper,
      flock=flock,
    )
    if build_cmd is not None:
      cmd = build_cmd()
    remaining = effective_deadline - monotonic()
    if remaining <= 0.0:
      raise AtCommandTimeout(cmd)
    with Serial(AT_PORT, baudrate=115200, timeout=max(0.001, min(serial_timeout, remaining))) as ser:
      ser.reset_input_buffer()
      ser.write(f"{cmd}\r".encode())
      collected: list[str | None] = []
      while True:
        remaining = effective_deadline - monotonic()
        if remaining <= 0.0:
          raise AtCommandTimeout(cmd)
        raw = ser.readline()
        if not raw:
          collected.append(None)
          break
        collected.append(raw.decode("utf-8", errors="replace"))
        text = collected[-1].strip() if collected[-1] is not None else ""
        if text in ("OK", "ERROR") or text.startswith("+CME ERROR"):
          break
      return consume_at_response(cmd, collected)


def gps_enabled() -> bool:
  return "QGPS: 1" in at_cmd("AT+QGPS?")


def _nv_item_match(expected_opcode: int, item_id: int):
  def match(opcode: int, payload: bytes) -> bool:
    if opcode != expected_opcode:
      return False
    if len(payload) < 2:
      return False
    return unpack_from("<H", payload)[0] == item_id

  return match


def _gps_oem_control_match(opcode: int, payload: bytes) -> bool:
  """Match DIAG_SUBSYS_CMD_F GPS OEM-control responses using echoed identifiers."""
  if opcode != DIAG_SUBSYS_CMD_F:
    return False
  # Request layout <BHBBIIII echoes: subsystem, CGPS cmd, OEM control, version,
  # feature, and on/off state when the response is long enough.
  if len(payload) < 13:
    return False
  subsys, cmd, oem_control, version, feature, state = unpack_from("<BHBBII", payload)
  return (
    subsys == DIAG_SUBSYS_GPS
    and cmd == CGPS_DIAG_PDAPI_CMD
    and oem_control == CGPS_OEM_CONTROL
    and version == GPSDIAG_OEM_CONTROL_VERSION
    and feature == GPSDIAG_OEMFEATURE_DRE
    and state == GPSDIAG_OEM_DRE_ON
  )


def parse_nv_uint32_response(
  opcode: int,
  payload: bytes,
  *,
  expected_opcode: int,
  item_id: int,
) -> int | None:
  """Return NV uint32 only from a full response with status==success.

  Classic Qualcomm layout after opcode: item(2) + data(128) + nv_stat(2).
  Statusless six-byte echoes are not proven successful on this target and are
  rejected.
  """
  if opcode != expected_opcode:
    return None
  if len(payload) != NV_FULL_RESPONSE_LEN:
    return None
  got_item = unpack_from("<H", payload)[0]
  if got_item != item_id:
    return None
  status = unpack_from("<H", payload, NV_FULL_RESPONSE_LEN - 2)[0]
  if status != NV_STATUS_OK:
    return None
  return unpack_from("<I", payload, 2)[0]


@dataclass(frozen=True)
class NvEnsureResult:
  wrote: bool
  value: int | None
  verified: bool
  degraded: bool = False


def ensure_gnss_oem_feature_mask(
  diag: ModemDiag,
  *,
  desired: int = NV_GNSS_OEM_FEATURE_MASK_DESIRED,
  item: int = NV_GNSS_OEM_FEATURE_MASK,
) -> NvEnsureResult:
  """Observe NV 7165; never write without proven OEMDRE mask-bit semantics.

  NV item 7165 is gnss_oem_feature_mask (32-bit). GPSDIAG_OEMFEATURE_DRE is a
  DIAG OEM-control feature identifier. No authoritative Qualcomm evidence maps
  that enum onto a specific NV bitmask bit for this target. Writing `desired=1`
  (or any whole-mask replacement) can clear unrelated OEM feature bits.

  Fail closed: read for observability when possible, never write NV 7165.
  Runtime OEMDRE enable remains via the DIAG subsystem OEM-control command.
  """
  del desired  # retained in signature for call-site compatibility; unused
  try:
    opcode, payload = send_recv(
      diag,
      DIAG_NV_READ_F,
      pack("<H", item),
      match=_nv_item_match(DIAG_NV_READ_F, item),
    )
  except (DiagTimeoutError, DiagFramingError, DiagCommandError, OSError) as exc:
    try:
      cloudlog.warning(f"GNSS NV 7165 read failed ({type(exc).__name__}); OEMDRE NV mask bit unproven — refusing write (fail-closed)")
    except Exception:
      pass
    return NvEnsureResult(wrote=False, value=None, verified=False, degraded=True)

  current = parse_nv_uint32_response(
    opcode,
    payload,
    expected_opcode=DIAG_NV_READ_F,
    item_id=item,
  )
  try:
    if current is None:
      cloudlog.warning("GNSS NV 7165 read untrusted; OEMDRE NV mask bit unproven — refusing write (fail-closed)")
    else:
      cloudlog.warning(f"GNSS NV 7165 observed=0x{current:08x}; OEMDRE NV mask bit unproven — refusing write (fail-closed)")
  except Exception:
    pass
  return NvEnsureResult(wrote=False, value=current, verified=False, degraded=True)


def _read_boottime_seconds() -> float | None:
  try:
    return time.clock_gettime(time.CLOCK_BOOTTIME)
  except OSError:
    return None


def qgpsxtratime_uncertainty_ms(
  uncertainty_seconds: float,
  age_seconds: float,
) -> int | None:
  """Convert host uncertainty + observation age to QGPSXTRATIME milliseconds.

  EG25-G LTE Standard GNSS Application Note V1.2 only establishes 3500 ms as
  the documented uncertainty value/default. Required uncertainty is ceiled and
  may be raised to 3500 ms; anything requiring more than 3500 ms is skipped
  (fail closed — no unproven larger EG25-G range).
  """
  if (
    isinstance(uncertainty_seconds, bool)
    or not isinstance(uncertainty_seconds, (int, float))
    or isinstance(age_seconds, bool)
    or not isinstance(age_seconds, (int, float))
  ):
    return None
  if not math.isfinite(uncertainty_seconds) or uncertainty_seconds < 0.0:
    return None
  if not math.isfinite(age_seconds) or age_seconds < 0.0:
    return None
  total_seconds = uncertainty_seconds + age_seconds
  # ceil(seconds * 1000) without floating understatement
  unc_ms = math.ceil(total_seconds * 1000.0 - 1e-12)
  if unc_ms < 0:
    return None
  if unc_ms > QGPSXTRATIME_PROVEN_UNCERTAINTY_MS:
    return None
  # Raise-only to the sole proven EG25-G value.
  return QGPSXTRATIME_PROVEN_UNCERTAINTY_MS


class _HostTimeSkip(Exception):
  """Internal: assistance skipped after lock (unrepresentable / invalid age)."""


def maybe_inject_host_time(
  *,
  read_observation=None,
  read_boottime=None,
  at_command=None,
  monotonic=time.monotonic,
  sleeper=time.sleep,
  flock=fcntl.flock,
) -> bool:
  """Inject host UTC into the modem only from independent network-synced time.

  Optional assistance under a short absolute budget (not modem-startup 90s).
  Timestamp/age/uncertainty are computed after the AT lock is held so lock wait
  is reflected in the sent parameters. Failures return False; AT+QGPS=1 must
  still proceed.
  """
  observe = read_observation or read_host_time_observation
  boottime = read_boottime or _read_boottime_seconds
  observation = observe()
  if not host_time_safe_for_qcom_injection(observation):
    try:
      cloudlog.info("QCOM host-time injection skipped: no authorized independent time")
    except Exception:
      pass
    return False
  assert observation is not None
  if observation.source is not HostTimeSource.NETWORK_SYNCHRONIZED:
    return False

  # Cheap pre-check: if the observation alone already exceeds the proven
  # uncertainty, skip without contending for the modem lock.
  if qgpsxtratime_uncertainty_ms(observation.uncertainty_seconds, 0.0) is None:
    try:
      cloudlog.info("QCOM host-time injection skipped: uncertainty unrepresentable")
    except Exception:
      pass
    return False

  deadline = monotonic() + QGPSXTRATIME_ASSISTANCE_BUDGET_SECONDS

  def build_cmd() -> str:
    boot_now = boottime()
    if boot_now is None:
      raise _HostTimeSkip("boottime unavailable")
    age = boot_now - observation.observed_boottime_seconds
    if not math.isfinite(age) or age < 0.0:
      raise _HostTimeSkip("observation age invalid")
    unc_ms = qgpsxtratime_uncertainty_ms(observation.uncertainty_seconds, age)
    if unc_ms is None:
      raise _HostTimeSkip("uncertainty unrepresentable")
    projected_utc = observation.utc + timedelta(seconds=age)
    time_str = projected_utc.replace(tzinfo=None).strftime("%Y/%m/%d,%H:%M:%S")
    return f'AT+QGPSXTRATIME=0,"{time_str}",1,1,{unc_ms}'

  try:
    if at_command is not None:
      # Test doubles: build once with current boottime (no real lock path).
      cmd = build_cmd()
      try:
        at_command(cmd, attempts=1, deadline=deadline)
      except TypeError:
        at_command(cmd)
    else:
      _at_cmd_once(
        "AT+QGPSXTRATIME",
        serial_timeout=QGPSXTRATIME_ASSISTANCE_BUDGET_SECONDS,
        deadline=deadline,
        monotonic=monotonic,
        sleeper=sleeper,
        flock=flock,
        build_cmd=build_cmd,
      )
  except _HostTimeSkip as exc:
    try:
      cloudlog.info(f"QCOM host-time injection skipped: {exc}")
    except Exception:
      pass
    return False
  except (AtCommandError, AtCommandTimeout, OSError) as exc:
    try:
      cloudlog.info(f"QCOM host-time injection failed open: {type(exc).__name__}")
    except Exception:
      pass
    return False
  return True


@retry(attempts=5, delay=1.0)
def setup_quectel(diag: ModemDiag):
  # Observe NV OEM feature mask; writes are fail-closed (unproven DRE bit).
  ensure_gnss_oem_feature_mask(diag)

  try_setup_logs(diag, LOG_TYPES)

  if gps_enabled():
    at_cmd("AT+QGPSEND")

  # disable DPO power savings for more accuracy
  at_cmd('AT+QGPSCFG="dpoenable",0')
  # don't automatically turn on GNSS on powerup
  at_cmd('AT+QGPSCFG="autogps",0')

  # Optional: must not raise into this @retry setup or delay AT+QGPS=1.
  maybe_inject_host_time()

  at_cmd('AT+QGPSCFG="outport","usbnmea"')
  at_cmd("AT+QGPS=1")

  # enable OEMDRE mode
  send_recv(
    diag,
    DIAG_SUBSYS_CMD_F,
    pack(
      "<BHBBIIII",
      DIAG_SUBSYS_GPS,
      CGPS_DIAG_PDAPI_CMD,
      CGPS_OEM_CONTROL,
      GPSDIAG_OEM_CONTROL_VERSION,
      GPSDIAG_OEMFEATURE_DRE,
      GPSDIAG_OEM_DRE_ON,
      0,
      0,
    ),
    match=_gps_oem_control_match,
  )


def teardown_quectel(diag):
  at_cmd('AT+QGPSCFG="outport","none"')
  if gps_enabled():
    at_cmd("AT+QGPSEND")
  try_setup_logs(diag, [])


def wait_for_modem(
  *,
  deadline_seconds: float = MODEM_STARTUP_DEADLINE_SECONDS,
  sleep_seconds: float = 0.5,
  monotonic=time.monotonic,
  sleeper=time.sleep,
  path_exists=os.path.exists,
  at_command=None,
) -> None:
  cloudlog.warning("waiting for modem to come up")
  started = monotonic()
  deadline = started + deadline_seconds

  def _at(cmd: str) -> str:
    if at_command is not None:
      # Test-injected command may accept deadline kwarg.
      try:
        return at_command(cmd, deadline=deadline)
      except TypeError:
        return at_command(cmd)
    return at_cmd(cmd, deadline=deadline, monotonic=monotonic, sleeper=sleeper)

  while not path_exists(AT_PORT):
    if monotonic() >= deadline:
      raise ModemStartupTimeout("modem AT port did not appear before deadline")
    sleeper(min(sleep_seconds, max(0.0, deadline - monotonic())))
  while True:
    if monotonic() >= deadline:
      raise ModemStartupTimeout("modem GNSS subsystem did not become ready before deadline")
    try:
      resp = _at("AT+QGPS?")
      if "+QGPS:" in resp:
        return
    except Exception:
      pass
    remaining = deadline - monotonic()
    if remaining <= 0.0:
      raise ModemStartupTimeout("modem GNSS subsystem did not become ready before deadline")
    sleeper(min(sleep_seconds, remaining))


def parse_diag_log_packet(payload: bytes) -> tuple[int, int, bytes] | None:
  """Return (log_type, log_time, log_payload) or None on malformed input."""
  if len(payload) < calcsize("<BH"):
    return None
  (_pending_msgs, log_outer_length), inner_log_packet = (
    unpack_from("<BH", payload),
    payload[calcsize("<BH") :],
  )
  if log_outer_length != len(inner_log_packet):
    return None
  if len(inner_log_packet) < calcsize("<HHQ"):
    return None
  (log_inner_length, log_type, log_time), log_payload = (
    unpack_from("<HHQ", inner_log_packet),
    inner_log_packet[calcsize("<HHQ") :],
  )
  if log_inner_length != len(inner_log_packet):
    return None
  return log_type, log_time, log_payload


def process_oemdre_measurement_report(
  log_time: int,
  log_payload: bytes,
  *,
  unpack_oemdre_meas,
  size_oemdre_meas: int,
  unpack_oemdre_meas_sv,
  size_oemdre_meas_sv: int,
):
  """Return a qcomGnss message or None when the payload is malformed."""
  if len(log_payload) < size_oemdre_meas:
    return None
  try:
    dat = unpack_oemdre_meas(log_payload)
  except (struct.error, ValueError, TypeError):
    return None
  if dat.get("version") != 2:
    return None
  sv_count = dat.get("svCount")
  if type(sv_count) is not int or sv_count < 0 or sv_count > MAX_SV_COUNT:
    return None
  sats = log_payload[size_oemdre_meas:]
  if len(sats) != sv_count * size_oemdre_meas_sv:
    return None

  msg = messaging.new_message("qcomGnss", valid=True)
  gnss = msg.qcomGnss
  gnss.logTs = log_time
  gnss.init("drMeasurementReport")
  report = gnss.drMeasurementReport
  for k, v in dat.items():
    if k in ["gpsTimeBias", "gpsClockTimeUncertainty"]:
      k += "Ms"
    if k == "version":
      pass
    elif k == "svCount" or k.startswith("cdmaClockInfo["):
      pass
    elif k == "systemRtcValid":
      setattr(report, k, bool(v))
    else:
      setattr(report, k, v)
  report.init("sv", sv_count)
  for i in range(sv_count):
    try:
      sat = unpack_oemdre_meas_sv(sats[size_oemdre_meas_sv * i : size_oemdre_meas_sv * (i + 1)])
    except (struct.error, ValueError, TypeError):
      return None
    sv = report.sv[i]
    sv.init("measurementStatus")
    for k, v in sat.items():
      if k in ["unkn", "measurementStatus2"]:
        pass
      elif k == "multipathEstimateValid":
        sv.measurementStatus.multipathEstimateIsValid = bool(v)
      elif k == "directionValid":
        sv.measurementStatus.directionIsValid = bool(v)
      elif k == "goodParity":
        setattr(sv, k, bool(v))
      elif k == "measurementStatus":
        for kk, vv in measurementStatusFields.items():
          setattr(sv.measurementStatus, kk, bool(v & (1 << vv)))
      else:
        setattr(sv, k, v)
  return msg


def process_position_report(
  log_payload: bytes,
  *,
  unpack_position,
  size_position: int,
  measurement_mono_ns: int | None = None,
):
  # Exact size contract for the openpilot/EG25 0x1476 struct layout.
  if len(log_payload) != size_position:
    return None
  try:
    report = unpack_position(log_payload)
  except (struct.error, ValueError, TypeError):
    return None
  version = report.get("u_Version")
  if type(version) is not int or version not in POSITION_REPORT_SUPPORTED_VERSIONS:
    return None
  if report.get("u_PosSource") != POS_SOURCE_KALMAN:
    return None
  fields = qcom_position_fields(report)
  if fields is None:
    return None
  msg = messaging.new_message("gpsLocation", valid=True)
  gps = msg.gpsLocation
  gps.latitude = fields.latitude
  gps.longitude = fields.longitude
  gps.altitude = fields.altitude
  gps.speed = fields.speed
  gps.bearingDeg = fields.bearing_deg
  gps.unixTimestampMillis = fields.unix_timestamp_millis
  gps.source = log.GpsLocationData.SensorSource.qcomdiag
  gps.vNED = list(fields.v_ned)
  gps.horizontalAccuracy = fields.horizontal_accuracy
  gps.verticalAccuracy = fields.vertical_accuracy
  gps.bearingAccuracyDeg = fields.bearing_accuracy_deg
  gps.speedAccuracy = fields.speed_accuracy
  gps.hasFix = fields.has_fix
  if hasattr(gps, "satelliteCount"):
    gps.satelliteCount = fields.satellite_count
  # Host mono when DIAG position payload became available — not later publish time.
  if measurement_mono_ns is not None and int(measurement_mono_ns) > 0:
    gps.measurementMonoNs = int(measurement_mono_ns)
  return msg


def process_svpoly_report(log_time: int, log_payload: bytes, *, unpack_svpoly, size_svpoly: int):
  if len(log_payload) < size_svpoly:
    return None
  try:
    dat = unpack_svpoly(log_payload)
  except (struct.error, ValueError, TypeError):
    return None
  dat = relist(dat)
  if dat.get("version") != 2:
    return None
  msg = messaging.new_message("qcomGnss", valid=True)
  gnss = msg.qcomGnss
  gnss.logTs = log_time
  gnss.init("drSvPoly")
  poly = gnss.drSvPoly
  for k, v in dat.items():
    if k in ("version", "flags"):
      continue
    setattr(poly, k, v)
  return msg


def process_constellation_measurement_report(
  log_type: int,
  log_time: int,
  log_payload: bytes,
  *,
  unpack_gps_meas,
  size_gps_meas: int,
  unpack_gps_meas_sv,
  size_gps_meas_sv: int,
  unpack_glonass_meas,
  size_glonass_meas: int,
  unpack_glonass_meas_sv,
  size_glonass_meas_sv: int,
):
  if log_type == LOG_GNSS_GPS_MEASUREMENT_REPORT:
    if len(log_payload) < size_gps_meas:
      return None
    try:
      dat = unpack_gps_meas(log_payload)
    except (struct.error, ValueError, TypeError):
      return None
    sats = log_payload[size_gps_meas:]
    unpack_meas_sv, size_meas_sv = unpack_gps_meas_sv, size_gps_meas_sv
    source = 0
    measurement_status_fields = (
      measurementStatusFields.items(),
      measurementStatusGPSFields.items(),
    )
  elif log_type == LOG_GNSS_GLONASS_MEASUREMENT_REPORT:
    if len(log_payload) < size_glonass_meas:
      return None
    try:
      dat = unpack_glonass_meas(log_payload)
    except (struct.error, ValueError, TypeError):
      return None
    sats = log_payload[size_glonass_meas:]
    unpack_meas_sv, size_meas_sv = unpack_glonass_meas_sv, size_glonass_meas_sv
    source = 1
    measurement_status_fields = (
      measurementStatusFields.items(),
      measurementStatusGlonassFields.items(),
    )
  else:
    return None

  if dat.get("version", 0) != 0:
    return None
  sv_count = dat.get("svCount")
  if type(sv_count) is not int or sv_count < 0 or sv_count > MAX_SV_COUNT:
    return None
  if len(sats) != sv_count * size_meas_sv:
    return None

  msg = messaging.new_message("qcomGnss", valid=True)
  gnss = msg.qcomGnss
  gnss.logTs = log_time
  gnss.init("measurementReport")
  report = gnss.measurementReport
  report.source = source
  for k, v in dat.items():
    if k == "version":
      pass
    elif k == "week":
      report.gpsWeek = v
    elif k == "svCount":
      pass
    else:
      setattr(report, k, v)
  report.init("sv", sv_count)
  for i in range(sv_count):
    try:
      sat = unpack_meas_sv(sats[size_meas_sv * i : size_meas_sv * (i + 1)])
    except (struct.error, ValueError, TypeError):
      return None
    sv = report.sv[i]
    sv.init("measurementStatus")
    for k, v in sat.items():
      if k == "parityErrorCount":
        sv.gpsParityErrorCount = v
      elif k == "frequencyIndex":
        sv.glonassFrequencyIndex = v
      elif k == "hemmingErrorCount":
        sv.glonassHemmingErrorCount = v
      elif k == "measurementStatus":
        for kk, vv in itertools.chain(*measurement_status_fields):
          setattr(sv.measurementStatus, kk, bool(v & (1 << vv)))
      elif k == "miscStatus":
        for kk, vv in miscStatusFields.items():
          setattr(sv.measurementStatus, kk, bool(v & (1 << vv)))
      elif k == "pad":
        pass
      else:
        setattr(sv, k, v)
  return msg


def main() -> NoReturn:
  unpack_gps_meas, size_gps_meas = dict_unpacker(gps_measurement_report, True)
  unpack_gps_meas_sv, size_gps_meas_sv = dict_unpacker(gps_measurement_report_sv, True)

  unpack_glonass_meas, size_glonass_meas = dict_unpacker(glonass_measurement_report, True)
  unpack_glonass_meas_sv, size_glonass_meas_sv = dict_unpacker(glonass_measurement_report_sv, True)

  unpack_oemdre_meas, size_oemdre_meas = dict_unpacker(oemdre_measurement_report, True)
  unpack_oemdre_meas_sv, size_oemdre_meas_sv = dict_unpacker(oemdre_measurement_report_sv, True)

  unpack_svpoly, size_svpoly = dict_unpacker(oemdre_svpoly_report, True)
  unpack_position, size_position = dict_unpacker(position_report)

  wait_for_modem()

  # GNSS_PWR_EN is the u-blox power rail (GPIO_UBLOX_PWR_EN). When u-blox is
  # present, pigeond owns that rail; qcomgpsd must not toggle it or cleanup
  # would power-kill the primary receiver during concurrent failover mode.
  manage_ublox_rail = not ublox_hardware_available()

  def cleanup(sig, frame):
    cloudlog.warning("caught sig disabling quectel gps")

    if manage_ublox_rail:
      gpio_set(GPIO.GNSS_PWR_EN, False)
    try:
      teardown_quectel(diag)
      cloudlog.warning("quectel cleanup done")
    except NameError:
      cloudlog.warning("quectel not yet setup")

    sys.exit(0)

  signal.signal(signal.SIGINT, cleanup)
  signal.signal(signal.SIGTERM, cleanup)

  diag = ModemDiag()
  setup_quectel(diag)
  cloudlog.warning("quectel setup done")
  if manage_ublox_rail:
    gpio_init(GPIO.GNSS_PWR_EN, True)
    gpio_set(GPIO.GNSS_PWR_EN, True)
  else:
    cloudlog.warning("qcomgpsd skipping GNSS_PWR_EN; ublox hardware owns rail")

  pm = messaging.PubMaster(["qcomGnss", "gpsLocation"])

  while 1:
    try:
      opcode, payload = diag.recv()
    except DiagFramingError:
      cloudlog.warning("QCOM DIAG framing error; resynchronizing")
      continue
    except DiagTimeoutError:
      continue
    if opcode != DIAG_LOG_F:
      cloudlog.error(f"Unhandled opcode: {opcode}")
      continue

    parsed = parse_diag_log_packet(payload)
    if parsed is None:
      cloudlog.warning("QCOM DIAG log packet malformed; discarding")
      continue
    log_type, log_time, log_payload = parsed

    if log_type not in LOG_TYPES:
      continue

    if DEBUG:
      print(f"{time.time():.4f}: got log: {log_type} len {len(log_payload)}")  # noqa: TID251

    try:
      if log_type == LOG_GNSS_OEMDRE_MEASUREMENT_REPORT:
        msg = process_oemdre_measurement_report(
          log_time,
          log_payload,
          unpack_oemdre_meas=unpack_oemdre_meas,
          size_oemdre_meas=size_oemdre_meas,
          unpack_oemdre_meas_sv=unpack_oemdre_meas_sv,
          size_oemdre_meas_sv=size_oemdre_meas_sv,
        )
        if msg is not None:
          pm.send("qcomGnss", msg)

      elif log_type == LOG_GNSS_POSITION_REPORT:
        # Capture host mono at DIAG payload availability (before parse/publish delay).
        measurement_mono_ns = time.monotonic_ns()
        msg = process_position_report(
          log_payload,
          unpack_position=unpack_position,
          size_position=size_position,
          measurement_mono_ns=measurement_mono_ns,
        )
        if msg is not None:
          pm.send("gpsLocation", msg)

      elif log_type == LOG_GNSS_OEMDRE_SVPOLY_REPORT:
        msg = process_svpoly_report(
          log_time,
          log_payload,
          unpack_svpoly=unpack_svpoly,
          size_svpoly=size_svpoly,
        )
        if msg is not None:
          pm.send("qcomGnss", msg)

      elif log_type in (
        LOG_GNSS_GPS_MEASUREMENT_REPORT,
        LOG_GNSS_GLONASS_MEASUREMENT_REPORT,
      ):
        msg = process_constellation_measurement_report(
          log_type,
          log_time,
          log_payload,
          unpack_gps_meas=unpack_gps_meas,
          size_gps_meas=size_gps_meas,
          unpack_gps_meas_sv=unpack_gps_meas_sv,
          size_gps_meas_sv=size_gps_meas_sv,
          unpack_glonass_meas=unpack_glonass_meas,
          size_glonass_meas=size_glonass_meas,
          unpack_glonass_meas_sv=unpack_glonass_meas_sv,
          size_glonass_meas_sv=size_glonass_meas_sv,
        )
        if msg is not None:
          pm.send("qcomGnss", msg)
    except (struct.error, ValueError, TypeError, KeyError) as exc:
      cloudlog.warning(f"QCOM DIAG log payload rejected: {type(exc).__name__}")
      continue


if __name__ == "__main__":
  main()
