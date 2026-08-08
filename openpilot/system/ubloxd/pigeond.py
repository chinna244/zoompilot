#!/usr/bin/env python3
import re
import json
import os
import sys
import time
import signal
from openpilot.common.serial import Serial
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, UTC
from enum import StrEnum
from functools import partial
from math import ceil, isfinite
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Any, cast

from openpilot.cereal import log, messaging
from openpilot.common.time_helpers import (
  HostTimeObservation,
  HostTimeSource,
  read_host_time_observation,
)
from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.common.hardware import HARDWARE, TICI
from openpilot.common.gpio import gpio_init, gpio_set
from openpilot.common.hardware.tici.pins import GPIO
from openpilot.system.ubloxd.ubx import Ubx
from openpilot.system.ubloxd.gps_assistance import (
  CacheAgeEvidence,
  CacheValidationError,
  CachePromotionStatus,
  CacheQualityTier,
  CaptureQualityTracker,
  MAXIMUM_NAV_PVT_GAP_SECONDS,
  MAXIMUM_NAV_SAT_AGE_SECONDS,
  MINIMUM_GLONASS_EPHEMERIS,
  MINIMUM_GPS_EPHEMERIS,
  MINIMUM_ORBIT_QUALITY_SECONDS,
  MINIMUM_RELIABLE_FIX_SECONDS,
  MINIMUM_SATELLITES_USED,
  MINIMUM_TOTAL_EPHEMERIS,
  NAVX5_MASK1_ACK_AIDING,
  NAVX5_MASK1_AOP,
  GPS_ASSISTANCE_CACHE_PATH,
  MAX_RTC_ASSISTANCE_ELAPSED_SECONDS,
  GnssConfig,
  MgaAck,
  MonVerInfo,
  NavAopStatus,
  NavSatQuality,
  Navx5Config,
  NavPvtFix,
  NavigationQuality,
  NavigationCacheStore,
  NavigationDatabaseDumpCollector,
  Pm2Config,
  PortConfig,
  RateConfig,
  Nav5Config,
  OdoConfig,
  ItfmConfig,
  MessageRateConfig,
  ReliableFixTracker,
  RestoredNavigationQuality,
  RtcEstimateRejection,
  RtcEstimateRejectionReason,
  RxmConfig,
  UbxStreamParser,
  build_cfg_gnss_poll_message,
  build_cfg_itfm_poll_message,
  build_cfg_msg_poll_message,
  build_cfg_nav5_poll_message,
  build_cfg_nav5_set_message,
  build_cfg_odo_poll_message,
  build_cfg_pm2_poll_message,
  build_cfg_prt_poll_message,
  build_cfg_rate_poll_message,
  build_cfg_rxm_poll_message,
  build_database_poll_message,
  build_durable_receiver_fingerprint,
  age_safe_restore_position_accuracy_cm,
  build_nav_aopstatus_poll_message,
  build_navx5_ack_aiding_enable_message,
  build_navx5_aop_enable_message,
  build_navx5_poll_message,
  build_position_assistance_message,
  build_time_assistance_message,
  capture_eligible,
  conservative_navigation_quality,
  create_cache,
  effective_restored_navigation_quality,
  load_cache,
  parse_mga_ack,
  parse_cfg_gnss,
  parse_cfg_itfm,
  parse_cfg_msg,
  parse_cfg_nav5,
  parse_cfg_odo,
  parse_cfg_pm2,
  parse_cfg_prt,
  parse_cfg_rate,
  parse_cfg_rxm,
  parse_mon_ver,
  parse_nav_aopstatus,
  parse_nav_pvt,
  parse_nav_sat,
  parse_navx5,
  parse_upd_sos_response,
  read_rtc_counter_seconds,
  select_rtc_estimate,
  normalized_receiver_identity,
  navx5_unrelated_fields_unchanged,
  navigation_quality_strictly_better,
  navigation_quality_tier,
  validate_ubx_frame,
)
from openpilot.system.ubloxd.navigation_database_restore import (
  NavigationDatabaseRestoreDisposition,
  is_current_independent_network_time,
)
from openpilot.system.ubloxd.navigation_database_restore_runtime import (
  NavigationDatabaseRestoreExecution,
  NavigationDatabaseRestoreFrameFailureKind,
  NavigationDatabaseRestoreInitializationError,
  NavigationDatabaseRestoreRuntime,
  NavigationDatabaseRestoreUnavailableRuntime,
  PositionAssistanceAckStatus,
  PositionAssistanceFailureKind,
  PositionAssistanceWriteStatus,
  quarantine_navigation_database_restore_boot_state,
  store_navigation_database_restore_boot_state,
)
from openpilot.system.ubloxd.position_assistance_retry import (
  PositionAssistanceRetryResult,
  PositionAssistanceRetryRuntime,
  PositionAssistanceRetryState,
  store_position_assistance_retry_state,
)
from openpilot.system.ubloxd.receiver_time_provenance import (
  ReceiverTimeProvenanceTracker,
  ReceiverUtcClassification,
  ReceiverUtcObservation,
  is_mga_time_assistance_message,
)
from openpilot.system.ubloxd.provisional_yuma_reference import (
  PROVISIONAL_YUMA_DISABLE_REASON_VALIDATION_DISAGREES,
  ProvisionalYumaReferenceDecision,
  ProvisionalYumaReferenceTime,
  ProvisionalYumaTransmissionOutcome,
  evaluate_provisional_yuma_reference,
  load_provisional_yuma_boot_disable_state,
  store_provisional_yuma_boot_disable_state,
  store_provisional_yuma_decision_event,
  transmit_provisional_yuma_reference,
)
from openpilot.system.ubloxd.rtc_time_observation import (
  CrossBootRtcObservation,
  RtcObservationState,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
  TrustedTimeSource,
  read_boot_id,
  read_boottime_seconds,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AnchorWriteStatus,
  AuthorizedTime,
  TimeAuthority,
  TimeAuthorityEvaluation,
)
from openpilot.system.ubloxd.trusted_time_validation import (
  CrossBootRtcValidation,
  CrossBootRtcValidationStatus,
  IndependentTimeObservation,
  ReceiverCorrectionDecision,
  evaluate_receiver_correction,
  validate_cross_boot_rtc,
)
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaDatabaseRestoreState,
  YumaSupplementationReason,
)
from openpilot.system.ubloxd.yuma_almanac_runtime import (
  YumaSupplementationRuntime,
  YumaSupplementationRuntimeOutcome,
)
from openpilot.system.ubloxd.yuma_almanac_transmit import (
  MgaReceiverNackError,
  MgaTransactionError,
  MgaWriteError,
  YumaAssistanceStateUnavailableError,
)
from openpilot.system.ubloxd.yuma_almanac_config import (
  PUBLIC_YUMA_ALMANAC_PARAM_POLL_SECONDS,
  public_yuma_almanac_enabled,
)
from openpilot.system.ubloxd.yuma_almanac_outcome import (
  YUMA_LAST_OUTCOME_PATH,
  save_yuma_supplementation_outcome,
)


UBLOX_TTY = "/dev/ttyHS0"

UBLOX_ACK = b"\xb5\x62\x05\x01\x02\x00"
UBLOX_NACK = b"\xb5\x62\x05\x00\x02\x00"
UBLOX_ASSIST_ACK = b"\xb5\x62\x13\x60\x08\x00"

TIME_SYNC_CHECK_INTERVAL = 5.0
HOST_TIME_PERSISTENCE_RETRY_INTERVAL = 5.0
TIME_ASSISTANCE_RETRY_INTERVAL = 30.0

GPS_ASSISTANCE_ACK_TIMEOUT = 0.75
GPS_ASSISTANCE_FRAME_RETRY_DELAY = 0.25
MON_VER_POLL_TIMEOUT = 0.5
RECEIVER_TRANSPORT_PROBE_TIMEOUT = MON_VER_POLL_TIMEOUT
PROCESS_START_RECEIVER_TRANSPORT_MAX_ATTEMPTS = 3
RUNTIME_RECOVERY_RECEIVER_TRANSPORT_MAX_ATTEMPTS = 1
NAVX5_POLL_TIMEOUT = 0.5
CFG_ACK_TIMEOUT = 1.1
NAVX5_ACK_TIMEOUT = CFG_ACK_TIMEOUT
AOP_STATUS_POLL_TIMEOUT = 0.1
AOP_IDLE_WAIT_TIMEOUT = 0.3
AOP_IDLE_POLL_INTERVAL = 0.05
ACQUISITION_CONFIG_POLL_TIMEOUT = 0.25
GPS_ASSISTANCE_CAPTURE_RETRY_INTERVAL = 60.0
GPS_ASSISTANCE_QUALIFIED_UPGRADE_COOLDOWN = 5.0 * 60.0
GPS_ACQUISITION_STATUS_INTERVAL = 30.0
PRE_TRANSACTION_DRAIN_MAX_BYTES = 64 * 1024
CONTROLLED_GNSS_STOP_MESSAGE = b"\xB5\x62\x06\x04\x04\x00\x00\x00\x08\x00\x16\x74"
CONTROLLED_GNSS_START_MESSAGE = b"\xB5\x62\x06\x04\x04\x00\x00\x00\x09\x00\x17\x76"
CONTROLLED_GNSS_TRANSITION_DELAY = 0.05
# The absolute pre-START deadline is measured from receiver cycle start.
# Receiver initialization samples trusted time once and never waits for
# independent network time before GNSS START.
NAVIGATION_DATABASE_PROCESS_START_TIME_DEADLINE_SECONDS = 45.0
# Leave enough time to return from a bounded assistance-setup wait and reach
# the GNSS START UART boundary. Unbounded factory/cache work never runs on the
# startup thread.
PRE_START_ASSISTANCE_SETUP_RESERVE_SECONDS = 0.5
# A validated MGA-DBD cache is capped at 64 KiB. Frequent dispatch between
# transactions is the primary bound; these limits retain four cache volumes or
# 512 small navigation frames if a dispatcher is temporarily unavailable.
PENDING_FRAME_MAX_COUNT = 512
PENDING_FRAME_MAX_BYTES = 256 * 1024
RECEIVER_CONFIGURATION_PROCESS_START_ID = (
  f"{os.getpid()}:{time.monotonic_ns()}"
)
RECEIVER_CONFIGURATION_BOOT_ID = (
  read_boot_id()
  or f"unavailable:{RECEIVER_CONFIGURATION_PROCESS_START_ID}"
)

NAVX5_ACK_AIDING_SOFTWARE_VERSION_PATTERN = re.compile(
  r"EXT CORE 3\.01(?: \([A-Z0-9][A-Z0-9._-]{0,31}\))?"
)


class ReceiverConfigurationError(RuntimeError):
  pass


class ReceiverConfigurationParserError(ReceiverConfigurationError):
  pass


class ReceiverConfigurationUnsupportedError(ReceiverConfigurationError):
  pass


class CfgNakError(ReceiverConfigurationError):
  def __init__(
    self,
    message: str,
    retry_not_before: float | None = None,
  ) -> None:
    self.retry_not_before = retry_not_before
    super().__init__(message)


class ResponseTransactionError(RuntimeError):
  pass


class PendingFrameOverflowError(ResponseTransactionError):
  def __init__(
    self,
    frame_count: int,
    byte_count: int,
    operation: str,
    receiver_cycle: int,
    exceeded: str,
  ) -> None:
    self.frame_count = frame_count
    self.byte_count = byte_count
    self.operation = operation
    self.receiver_cycle = receiver_cycle
    self.exceeded = exceeded
    super().__init__(
      f"Pending u-blox frame queue exceeded {exceeded}: "
      + f"frame_count={frame_count}, byte_count={byte_count}, "
      + f"operation={operation}, receiver_cycle={receiver_cycle}"
    )


class RawPublicationError(ResponseTransactionError):
  pass


class CfgPollTimeoutError(TimeoutError):
  pass


@dataclass
class ResponseTransaction:
  parser: UbxStreamParser
  request: bytes = b""
  operation: str = "response_transaction"
  sent_at: float = 0.0

def set_power(enabled: bool) -> None:
  gpio_init(GPIO.UBLOX_SAFEBOOT_N, True)
  gpio_init(GPIO.GNSS_PWR_EN, True)
  gpio_init(GPIO.UBLOX_RST_N, True)

  gpio_set(GPIO.UBLOX_SAFEBOOT_N, True)
  gpio_set(GPIO.GNSS_PWR_EN, enabled)
  gpio_set(GPIO.UBLOX_RST_N, enabled)

def add_ubx_checksum(msg: bytes) -> bytes:
  A = B = 0
  for b in msg[2:]:
    A = (A + b) % 256
    B = (B + A) % 256
  return msg + bytes([A, B])

class TTYPigeon:
  def __init__(
    self,
    raw_publisher: Callable[[bytes], None] | None = None,
    frame_dispatcher: Callable[[list[bytes]], None] | None = None,
  ):
    self.tty = Serial(UBLOX_TTY, baudrate=9600, timeout=0)
    self._stream_parser = UbxStreamParser()
    self._pending_frames: deque[bytes] = deque()
    self._pending_frame_bytes = 0
    # A kernel-read chunk remains here until ubloxRaw publication succeeds, so
    # it stays accounted for while this process is alive. An unrecoverable
    # process-level messaging failure cannot provide the same guarantee.
    self._pending_unpublished: bytes | None = None
    self._raw_publisher = raw_publisher
    self._frame_dispatcher = frame_dispatcher
    self._receiver_cycle = 0
    self._receiver_cycle_response_state_prepared = False
    self.time_provenance: ReceiverTimeProvenanceTracker | None = None

  def set_frame_dispatcher(
    self,
    frame_dispatcher: Callable[[list[bytes]], None] | None,
  ) -> None:
    self._frame_dispatcher = frame_dispatcher

  def send(self, dat: bytes) -> None:
    self.tty.write(dat)

  @property
  def receiver_cycle(self) -> int:
    return self._receiver_cycle

  def _receive_tty(self) -> bytes:
    dat = b''
    while len(dat) < 0x1000:
      d = self.tty.read(0x40)
      dat += d
      if len(d) == 0:
        break
    return dat

  def receive(self) -> bytes:
    data, _ = self.receive_normal()
    return data

  def receive_normal(self) -> tuple[bytes, list[bytes]]:
    if self._pending_frames:
      frames = list(self._pending_frames)
      self._pending_frames.clear()
      self._pending_frame_bytes = 0
      return b"", frames

    return self._read_stream()

  def _publish_raw(self, data: bytes) -> None:
    if data and self._raw_publisher is not None:
      try:
        self._raw_publisher(data)
      except Exception as exc:
        raise RawPublicationError(
          f"Failed to publish retained u-blox UART chunk: byte_count={len(data)}"
        ) from exc

  def _read_stream(self) -> tuple[bytes, list[bytes]]:
    data = self._pending_unpublished
    if data is None:
      data = self._receive_tty()
      if data:
        self._pending_unpublished = data
    self._publish_raw(data)
    if data:
      self._pending_unpublished = None
    return data, self._stream_parser.feed(data)

  def receive_transaction_data(
    self,
    transaction: ResponseTransaction,
  ) -> tuple[bytes, list[bytes], list[bytes]]:
    data, stream_frames = self._read_stream()
    return data, stream_frames, transaction.parser.feed(data)

  def queue_pending_frames(
    self,
    frames: list[bytes],
    operation: str = "response_transaction",
  ) -> None:
    added_bytes = sum(len(frame) for frame in frames)
    self._pending_frames.extend(frames)
    self._pending_frame_bytes += added_bytes
    exceeded = None
    if len(self._pending_frames) > PENDING_FRAME_MAX_COUNT:
      exceeded = "frame_limit"
    elif self._pending_frame_bytes > PENDING_FRAME_MAX_BYTES:
      exceeded = "byte_limit"
    if exceeded is not None:
      error = PendingFrameOverflowError(
        len(self._pending_frames),
        self._pending_frame_bytes,
        operation,
        self._receiver_cycle,
        exceeded,
      )
      cloudlog.error(f"GPS synchronous frame queue overflow: {error}")
      raise error

  def dispatch_pending_frames(self) -> None:
    if not self._pending_frames or self._frame_dispatcher is None:
      return
    frames = list(self._pending_frames)
    self._frame_dispatcher(frames)
    self._pending_frames.clear()
    self._pending_frame_bytes = 0

  def drain_before_transaction(
    self,
    operation: str = "pre_transaction_drain",
    deadline: float | None = None,
  ) -> None:
    if deadline is not None and time.monotonic() >= deadline:
      raise TimeoutError(
        f"{operation} deadline exhausted before pending-frame dispatch"
      )
    self.dispatch_pending_frames()
    if deadline is not None and time.monotonic() >= deadline:
      raise TimeoutError(
        f"{operation} deadline exhausted during pending-frame dispatch"
      )
    drained_bytes = 0
    while True:
      if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(
          f"{operation} deadline exhausted before receiver read"
        )
      data, frames = self._read_stream()
      self.queue_pending_frames(frames, operation)
      if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(
          f"{operation} deadline exhausted during receiver read"
        )
      self.dispatch_pending_frames()
      if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError(
          f"{operation} deadline exhausted during pending-frame dispatch"
        )
      if not data:
        return
      drained_bytes += len(data)
      if drained_bytes >= PRE_TRANSACTION_DRAIN_MAX_BYTES:
        raise ResponseTransactionError(
          "Pre-transaction u-blox input drain exceeded its deterministic bound"
        )

  def begin_response_transaction(
    self,
    data: bytes,
    operation: str = "response_transaction",
    before_send: Callable[[], None] | None = None,
    deadline: float | None = None,
  ) -> ResponseTransaction:
    self.drain_before_transaction(operation, deadline=deadline)
    if deadline is not None and time.monotonic() >= deadline:
      raise TimeoutError(
        f"{operation} deadline exhausted before UART write"
      )
    if before_send is not None:
      before_send()
    if deadline is not None and time.monotonic() >= deadline:
      raise TimeoutError(
        f"{operation} deadline exhausted before UART write"
      )
    parser = UbxStreamParser()
    self.send(data)
    sent_at = time.monotonic()
    return ResponseTransaction(parser, data, operation, sent_at)

  def reset_response_state(self) -> None:
    if self._pending_unpublished is not None:
      self._publish_raw(self._pending_unpublished)
      data = self._pending_unpublished
      self._pending_unpublished = None
      self.queue_pending_frames(
        self._stream_parser.feed(data), "receiver_cycle_reset",
      )
    self.dispatch_pending_frames()
    self._stream_parser.reset()
    self._pending_frames.clear()
    self._pending_frame_bytes = 0
    self._receiver_cycle += 1

  def set_baud(self, baud: int) -> None:
    self.tty.baudrate = baud

  def wait_for_ack(
    self,
    transaction: ResponseTransaction,
    ack: bytes = UBLOX_ACK,
    nack: bytes = UBLOX_NACK,
    timeout: float = 0.5,
  ) -> bool:
    ubx_ack_response = ack == UBLOX_ACK and nack == UBLOX_NACK
    acknowledged_key = transaction.request[2:4]
    st = time.monotonic()

    while True:
      result: bool | None = None
      _, stream_frames, transaction_frames = _receive_transaction_data(
        self,
        transaction,
      )

      for frame in transaction_frames:
        if ubx_ack_response:
          if (
            result is None
            and len(frame) == 10
            and frame[2] == 0x05
            and frame[3] in (0x00, 0x01)
            and frame[6:8] == acknowledged_key
          ):
            result = frame[3] == 0x01
            continue
        elif (
          result is None
          and frame.startswith((ack, nack))
        ):
          result = frame.startswith(ack)
          continue

      _queue_unrelated_frames(
        self,
        stream_frames,
        lambda frame: (
          len(frame) == 10
          and frame[2] == 0x05
          and frame[3] in (0x00, 0x01)
          and frame[6:8] == acknowledged_key
        ) if ubx_ack_response else frame.startswith((ack, nack)),
        transaction.operation,
      )

      if result is not None:
        if result:
          cloudlog.debug("Received ACK from ublox")
        else:
          cloudlog.error("Received NACK from ublox")

        return result

      if time.monotonic() - st > timeout:
        cloudlog.error("No response from ublox")
        raise TimeoutError("No response from ublox")

      time.sleep(0.001)

  def send_with_ack(
    self,
    dat: bytes,
    ack: bytes = UBLOX_ACK,
    nack: bytes = UBLOX_NACK,
    before_send: Callable[[], None] | None = None,
    timeout: float | None = None,
    deadline: float | None = None,
  ) -> None:
    if (
      ack == UBLOX_ACK
      and nack == UBLOX_NACK
      and len(dat) >= 4
      and dat[:3] == b"\xB5\x62\x06"
    ):
      if not validate_ubx_frame(dat):
        raise ReceiverConfigurationError("Attempted to send an invalid UBX CFG frame")
      transaction = self.begin_response_transaction(
        dat,
        f"cfg_write_{dat[2]:02x}_{dat[3]:02x}",
        before_send=before_send,
        deadline=deadline,
      )
      ack_timeout = CFG_ACK_TIMEOUT if timeout is None else timeout
      if deadline is not None:
        ack_timeout = min(ack_timeout, max(0.0, deadline - time.monotonic()))
      acknowledgment = wait_for_cfg_ack(
        self, transaction, dat[2], dat[3],
        ack_timeout,
        deadline=deadline,
      )
      if acknowledgment is False:
        raise CfgNakError(
          f"u-blox rejected CFG message 0x{dat[2]:02X} 0x{dat[3]:02X}",
          transaction.sent_at + CFG_ACK_TIMEOUT,
        )
      if acknowledgment is None:
        raise TimeoutError(f"No matching acknowledgment for CFG message 0x{dat[2]:02X} 0x{dat[3]:02X}")
      return
    if ack == UBLOX_ASSIST_ACK:
      send_mga_with_strict_ack(
        self,
        dat,
        timeout=GPS_ASSISTANCE_ACK_TIMEOUT,
        time_provenance=getattr(
          self,
          "time_provenance",
          None,
        ),
        time_assistance_source="assistnow_online",
      )
      return
    transaction = self.begin_response_transaction(
      dat, f"ubx_write_{dat[2]:02x}_{dat[3]:02x}",
    )
    if not self.wait_for_ack(
      transaction, ack, nack,
      timeout=CFG_ACK_TIMEOUT if ack == UBLOX_ACK else 0.5,
    ):
      raise CfgNakError(
        f"u-blox rejected message 0x{dat[2]:02X} 0x{dat[3]:02X}",
        transaction.sent_at + CFG_ACK_TIMEOUT,
      )

  def wait_for_backup_restore_status(
    self,
    transaction: ResponseTransaction,
    timeout: float = 1.,
  ) -> int:
    st = time.monotonic()
    while True:
      status = None
      _, stream_frames, transaction_frames = _receive_transaction_data(self, transaction)
      for frame in transaction_frames:
        response = parse_upd_sos_response(frame)
        if (
          status is None
          and response is not None
          and response.command == 3
          and response.response in (1, 2, 3)
        ):
          status = response.response
          continue
      _queue_unrelated_frames(
        self,
        stream_frames,
        lambda frame: (
          (response := parse_upd_sos_response(frame)) is not None
          and response.command == 3
        ),
        transaction.operation,
      )
      if status is not None:
        return status
      if time.monotonic() - st > timeout:
        cloudlog.error("No backup restore response from ublox")
        raise TimeoutError('No response from ublox')
      time.sleep(0.001)

  def poll_backup_restore_status(self, timeout: float = 1.) -> int:
    transaction = self.begin_response_transaction(
      b"\xB5\x62\x09\x14\x00\x00\x1D\x60",
      "upd_sos_restore_status_poll",
    )
    return self.wait_for_backup_restore_status(transaction, timeout)

  def reset_device(self) -> bool:
    # deleting the backup does not always work on first try (mostly on second try)
    for _ in range(5):
      # device cold start
      self.send(b"\xb5\x62\x06\x04\x04\x00\xff\xff\x00\x00\x0c\x5d")
      time.sleep(1) # wait for cold start
      init_baudrate(self)

      # clear configuration
      self.send_with_ack(b"\xb5\x62\x06\x09\x0d\x00\x1f\x1f\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x17\x71\xd7")

      # clear flash memory (almanac backup)
      self.send_with_ack(b"\xB5\x62\x09\x14\x04\x00\x01\x00\x00\x00\x22\xf0")

      # try restoring backup to verify it got deleted
      # 1: failed to restore, 2: could restore, 3: no backup
      status = self.poll_backup_restore_status()
      if status == 1 or status == 3:
        return True
    return False


def _receive_transaction_data(
  pigeon: TTYPigeon,
  transaction: ResponseTransaction,
) -> tuple[bytes, list[bytes], list[bytes]]:
  if hasattr(pigeon, "_stream_parser"):
    return pigeon.receive_transaction_data(transaction)
  data = pigeon.receive()
  frames = transaction.parser.feed(data)
  return data, frames, frames


def _queue_unrelated_frames(
  pigeon: TTYPigeon,
  frames: list[bytes],
  is_response: Callable[[bytes], bool],
  operation: str = "response_transaction",
) -> None:
  if hasattr(pigeon, "_pending_frames"):
    pigeon.queue_pending_frames([
      frame for frame in frames if not is_response(frame)
    ], operation)
    pigeon.dispatch_pending_frames()


def _begin_response_transaction(
  pigeon: TTYPigeon,
  message: bytes,
  operation: str | None = None,
  before_send: Callable[[], None] | None = None,
  deadline: float | None = None,
) -> ResponseTransaction:
  operation = operation or f"ubx_{message[2]:02x}_{message[3]:02x}"
  if hasattr(pigeon, "begin_response_transaction"):
    if isinstance(pigeon, TTYPigeon):
      return pigeon.begin_response_transaction(
        message,
        operation,
        before_send=before_send,
        deadline=deadline,
      )
    if before_send is None:
      return pigeon.begin_response_transaction(message, operation)
    return pigeon.begin_response_transaction(message, operation, before_send)
  if deadline is not None and time.monotonic() >= deadline:
    raise TimeoutError(
      f"{operation} deadline exhausted before UART write"
    )
  if before_send is not None:
    before_send()
  if deadline is not None and time.monotonic() >= deadline:
    raise TimeoutError(
      f"{operation} deadline exhausted before UART write"
    )
  pigeon.send(message)
  return ResponseTransaction(UbxStreamParser(), message, operation, time.monotonic())


def build_mon_ver_poll_message() -> bytes:
  return add_ubx_checksum(b"\xb5\x62\x0a\x04\x00\x00")


def _wait_for_parsed_response[Response](
  pigeon: TTYPigeon,
  transaction: ResponseTransaction,
  response_parser: Callable[[bytes], Response | None],
  message_class: int,
  message_id: int,
  timeout: float,
  response_matches: Callable[[Response], bool] | None = None,
  malformed_response_name: str | None = None,
  deadline: float | None = None,
) -> Response | None:
  expected_key = bytes((message_class, message_id))

  def parse_response(frame: bytes) -> Response | None:
    is_expected = len(frame) >= 4 and frame[2:4] == expected_key
    try:
      parsed = response_parser(frame)
    except Exception as exc:
      if is_expected and malformed_response_name is not None:
        raise ReceiverConfigurationParserError(
          f"Malformed {malformed_response_name} response"
        ) from exc
      raise
    if is_expected and parsed is None and malformed_response_name is not None:
      raise ReceiverConfigurationParserError(
        f"Malformed {malformed_response_name} response"
      )
    return parsed

  response_deadline = time.monotonic() + timeout
  if deadline is not None:
    response_deadline = min(response_deadline, deadline)
  while time.monotonic() < response_deadline:
    result = None
    _, stream_frames, transaction_frames = _receive_transaction_data(pigeon, transaction)
    if deadline is not None and time.monotonic() >= deadline:
      return None
    for frame in transaction_frames:
      parsed = parse_response(frame)
      if parsed is not None:
        matches = response_matches is None or response_matches(parsed)
        if result is None and matches:
          result = parsed
    _queue_unrelated_frames(
      pigeon,
      stream_frames,
      lambda frame: (
        frame[2:4] == expected_key
        and (parsed := parse_response(frame)) is not None
        and (response_matches is None or response_matches(parsed))
      ),
      transaction.operation,
    )
    if deadline is not None and time.monotonic() >= deadline:
      return None
    if result is not None:
      return result
    remaining = response_deadline - time.monotonic()
    if remaining > 0.0:
      time.sleep(min(0.001, remaining))
  return None


def poll_mon_ver(
  pigeon: TTYPigeon,
  timeout: float = MON_VER_POLL_TIMEOUT,
) -> MonVerInfo | None:
  transaction = _begin_response_transaction(pigeon, build_mon_ver_poll_message())
  return _wait_for_parsed_response(
    pigeon, transaction, parse_mon_ver, 0x0A, 0x04, timeout,
  )


def _poll_acquisition_config[AcquisitionConfig](
  pigeon: TTYPigeon,
  poll_message: bytes,
  response_parser: Callable[[bytes], AcquisitionConfig | None],
  timeout: float,
  deadline: float | None = None,
) -> AcquisitionConfig | None:
  transaction = _begin_response_transaction(
    pigeon,
    poll_message,
    deadline=deadline,
  )
  config = _wait_for_parsed_response(
    pigeon,
    transaction,
    response_parser,
    poll_message[2],
    poll_message[3],
    timeout,
    deadline=deadline,
  )
  if (
    config is None
    and deadline is not None
    and time.monotonic() >= deadline
  ):
    raise TimeoutError(
      "receiver configuration pre-START deadline exhausted during readback"
    )
  return config


def poll_cfg_gnss(
  pigeon: TTYPigeon,
  timeout: float = ACQUISITION_CONFIG_POLL_TIMEOUT,
  deadline: float | None = None,
) -> GnssConfig | None:
  return _poll_acquisition_config(
    pigeon,
    build_cfg_gnss_poll_message(),
    parse_cfg_gnss,
    timeout,
    deadline,
  )


def poll_cfg_rxm(
  pigeon: TTYPigeon,
  timeout: float = ACQUISITION_CONFIG_POLL_TIMEOUT,
  deadline: float | None = None,
) -> RxmConfig | None:
  return _poll_acquisition_config(
    pigeon,
    build_cfg_rxm_poll_message(),
    parse_cfg_rxm,
    timeout,
    deadline,
  )


def poll_cfg_pm2(
  pigeon: TTYPigeon,
  timeout: float = ACQUISITION_CONFIG_POLL_TIMEOUT,
  deadline: float | None = None,
) -> Pm2Config | None:
  return _poll_acquisition_config(
    pigeon,
    build_cfg_pm2_poll_message(),
    parse_cfg_pm2,
    timeout,
    deadline,
  )


GNSS_NAMES = {
  0: "GPS",
  1: "SBAS",
  2: "Galileo",
  3: "BeiDou",
  4: "IMES",
  5: "QZSS",
  6: "GLONASS",
}


def _log_cfg_gnss(config: GnssConfig, info: MonVerInfo | None) -> None:
  cloudlog.info(", ".join((
    "GPS acquisition configuration CFG-GNSS",
    f"protocol_versions={list(info.protocol_versions) if info is not None else []}",
    f"message_version={config.version}",
    f"hardware_tracking_channels={config.hardware_tracking_channels}",
    f"configured_tracking_channels={config.configured_tracking_channels}",
    f"block_count={len(config.blocks)}",
  )))
  for block in config.blocks:
    cloudlog.info(", ".join((
      "GPS acquisition configuration CFG-GNSS block",
      f"gnss_id={block.gnss_id}",
      f"gnss_name={GNSS_NAMES.get(block.gnss_id, 'unknown')}",
      f"enabled={str(block.enabled).lower()}",
      f"reserved_tracking_channels={block.reserved_tracking_channels}",
      f"maximum_tracking_channels={block.maximum_tracking_channels}",
      f"signal_configuration_mask=0x{block.signal_configuration_mask:02X}",
      f"raw_flags=0x{block.flags:08X}",
    )))


def _log_cfg_rxm(config: RxmConfig) -> None:
  interpretation = {
    0: "continuous",
    1: "power_save",
    4: "continuous",
  }.get(config.low_power_mode, "unknown")
  cloudlog.info(", ".join((
    "GPS acquisition configuration CFG-RXM",
    f"low_power_mode={config.low_power_mode}",
    f"low_power_mode_interpretation={interpretation}",
  )))


def _log_cfg_pm2(config: Pm2Config) -> None:
  limit_peak_current = {
    0: "disabled",
    1: "enabled",
    2: "reserved",
    3: "reserved",
  }[(config.flags >> 8) & 0x03]
  power_mode = {
    0: "on_off",
    1: "cyclic_tracking",
    2: "reserved",
    3: "reserved",
  }[(config.flags >> 17) & 0x03]
  fields = [
    "GPS acquisition configuration CFG-PM2",
    f"message_version={config.version}",
    f"raw_flags=0x{config.flags:08X}",
    f"maximum_startup_state_duration_s={config.maximum_startup_state_duration_s}",
    f"update_period_ms={config.update_period_ms}",
    f"search_period_ms={config.search_period_ms}",
    f"grid_offset_ms={config.grid_offset_ms}",
    f"on_time_s={config.on_time_s}",
    f"minimum_acquisition_time_s={config.minimum_acquisition_time_s}",
    f"external_interrupt_pin={'EXTINT1' if config.flags & (1 << 4) else 'EXTINT0'}",
    f"external_interrupt_wake={str(bool(config.flags & (1 << 5))).lower()}",
    f"external_interrupt_backup={str(bool(config.flags & (1 << 6))).lower()}",
    f"limit_peak_current={limit_peak_current}",
    f"wait_for_time_fix={str(bool(config.flags & (1 << 10))).lower()}",
    f"update_rtc={str(bool(config.flags & (1 << 11))).lower()}",
    f"update_ephemeris={str(bool(config.flags & (1 << 12))).lower()}",
    f"do_not_enter_off={str(bool(config.flags & (1 << 16))).lower()}",
    f"power_mode={power_mode}",
  ]
  if config.version == 2:
    fields.extend((
      f"external_interrupt_inactive={str(bool(config.flags & (1 << 7))).lower()}",
      f"external_interrupt_inactivity_ms={config.external_interrupt_inactivity_ms}",
    ))
  cloudlog.info(", ".join(fields))


def verify_cfg_gnss_conservatively(config: GnssConfig) -> None:
  if config.version != 0:
    raise ReceiverConfigurationUnsupportedError("CFG-GNSS unsupported version")
  if not config.blocks or config.configured_tracking_channels > config.hardware_tracking_channels:
    raise ReceiverConfigurationError("CFG-GNSS invalid channel totals")
  if len({block.gnss_id for block in config.blocks}) != len(config.blocks):
    raise ReceiverConfigurationError("CFG-GNSS duplicate constellation block")
  if any(block.reserved_tracking_channels > block.maximum_tracking_channels for block in config.blocks):
    raise ReceiverConfigurationError("CFG-GNSS invalid block channels")
  if sum(block.maximum_tracking_channels for block in config.blocks) > config.hardware_tracking_channels:
    raise ReceiverConfigurationError("CFG-GNSS allocated channels exceed hardware")
  if not any(block.gnss_id == 0 and block.enabled for block in config.blocks):
    raise ReceiverConfigurationError("CFG-GNSS GPS is disabled")


def verify_cfg_rxm_conservatively(config: RxmConfig) -> None:
  if config.low_power_mode not in (0, 1, 4):
    raise ReceiverConfigurationUnsupportedError("CFG-RXM unsupported low-power mode")
  if config.low_power_mode not in (0, 4):
    raise ReceiverConfigurationError("CFG-RXM power-save mode does not match intended continuous acquisition")


def verify_cfg_pm2_conservatively(config: Pm2Config) -> None:
  if config.version not in (1, 2):
    raise ReceiverConfigurationUnsupportedError("CFG-PM2 unsupported version")
  if (
    config.maximum_startup_state_duration_s != 0
    or config.flags != 0
    or config.update_period_ms != 0
    or config.search_period_ms != 0
    or config.grid_offset_ms != 0
    or config.on_time_s != 0
    or config.minimum_acquisition_time_s != 0
    or config.external_interrupt_inactivity_ms not in (None, 0)
  ):
    raise ReceiverConfigurationError("CFG-PM2 does not match intended inactive power-management state")


def _is_hpg_product(info: MonVerInfo | None) -> bool:
  if info is None:
    return False

  for firmware_version in info.firmware_versions:
    fields = firmware_version.removeprefix("FWVER=").split(maxsplit=1)
    if fields and fields[0] == "HPG":
      return True
  return False


def log_acquisition_configuration_diagnostics(
  pigeon: TTYPigeon,
  info: MonVerInfo | None,
) -> None:
  diagnostics = (
    ("CFG-GNSS", poll_cfg_gnss, lambda config: _log_cfg_gnss(config, info)),
    ("CFG-RXM", poll_cfg_rxm, _log_cfg_rxm),
    ("CFG-PM2", poll_cfg_pm2, _log_cfg_pm2),
  )
  for name, poll, log_config in diagnostics:
    if name == "CFG-PM2" and _is_hpg_product(info):
      cloudlog.info(
        "GPS acquisition configuration CFG-PM2 skipped, supported=false, reason=hpg_product_unsupported"
      )
      continue
    try:
      config = poll(pigeon)
      if config is None:
        cloudlog.warning(f"GPS acquisition configuration {name} response unavailable or malformed")
        continue
      log_config(config)
    except Exception:
      cloudlog.exception(f"GPS acquisition configuration {name} diagnostic poll failed")


def log_mon_ver_info(info: MonVerInfo) -> None:
  cloudlog.info(", ".join((
    "GPS MON-VER diagnostics",
    f"software_version={info.software_version}",
    f"hardware_version={info.hardware_version}",
    f"protocol_versions={list(info.protocol_versions)}",
    f"firmware_extensions={list(info.firmware_versions)}",
    f"module_identifiers={list(info.module_identifiers)}",
    f"supported_gnss={list(info.supported_gnss)}",
    f"extensions={list(info.extensions)}",
    f"diagnostic_identity={normalized_receiver_identity(info)}",
  )))


def log_mon_ver_diagnostics(pigeon: TTYPigeon) -> MonVerInfo | None:
  try:
    info = poll_mon_ver(pigeon)
  except Exception:
    cloudlog.exception("GPS MON-VER diagnostic poll failed")
    return None
  if info is None:
    cloudlog.warning("GPS MON-VER diagnostic response unavailable or malformed")
    return None
  log_mon_ver_info(info)
  return info


def resolve_pre_acquisition_mon_ver(
  pigeon: TTYPigeon,
  verified_info: MonVerInfo | None,
  collect_diagnostics: bool,
) -> MonVerInfo | None:
  if verified_info is not None:
    if collect_diagnostics:
      log_mon_ver_info(verified_info)
    return verified_info
  if collect_diagnostics:
    return log_mon_ver_diagnostics(pigeon)
  try:
    return poll_mon_ver(pigeon)
  except Exception:
    cloudlog.exception("GPS MON-VER compatibility poll failed")
    return None


def navx5_ack_aiding_compatibility(info: MonVerInfo | None) -> tuple[bool, str]:
  if info is None:
    return False, "mon_ver_unavailable"

  software_supported = NAVX5_ACK_AIDING_SOFTWARE_VERSION_PATTERN.fullmatch(
    info.software_version.strip().upper()
  ) is not None
  protocol_supported = any(value.strip().upper() == "PROTVER=20.30" for value in info.protocol_versions)
  firmware_supported = any(value.strip().upper() == "FWVER=HPG 1.40ROV" for value in info.firmware_versions)
  if not software_supported:
    return False, "unsupported_software_version"
  if not protocol_supported:
    return False, "unsupported_protocol_version"
  if not firmware_supported:
    return False, "unsupported_firmware_version"
  return True, "m8_hpg_1_40_protver_20_30"


def assistnow_autonomous_compatibility(info: MonVerInfo | None) -> tuple[bool, str]:
  navx5_supported, reason = navx5_ack_aiding_compatibility(info)
  if not navx5_supported:
    return False, reason

  # u-blox HPG release documentation explicitly excludes AssistNow
  # Autonomous even though the generic protocol exposes its NAVX5 fields.
  return False, "hpg_1_40_rover_assistnow_autonomous_unsupported"


def log_navx5_ack_aiding_support(info: MonVerInfo | None) -> bool:
  supported, reason = navx5_ack_aiding_compatibility(info)
  message = ", ".join((
    "GPS NAVX5 ACK aiding support",
    f"supported={str(supported).lower()}",
    f"reason={reason}",
  ))
  if supported:
    cloudlog.info(message)
  else:
    cloudlog.warning(message)
  return supported


def log_assistnow_autonomous_support(info: MonVerInfo | None) -> bool:
  supported, reason = assistnow_autonomous_compatibility(info)
  message = ", ".join((
    "GPS AssistNow Autonomous support",
    f"supported={str(supported).lower()}",
    f"reason={reason}",
  ))
  if supported:
    cloudlog.info(message)
  else:
    cloudlog.warning(message)
  return supported


def poll_navx5_config(
  pigeon: TTYPigeon,
  timeout: float = NAVX5_POLL_TIMEOUT,
  pre_start_deadline: float | None = None,
) -> Navx5Config | None:
  def verify_poll_deadline() -> None:
    if pre_start_deadline is not None and time.monotonic() >= pre_start_deadline:
      raise TimeoutError("NAVX5 poll pre-START deadline exhausted before UART write")

  transaction = _begin_response_transaction(
    pigeon,
    build_navx5_poll_message(),
    before_send=verify_poll_deadline if pre_start_deadline is not None else None,
    deadline=pre_start_deadline,
  )
  if pre_start_deadline is not None:
    remaining = pre_start_deadline - time.monotonic()
    if remaining <= 0.0:
      raise TimeoutError("NAVX5 poll pre-START deadline exhausted")
    timeout = min(timeout, remaining)
  return _wait_for_parsed_response(
    pigeon,
    transaction,
    parse_navx5,
    0x06,
    0x23,
    timeout,
    deadline=pre_start_deadline,
  )


def wait_for_cfg_ack(
  pigeon: TTYPigeon,
  transaction: ResponseTransaction,
  message_class: int,
  message_id: int,
  timeout: float = NAVX5_ACK_TIMEOUT,
  deadline: float | None = None,
) -> bool | None:
  response_deadline = time.monotonic() + timeout
  if deadline is not None:
    response_deadline = min(response_deadline, deadline)
  while time.monotonic() < response_deadline:
    result: bool | None = None
    _, stream_frames, transaction_frames = _receive_transaction_data(pigeon, transaction)
    if deadline is not None and time.monotonic() >= deadline:
      return None
    for frame in transaction_frames:
      if (
        result is None
        and len(frame) == 10
        and frame[2] == 0x05
        and frame[3] in (0x00, 0x01)
        and frame[6:8] == bytes((message_class, message_id))
      ):
        result = frame[3] == 0x01
        continue
    _queue_unrelated_frames(
      pigeon,
      stream_frames,
      lambda frame: (
        len(frame) == 10
        and frame[2] == 0x05
        and frame[3] in (0x00, 0x01)
        and frame[6:8] == bytes((message_class, message_id))
      ),
      transaction.operation,
    )
    if deadline is not None and time.monotonic() >= deadline:
      return None
    if result is not None:
      return result
    remaining = response_deadline - time.monotonic()
    if remaining > 0.0:
      time.sleep(min(0.001, remaining))
  return None


class Navx5AckAidingConfigurationResult(StrEnum):
  ENABLED_AND_VERIFIED = "enabled_and_verified"
  ALREADY_ENABLED = "already_enabled"
  UNSUPPORTED = "unsupported"
  POLL_UNAVAILABLE = "poll_unavailable"
  UNSUPPORTED_NAVX5_VERSION = "unsupported_navx5_version"
  WRITE_REJECTED = "write_rejected"
  WRITE_TIMED_OUT = "write_timed_out"
  READBACK_UNAVAILABLE = "readback_unavailable"
  READBACK_ACK_AIDING_FALSE = "readback_ack_aiding_false"
  READBACK_AOP_FIELD_CHANGED = "readback_aop_field_changed"
  READBACK_UNRELATED_FIELDS_CHANGED = "readback_unrelated_fields_changed"
  DEADLINE_EXHAUSTED = "deadline_exhausted"
  ERROR = "error"


class AssistNowAutonomousConfigurationResult(StrEnum):
  ENABLED_AND_VERIFIED = "enabled_and_verified"
  ALREADY_ENABLED = "already_enabled"
  UNSUPPORTED = "unsupported"
  POLL_UNAVAILABLE = "poll_unavailable"
  UNSUPPORTED_NAVX5_VERSION = "unsupported_navx5_version"
  WRITE_REJECTED = "write_rejected"
  WRITE_TIMED_OUT = "write_timed_out"
  READBACK_UNAVAILABLE = "readback_unavailable"
  READBACK_USE_AOP_FALSE = "readback_use_aop_false"
  READBACK_ORBIT_ERROR_THRESHOLD_CHANGED = "readback_orbit_error_threshold_changed"
  READBACK_UNRELATED_FIELDS_CHANGED = "readback_unrelated_fields_changed"
  ERROR = "error"


def configure_navx5_ack_aiding(
  pigeon: TTYPigeon,
  info: MonVerInfo | None,
  pre_start_deadline: float | None = None,
) -> Navx5AckAidingConfigurationResult:
  supported, support_reason = navx5_ack_aiding_compatibility(info)
  if not supported:
    cloudlog.warning(f"GPS NAVX5 ACK aiding configuration skipped, reason={support_reason}")
    return Navx5AckAidingConfigurationResult.UNSUPPORTED

  def remaining_timeout(maximum: float) -> float:
    if pre_start_deadline is None:
      return maximum
    remaining = pre_start_deadline - time.monotonic()
    if remaining <= 0.0:
      raise TimeoutError("NAVX5 ACK-aiding pre-START deadline exhausted")
    return min(maximum, remaining)

  def deadline_expired() -> bool:
    return pre_start_deadline is not None and time.monotonic() >= pre_start_deadline

  def poll_with_deadline() -> Navx5Config | None:
    timeout = remaining_timeout(NAVX5_POLL_TIMEOUT)
    try:
      return poll_navx5_config(
        pigeon,
        timeout=timeout,
        pre_start_deadline=pre_start_deadline,
      )
    except TypeError as exc:
      if "unexpected keyword argument 'pre_start_deadline'" in str(exc):
        try:
          return poll_navx5_config(pigeon, timeout=timeout)
        except TypeError as timeout_exc:
          if "unexpected keyword argument 'timeout'" not in str(timeout_exc):
            raise
          return poll_navx5_config(pigeon)
      if "unexpected keyword argument 'timeout'" not in str(exc):
        raise
      return poll_navx5_config(pigeon)

  def verify_write_deadline() -> None:
    remaining_timeout(NAVX5_ACK_TIMEOUT)

  try:
    current = poll_with_deadline()
    if current is None:
      if deadline_expired():
        return Navx5AckAidingConfigurationResult.DEADLINE_EXHAUSTED
      cloudlog.warning("GPS NAVX5 ACK aiding configuration failed, result=poll_unavailable")
      return Navx5AckAidingConfigurationResult.POLL_UNAVAILABLE
    if current.version != 2:
      cloudlog.warning(", ".join((
        "GPS NAVX5 ACK aiding configuration failed",
        f"navx5_version={current.version}",
        "result=unsupported_navx5_version",
      )))
      return Navx5AckAidingConfigurationResult.UNSUPPORTED_NAVX5_VERSION

    cloudlog.info(", ".join((
      "GPS NAVX5 ACK aiding configuration before",
      f"navx5_version={current.version}",
      f"ackAiding={str(current.ack_aiding).lower()}",
      f"useAOP={str(current.use_aop).lower()}",
      f"aop_orbit_max_error_m={current.aop_orbit_max_error_m}",
    )))
    if current.ack_aiding:
      cloudlog.info("GPS NAVX5 ACK aiding configuration unchanged, ackAiding=true, result=already_enabled")
      return Navx5AckAidingConfigurationResult.ALREADY_ENABLED

    transaction = _begin_response_transaction(
      pigeon,
      build_navx5_ack_aiding_enable_message(current),
      before_send=verify_write_deadline,
      deadline=pre_start_deadline,
    )
    acknowledgment = (
      wait_for_cfg_ack(pigeon, transaction, 0x06, 0x23)
      if pre_start_deadline is None
      else wait_for_cfg_ack(
        pigeon,
        transaction,
        0x06,
        0x23,
        timeout=remaining_timeout(NAVX5_ACK_TIMEOUT),
        deadline=pre_start_deadline,
      )
    )
    if acknowledgment is False:
      cloudlog.warning(f"GPS NAVX5 ACK aiding configuration rejected, mask1=0x{NAVX5_MASK1_ACK_AIDING:04X}, result=write_rejected")
      return Navx5AckAidingConfigurationResult.WRITE_REJECTED
    if acknowledgment is None:
      if deadline_expired():
        return Navx5AckAidingConfigurationResult.DEADLINE_EXHAUSTED
      cloudlog.warning(f"GPS NAVX5 ACK aiding configuration timed out, mask1=0x{NAVX5_MASK1_ACK_AIDING:04X}, result=write_timed_out")
      return Navx5AckAidingConfigurationResult.WRITE_TIMED_OUT

    resulting = poll_with_deadline()
    if resulting is None:
      if deadline_expired():
        return Navx5AckAidingConfigurationResult.DEADLINE_EXHAUSTED
      cloudlog.warning("GPS NAVX5 ACK aiding verification failed, result=readback_unavailable")
      return Navx5AckAidingConfigurationResult.READBACK_UNAVAILABLE
    if not resulting.ack_aiding:
      cloudlog.warning("GPS NAVX5 ACK aiding verification failed, ackAiding=false, result=readback_ack_aiding_false")
      return Navx5AckAidingConfigurationResult.READBACK_ACK_AIDING_FALSE
    if (
      resulting.use_aop != current.use_aop
      or resulting.aop_orbit_max_error_m != current.aop_orbit_max_error_m
    ):
      cloudlog.warning("GPS NAVX5 ACK aiding verification failed, result=readback_aop_field_changed")
      return Navx5AckAidingConfigurationResult.READBACK_AOP_FIELD_CHANGED
    if not navx5_unrelated_fields_unchanged(current, resulting, enabling_ack_aiding=True):
      cloudlog.warning("GPS NAVX5 ACK aiding verification failed, result=readback_unrelated_fields_changed")
      return Navx5AckAidingConfigurationResult.READBACK_UNRELATED_FIELDS_CHANGED

    cloudlog.info(", ".join((
      "GPS NAVX5 ACK aiding configuration accepted and verified",
      "previous_ackAiding=false",
      "resulting_ackAiding=true",
      f"useAOP={str(resulting.use_aop).lower()}",
      f"aop_orbit_max_error_m={resulting.aop_orbit_max_error_m}",
      f"mask1=0x{NAVX5_MASK1_ACK_AIDING:04X}",
      "result=enabled_and_verified",
    )))
    return Navx5AckAidingConfigurationResult.ENABLED_AND_VERIFIED
  except TimeoutError:
    cloudlog.warning("GPS NAVX5 ACK aiding configuration skipped, result=deadline_exhausted")
    return Navx5AckAidingConfigurationResult.DEADLINE_EXHAUSTED
  except Exception:
    cloudlog.exception("GPS NAVX5 ACK aiding configuration failed, reason=unexpected_error")
    return Navx5AckAidingConfigurationResult.ERROR


def configure_assistnow_autonomous(
  pigeon: TTYPigeon,
  info: MonVerInfo | None,
) -> AssistNowAutonomousConfigurationResult:
  supported, support_reason = assistnow_autonomous_compatibility(info)
  if not supported:
    cloudlog.warning(", ".join((
      "GPS AssistNow Autonomous configuration skipped",
      f"reason={support_reason}",
    )))
    return AssistNowAutonomousConfigurationResult.UNSUPPORTED

  try:
    current = poll_navx5_config(pigeon)
    if current is None:
      cloudlog.warning("GPS AssistNow Autonomous configuration failed, result=poll_unavailable")
      return AssistNowAutonomousConfigurationResult.POLL_UNAVAILABLE
    if current.version != 2:
      cloudlog.warning(", ".join((
        "GPS AssistNow Autonomous configuration failed",
        f"navx5_version={current.version}",
        "result=unsupported_navx5_version",
      )))
      return AssistNowAutonomousConfigurationResult.UNSUPPORTED_NAVX5_VERSION

    cloudlog.info(", ".join((
      "GPS AssistNow Autonomous configuration before",
      f"navx5_version={current.version}",
      f"ackAiding={str(current.ack_aiding).lower()}",
      f"useAOP={str(current.use_aop).lower()}",
      f"aop_orbit_max_error_m={current.aop_orbit_max_error_m}",
    )))
    if current.use_aop:
      cloudlog.info("GPS AssistNow Autonomous configuration unchanged: useAOP=true, result=already_enabled")
      return AssistNowAutonomousConfigurationResult.ALREADY_ENABLED

    transaction = _begin_response_transaction(
      pigeon, build_navx5_aop_enable_message(current),
    )
    acknowledgment = wait_for_cfg_ack(pigeon, transaction, 0x06, 0x23)
    if acknowledgment is False:
      cloudlog.warning(f"GPS AssistNow Autonomous configuration rejected, mask1=0x{NAVX5_MASK1_AOP:04X}, result=write_rejected")
      return AssistNowAutonomousConfigurationResult.WRITE_REJECTED
    if acknowledgment is None:
      cloudlog.warning(f"GPS AssistNow Autonomous configuration timed out, mask1=0x{NAVX5_MASK1_AOP:04X}, result=write_timed_out")
      return AssistNowAutonomousConfigurationResult.WRITE_TIMED_OUT

    resulting = poll_navx5_config(pigeon)
    if resulting is None:
      cloudlog.warning("GPS AssistNow Autonomous verification failed, result=readback_unavailable")
      return AssistNowAutonomousConfigurationResult.READBACK_UNAVAILABLE
    if not resulting.use_aop:
      cloudlog.warning("GPS AssistNow Autonomous verification failed, useAOP=false, result=readback_use_aop_false")
      return AssistNowAutonomousConfigurationResult.READBACK_USE_AOP_FALSE
    if resulting.aop_orbit_max_error_m != current.aop_orbit_max_error_m:
      cloudlog.warning(", ".join((
        "GPS AssistNow Autonomous verification failed",
        "reason=orbit_error_threshold_changed",
        f"previous_aop_orbit_max_error_m={current.aop_orbit_max_error_m}",
        f"resulting_aop_orbit_max_error_m={resulting.aop_orbit_max_error_m}",
      )))
      return AssistNowAutonomousConfigurationResult.READBACK_ORBIT_ERROR_THRESHOLD_CHANGED
    if not navx5_unrelated_fields_unchanged(current, resulting):
      cloudlog.warning("GPS AssistNow Autonomous verification failed, result=readback_unrelated_fields_changed")
      return AssistNowAutonomousConfigurationResult.READBACK_UNRELATED_FIELDS_CHANGED

    cloudlog.info(", ".join((
      "GPS AssistNow Autonomous configuration accepted and verified",
      f"previous_useAOP={str(current.use_aop).lower()}",
      f"resulting_useAOP={str(resulting.use_aop).lower()}",
      f"ackAiding={str(resulting.ack_aiding).lower()}",
      f"aop_orbit_max_error_m={resulting.aop_orbit_max_error_m}",
      f"mask1=0x{NAVX5_MASK1_AOP:04X}",
      "result=enabled_and_verified",
    )))
    return AssistNowAutonomousConfigurationResult.ENABLED_AND_VERIFIED
  except Exception:
    cloudlog.exception("GPS AssistNow Autonomous configuration failed: reason=unexpected_error")
    return AssistNowAutonomousConfigurationResult.ERROR


def poll_nav_aopstatus(
  pigeon: TTYPigeon,
  timeout: float = AOP_STATUS_POLL_TIMEOUT,
) -> NavAopStatus | None:
  transaction = _begin_response_transaction(pigeon, build_nav_aopstatus_poll_message())
  return _wait_for_parsed_response(
    pigeon, transaction, parse_nav_aopstatus, 0x01, 0x60, timeout,
  )


class AopCaptureState(StrEnum):
  IDLE = "idle"
  BUSY = "busy"
  UNKNOWN = "unknown"
  UNSUPPORTED = "unsupported"


def wait_for_aop_idle(
  pigeon: TTYPigeon,
  timeout: float = AOP_IDLE_WAIT_TIMEOUT,
  poll_interval: float = AOP_IDLE_POLL_INTERVAL,
) -> AopCaptureState:
  deadline = time.monotonic() + timeout
  observed_busy = False
  while time.monotonic() < deadline:
    remaining = deadline - time.monotonic()
    try:
      status = poll_nav_aopstatus(
        pigeon,
        timeout=min(AOP_STATUS_POLL_TIMEOUT, remaining),
      )
    except Exception:
      cloudlog.exception("GPS AssistNow Autonomous status unavailable: reason=poll_error")
      return AopCaptureState.UNKNOWN
    if status is None:
      cloudlog.warning("GPS AssistNow Autonomous status unavailable")
      return AopCaptureState.UNKNOWN
    if status.idle:
      cloudlog.info(f"GPS AssistNow Autonomous status: state=idle, enabled={str(status.enabled).lower()}")
      return AopCaptureState.IDLE
    observed_busy = True
    cloudlog.info(f"GPS AssistNow Autonomous status: state=running, status={status.status}")
    remaining = deadline - time.monotonic()
    if remaining > 0:
      time.sleep(min(poll_interval, remaining))

  if observed_busy:
    cloudlog.warning("GPS AssistNow Autonomous status remained running through bounded wait")
    return AopCaptureState.BUSY
  cloudlog.warning("GPS AssistNow Autonomous status unavailable through bounded wait")
  return AopCaptureState.UNKNOWN

def init_baudrate(pigeon: TTYPigeon):
  # ublox default setting on startup is 9600 baudrate. Stop GNSS before
  # changing baud so no synchronous startup transaction can race the
  # trusted-age database decision.
  pigeon.set_baud(9600)
  pigeon.send(CONTROLLED_GNSS_STOP_MESSAGE)
  time.sleep(CONTROLLED_GNSS_TRANSITION_DELAY)

  # $PUBX,41,1,0007,0003,460800,0*15\r\n
  pigeon.send(b"\x24\x50\x55\x42\x58\x2C\x34\x31\x2C\x31\x2C\x30\x30\x30\x37\x2C\x30\x30\x30\x33\x2C\x34\x36\x30\x38\x30\x30\x2C\x30\x2A\x31\x35\x0D\x0A")
  time.sleep(0.1)
  pigeon.set_baud(460800)
  if hasattr(pigeon, "_stream_parser"):
    pigeon.reset_response_state()


def _poll_cfg[Config](
  pigeon: TTYPigeon,
  poll_message: bytes,
  response_parser: Callable[[bytes], Config | None],
  timeout: float = 0.5,
  response_matches: Callable[[Config], bool] | None = None,
  deadline: float | None = None,
) -> Config:
  transaction = _begin_response_transaction(
    pigeon,
    poll_message,
    deadline=deadline,
  )
  config = _wait_for_parsed_response(
    pigeon,
    transaction,
    response_parser,
    poll_message[2],
    poll_message[3],
    timeout,
    response_matches,
    f"CFG 0x{poll_message[2]:02X} 0x{poll_message[3]:02X}",
    deadline,
  )
  if config is None:
    if deadline is not None and time.monotonic() >= deadline:
      raise TimeoutError(
        "receiver configuration pre-START deadline exhausted during readback"
      )
    raise CfgPollTimeoutError(
      f"No valid CFG response for message 0x{poll_message[2]:02X} 0x{poll_message[3]:02X}"
    )
  return config


def poll_cfg_rate(
  pigeon: TTYPigeon,
  timeout: float = 0.5,
  deadline: float | None = None,
) -> RateConfig:
  return _poll_cfg(
    pigeon, build_cfg_rate_poll_message(), parse_cfg_rate, timeout,
    deadline=deadline,
  )


def poll_cfg_nav5(
  pigeon: TTYPigeon,
  timeout: float = 0.5,
  deadline: float | None = None,
) -> Nav5Config:
  return _poll_cfg(
    pigeon, build_cfg_nav5_poll_message(), parse_cfg_nav5, timeout,
    deadline=deadline,
  )


def poll_cfg_odo(
  pigeon: TTYPigeon,
  timeout: float = 0.5,
  deadline: float | None = None,
) -> OdoConfig:
  return _poll_cfg(
    pigeon, build_cfg_odo_poll_message(), parse_cfg_odo, timeout,
    deadline=deadline,
  )


def poll_cfg_itfm(
  pigeon: TTYPigeon,
  timeout: float = 0.5,
  deadline: float | None = None,
) -> ItfmConfig:
  return _poll_cfg(
    pigeon, build_cfg_itfm_poll_message(), parse_cfg_itfm, timeout,
    deadline=deadline,
  )


def poll_cfg_msg(
  pigeon: TTYPigeon,
  message_class: int,
  message_id: int,
  timeout: float = 0.5,
  deadline: float | None = None,
) -> MessageRateConfig:
  poll_message = build_cfg_msg_poll_message(message_class, message_id)
  return _poll_cfg(
    pigeon,
    poll_message,
    parse_cfg_msg,
    timeout,
    lambda config: (
      config.message_class == message_class
      and config.message_id == message_id
    ),
    deadline,
  )


def poll_cfg_prt(
  pigeon: TTYPigeon,
  port_id: int,
  timeout: float = 0.5,
  deadline: float | None = None,
) -> PortConfig:
  poll_message = build_cfg_prt_poll_message(port_id)
  return _poll_cfg(
    pigeon,
    poll_message,
    parse_cfg_prt,
    timeout,
    lambda response: response.port_id == port_id,
    deadline,
  )


def verify_cfg_prt_config(
  actual: PortConfig,
  expected: PortConfig,
) -> None:
  fields_by_port = {
    0: ("tx_ready", "mode", "input_protocol_mask", "output_protocol_mask", "flags"),
    1: ("tx_ready", "mode", "baud_rate", "input_protocol_mask", "output_protocol_mask", "flags"),
    3: ("tx_ready", "input_protocol_mask", "output_protocol_mask"),
    4: ("tx_ready", "mode", "input_protocol_mask", "output_protocol_mask", "flags"),
  }
  fields = fields_by_port.get(expected.port_id)
  if actual.port_id != expected.port_id or fields is None:
    raise ReceiverConfigurationError(
      f"CFG-PRT readback port mismatch: expected={expected.port_id}, actual={actual.port_id}"
    )
  mismatches = [
    field for field in fields
    if getattr(actual, field) != getattr(expected, field)
  ]
  if mismatches:
    raise ReceiverConfigurationError(
      f"CFG-PRT readback mismatch for port {expected.port_id}: fields={mismatches}"
    )


def verify_startup_configuration(
  rate: RateConfig,
  nav5: Nav5Config,
  odo: OdoConfig,
  itfm: ItfmConfig,
  nav_pvt: MessageRateConfig,
  rawx: MessageRateConfig,
) -> None:
  if rate != RateConfig(100, 1, 0):
    raise ReceiverConfigurationError(f"CFG-RATE readback mismatch: {rate}")
  if nav5.dynamic_model != 4 or nav5.fix_mode != 3:
    raise ReceiverConfigurationError(f"CFG-NAV5 readback mismatch: {nav5}")
  if (odo.flags & 0x0F) != 0x01 or odo.profile != 3:
    raise ReceiverConfigurationError(f"CFG-ODO readback mismatch: {odo}")
  if itfm != ItfmConfig(0xAD62ADFF, 0x0000631E):
    raise ReceiverConfigurationError(f"CFG-ITFM readback mismatch: {itfm}")
  if nav_pvt.rates[1] != 1:
    raise ReceiverConfigurationError(f"CFG-MSG NAV-PVT readback mismatch: {nav_pvt}")
  if rawx.rates[1] != 1:
    raise ReceiverConfigurationError(f"CFG-MSG RXM-RAWX readback mismatch: {rawx}")


def log_startup_configuration(
  rate: RateConfig,
  nav5: Nav5Config,
  odo: OdoConfig,
  itfm: ItfmConfig,
  nav_pvt: MessageRateConfig,
  rawx: MessageRateConfig,
) -> None:
  cloudlog.info(", ".join((
    "GPS startup configuration CFG-RATE effective",
    f"measurement_period_ms={rate.measurement_period_ms}",
    f"navigation_rate={rate.navigation_rate}",
    f"time_reference={rate.time_reference}",
  )))
  cloudlog.info(", ".join((
    "GPS startup configuration CFG-NAV5 effective",
    f"dynamic_model={nav5.dynamic_model}",
    f"fix_mode={nav5.fix_mode}",
  )))
  cloudlog.info(", ".join((
    "GPS startup configuration CFG-ODO effective",
    f"version={odo.version}",
    f"flags=0x{odo.flags:02X}",
    f"profile={odo.profile}",
  )))
  cloudlog.info(", ".join((
    "GPS startup configuration CFG-ITFM effective",
    f"config=0x{itfm.config:08X}",
    f"config2=0x{itfm.config2:08X}",
  )))
  for name, config in (("NAV-PVT", nav_pvt), ("RXM-RAWX", rawx)):
    cloudlog.info(", ".join((
      f"GPS startup configuration CFG-MSG {name} effective",
      f"message_class=0x{config.message_class:02X}",
      f"message_id=0x{config.message_id:02X}",
      f"rates={list(config.rates)}",
      f"uart1_rate={config.rates[1]}",
    )))


RECEIVER_CONFIGURATION_ITEM_MAX_WRITE_ATTEMPTS = 2


class ReceiverConfigurationFailureKind(StrEnum):
  ACK_REJECTED = "ack_rejected"
  ACK_TIMEOUT = "ack_timeout"
  WRITE_ERROR = "write_error"
  POLL_TIMEOUT = "poll_timeout"
  UNSUPPORTED = "unsupported"
  READBACK_MISMATCH = "readback_mismatch"
  PARSER_ERROR = "parser_error"
  TRANSACTION_ERROR = "transaction_error"
  DEADLINE_EXHAUSTED = "deadline_exhausted"
  DEFERRED_POST_START = "deferred_post_start"


class ReceiverConfigurationAckStatus(StrEnum):
  NOT_REQUIRED = "not_required"
  NOT_SENT = "not_sent"
  ACKNOWLEDGED = "acknowledged"
  REJECTED = "rejected"
  TIMED_OUT = "timed_out"
  WRITE_ERROR = "write_error"


class ReceiverConfigurationReadbackStatus(StrEnum):
  VERIFIED = "verified"
  MISMATCHED = "mismatched"
  TIMED_OUT = "timed_out"
  PARSER_ERROR = "parser_error"
  NOT_SUPPORTED = "not_supported"
  DEADLINE_EXHAUSTED = "deadline_exhausted"
  DEFERRED_POST_START = "deferred_post_start"


@dataclass(frozen=True)
class ReceiverConfigurationItemResult:
  item_name: str
  mandatory: bool
  attempted: bool
  write_attempt_count: int
  ack_status: ReceiverConfigurationAckStatus
  poll_attempt_count: int
  readback_status: ReceiverConfigurationReadbackStatus
  verified: bool
  expected_value: str
  observed_value: str | None
  failure_kind: ReceiverConfigurationFailureKind | None
  failure_phase: str | None
  error_type: str | None
  error: str | None


@dataclass(frozen=True)
class ReceiverConfigurationItemDefinition:
  item_name: str
  message_class: int
  message_id: int
  mandatory: bool
  expected_value: str
  maximum_attempts: int = RECEIVER_CONFIGURATION_ITEM_MAX_WRITE_ATTEMPTS


RECEIVER_OUTPUT_STREAM_ITEMS = (
  ReceiverConfigurationItemDefinition("CFG-MSG-NAV-PVT", 0x01, 0x07, True, "uart1_rate=1"),
  ReceiverConfigurationItemDefinition("CFG-MSG-RXM-RAWX", 0x02, 0x15, True, "uart1_rate=1"),
  ReceiverConfigurationItemDefinition("CFG-MSG-RXM-SFRBX", 0x02, 0x13, True, "uart1_rate=1"),
  ReceiverConfigurationItemDefinition("CFG-MSG-NAV-SAT", 0x01, 0x35, False, "uart1_rate=1"),
  ReceiverConfigurationItemDefinition("CFG-MSG-MON-HW", 0x0A, 0x09, False, "uart1_rate=1"),
  ReceiverConfigurationItemDefinition("CFG-MSG-MON-HW2", 0x0A, 0x0B, False, "uart1_rate=1"),
)


RECEIVER_CONFIGURATION_ITEM_INVENTORY = (
  ("CFG-PRT-3", True),
  ("CFG-PRT-0", True),
  ("CFG-PRT-1", True),
  ("CFG-PRT-4", True),
  ("CFG-RATE", True),
  ("CFG-NAV5", True),
  ("CFG-ODO", True),
  ("CFG-ITFM", True),
  *((definition.item_name, definition.mandatory) for definition in RECEIVER_OUTPUT_STREAM_ITEMS),
  ("CFG-GNSS", False),
  ("CFG-RXM", False),
  ("CFG-PM2", False),
)


def _configuration_failure_kind(exc: Exception) -> ReceiverConfigurationFailureKind:
  if isinstance(exc, ReceiverConfigurationParserError):
    return ReceiverConfigurationFailureKind.PARSER_ERROR
  if isinstance(exc, ReceiverConfigurationUnsupportedError):
    return ReceiverConfigurationFailureKind.UNSUPPORTED
  if isinstance(exc, CfgNakError):
    return ReceiverConfigurationFailureKind.ACK_REJECTED
  if isinstance(exc, CfgPollTimeoutError):
    return ReceiverConfigurationFailureKind.POLL_TIMEOUT
  if isinstance(exc, TimeoutError):
    return ReceiverConfigurationFailureKind.ACK_TIMEOUT
  if isinstance(exc, ResponseTransactionError):
    return ReceiverConfigurationFailureKind.TRANSACTION_ERROR
  if isinstance(exc, ReceiverConfigurationError):
    return ReceiverConfigurationFailureKind.READBACK_MISMATCH
  return ReceiverConfigurationFailureKind.WRITE_ERROR


def receiver_configuration_ack_status(
  write_attempt_count: int,
  last_write_error: Exception | None,
  failure_phase: str | None = None,
) -> ReceiverConfigurationAckStatus:
  if write_attempt_count == 0:
    return ReceiverConfigurationAckStatus.WRITE_ERROR if failure_phase == "write" else ReceiverConfigurationAckStatus.NOT_REQUIRED
  if last_write_error is None:
    return ReceiverConfigurationAckStatus.ACKNOWLEDGED
  if isinstance(last_write_error, CfgNakError):
    return ReceiverConfigurationAckStatus.REJECTED
  if isinstance(last_write_error, TimeoutError):
    return ReceiverConfigurationAckStatus.TIMED_OUT
  return ReceiverConfigurationAckStatus.WRITE_ERROR


def run_receiver_configuration_item(
  *,
  item_name: str,
  mandatory: bool,
  expected_value: str,
  poll: Callable[[], object],
  verify: Callable[[object], None],
  write: Callable[[], bool | None],
  max_write_attempts: int = RECEIVER_CONFIGURATION_ITEM_MAX_WRITE_ATTEMPTS,
  pre_start_deadline: float | None = None,
) -> ReceiverConfigurationItemResult:
  """Read, correct, and verify exactly one receiver configuration item."""
  if max_write_attempts < 0:
    raise ValueError("max_write_attempts must not be negative")
  writes = 0
  polls = 0
  observed_value = None
  last_write_error: Exception | None = None
  terminal_readback_error: Exception | None = None
  terminal_readback_phase: str | None = None
  deadline_error: Exception | None = None
  failure_phase = "initial_readback"
  for attempt in range(max_write_attempts + 1):
    if pre_start_deadline is not None and time.monotonic() >= pre_start_deadline:
      deadline_error = TimeoutError("receiver configuration pre-START deadline exhausted")
      failure_phase = "deadline"
      break
    failure_phase = "initial_readback" if attempt == 0 else "readback"
    polls += 1
    try:
      observed = poll()
      observed_value = repr(observed)[:512]
      verify(observed)
      return ReceiverConfigurationItemResult(
        item_name=item_name, mandatory=mandatory, attempted=writes > 0,
        write_attempt_count=writes,
        ack_status=receiver_configuration_ack_status(writes, last_write_error),
        poll_attempt_count=polls,
        readback_status=ReceiverConfigurationReadbackStatus.VERIFIED,
        verified=True, expected_value=expected_value, observed_value=observed_value,
        failure_kind=None, failure_phase=None, error_type=None, error=None,
      )
    except Exception as exc:
      terminal_readback_error = exc
      terminal_readback_phase = failure_phase
      if attempt == max_write_attempts:
        break
      if pre_start_deadline is not None and time.monotonic() >= pre_start_deadline:
        deadline_error = TimeoutError("receiver configuration pre-START deadline exhausted")
        failure_phase = "deadline"
        break
      failure_phase = "write"
      try:
        # A real TTYPigeon reports this at its send boundary. Test doubles
        # that cannot expose that boundary retain the conservative legacy
        # interpretation: invoking write means one receiver attempt.
        write_attempted = write()
        if write_attempted is not False:
          writes += 1
        last_write_error = None
        failure_phase = "readback"
      except Exception as write_exc:
        last_write_error = write_exc
        # send_configuration_with_ack marks the attempt before UART I/O, so
        # an exception after that boundary still counts exactly once.
        if getattr(write_exc, "receiver_write_attempted", True):
          writes += 1
        if isinstance(write_exc, CfgNakError) and write_exc.retry_not_before is not None:
          delay = write_exc.retry_not_before - time.monotonic()
          if pre_start_deadline is not None:
            delay = min(delay, max(0.0, pre_start_deadline - time.monotonic()))
          if delay > 0.0:
            time.sleep(delay)
  if failure_phase == "deadline":
    assert deadline_error is not None
    terminal_error = deadline_error
    terminal_failure_phase = failure_phase
  elif terminal_readback_error is not None:
    terminal_error = terminal_readback_error
    assert terminal_readback_phase is not None
    terminal_failure_phase = terminal_readback_phase
  else:
    assert last_write_error is not None
    terminal_error = last_write_error
    terminal_failure_phase = "write"
  kind = ReceiverConfigurationFailureKind.DEADLINE_EXHAUSTED if terminal_failure_phase == "deadline" else _configuration_failure_kind(terminal_error)
  ack_status = receiver_configuration_ack_status(
    writes,
    last_write_error,
    "write" if last_write_error is not None else terminal_failure_phase,
  )
  readback_status = ReceiverConfigurationReadbackStatus.MISMATCHED
  if isinstance(terminal_error, CfgPollTimeoutError):
    readback_status = ReceiverConfigurationReadbackStatus.TIMED_OUT
  elif isinstance(terminal_error, ReceiverConfigurationParserError):
    readback_status = ReceiverConfigurationReadbackStatus.PARSER_ERROR
  elif isinstance(terminal_error, ReceiverConfigurationUnsupportedError):
    readback_status = ReceiverConfigurationReadbackStatus.NOT_SUPPORTED
  elif terminal_failure_phase == "deadline":
    readback_status = ReceiverConfigurationReadbackStatus.DEADLINE_EXHAUSTED
  result = ReceiverConfigurationItemResult(
    item_name=item_name, mandatory=mandatory, attempted=writes > 0,
    write_attempt_count=writes, ack_status=ack_status, poll_attempt_count=polls,
    readback_status=readback_status, verified=False, expected_value=expected_value,
    observed_value=observed_value, failure_kind=kind, failure_phase=terminal_failure_phase,
    error_type=type(terminal_error).__name__, error=str(terminal_error)[:512],
  )
  cloudlog.warning(
    "GPS receiver configuration item failed, "
    + f"item={item_name}, mandatory={str(mandatory).lower()}, "
    + f"writes={writes}, polls={polls}, failure_kind={kind.value}, "
    + f"error={result.error}"
  )
  return result


def configuration_poll_timeout(pre_start_deadline: float | None) -> float:
  """Bound a configuration readback by the remaining shared pre-START time."""
  if pre_start_deadline is None:
    return 0.5
  remaining = pre_start_deadline - time.monotonic()
  if remaining <= 0.0:
    raise TimeoutError("receiver configuration pre-START deadline exhausted")
  return min(0.5, remaining)


def configuration_ack_timeout(pre_start_deadline: float | None) -> float:
  if pre_start_deadline is None:
    return CFG_ACK_TIMEOUT
  remaining = pre_start_deadline - time.monotonic()
  if remaining <= 0.0:
    raise TimeoutError("receiver configuration pre-START deadline exhausted")
  return min(CFG_ACK_TIMEOUT, remaining)


def send_configuration_with_ack(
  pigeon: TTYPigeon,
  message: bytes,
  pre_start_deadline: float | None = None,
) -> bool:
  """Send one CFG item and report whether its UART write boundary was crossed."""
  write_attempted = False

  def mark_write_attempt() -> None:
    nonlocal write_attempted
    if pre_start_deadline is not None and time.monotonic() >= pre_start_deadline:
      raise TimeoutError("receiver configuration pre-START deadline exhausted before UART write")
    write_attempted = True

  if isinstance(pigeon, TTYPigeon):
    try:
      pigeon.send_with_ack(
        message,
        before_send=mark_write_attempt,
        timeout=configuration_ack_timeout(pre_start_deadline),
        deadline=pre_start_deadline,
      )
    except Exception as exc:
      exc.receiver_write_attempted = write_attempted
      raise
  else:
    # Test/fake pigeons implement send_with_ack as their physical boundary.
    mark_write_attempt()
    try:
      pigeon.send_with_ack(message)
    except Exception as exc:
      exc.receiver_write_attempted = write_attempted
      raise
  return write_attempted


@dataclass(frozen=True)
class ReceiverConfigurationSummary:
  receiver_cycle: int
  transport_verified: bool
  configuration_started_at: float
  configuration_completed_at: float
  items: tuple[ReceiverConfigurationItemResult, ...]
  gnss_start_attempted: bool = False
  gnss_start_sent: bool = False
  boot_id: str = RECEIVER_CONFIGURATION_BOOT_ID
  process_start_id: str = RECEIVER_CONFIGURATION_PROCESS_START_ID
  receiver_fingerprint: str = "unidentified"
  navx5_ack_aiding_result: Navx5AckAidingConfigurationResult | None = None

  @property
  def configuration_elapsed_seconds(self) -> float:
    return self.configuration_completed_at - self.configuration_started_at

  @property
  def total_items(self) -> int:
    return len(self.items)

  @property
  def verified_items(self) -> int:
    return sum(item.verified for item in self.items)

  @property
  def failed_items(self) -> int:
    return self.total_items - self.verified_items

  @property
  def unsupported_items(self) -> int:
    return sum(
      item.failure_kind is ReceiverConfigurationFailureKind.UNSUPPORTED
      for item in self.items
    )

  @property
  def mandatory_failures(self) -> tuple[ReceiverConfigurationItemResult, ...]:
    return tuple(item for item in self.items if item.mandatory and not item.verified)

  @property
  def optional_failures(self) -> tuple[ReceiverConfigurationItemResult, ...]:
    return tuple(item for item in self.items if not item.mandatory and not item.verified)

  @property
  def all_mandatory_items_verified(self) -> bool:
    return all(item.verified for item in self.items if item.mandatory)

  @property
  def configuration_degraded(self) -> bool:
    navx5_failed = self.navx5_ack_aiding_result not in (
      Navx5AckAidingConfigurationResult.ENABLED_AND_VERIFIED,
      Navx5AckAidingConfigurationResult.ALREADY_ENABLED,
      Navx5AckAidingConfigurationResult.UNSUPPORTED,
    )
    return not self.all_mandatory_items_verified or navx5_failed


@dataclass(frozen=True)
class ReceiverConfigurationPersistenceStatus:
  path: str
  succeeded: bool
  error_type: str | None = None
  error: str | None = None


RECEIVER_CONFIGURATION_SUMMARY_SCHEMA_VERSION = 3
RECEIVER_CONFIGURATION_SUMMARY_MAX_BYTES = 16384


_last_receiver_configuration_summary: ReceiverConfigurationSummary | None = None
_last_receiver_configuration_persistence_status: ReceiverConfigurationPersistenceStatus | None = None
_current_receiver_configuration_fingerprint: str | None = None
_current_receiver_configuration_cycle: int | None = None
_current_receiver_configuration_record_ready = False


def last_receiver_configuration_summary() -> ReceiverConfigurationSummary | None:
  return _last_receiver_configuration_summary


def last_receiver_configuration_persistence_status() -> ReceiverConfigurationPersistenceStatus | None:
  return _last_receiver_configuration_persistence_status


def receiver_configuration_summary_path() -> Path:
  return Path(GPS_ASSISTANCE_CACHE_PATH).with_name("receiver_configuration_summary.json")


def validate_receiver_configuration_summary_record(record: object) -> None:
  if not isinstance(record, dict):
    raise ReceiverConfigurationError("receiver configuration summary must be an object")
  record = cast(dict[str, Any], record)
  required_fields = {
    "schema_version",
    "boot_id",
    "process_start_id",
    "receiver_fingerprint",
    "receiver_cycle",
    "transport_verified",
    "configuration_started_at",
    "configuration_completed_at",
    "configuration_elapsed_seconds",
    "total_items",
    "verified_items",
    "failed_items",
    "unsupported_items",
    "mandatory_failures",
    "optional_failures",
    "all_mandatory_items_verified",
    "configuration_degraded",
    "navx5_ack_aiding_result",
    "gnss_start_attempted",
    "gnss_start_sent",
    "items",
  }
  if set(record) != required_fields:
    raise ReceiverConfigurationError("receiver configuration summary fields are invalid")
  if type(record["schema_version"]) is not int or record["schema_version"] != RECEIVER_CONFIGURATION_SUMMARY_SCHEMA_VERSION:
    raise ReceiverConfigurationError("receiver configuration summary schema version is invalid")
  for field, maximum_length in (
    ("boot_id", 256),
    ("process_start_id", 256),
    ("receiver_fingerprint", 128),
  ):
    if (
      not isinstance(record[field], str)
      or not record[field]
      or len(record[field]) > maximum_length
    ):
      raise ReceiverConfigurationError(
        f"receiver configuration summary {field} is invalid"
      )
  receiver_cycle = record["receiver_cycle"]
  if type(receiver_cycle) is not int or receiver_cycle < 0:
    raise ReceiverConfigurationError("receiver configuration summary cycle is invalid")
  for field in (
    "transport_verified",
    "all_mandatory_items_verified",
    "configuration_degraded",
    "gnss_start_attempted",
    "gnss_start_sent",
  ):
    if type(record[field]) is not bool:
      raise ReceiverConfigurationError(f"receiver configuration summary {field} is invalid")
  started_at = record["configuration_started_at"]
  completed_at = record["configuration_completed_at"]
  elapsed = record["configuration_elapsed_seconds"]
  if any(type(value) not in (int, float) or not isfinite(float(value)) for value in (started_at, completed_at, elapsed)):
    raise ReceiverConfigurationError("receiver configuration summary timing is invalid")
  if completed_at < started_at or abs(float(elapsed) - (float(completed_at) - float(started_at))) > 1e-9:
    raise ReceiverConfigurationError("receiver configuration summary elapsed time is inconsistent")
  if record["gnss_start_sent"] and not record["gnss_start_attempted"]:
    raise ReceiverConfigurationError("receiver configuration summary START state is inconsistent")
  navx5_result = record["navx5_ack_aiding_result"]
  if (
    not isinstance(navx5_result, str)
    or navx5_result not in {
      result.value for result in Navx5AckAidingConfigurationResult
    }
  ):
    raise ReceiverConfigurationError(
      "receiver configuration summary NAVX5 result is invalid"
    )
  items = record["items"]
  if not isinstance(items, list) or len(items) != len(RECEIVER_CONFIGURATION_ITEM_INVENTORY):
    raise ReceiverConfigurationError("receiver configuration summary item list is invalid")
  item_fields = {
    "item_name",
    "mandatory",
    "attempted",
    "write_attempt_count",
    "ack_status",
    "poll_attempt_count",
    "readback_status",
    "expected_value",
    "observed_value",
    "verified",
    "failure_kind",
    "failure_phase",
    "error_type",
    "error",
  }
  ack_values = {status.value for status in ReceiverConfigurationAckStatus}
  readback_values = {status.value for status in ReceiverConfigurationReadbackStatus}
  failure_values = {kind.value for kind in ReceiverConfigurationFailureKind}
  for item in items:
    if not isinstance(item, dict) or set(item) != item_fields:
      raise ReceiverConfigurationError("receiver configuration item fields are invalid")
    item = cast(dict[str, Any], item)
    for field in ("item_name", "expected_value"):
      if not isinstance(item[field], str) or not item[field] or len(item[field]) > 128:
        raise ReceiverConfigurationError(f"receiver configuration item {field} is invalid")
    for field in ("observed_value", "failure_phase", "error_type", "error"):
      if item[field] is not None and (not isinstance(item[field], str) or len(item[field]) > 128):
        raise ReceiverConfigurationError(f"receiver configuration item {field} is invalid")
    for field in ("mandatory", "attempted", "verified"):
      if type(item[field]) is not bool:
        raise ReceiverConfigurationError(f"receiver configuration item {field} is invalid")
    for field in ("write_attempt_count", "poll_attempt_count"):
      if type(item[field]) is not int or item[field] < 0:
        raise ReceiverConfigurationError(f"receiver configuration item {field} is invalid")
    if item["attempted"] != (item["write_attempt_count"] > 0):
      raise ReceiverConfigurationError("receiver configuration write accounting is inconsistent")
    if (
      not isinstance(item["ack_status"], str)
      or item["ack_status"] not in ack_values
      or not isinstance(item["readback_status"], str)
      or item["readback_status"] not in readback_values
    ):
      raise ReceiverConfigurationError("receiver configuration item status enum is invalid")
    if item["failure_kind"] is not None and (not isinstance(item["failure_kind"], str) or item["failure_kind"] not in failure_values):
      raise ReceiverConfigurationError("receiver configuration failure enum is invalid")
    if item["verified"]:
      if (
        item["readback_status"] != ReceiverConfigurationReadbackStatus.VERIFIED.value
        or item["failure_kind"] is not None
        or item["failure_phase"] is not None
        or item["error_type"] is not None
        or item["error"] is not None
      ):
        raise ReceiverConfigurationError("verified receiver configuration item is inconsistent")
    elif (
      item["readback_status"] == ReceiverConfigurationReadbackStatus.VERIFIED.value
      or item["failure_kind"] is None
      or not item["failure_phase"]
      or not item["error_type"]
      or item["error"] is None
    ):
      raise ReceiverConfigurationError("failed receiver configuration item is inconsistent")
    if item["write_attempt_count"] == 0 and item["ack_status"] == ReceiverConfigurationAckStatus.ACKNOWLEDGED.value:
      raise ReceiverConfigurationError("receiver configuration ACK accounting is inconsistent")
    if item["write_attempt_count"] > 0 and item["ack_status"] in (
      ReceiverConfigurationAckStatus.NOT_REQUIRED.value,
      ReceiverConfigurationAckStatus.NOT_SENT.value,
    ):
      raise ReceiverConfigurationError("receiver configuration ACK status is inconsistent")
    expected_readback_for_failure = {
      ReceiverConfigurationFailureKind.UNSUPPORTED.value: ReceiverConfigurationReadbackStatus.NOT_SUPPORTED.value,
      ReceiverConfigurationFailureKind.PARSER_ERROR.value: ReceiverConfigurationReadbackStatus.PARSER_ERROR.value,
      ReceiverConfigurationFailureKind.POLL_TIMEOUT.value: ReceiverConfigurationReadbackStatus.TIMED_OUT.value,
      ReceiverConfigurationFailureKind.DEADLINE_EXHAUSTED.value: ReceiverConfigurationReadbackStatus.DEADLINE_EXHAUSTED.value,
      ReceiverConfigurationFailureKind.DEFERRED_POST_START.value: ReceiverConfigurationReadbackStatus.DEFERRED_POST_START.value,
    }
    if item["failure_kind"] in expected_readback_for_failure and item["readback_status"] != expected_readback_for_failure[item["failure_kind"]]:
      raise ReceiverConfigurationError("receiver configuration failure/readback state is inconsistent")
    expected_ack_for_failure = {
      ReceiverConfigurationFailureKind.ACK_REJECTED.value: ReceiverConfigurationAckStatus.REJECTED.value,
      ReceiverConfigurationFailureKind.ACK_TIMEOUT.value: ReceiverConfigurationAckStatus.TIMED_OUT.value,
      ReceiverConfigurationFailureKind.WRITE_ERROR.value: ReceiverConfigurationAckStatus.WRITE_ERROR.value,
    }
    if item["failure_kind"] in expected_ack_for_failure and item["ack_status"] != expected_ack_for_failure[item["failure_kind"]]:
      raise ReceiverConfigurationError("receiver configuration failure/ACK state is inconsistent")
  verified_items = sum(item["verified"] for item in items)
  unsupported_items = sum(item["failure_kind"] == ReceiverConfigurationFailureKind.UNSUPPORTED.value for item in items)
  mandatory_failures = [item["item_name"] for item in items if item["mandatory"] and not item["verified"]]
  optional_failures = [item["item_name"] for item in items if not item["mandatory"] and not item["verified"]]
  observed_inventory = tuple(
    (item["item_name"], item["mandatory"])
    for item in items
  )
  if observed_inventory != RECEIVER_CONFIGURATION_ITEM_INVENTORY:
    raise ReceiverConfigurationError(
      "receiver configuration summary inventory is incomplete or invalid"
    )
  expected_counts = {
    "total_items": len(items),
    "verified_items": verified_items,
    "failed_items": len(items) - verified_items,
    "unsupported_items": unsupported_items,
  }
  if any(type(record[field]) is not int or record[field] != value for field, value in expected_counts.items()):
    raise ReceiverConfigurationError("receiver configuration summary counts are inconsistent")
  if (
    not isinstance(record["mandatory_failures"], list)
    or not isinstance(record["optional_failures"], list)
    or record["mandatory_failures"] != mandatory_failures
    or record["optional_failures"] != optional_failures
  ):
    raise ReceiverConfigurationError("receiver configuration failure lists are inconsistent")
  if record["all_mandatory_items_verified"] != (not mandatory_failures):
    raise ReceiverConfigurationError("receiver configuration mandatory status is inconsistent")
  if not record["transport_verified"] or not record["gnss_start_attempted"]:
    raise ReceiverConfigurationError(
      "receiver configuration summary is not terminal"
    )
  navx5_degraded = navx5_result not in (
    Navx5AckAidingConfigurationResult.ENABLED_AND_VERIFIED.value,
    Navx5AckAidingConfigurationResult.ALREADY_ENABLED.value,
    Navx5AckAidingConfigurationResult.UNSUPPORTED.value,
  )
  if record["configuration_degraded"] != (
    bool(mandatory_failures) or navx5_degraded
  ):
    raise ReceiverConfigurationError(
      "receiver configuration degraded status is inconsistent"
    )


def load_receiver_configuration_summary_record(
  expected_receiver_fingerprint: str | None = None,
  expected_receiver_cycle: int | None = None,
) -> dict[str, object] | None:
  """Load the durable terminal record when it is a complete JSON object."""
  path = receiver_configuration_summary_path()
  if not _current_receiver_configuration_record_ready:
    return None
  persistence_status = last_receiver_configuration_persistence_status()
  if persistence_status is not None and persistence_status.path == str(path) and not persistence_status.succeeded:
    return None
  try:
    payload = path.read_text()
    if len(payload.encode()) > RECEIVER_CONFIGURATION_SUMMARY_MAX_BYTES:
      raise ReceiverConfigurationError("receiver configuration summary exceeds 16 KiB")
    record = json.loads(payload)
    validate_receiver_configuration_summary_record(record)
    expected_fingerprint = (
      expected_receiver_fingerprint
      or _current_receiver_configuration_fingerprint
    )
    expected_cycle = (
      expected_receiver_cycle
      if expected_receiver_cycle is not None
      else _current_receiver_configuration_cycle
    )
    if (
      expected_fingerprint is None
      or expected_cycle is None
      or type(expected_cycle) is not int
      or expected_cycle < 0
      or record["boot_id"] != RECEIVER_CONFIGURATION_BOOT_ID
      or record["process_start_id"]
      != RECEIVER_CONFIGURATION_PROCESS_START_ID
      or record["receiver_fingerprint"]
      != expected_fingerprint[:128]
      or record["receiver_cycle"] != expected_cycle
    ):
      raise ReceiverConfigurationError(
        "receiver configuration summary identity is stale"
      )
    return record
  except FileNotFoundError:
    return None
  except Exception:
    cloudlog.warning("GPS receiver configuration summary load failed")
    return None


def persist_receiver_configuration_summary(summary: ReceiverConfigurationSummary) -> bool:
  """Persist a bounded, restart-survivable terminal configuration record."""
  global _current_receiver_configuration_record_ready
  global _last_receiver_configuration_persistence_status

  def bounded(value: str | None) -> str | None:
    return value[:128] if value is not None else None

  path = receiver_configuration_summary_path()
  temporary_path = path.with_suffix(".tmp")
  stale_path = path.with_suffix(".stale")
  _current_receiver_configuration_record_ready = False
  try:
    if not receiver_configuration_summary_matches_active_cycle(summary):
      raise ReceiverConfigurationError(
        "receiver configuration summary does not match the active cycle"
      )
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
      os.replace(path, stale_path)
    items = [
      {
        "item_name": bounded(item.item_name),
        "mandatory": item.mandatory,
        "attempted": item.attempted,
        "write_attempt_count": item.write_attempt_count,
        "ack_status": item.ack_status.value,
        "poll_attempt_count": item.poll_attempt_count,
        "readback_status": item.readback_status.value,
        "expected_value": bounded(item.expected_value),
        "observed_value": bounded(item.observed_value),
        "verified": item.verified,
        "failure_kind": item.failure_kind.value if item.failure_kind is not None else None,
        "failure_phase": bounded(item.failure_phase),
        "error_type": bounded(item.error_type),
        "error": bounded(item.error),
      }
      for item in summary.items
    ]
    mandatory_failures = [item["item_name"] for item in items if item["mandatory"] and not item["verified"]]
    optional_failures = [item["item_name"] for item in items if not item["mandatory"] and not item["verified"]]
    record = {
      "schema_version": RECEIVER_CONFIGURATION_SUMMARY_SCHEMA_VERSION,
      "boot_id": summary.boot_id,
      "process_start_id": summary.process_start_id,
      "receiver_fingerprint": bounded(summary.receiver_fingerprint),
      "receiver_cycle": summary.receiver_cycle,
      "transport_verified": summary.transport_verified,
      "configuration_started_at": summary.configuration_started_at,
      "configuration_completed_at": summary.configuration_completed_at,
      "configuration_elapsed_seconds": summary.configuration_elapsed_seconds,
      "total_items": len(items),
      "verified_items": sum(item["verified"] for item in items),
      "failed_items": sum(not item["verified"] for item in items),
      "unsupported_items": sum(item["failure_kind"] == ReceiverConfigurationFailureKind.UNSUPPORTED.value for item in items),
      "mandatory_failures": mandatory_failures,
      "optional_failures": optional_failures,
      "all_mandatory_items_verified": not mandatory_failures,
      "configuration_degraded": summary.configuration_degraded,
      "navx5_ack_aiding_result": (
        summary.navx5_ack_aiding_result.value
        if summary.navx5_ack_aiding_result is not None
        else None
      ),
      "gnss_start_attempted": summary.gnss_start_attempted,
      "gnss_start_sent": summary.gnss_start_sent,
      "items": items,
    }
    validate_receiver_configuration_summary_record(record)
    payload = json.dumps(record, separators=(",", ":"))
    if len(payload.encode()) > RECEIVER_CONFIGURATION_SUMMARY_MAX_BYTES:
      raise ReceiverConfigurationError("bounded receiver configuration summary exceeds 16 KiB")
    temporary_path.write_text(payload)
    validate_receiver_configuration_summary_record(json.loads(temporary_path.read_text()))
    os.replace(temporary_path, path)
    try:
      stale_path.unlink(missing_ok=True)
    except OSError:
      cloudlog.warning("GPS stale receiver configuration summary cleanup failed")
    _last_receiver_configuration_persistence_status = ReceiverConfigurationPersistenceStatus(
      str(path),
      True,
    )
    _current_receiver_configuration_record_ready = True
    return True
  except Exception as exc:
    try:
      temporary_path.unlink(missing_ok=True)
    except OSError:
      pass
    _last_receiver_configuration_persistence_status = ReceiverConfigurationPersistenceStatus(
      str(path),
      False,
      type(exc).__name__,
      str(exc)[:128],
    )
    cloudlog.warning("GPS receiver configuration summary persistence failed, " + f"error_type={type(exc).__name__}, error={str(exc)[:128]}")
    return False



def receiver_configuration_cycle_id(pigeon: object) -> int:
  for attribute in ("receiver_cycle", "_receiver_cycle"):
    try:
      value = getattr(pigeon, attribute)
    except Exception:
      continue
    if type(value) is int and value >= 0:
      return value
  cloudlog.warning("GPS receiver configuration cycle unavailable; using startup cycle 0")
  return 0


def receiver_configuration_fingerprint(pigeon: object) -> str:
  initialization = _ACTIVE_PRE_ACQUISITION_INITIALIZATION
  candidate = (
    initialization.receiver_fingerprint
    if initialization is not None
    else getattr(pigeon, "receiver_fingerprint", "unidentified")
  )
  if not isinstance(candidate, str) or not candidate.strip():
    return "unidentified"
  return candidate[:128]


def set_receiver_configuration_context(
  receiver_cycle: int,
  receiver_fingerprint: str,
) -> None:
  global _current_receiver_configuration_cycle
  global _current_receiver_configuration_fingerprint
  if type(receiver_cycle) is not int or receiver_cycle < 0:
    raise ValueError("receiver configuration cycle must be non-negative")
  if not isinstance(receiver_fingerprint, str):
    raise ValueError("receiver configuration fingerprint must be a string")
  _current_receiver_configuration_cycle = receiver_cycle
  _current_receiver_configuration_fingerprint = (
    receiver_fingerprint.strip()[:128] or "unidentified"
  )


def receiver_configuration_summary_matches_active_cycle(
  summary: ReceiverConfigurationSummary,
) -> bool:
  return (
    summary.boot_id == RECEIVER_CONFIGURATION_BOOT_ID
    and summary.process_start_id == RECEIVER_CONFIGURATION_PROCESS_START_ID
    and _current_receiver_configuration_cycle is not None
    and summary.receiver_cycle == _current_receiver_configuration_cycle
    and _current_receiver_configuration_fingerprint is not None
    and summary.receiver_fingerprint[:128]
    == _current_receiver_configuration_fingerprint
  )


def begin_receiver_configuration_cycle(
  pigeon: object,
  receiver_fingerprint: str,
  *,
  transport_verified: bool,
) -> None:
  global _current_receiver_configuration_record_ready
  global _last_receiver_configuration_persistence_status
  global _last_receiver_configuration_summary
  _last_receiver_configuration_summary = None
  _last_receiver_configuration_persistence_status = None
  _current_receiver_configuration_record_ready = False
  set_receiver_configuration_context(
    receiver_configuration_cycle_id(pigeon),
    receiver_fingerprint,
  )
  try:
    cast(Any, pigeon)._transport_verified_for_receiver_cycle = (
      transport_verified
    )
  except (AttributeError, TypeError):
    pass


def deferred_post_start_configuration_result(
  item_name: str,
  expected_value: str,
) -> ReceiverConfigurationItemResult:
  return ReceiverConfigurationItemResult(
    item_name=item_name,
    mandatory=False,
    attempted=False,
    write_attempt_count=0,
    ack_status=ReceiverConfigurationAckStatus.NOT_REQUIRED,
    poll_attempt_count=0,
    readback_status=(
      ReceiverConfigurationReadbackStatus.DEFERRED_POST_START
    ),
    verified=False,
    expected_value=expected_value,
    observed_value=None,
    failure_kind=(
      ReceiverConfigurationFailureKind.DEFERRED_POST_START
    ),
    failure_phase="post_start",
    error_type="DeferredPostStart",
    error="optional receiver configuration deferred until after GNSS START",
  )


def run_optional_receiver_configuration_items(
  pigeon: TTYPigeon,
) -> tuple[ReceiverConfigurationItemResult, ...]:
  """Run optional output and diagnostic checks only after GNSS START."""
  results: list[ReceiverConfigurationItemResult] = []

  def poll(poll_function: Callable[..., object], *args: object) -> object:
    try:
      return poll_function(pigeon, *args, timeout=0.5, deadline=None)
    except TypeError as exc:
      if not any(
        keyword in str(exc)
        for keyword in (
          "unexpected keyword argument 'timeout'",
          "unexpected keyword argument 'deadline'",
        )
      ):
        raise
      return poll_function(pigeon, *args)

  for definition in RECEIVER_OUTPUT_STREAM_ITEMS:
    if definition.mandatory:
      continue
    message = add_ubx_checksum(bytes((
      0xB5, 0x62, 0x06, 0x01, 0x03, 0x00,
      definition.message_class, definition.message_id, 0x01,
    )))
    results.append(run_receiver_configuration_item(
      item_name=definition.item_name,
      mandatory=False,
      expected_value=definition.expected_value,
      poll=lambda cls=definition.message_class, msg=definition.message_id: poll(
        poll_cfg_msg, cls, msg,
      ),
      verify=lambda value: (
        (_ for _ in ()).throw(
          ReceiverConfigurationError("CFG-MSG UART1 rate mismatch")
        )
        if value.rates[1] != 1 else None
      ),
      write=lambda message=message: send_configuration_with_ack(
        pigeon, message,
      ),
    ))

  def verify_diagnostic_readback(
    value: object,
    expected_type: type,
    name: str,
    verifier: Callable[[object], None],
  ) -> None:
    if not isinstance(value, expected_type):
      raise ReceiverConfigurationParserError(
        f"{name} response unavailable, malformed, or unsupported layout"
      )
    verifier(value)

  for name, expected, poll_item, verify in (
    (
      "CFG-GNSS",
      "gps_enabled_valid_channels",
      lambda: poll(poll_cfg_gnss),
      lambda value: verify_diagnostic_readback(
        value,
        GnssConfig,
        "CFG-GNSS",
        lambda parsed: verify_cfg_gnss_conservatively(
          cast(GnssConfig, parsed)
        ),
      ),
    ),
    (
      "CFG-RXM",
      "continuous_mode=0_or_4",
      lambda: poll(poll_cfg_rxm),
      lambda value: verify_diagnostic_readback(
        value,
        RxmConfig,
        "CFG-RXM",
        lambda parsed: verify_cfg_rxm_conservatively(
          cast(RxmConfig, parsed)
        ),
      ),
    ),
    (
      "CFG-PM2",
      "inactive_power_management",
      lambda: poll(poll_cfg_pm2),
      lambda value: verify_diagnostic_readback(
        value,
        Pm2Config,
        "CFG-PM2",
        lambda parsed: verify_cfg_pm2_conservatively(
          cast(Pm2Config, parsed)
        ),
      ),
    ),
  ):
    results.append(run_receiver_configuration_item(
      item_name=name,
      mandatory=False,
      expected_value=expected,
      poll=poll_item,
      verify=verify,
      write=lambda: None,
      max_write_attempts=0,
    ))
  return tuple(results)


def init_pigeon(
  pigeon: TTYPigeon,
  *,
  pre_start_deadline: float | None = None,
  include_optional: bool = True,
) -> bool:
  """Configure independent receiver items without replaying earlier items."""
  global _last_receiver_configuration_summary
  _last_receiver_configuration_summary = None
  started_at = time.monotonic()
  receiver_cycle = receiver_configuration_cycle_id(pigeon)
  fingerprint = receiver_configuration_fingerprint(pigeon)
  set_receiver_configuration_context(receiver_cycle, fingerprint)
  port_messages = (
    b"\xb5\x62\x06\x00\x14\x00\x03\xFF\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x01\x00\x01\x00\x00\x00\x00\x00\x1E\x7F",
    b"\xb5\x62\x06\x00\x14\x00\x00\xFF\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x19\x35",
    b"\xb5\x62\x06\x00\x14\x00\x01\x00\x00\x00\xC0\x08\x00\x00\x00\x08\x07\x00\x01\x00\x01\x00\x00\x00\x00\x00\xF4\x80",
    b"\xb5\x62\x06\x00\x14\x00\x04\xFF\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x1D\x85",
  )
  results: list[ReceiverConfigurationItemResult] = []

  def add(item: ReceiverConfigurationItemResult) -> None:
    results.append(item)

  def poll_with_remaining_deadline(
    poll_function: Callable[..., object],
    *args: object,
  ) -> object:
    timeout = configuration_poll_timeout(pre_start_deadline)
    try:
      return poll_function(
        pigeon,
        *args,
        timeout=timeout,
        deadline=pre_start_deadline,
      )
    except TypeError as exc:
      # Lightweight legacy test doubles predate timeout support. Production
      # poll functions all accept it, so this compatibility path cannot
      # weaken the receiver's actual pre-START deadline.
      if not any(
        keyword in str(exc)
        for keyword in (
          "unexpected keyword argument 'timeout'",
          "unexpected keyword argument 'deadline'",
        )
      ):
        raise
      return poll_function(pigeon, *args)

  for message in port_messages:
    expected = parse_cfg_prt(message)
    if expected is None:
      raise ReceiverConfigurationError("Invalid built-in CFG-PRT configuration message")
    add(run_receiver_configuration_item(
      item_name=f"CFG-PRT-{expected.port_id}", mandatory=True,
      expected_value=repr(expected),
      poll=lambda port_id=expected.port_id: poll_with_remaining_deadline(poll_cfg_prt, port_id),
      verify=lambda observed, expected=expected: verify_cfg_prt_config(cast(PortConfig, observed), expected),
      write=lambda message=message: send_configuration_with_ack(pigeon, message, pre_start_deadline),
      pre_start_deadline=pre_start_deadline,
    ))

  def exact_item(
    name: str, message: bytes, poll: Callable[[], object], verify: Callable[[object], None], expected: str,
    mandatory: bool = True,
  ) -> None:
    add(run_receiver_configuration_item(
      item_name=name, mandatory=mandatory, expected_value=expected,
      poll=poll, verify=verify,
      write=lambda: send_configuration_with_ack(pigeon, message, pre_start_deadline),
      pre_start_deadline=pre_start_deadline,
    ))

  exact_item(
    "CFG-RATE", b"\xB5\x62\x06\x08\x06\x00\x64\x00\x01\x00\x00\x00\x79\x10", lambda: poll_with_remaining_deadline(poll_cfg_rate),
    lambda value: (_ for _ in ()).throw(ReceiverConfigurationError("CFG-RATE mismatch"))
    if value != RateConfig(100, 1, 0) else None, "100ms/1",
  )
  nav5_message = build_cfg_nav5_set_message(
    dynamic_model=4,
    fix_mode=3,
  )
  exact_item(
    "CFG-NAV5", nav5_message, lambda: poll_with_remaining_deadline(poll_cfg_nav5),
    lambda value: (_ for _ in ()).throw(ReceiverConfigurationError("CFG-NAV5 mismatch"))
    if value.dynamic_model != 4 or value.fix_mode != 3 else None, "dynamic=4,fix=3",
  )
  odo_message = (
    b"\xB5\x62\x06\x1E\x14\x00\x00\x00\x00\x00\x01\x03\x00\x00"
    + b"\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x3C\x37"
  )
  exact_item(
    "CFG-ODO", odo_message, lambda: poll_with_remaining_deadline(poll_cfg_odo),
    lambda value: (_ for _ in ()).throw(ReceiverConfigurationError("CFG-ODO mismatch"))
    if (value.flags & 0x0F) != 1 or value.profile != 3 else None, "flags=1,profile=3",
  )
  exact_item(
    "CFG-ITFM", b"\xB5\x62\x06\x39\x08\x00\xFF\xAD\x62\xAD\x1E\x63\x00\x00\x83\x0C", lambda: poll_with_remaining_deadline(poll_cfg_itfm),
    lambda value: (_ for _ in ()).throw(ReceiverConfigurationError("CFG-ITFM mismatch"))
    if value != ItfmConfig(0xAD62ADFF, 0x0000631E) else None, "0xAD62ADFF/0x0000631E",
  )
  for definition in RECEIVER_OUTPUT_STREAM_ITEMS:
    if not definition.mandatory:
      continue
    message = bytes((
      0xB5, 0x62, 0x06, 0x01, 0x03, 0x00,
      definition.message_class, definition.message_id, 0x01,
    ))
    message = add_ubx_checksum(message)
    exact_item(
      definition.item_name, message,
      lambda cls=definition.message_class, msg=definition.message_id: poll_with_remaining_deadline(poll_cfg_msg, cls, msg),
      lambda value: (_ for _ in ()).throw(ReceiverConfigurationError("CFG-MSG UART1 rate mismatch"))
      if value.rates[1] != 1 else None,
      definition.expected_value, definition.mandatory,
    )
  if include_optional:
    results.extend(run_optional_receiver_configuration_items(pigeon))
  else:
    optional_expected_values = {
      definition.item_name: definition.expected_value
      for definition in RECEIVER_OUTPUT_STREAM_ITEMS
      if not definition.mandatory
    }
    optional_expected_values.update({
      "CFG-GNSS": "gps_enabled_valid_channels",
      "CFG-RXM": "continuous_mode=0_or_4",
      "CFG-PM2": "inactive_power_management",
    })
    for item_name, mandatory in RECEIVER_CONFIGURATION_ITEM_INVENTORY:
      if not mandatory:
        add(deferred_post_start_configuration_result(
          item_name,
          optional_expected_values[item_name],
        ))
  _last_receiver_configuration_summary = ReceiverConfigurationSummary(
    receiver_cycle,
    (
      getattr(pigeon, "_transport_verified_for_receiver_cycle", False)
      or (
        _ACTIVE_PRE_ACQUISITION_INITIALIZATION is not None
        and _ACTIVE_PRE_ACQUISITION_INITIALIZATION.transport_mon_ver_info is not None
      )
    ),
    started_at,
    time.monotonic(),
    tuple(results),
    boot_id=RECEIVER_CONFIGURATION_BOOT_ID,
    process_start_id=RECEIVER_CONFIGURATION_PROCESS_START_ID,
    receiver_fingerprint=fingerprint,
    navx5_ack_aiding_result=(
      _ACTIVE_PRE_ACQUISITION_INITIALIZATION.navx5_ack_aiding_result
      if _ACTIVE_PRE_ACQUISITION_INITIALIZATION is not None
      else None
    ),
  )
  mandatory_failures = [result for result in results if result.mandatory and not result.verified]
  if mandatory_failures:
    cloudlog.warning(
      "GPS receiver configuration summary, "
      + f"total_items={len(results)}, "
      + f"verified_items={sum(result.verified for result in results)}, "
      + f"failed_items={sum(not result.verified for result in results)}, "
      + "mandatory_failures="
      + ",".join(result.item_name for result in mandatory_failures)
    )
    return False
  cloudlog.info(
    "GPS receiver configuration summary, "
    + f"total_items={len(results)}, verified_items={sum(result.verified for result in results)}, "
    + "mandatory_failures=0"
  )
  return True


def finish_post_start_receiver_configuration(
  pigeon: TTYPigeon,
) -> None:
  """Replace deferred optional results without delaying GNSS START."""
  global _last_receiver_configuration_summary
  summary = _last_receiver_configuration_summary
  if (
    summary is None
    or not receiver_configuration_summary_matches_active_cycle(summary)
  ):
    return
  optional_results = {
    result.item_name: result
    for result in run_optional_receiver_configuration_items(pigeon)
  }
  expected_optional_names = {
    item_name
    for item_name, mandatory in RECEIVER_CONFIGURATION_ITEM_INVENTORY
    if not mandatory
  }
  if set(optional_results) != expected_optional_names:
    raise ReceiverConfigurationError(
      "post-START receiver configuration inventory is incomplete"
    )
  _last_receiver_configuration_summary = replace(
    summary,
    configuration_completed_at=time.monotonic(),
    items=tuple(
      optional_results.get(result.item_name, result)
      for result in summary.items
    ),
    navx5_ack_aiding_result=(
      _ACTIVE_PRE_ACQUISITION_INITIALIZATION.navx5_ack_aiding_result
      if _ACTIVE_PRE_ACQUISITION_INITIALIZATION is not None
      else summary.navx5_ack_aiding_result
    ),
  )


def run_post_start_legacy_assistance(pigeon: TTYPigeon) -> None:
  """Run legacy backup diagnostics after GNSS START.

  AssistNow Online download/injection is retired. Current YUMA/DBD/position
  assistance and AssistNow Autonomous (NAVX5 AOP) supersede that path.
  """
  try:
    restore_status = pigeon.poll_backup_restore_status()
    if restore_status == 2:
      cloudlog.warning("almanac backup restored")
    elif restore_status == 3:
      cloudlog.warning("no almanac backup found")
    else:
      cloudlog.error(f"failed to restore almanac backup, status: {restore_status}")
  except Exception:
    cloudlog.exception("GPS almanac backup status poll failed")
  cloudlog.warning("Pigeon GPS on!")


def deinitialize_and_exit(pigeon: TTYPigeon | None):
  if pigeon is not None:
    # controlled GNSS stop
    pigeon.send(CONTROLLED_GNSS_STOP_MESSAGE)

  # turn off power and exit cleanly
  set_power(False)
  sys.exit(0)

@dataclass
class PreAcquisitionInitialization:
  callback: Callable[[], None]
  gnss_start_callback: Callable[[float], None] | None = None
  transport_already_started: bool = False
  transport_mon_ver_info: MonVerInfo | None = None
  executed: bool = False
  gnss_start_sent_at: float | None = None
  pre_gnss_start_drain_required: bool = False
  pre_start_deadline: float | None = None
  receiver_fingerprint: str = "unidentified"
  navx5_ack_aiding_result: Navx5AckAidingConfigurationResult | None = None

  def run(self) -> None:
    if self.executed:
      return
    self.executed = True
    self.callback()

  def note_gnss_start_sent(self, now: float) -> None:
    self.gnss_start_sent_at = now
    self.mark_gnss_start_sent()
    if self.gnss_start_callback is not None:
      self.gnss_start_callback(now)

  def mark_gnss_start_sent(self) -> None:
    global _last_receiver_configuration_summary
    if (
      _last_receiver_configuration_summary is not None
      and receiver_configuration_summary_matches_active_cycle(
        _last_receiver_configuration_summary
      )
    ):
      _last_receiver_configuration_summary = replace(
        _last_receiver_configuration_summary,
        gnss_start_attempted=True,
        gnss_start_sent=True,
        navx5_ack_aiding_result=self.navx5_ack_aiding_result,
      )

  def note_gnss_start_attempted(self) -> None:
    global _last_receiver_configuration_summary
    if (
      _last_receiver_configuration_summary is not None
      and receiver_configuration_summary_matches_active_cycle(
        _last_receiver_configuration_summary
      )
    ):
      _last_receiver_configuration_summary = replace(
        _last_receiver_configuration_summary,
        gnss_start_attempted=True,
        navx5_ack_aiding_result=self.navx5_ack_aiding_result,
      )

  def require_pre_gnss_start_drain(self) -> None:
    self.pre_gnss_start_drain_required = True


_ACTIVE_PRE_ACQUISITION_INITIALIZATION: (
  PreAcquisitionInitialization | None
) = None


@contextmanager
def install_pre_acquisition_initialization(
  callback: Callable[[], None],
  gnss_start_callback: Callable[[float], None] | None = None,
  transport_already_started: bool = False,
  transport_mon_ver_info: MonVerInfo | None = None,
  pre_start_deadline: float | None = None,
  receiver_fingerprint: str = "unidentified",
) -> Iterator[PreAcquisitionInitialization]:
  global _ACTIVE_PRE_ACQUISITION_INITIALIZATION
  if _ACTIVE_PRE_ACQUISITION_INITIALIZATION is not None:
    raise RuntimeError("pre-acquisition initialization is already active")
  if transport_already_started and transport_mon_ver_info is None:
    raise ValueError(
      "transport_mon_ver_info is required when transport is already started"
    )
  if not isinstance(receiver_fingerprint, str):
    raise ValueError("receiver_fingerprint must be a string")
  normalized_receiver_fingerprint = (
    receiver_fingerprint.strip()[:128] or "unidentified"
  )
  state = PreAcquisitionInitialization(
    callback,
    gnss_start_callback,
    transport_already_started=transport_already_started,
    transport_mon_ver_info=transport_mon_ver_info,
    pre_start_deadline=pre_start_deadline,
    receiver_fingerprint=normalized_receiver_fingerprint,
  )
  _ACTIVE_PRE_ACQUISITION_INITIALIZATION = state
  try:
    yield state
  finally:
    _ACTIVE_PRE_ACQUISITION_INITIALIZATION = None


def prepare_receiver_cycle_response_state(
  pigeon: TTYPigeon,
) -> None:
  if hasattr(pigeon, "_stream_parser"):
    pigeon.reset_response_state()
  pigeon._receiver_cycle_response_state_prepared = True


def _start_pigeon_transport_attempts(
  pigeon: TTYPigeon,
  max_attempts: int,
) -> MonVerInfo:
  if type(max_attempts) is not int or max_attempts < 1:
    raise ValueError("max_attempts must be a positive integer")

  signal.signal(signal.SIGINT, lambda sig, frame: deinitialize_and_exit(pigeon))
  response_state_prepared = (
    getattr(
      pigeon,
      "_receiver_cycle_response_state_prepared",
      False,
    )
    is True
  )
  if response_state_prepared:
    pigeon._receiver_cycle_response_state_prepared = False

  last_transport_error: BaseException | None = None
  for attempt in range(1, max_attempts + 1):
    try:
      if (
        not (attempt == 1 and response_state_prepared)
        and hasattr(pigeon, "_stream_parser")
      ):
        pigeon.reset_response_state()
      set_power(False)
      time.sleep(0.1)
      set_power(True)
      # STOP is the first possible receiver command after power-on. It is
      # repeated by init_baudrate after the receiver boot interval.
      pigeon.send(CONTROLLED_GNSS_STOP_MESSAGE)
      time.sleep(0.5)
      init_baudrate(pigeon)
      mon_ver_info = poll_mon_ver(
        pigeon,
        RECEIVER_TRANSPORT_PROBE_TIMEOUT,
      )
    except RawPublicationError:
      raise
    except (OSError, ResponseTransactionError) as exc:
      last_transport_error = exc
      cloudlog.warning(", ".join((
        "GPS receiver transport verification",
        f"attempt={attempt}",
        f"max_attempts={max_attempts}",
        "baud=460800",
        "probe=MON-VER",
        "result=transport_error",
        f"error_type={type(exc).__name__}",
        f"error={exc}",
      )))
      continue

    if mon_ver_info is not None:
      cloudlog.info(", ".join((
        "GPS receiver transport verification",
        f"attempt={attempt}",
        f"max_attempts={max_attempts}",
        "baud=460800",
        "probe=MON-VER",
        "result=verified",
      )))
      return mon_ver_info

    cloudlog.warning(", ".join((
      "GPS receiver transport verification",
      f"attempt={attempt}",
      f"max_attempts={max_attempts}",
      "baud=460800",
      "probe=MON-VER",
      "result=no_response",
    )))

  error = ReceiverConfigurationError(
    "Failed to verify u-blox transport at 460800 baud after "
    + f"{max_attempts} physical receiver attempt"
    + ("" if max_attempts == 1 else "s")
  )
  if last_transport_error is not None:
    raise error from last_transport_error
  raise error


def start_pigeon_transport(pigeon: TTYPigeon) -> None:
  mon_ver_info = _start_pigeon_transport_attempts(
    pigeon,
    RUNTIME_RECOVERY_RECEIVER_TRANSPORT_MAX_ATTEMPTS,
  )
  pigeon._transport_verified_for_receiver_cycle = True
  initialization = _ACTIVE_PRE_ACQUISITION_INITIALIZATION
  if initialization is not None:
    initialization.transport_mon_ver_info = mon_ver_info


def bootstrap_process_start_transport(
  pigeon: TTYPigeon,
) -> MonVerInfo:
  return _start_pigeon_transport_attempts(
    pigeon,
    PROCESS_START_RECEIVER_TRANSPORT_MAX_ATTEMPTS,
  )


def supports_process_start_transport_bootstrap(
  pigeon: object,
) -> bool:
  return all((
    callable(getattr(pigeon, "send", None)),
    callable(getattr(pigeon, "reset_response_state", None)),
    callable(getattr(pigeon, "set_frame_dispatcher", None)),
    callable(getattr(pigeon, "dispatch_pending_frames", None)),
    callable(getattr(pigeon, "receive_transaction_data", None)),
  ))


@contextmanager
def paused_gnss_acquisition(pigeon: TTYPigeon) -> Iterator[None]:
  # UBX-CFG-RST resetMode 0x08/0x09 stops/starts GNSS tasks without
  # clearing the hot-start BBR data. Newer firmware does not ACK these
  # commands, so the transition is bounded by a short deterministic delay.
  pigeon.send(CONTROLLED_GNSS_STOP_MESSAGE)
  stopped_at: float | None = None
  body_error: BaseException | None = None
  drain_error: BaseException | None = None
  start_error: BaseException | None = None
  initialization = _ACTIVE_PRE_ACQUISITION_INITIALIZATION
  try:
    stopped_at = time.monotonic()
    cloudlog.info(f"GPS acquisition transition: phase=stop_sent monotonic={stopped_at:.6f}")
    transition_delay = CONTROLLED_GNSS_TRANSITION_DELAY
    if (
      initialization is not None
      and initialization.pre_start_deadline is not None
    ):
      transition_delay = min(
        transition_delay,
        max(
          0.0,
          initialization.pre_start_deadline - time.monotonic(),
        ),
      )
    if transition_delay > 0.0:
      time.sleep(transition_delay)
    yield
  except BaseException as exc:
    body_error = exc
  finally:
    initialization = _ACTIVE_PRE_ACQUISITION_INITIALIZATION
    if (
      body_error is None
      and initialization is not None
      and initialization.pre_gnss_start_drain_required
    ):
      try:
        drain = getattr(pigeon, "drain_before_transaction", None)
        drain_deadline_expired = (
          initialization.pre_start_deadline is not None
          and time.monotonic() >= initialization.pre_start_deadline
        )
        if drain_deadline_expired:
          cloudlog.warning(
            "GPS pre-START input drain skipped: shared deadline exhausted"
          )
        elif callable(drain):
          if isinstance(pigeon, TTYPigeon):
            drain(
              "position_assistance_pre_gnss_start_boundary",
              deadline=initialization.pre_start_deadline,
            )
          else:
            drain("position_assistance_pre_gnss_start_boundary")
      except BaseException as exc:
        if (
          isinstance(exc, TimeoutError)
          and initialization.pre_start_deadline is not None
        ):
          cloudlog.warning(
            "GPS pre-START input drain ended at shared deadline; "
            + "GNSS START continues"
          )
        else:
          drain_error = exc
          cloudlog.exception(
            "GPS pre-START input drain failed; GNSS START still attempted"
          )
    try:
      if initialization is not None:
        initialization.note_gnss_start_attempted()
      pigeon.send(CONTROLLED_GNSS_START_MESSAGE)
      if initialization is not None:
        initialization.mark_gnss_start_sent()
      started_at = time.monotonic()
      prestart_elapsed_seconds = (
        max(0.0, started_at - stopped_at)
        if stopped_at is not None
        else None
      )
      cloudlog.info(
        "GPS acquisition transition: phase=start_sent "
        + f"monotonic={started_at:.6f} "
        + "prestart_elapsed_seconds="
        + (
          f"{prestart_elapsed_seconds:.6f}"
          if prestart_elapsed_seconds is not None
          else "unavailable"
        )
      )
      if initialization is not None:
        initialization.note_gnss_start_sent(started_at)
      time.sleep(CONTROLLED_GNSS_TRANSITION_DELAY)
    except BaseException as exc:
      start_error = exc
      cloudlog.exception("GPS controlled GNSS START failed")
    if (
      initialization is not None
      and _last_receiver_configuration_summary is not None
      and receiver_configuration_summary_matches_active_cycle(
        _last_receiver_configuration_summary
      )
    ):
      persist_receiver_configuration_summary(
        _last_receiver_configuration_summary
      )

  if body_error is not None:
    raise body_error.with_traceback(body_error.__traceback__)
  if drain_error is not None:
    raise drain_error.with_traceback(drain_error.__traceback__)
  if start_error is not None:
    raise start_error.with_traceback(start_error.__traceback__)


def finish_pigeon_initialization(pigeon: TTYPigeon) -> bool:
  initialization = _ACTIVE_PRE_ACQUISITION_INITIALIZATION
  initialized = (
    init_pigeon(
      pigeon,
      pre_start_deadline=initialization.pre_start_deadline,
      include_optional=False,
    )
    if initialization is not None
    else init_pigeon(pigeon)
  )
  if not initialized:
    cloudlog.error("GPS receiver configuration degraded; bounded mandatory failures " + "were recorded and GNSS acquisition continues")
  return initialized


def init(pigeon: TTYPigeon) -> None:
  initialization = _ACTIVE_PRE_ACQUISITION_INITIALIZATION
  if initialization is None or not initialization.transport_already_started:
    start_pigeon_transport(pigeon)
  if initialization is not None:
    with paused_gnss_acquisition(pigeon):
      finish_pigeon_initialization(pigeon)
      initialization.run()
    try:
      finish_post_start_receiver_configuration(pigeon)
    except Exception:
      cloudlog.exception(
        "GPS optional post-START receiver configuration failed"
      )
    if (
      _last_receiver_configuration_summary is not None
      and receiver_configuration_summary_matches_active_cycle(
        _last_receiver_configuration_summary
      )
    ):
      persist_receiver_configuration_summary(
        _last_receiver_configuration_summary
      )
    run_post_start_legacy_assistance(pigeon)
    return
  finish_pigeon_initialization(pigeon)
  run_post_start_legacy_assistance(pigeon)


class TimeAssistanceWriteStatus(StrEnum):
  NOT_ATTEMPTED = "not_attempted"
  SUCCEEDED = "succeeded"
  FAILED = "failed"


class TimeAssistanceAckStatus(StrEnum):
  NOT_ATTEMPTED = "not_attempted"
  ACCEPTED = "accepted"
  REJECTED = "rejected"
  TIMED_OUT = "timed_out"
  OBSERVATION_FAILED = "observation_failed"


@dataclass(frozen=True)
class TimeAssistanceAttemptDiagnostic:
  attempted_at: float
  written_at: float | None
  ack_observed_at: float | None
  write_status: TimeAssistanceWriteStatus
  ack_status: TimeAssistanceAckStatus
  ack_info_code: int | None
  accepted_at: float | None
  message_id: int | None
  message_type: int | None
  source: str
  correction: bool
  diagnostic_context: str | None = None
  error_type: str | None = None
  error: str | None = None


TimeAssistanceDiagnosticCallback = Callable[
  [TimeAssistanceAttemptDiagnostic],
  None,
]


def format_time_assistance_attempt_diagnostic(
  diagnostic: TimeAssistanceAttemptDiagnostic,
) -> str:
  def optional(value: object | None) -> str:
    return "none" if value is None else str(value)

  def message_byte(value: int | None) -> str:
    return "none" if value is None else f"0x{value:02X}"

  return ", ".join((
    "GPS time assistance diagnostic",
    f"attempted_at={diagnostic.attempted_at}",
    f"written_at={optional(diagnostic.written_at)}",
    f"ack_observed_at={optional(diagnostic.ack_observed_at)}",
    f"write_status={diagnostic.write_status.value}",
    f"ack_status={diagnostic.ack_status.value}",
    f"ack_info_code={optional(diagnostic.ack_info_code)}",
    f"accepted_at={optional(diagnostic.accepted_at)}",
    f"message_id={message_byte(diagnostic.message_id)}",
    f"message_type={message_byte(diagnostic.message_type)}",
    f"source={diagnostic.source}",
    f"correction={str(diagnostic.correction).lower()}",
    "diagnostic_context="
    + optional(diagnostic.diagnostic_context),
    f"error_type={optional(diagnostic.error_type)}",
    f"error={optional(diagnostic.error)}",
  ))


def log_time_assistance_attempt_diagnostic(
  diagnostic: TimeAssistanceAttemptDiagnostic,
) -> None:
  message = format_time_assistance_attempt_diagnostic(diagnostic)
  if diagnostic.ack_status in (
    TimeAssistanceAckStatus.REJECTED,
    TimeAssistanceAckStatus.TIMED_OUT,
    TimeAssistanceAckStatus.OBSERVATION_FAILED,
  ) or diagnostic.write_status is TimeAssistanceWriteStatus.FAILED:
    cloudlog.warning(message)
  else:
    cloudlog.info(message)


def _publish_time_assistance_attempt_diagnostic(
  callback: TimeAssistanceDiagnosticCallback | None,
  diagnostic: TimeAssistanceAttemptDiagnostic,
) -> None:
  if callback is None:
    return
  try:
    callback(diagnostic)
  except Exception:
    cloudlog.exception(
      "GPS time assistance diagnostic callback failed"
    )


def send_time_assistance(
  pigeon: TTYPigeon,
  assistance_time: datetime | None = None,
  accuracy_seconds: int = 30,
  source: str = "synchronized",
  diagnostic_context: str | None = None,
  ack_timeout: float = GPS_ASSISTANCE_ACK_TIMEOUT,
  time_provenance: ReceiverTimeProvenanceTracker | None = None,
  assistance_boottime_seconds: float | None = None,
  independent: bool | None = None,
  source_provenance: TimeProvenance | None = None,
  correction: bool = False,
  diagnostic_callback: TimeAssistanceDiagnosticCallback | None = None,
  monotonic: Callable[[], float] | None = None,
) -> bool:
  """Send trusted UTC or an explicit RTC-derived estimate."""
  clock = time.monotonic if monotonic is None else monotonic
  attempted_at = clock()
  time_tracker = time_provenance or getattr(
    pigeon,
    "time_provenance",
    None,
  )
  if assistance_time is None:
    host_time = read_host_time_observation()
    if host_time is None or not host_time.independent:
      _publish_time_assistance_attempt_diagnostic(
        diagnostic_callback,
        TimeAssistanceAttemptDiagnostic(
          attempted_at=attempted_at,
          written_at=None,
          ack_observed_at=None,
          write_status=TimeAssistanceWriteStatus.NOT_ATTEMPTED,
          ack_status=TimeAssistanceAckStatus.NOT_ATTEMPTED,
          ack_info_code=None,
          accepted_at=None,
          message_id=None,
          message_type=None,
          source=source,
          correction=correction,
          diagnostic_context=diagnostic_context,
          error_type="TrustedTimeUnavailable",
          error="independent host time is unavailable",
        ),
      )
      return False

    assistance_time = host_time.utc
    accuracy_seconds = min(
      65_535,
      max(0, ceil(host_time.uncertainty_seconds)),
    )
    assistance_boottime_seconds = (
      host_time.observed_boottime_seconds
    )
    independent = True
    source_provenance = TimeProvenance.NETWORK_INDEPENDENT
    source = host_time.source.value

  try:
    msg = build_time_assistance_message(
      assistance_time,
      accuracy_seconds=accuracy_seconds,
    )
  except Exception as exc:
    _publish_time_assistance_attempt_diagnostic(
      diagnostic_callback,
      TimeAssistanceAttemptDiagnostic(
        attempted_at=attempted_at,
        written_at=None,
        ack_observed_at=None,
        write_status=TimeAssistanceWriteStatus.NOT_ATTEMPTED,
        ack_status=TimeAssistanceAckStatus.NOT_ATTEMPTED,
        ack_info_code=None,
        accepted_at=None,
        message_id=None,
        message_type=None,
        source=source,
        correction=correction,
        diagnostic_context=diagnostic_context,
        error_type=type(exc).__name__,
        error=str(exc),
      ),
    )
    raise

  message_id = msg[3] if len(msg) > 3 else None
  payload_length = (
    int.from_bytes(msg[4:6], "little")
    if len(msg) >= 6
    else 0
  )
  message_type = (
    msg[6]
    if payload_length > 0 and len(msg) > 6
    else None
  )
  context_suffix = (
    f", {diagnostic_context}"
    if diagnostic_context is not None
    else ""
  )
  message_fields = (
    f"source={source}",
    f"uncertainty_seconds={accuracy_seconds}",
    f"correction={str(correction).lower()}",
    f"mga_message_id=0x{msg[3]:02X}",
    f"mga_message_type=0x{msg[6]:02X}",
  )

  try:
    transaction = _begin_response_transaction(pigeon, msg)
    if time_tracker is not None:
      written_boottime = assistance_boottime_seconds
      if written_boottime is None:
        written_boottime = read_boottime_seconds()
      time_tracker.note_time_assistance_written(
        source=source,
        assistance_utc=assistance_time,
        uncertainty_seconds=accuracy_seconds,
        now=transaction.sent_at,
        written_boottime_seconds=written_boottime,
        independent=independent,
        provenance=source_provenance,
        correction=correction,
      )
  except Exception as exc:
    cloudlog.exception(
      ", ".join((
        "Time assistance serial write failed",
        *message_fields,
        "write_result=failed",
        "ack_result=not_attempted",
      )) + context_suffix
    )
    _publish_time_assistance_attempt_diagnostic(
      diagnostic_callback,
      TimeAssistanceAttemptDiagnostic(
        attempted_at=attempted_at,
        written_at=None,
        ack_observed_at=None,
        write_status=TimeAssistanceWriteStatus.FAILED,
        ack_status=TimeAssistanceAckStatus.NOT_ATTEMPTED,
        ack_info_code=None,
        accepted_at=None,
        message_id=message_id,
        message_type=message_type,
        source=source,
        correction=correction,
        diagnostic_context=diagnostic_context,
        error_type=type(exc).__name__,
        error=str(exc),
      ),
    )
    return False

  try:
    acknowledgment = wait_for_matching_mga_ack(
      pigeon,
      transaction,
      msg,
      timeout=ack_timeout,
    )
  except Exception as exc:
    cloudlog.exception(
      ", ".join((
        "Time assistance written; ublox ACK observation failed",
        *message_fields,
        "write_result=succeeded",
        "ack_result=observation_failed",
      )) + context_suffix
    )
    _publish_time_assistance_attempt_diagnostic(
      diagnostic_callback,
      TimeAssistanceAttemptDiagnostic(
        attempted_at=attempted_at,
        written_at=transaction.sent_at,
        ack_observed_at=None,
        write_status=TimeAssistanceWriteStatus.SUCCEEDED,
        ack_status=TimeAssistanceAckStatus.OBSERVATION_FAILED,
        ack_info_code=None,
        accepted_at=None,
        message_id=message_id,
        message_type=message_type,
        source=source,
        correction=correction,
        diagnostic_context=diagnostic_context,
        error_type=type(exc).__name__,
        error=str(exc),
      ),
    )
    return False

  if acknowledgment is None:
    cloudlog.warning(
      ", ".join((
        "Time assistance written; matching ublox ACK timed out",
        *message_fields,
        "write_result=succeeded",
        "ack_result=timed_out",
      )) + context_suffix
    )
    _publish_time_assistance_attempt_diagnostic(
      diagnostic_callback,
      TimeAssistanceAttemptDiagnostic(
        attempted_at=attempted_at,
        written_at=transaction.sent_at,
        ack_observed_at=None,
        write_status=TimeAssistanceWriteStatus.SUCCEEDED,
        ack_status=TimeAssistanceAckStatus.TIMED_OUT,
        ack_info_code=None,
        accepted_at=None,
        message_id=message_id,
        message_type=message_type,
        source=source,
        correction=correction,
        diagnostic_context=diagnostic_context,
        error_type="TimeoutError",
        error="matching u-blox MGA acknowledgment timed out",
      ),
    )
    return False

  ack_observed_at = clock()
  ack_fields = (
    f"ack_type={acknowledgment.acknowledgment_type}",
    f"ack_version={acknowledgment.version}",
    f"ack_infoCode={acknowledgment.info_code}",
    f"ack_message_id=0x{acknowledgment.message_id:02X}",
  )
  if acknowledgment.accepted:
    cloudlog.info(
      ", ".join((
        "Time assistance written and accepted by ublox",
        *message_fields,
        "write_result=succeeded",
        "ack_result=accepted",
        *ack_fields,
      )) + context_suffix
    )
    _publish_time_assistance_attempt_diagnostic(
      diagnostic_callback,
      TimeAssistanceAttemptDiagnostic(
        attempted_at=attempted_at,
        written_at=transaction.sent_at,
        ack_observed_at=ack_observed_at,
        write_status=TimeAssistanceWriteStatus.SUCCEEDED,
        ack_status=TimeAssistanceAckStatus.ACCEPTED,
        ack_info_code=acknowledgment.info_code,
        accepted_at=ack_observed_at,
        message_id=message_id,
        message_type=message_type,
        source=source,
        correction=correction,
        diagnostic_context=diagnostic_context,
      ),
    )
    return True

  cloudlog.warning(
    ", ".join((
      "Time assistance written but rejected by ublox",
      *message_fields,
      "write_result=succeeded",
      "ack_result=rejected",
      *ack_fields,
    )) + context_suffix
  )
  _publish_time_assistance_attempt_diagnostic(
    diagnostic_callback,
    TimeAssistanceAttemptDiagnostic(
      attempted_at=attempted_at,
      written_at=transaction.sent_at,
      ack_observed_at=ack_observed_at,
      write_status=TimeAssistanceWriteStatus.SUCCEEDED,
      ack_status=TimeAssistanceAckStatus.REJECTED,
      ack_info_code=acknowledgment.info_code,
      accepted_at=None,
      message_id=message_id,
      message_type=message_type,
      source=source,
      correction=correction,
      diagnostic_context=diagnostic_context,
    ),
  )
  return False


def evaluate_time_authority(
  time_authority: TimeAuthority,
  host_time_observation: HostTimeObservation | None,
) -> TimeAuthorityEvaluation:
  evaluation = time_authority.current_authorized_time(
    host_time_observation=host_time_observation,
  )
  authorized = evaluation.authorized_time
  fields = [
    "GPS trusted time authority evaluation",
    (
      f"authorized={str(authorized is not None).lower()}"
    ),
    (
      f"evidence={authorized.evidence.value}"
      if authorized is not None
      else "evidence=none"
    ),
    (
      f"source={authorized.source.value}"
      if authorized is not None
      else "source=none"
    ),
    (
      f"independent={str(authorized.independent).lower()}"
      if authorized is not None
      else "independent=false"
    ),
    (
      f"uncertainty_seconds={authorized.uncertainty_seconds}"
      if authorized is not None
      else "uncertainty_seconds=none"
    ),
    (
      "host_source="
      + (
        host_time_observation.source.value
        if host_time_observation is not None
        else "none"
      )
    ),
    (
      "host_independent="
      + (
        str(host_time_observation.independent).lower()
        if host_time_observation is not None
        else "false"
      )
    ),
    (
      "host_generation="
      + (
        host_time_observation.generation
        if host_time_observation is not None
        else "none"
      )
    ),
    (
      f"rejection_reason={evaluation.rejection_reason.value}"
      if evaluation.rejection_reason is not None
      else "rejection_reason=none"
    ),
    f"anchor_write_status={evaluation.anchor_write_status.value}",
    (
      f"anchor_write_error={evaluation.anchor_write_error}"
      if evaluation.anchor_write_error is not None
      else "anchor_write_error=none"
    ),
    (
      "selected_anchor_generation="
      + (
        evaluation.selected_anchor_generation
        if evaluation.selected_anchor_generation is not None
        else "none"
      )
    ),
    (
      "selected_anchor_sequence="
      + str(evaluation.selected_anchor_sequence)
    ),
    (
      "anchor_write_reason="
      + (
        evaluation.anchor_write_reason.value
        if evaluation.anchor_write_reason is not None
        else "none"
      )
    ),
    (
      "anchor_comparison_status="
      + (
        evaluation.anchor_comparison.status.value
        if evaluation.anchor_comparison is not None
        else "none"
      )
    ),
    (
      "anchor_error_seconds="
      + (
        str(evaluation.anchor_comparison.error_seconds)
        if evaluation.anchor_comparison is not None
        else "none"
      )
    ),
    (
      "anchor_allowed_error_seconds="
      + (
        str(
          evaluation
          .anchor_comparison
          .allowed_error_seconds
        )
        if evaluation.anchor_comparison is not None
        else "none"
      )
    ),
  ]
  message = ", ".join(fields)
  if authorized is not None:
    cloudlog.info(message)
  else:
    cloudlog.warning(message)
  return evaluation


def cached_rtc_time_assistance(
  receiver_fingerprint: str,
) -> tuple[datetime, int] | None:
  """Choose the freshest defensible RTC estimate from either fixed cache."""
  store = NavigationCacheStore(GPS_ASSISTANCE_CACHE_PATH, loader=load_cache)
  cleanup_failure = store.remove_stale_candidate()
  if cleanup_failure is not None:
    cloudlog.warning(f"GPS stale cache candidate cleanup failed: reason={cleanup_failure}")
  inventory = store.inspect(receiver_fingerprint, None)
  current_rtc = read_rtc_counter_seconds()
  selected, evaluations = select_rtc_estimate(inventory, current_rtc)
  for inspection, result in evaluations:
    if result is None:
      cloudlog.info(
        f"GPS RTC anchor generation: generation={inspection.generation}, status={inspection.state.name.lower()}, reason={inspection.error or 'unavailable'}"
      )
      continue
    if isinstance(result, RtcEstimateRejection):
      level = cloudlog.warning if result.reason is RtcEstimateRejectionReason.RTC_ROLLBACK else cloudlog.info
      reason_text = {
        RtcEstimateRejectionReason.MISSING_CACHED_RTC_ANCHOR: "cache has no RTC anchor",
        RtcEstimateRejectionReason.CURRENT_RTC_UNAVAILABLE: "current RTC unavailable",
        RtcEstimateRejectionReason.RTC_ROLLBACK: "RTC rollback detected",
        RtcEstimateRejectionReason.ELAPSED_TIME_ABOVE_MAXIMUM: "elapsed time above maximum",
        RtcEstimateRejectionReason.UTC_BEFORE_SUPPORTED_MINIMUM: "estimated UTC before supported minimum",
        RtcEstimateRejectionReason.UTC_AFTER_SUPPORTED_MAXIMUM: "estimated UTC after supported maximum",
        RtcEstimateRejectionReason.INVALID_RTC_ESTIMATE: "RTC estimate invalid",
      }[result.reason]
      level(", ".join((
        "GPS RTC anchor generation rejected",
        f"generation={inspection.generation}",
        f"reason={reason_text}",
        f"saved_rtc_seconds={inspection.cache.rtc_counter_seconds}",
        f"current_rtc_seconds={current_rtc}",
        f"elapsed_seconds={result.elapsed_seconds}",
        f"maximum_elapsed_seconds={MAX_RTC_ASSISTANCE_ELAPSED_SECONDS}",
      )))

  if selected is None:
    cloudlog.info("GPS RTC time assistance skipped: no valid fixed-file RTC anchor")
    return None
  cloudlog.info(", ".join((
    "GPS RTC time assistance ready",
    f"generation={selected.generation}",
    f"elapsed_seconds={selected.estimate.elapsed_seconds}",
    f"uncertainty_seconds={selected.estimate.uncertainty_seconds}",
  )))
  return selected.estimate.estimated_utc, selected.estimate.uncertainty_seconds


def gps_assistance_receiver_fingerprint(
  params: Params,
  mon_ver_info: MonVerInfo | None = None,
) -> str:
  hardware_serial = (
    params.get("HardwareSerial")
    or HARDWARE.get_serial()
  )
  return build_durable_receiver_fingerprint(
    str(hardware_serial or ""),
    mon_ver_info,
  )


def wait_for_matching_mga_ack(
  pigeon: TTYPigeon,
  transaction: ResponseTransaction,
  message: bytes,
  timeout: float = GPS_ASSISTANCE_ACK_TIMEOUT,
) -> MgaAck | None:
  if len(message) < 8:
    raise CacheValidationError("MGA message is truncated")

  expected_message_id = message[3]
  expected_payload_start = message[6:10].ljust(
    4,
    b"\x00",
  )
  deadline = time.monotonic() + timeout

  while time.monotonic() < deadline:
    result = None
    _, stream_frames, transaction_frames = _receive_transaction_data(pigeon, transaction)
    for frame in transaction_frames:
      acknowledgment = parse_mga_ack(frame)

      if (
        result is None
        and acknowledgment is not None
        and (
          acknowledgment.message_id != expected_message_id
          or acknowledgment.message_payload_start
          != expected_payload_start
        )
      ):
        continue
      if result is None and acknowledgment is not None:
        result = acknowledgment
        continue
    _queue_unrelated_frames(
      pigeon,
      stream_frames,
      lambda frame: (
        (acknowledgment := parse_mga_ack(frame)) is not None
        and acknowledgment.message_id == expected_message_id
        and acknowledgment.message_payload_start == expected_payload_start
      ),
      transaction.operation,
    )

    if result is not None:
      return result
    time.sleep(0.001)

  return None


def send_mga_with_strict_ack(
  pigeon: TTYPigeon,
  message: bytes,
  timeout: float = GPS_ASSISTANCE_ACK_TIMEOUT,
  database_frame_index: int | None = None,
  before_send: Callable[[], None] | None = None,
  time_provenance: ReceiverTimeProvenanceTracker | None = None,
  time_assistance_source: str = "mga_time_assistance",
) -> None:
  if len(message) < 8:
    raise CacheValidationError("MGA message is truncated")

  expected_message_id = message[3]
  payload_length = int.from_bytes(message[4:6], "little")
  expected_message_type = message[6] if payload_length > 0 else None
  mga_message_type = (
    f"0x{expected_message_type:02X}"
    if expected_message_type is not None
    else "unavailable"
  )

  try:
    if before_send is None:
      transaction = _begin_response_transaction(pigeon, message)
    else:
      transaction = _begin_response_transaction(
        pigeon,
        message,
        before_send=before_send,
      )
    if (
      time_provenance is not None
      and is_mga_time_assistance_message(message)
    ):
      time_provenance.note_time_assistance_written(
        source=time_assistance_source,
        assistance_utc=None,
        uncertainty_seconds=None,
        now=transaction.sent_at,
      )
  except OSError as exc:
    raise MgaWriteError(
      f"Failed to write MGA message 0x{expected_message_id:02X}: {type(exc).__name__}: {exc}",
      message_id=expected_message_id,
      message_type=expected_message_type,
    ) from exc
  except ResponseTransactionError as exc:
    raise MgaTransactionError(
      f"Failed to start MGA acknowledgment transaction for message 0x{expected_message_id:02X}: {type(exc).__name__}: {exc}",
      message_id=expected_message_id,
      message_type=expected_message_type,
      write_succeeded=False,
    ) from exc

  try:
    acknowledgment = wait_for_matching_mga_ack(
      pigeon,
      transaction,
      message,
      timeout=timeout,
    )
  except TimeoutError:
    raise
  except (OSError, ResponseTransactionError) as exc:
    raise MgaTransactionError(
      f"MGA acknowledgment transaction failed for message 0x{expected_message_id:02X}: {type(exc).__name__}: {exc}",
      message_id=expected_message_id,
      message_type=expected_message_type,
      write_succeeded=True,
    ) from exc

  if acknowledgment is None:
    raise TimeoutError(f"No matching MGA acknowledgment for message 0x{expected_message_id:02X}")

  if not acknowledgment.accepted:
    rejection_fields = [
      f"mga_message_type={mga_message_type}",
      f"message_id=0x{expected_message_id:02X}",
      f"ack_type={acknowledgment.acknowledgment_type}",
      f"ack_version={acknowledgment.version}",
      f"ack_infoCode={acknowledgment.info_code}",
      f"rejected_message_id=0x{acknowledgment.message_id:02X}",
    ]
    if database_frame_index is not None:
      rejection_fields.append(
        f"database_frame_index={database_frame_index}"
      )
    raise MgaReceiverNackError(
      "u-blox rejected MGA message: "
      + ", ".join(rejection_fields),
      message_id=expected_message_id,
      message_type=expected_message_type,
      ack_type=acknowledgment.acknowledgment_type,
      ack_version=acknowledgment.version,
      info_code=acknowledgment.info_code,
      rejected_message_id=acknowledgment.message_id,
    )


class NavigationAssistanceRestoreStatus(StrEnum):
  COMPLETE = "complete"
  PARTIAL = "partial"
  FAILED = "failed"


class NavigationAssistanceRestoreFailurePhase(StrEnum):
  CACHE_LOAD = "cache_load"
  POSITION_ASSISTANCE_BUILD = "position_assistance_build"
  POSITION_ASSISTANCE_WRITE = "position_assistance_write"
  POSITION_ASSISTANCE_ACK_REJECTED = "position_assistance_ack_rejected"
  POSITION_ASSISTANCE_ACK_TIMEOUT = "position_assistance_ack_timeout"
  POSITION_ASSISTANCE_ACK_OBSERVATION_FAILED = (
    "position_assistance_ack_observation_failed"
  )
  DATABASE_FRAME_RESTORE = "database_frame_restore"


class NavigationAssistanceCacheResult(StrEnum):
  SAVED = "saved"
  PRESERVED_EXISTING = "preserved_existing"
  FAILED = "failed"


def navigation_cache_phase_completed(result: NavigationAssistanceCacheResult) -> bool:
  return result in (
    NavigationAssistanceCacheResult.SAVED,
    NavigationAssistanceCacheResult.PRESERVED_EXISTING,
  )


@dataclass
class NavigationCaptureState:
  drive_cache_saved: bool = False
  post_drive_refresh_pending: bool = False
  durable_baseline_quality: NavigationQuality | None = None
  durable_cache_ready: bool = False
  readiness_log_key: tuple[object, ...] | None = None
  last_successful_qualified_upgrade: float | None = None
  capture_fix: NavPvtFix | None = None
  capture_quality: NavigationQuality | None = None
  capture_reason: str | None = None
  capture_receiver_cycle: int | None = None
  capture_is_upgrade: bool = False
  next_capture_attempt: float = 0.0

  @property
  def frozen(self) -> bool:
    return (
      self.capture_fix is not None
      or self.capture_quality is not None
      or self.capture_reason is not None
      or self.capture_receiver_cycle is not None
      or self.capture_is_upgrade
    )

  def road_state_changed(self, started: bool) -> None:
    if started:
      self.drive_cache_saved = False
      self.post_drive_refresh_pending = False
      self.durable_baseline_quality = None
      self.durable_cache_ready = False
      self.readiness_log_key = None
      self.last_successful_qualified_upgrade = None
      self.reset_receiver_cycle()
    else:
      self.post_drive_refresh_pending = True

  def reset_receiver_cycle(self) -> None:
    self.capture_fix = None
    self.capture_quality = None
    self.capture_reason = None
    self.capture_receiver_cycle = None
    self.capture_is_upgrade = False
    self.next_capture_attempt = 0.0

  def fail(self, now: float) -> None:
    self.capture_fix = None
    self.capture_quality = None
    self.capture_reason = None
    self.capture_receiver_cycle = None
    self.capture_is_upgrade = False
    self.next_capture_attempt = now + GPS_ASSISTANCE_CAPTURE_RETRY_INTERVAL

  def request(
    self,
    now: float,
    started: bool | None,
    collector_active: bool,
    tracker: CaptureQualityTracker,
    receiver_cycle: int | None = None,
    stable_fix: NavPvtFix | None = None,
  ) -> bool:
    if collector_active or self.frozen or now < self.next_capture_attempt:
      return False
    if started is True:
      context = "onroad" if not self.drive_cache_saved else "onroad_refresh"
    elif started is False and self.post_drive_refresh_pending:
      context = "post_drive"
    else:
      return False

    quality = tracker.quality(
      now,
      "onroad" if context == "onroad_refresh" else context,
    )
    fix = tracker.latest_fix
    if not capture_eligible(quality, stable_fix, fix):
      return False

    is_upgrade = self.drive_cache_saved
    if is_upgrade:
      if (
        quality is None
        or not quality.passes_policy
        or self.durable_baseline_quality is None
        or not navigation_quality_strictly_better(
          quality, self.durable_baseline_quality,
        )
      ):
        return False
      if (
        self.last_successful_qualified_upgrade is not None
        and now - self.last_successful_qualified_upgrade
        < GPS_ASSISTANCE_QUALIFIED_UPGRADE_COOLDOWN
      ):
        return False

    assert quality is not None and fix is not None
    self.capture_fix = fix
    self.capture_quality = quality
    self.capture_reason = context
    self.capture_receiver_cycle = 0 if receiver_cycle is None else receiver_cycle
    self.capture_is_upgrade = is_upgrade
    return True

  def complete(
    self,
    result: NavigationAssistanceCacheResult,
    now: float,
    durable_quality: NavigationQuality | None = None,
    finalized_quality: NavigationQuality | None = None,
  ) -> str | None:
    readiness_message = None
    if navigation_cache_phase_completed(result):
      durable_tier = navigation_quality_tier(durable_quality)
      durable_confirmed = durable_tier in (
        CacheQualityTier.USABLE,
        CacheQualityTier.QUALIFIED,
      )
      if not durable_confirmed:
        self.fail(now)
        return None

      self.durable_cache_ready = True
      self.drive_cache_saved = True
      self.durable_baseline_quality = durable_quality
      if self.capture_reason == "post_drive":
        self.post_drive_refresh_pending = False
      if (
        self.capture_is_upgrade
        and result is NavigationAssistanceCacheResult.SAVED
        and finalized_quality is not None
        and finalized_quality.passes_policy
      ):
        self.last_successful_qualified_upgrade = now
      if result is NavigationAssistanceCacheResult.PRESERVED_EXISTING:
        action = "existing_cache_preserved"
      elif finalized_quality == durable_quality:
        action = "candidate_saved_selected"
      else:
        action = "candidate_saved_existing_selected"
      context = self.capture_reason or "unknown"
      readiness_message = self.completion_readiness_message(
        action,
        context,
        finalized_quality,
        self.durable_baseline_quality,
      )
      self.capture_fix = None
      self.capture_quality = None
      self.capture_reason = None
      self.capture_receiver_cycle = None
      self.capture_is_upgrade = False
      self.next_capture_attempt = 0.0
    else:
      self.fail(now)
    return readiness_message

  def readiness_message(
    self,
    ready: bool,
    reason: str,
    quality: NavigationQuality | None = None,
  ) -> str | None:
    key = (ready, reason, self.quality_log_signature(quality))
    if self.readiness_log_key == key:
      return None
    self.readiness_log_key = key
    fields = [
      "GPS navigation cache power-removal readiness",
      f"ready={ready}",
      f"reason={reason}",
    ]
    if quality is not None:
      tier = navigation_quality_tier(quality)
      fields.extend((
        f"quality_tier={tier.value if tier is not None else 'invalid'}",
        f"gps_ephemeris={quality.gps_ephemeris_available}",
        f"glonass_ephemeris={quality.glonass_ephemeris_available}",
        f"total_ephemeris={quality.total_ephemeris_available}",
        f"satellites_used={quality.satellites_used}",
      ))
    return ", ".join(fields)

  @staticmethod
  def quality_log_signature(
    quality: NavigationQuality | None,
  ) -> tuple[object, ...] | None:
    if quality is None:
      return None
    return (
      navigation_quality_tier(quality),
      quality.continuous_reliable_fix_seconds,
      quality.continuous_orbit_quality_seconds,
      quality.gps_ephemeris_available,
      quality.glonass_ephemeris_available,
      quality.satellites_used,
      quality.gps_almanac_available,
      quality.glonass_almanac_available,
      quality.assistnow_offline_available,
    )

  def completion_readiness_message(
    self,
    action: str,
    context: str,
    candidate_quality: NavigationQuality | None,
    selected_quality: NavigationQuality,
  ) -> str | None:
    candidate_tier = navigation_quality_tier(candidate_quality)
    selected_tier = navigation_quality_tier(selected_quality)
    key = (
      True,
      action,
      context,
      self.quality_log_signature(candidate_quality),
      self.quality_log_signature(selected_quality),
    )
    if self.readiness_log_key == key:
      return None
    self.readiness_log_key = key
    return ", ".join((
      "GPS navigation cache power-removal readiness",
      "ready=True",
      f"candidate_quality_tier={candidate_tier.value if candidate_tier is not None else 'unavailable'}",
      f"selected_quality_tier={selected_tier.value if selected_tier is not None else 'invalid'}",
      f"action={action}",
      f"context={context}",
      f"selected_gps_ephemeris={selected_quality.gps_ephemeris_available}",
      f"selected_glonass_ephemeris={selected_quality.glonass_ephemeris_available}",
      f"selected_total_ephemeris={selected_quality.total_ephemeris_available}",
      f"selected_satellites_used={selected_quality.satellites_used}",
    ))

  def drive_end_readiness_message(self) -> str | None:
    if self.durable_cache_ready:
      return None
    return self.readiness_message(
      False,
      "no_usable_cache_completed",
    )


def request_navigation_database_capture(
  pigeon: TTYPigeon,
  dump_collector: NavigationDatabaseDumpCollector,
  capture_state: NavigationCaptureState,
  now: float,
  assistnow_autonomous_supported: bool,
) -> AopCaptureState:
  aop_state = (
    wait_for_aop_idle(pigeon)
    if assistnow_autonomous_supported
    else AopCaptureState.UNSUPPORTED
  )
  cloudlog.info(", ".join((
    "GPS navigation cache capture AOP state",
    f"aop_state={aop_state.value}",
    f"capture_reason={capture_state.capture_reason}",
    "action=proceed",
  )))
  dump_collector.start(now)
  pigeon.send(build_database_poll_message())
  cloudlog.info(f"Requested GPS navigation database for {capture_state.capture_reason} cache: frozen_quality={capture_state.capture_quality}")
  return aop_state


@dataclass
class AutonomousOrbitDiagnostics:
  logged_state_mask: int = 0

  def note_nav_sat(self, nav_sat: NavSatQuality) -> None:
    available = nav_sat.assistnow_autonomous_available
    used = nav_sat.orbit_source_counts.get("assistnow_autonomous", 0)
    state_index = (int(available > 0) << 1) | int(used > 0)
    state_bit = 1 << state_index
    if self.logged_state_mask & state_bit:
      return
    self.logged_state_mask |= state_bit
    cloudlog.info(", ".join((
      "GPS AssistNow Autonomous orbit diagnostics",
      f"available_satellites={available}",
      f"used_as_orbit_source_satellites={used}",
      f"autonomous_orbit_data_present={str(available > 0).lower()}",
      f"autonomous_orbit_data_used={str(used > 0).lower()}",
    )))


def finalized_capture_quality(
  state: NavigationCaptureState,
  tracker: CaptureQualityTracker,
  now: float,
  active_receiver_cycle: int | None = None,
  stable_fix: NavPvtFix | None = None,
) -> NavigationQuality | None:
  if state.capture_reason is None:
    return None
  if (
    active_receiver_cycle is not None
    and state.capture_receiver_cycle != active_receiver_cycle
  ):
    return None
  live_quality = tracker.quality(
    now,
    "onroad" if state.capture_reason == "onroad_refresh" else state.capture_reason,
  )
  if (
    not capture_eligible(live_quality, stable_fix, tracker.latest_fix)
    or state.capture_quality is None
    or not state.capture_quality.usable_for_capture
  ):
    return None
  assert live_quality is not None
  conservative_quality = conservative_navigation_quality(
    state.capture_quality, live_quality,
  )
  if conservative_quality is None or not conservative_quality.usable_for_capture:
    return None
  if not state.capture_is_upgrade:
    return conservative_quality
  if (
    conservative_quality.passes_policy
    and state.durable_baseline_quality is not None
    and navigation_quality_strictly_better(
      conservative_quality,
      state.durable_baseline_quality,
    )
  ):
    return conservative_quality
  return None


def capture_quality_remains_valid(
  state: NavigationCaptureState,
  tracker: CaptureQualityTracker,
  now: float,
  active_receiver_cycle: int | None = None,
  stable_fix: NavPvtFix | None = None,
) -> bool:
  return finalized_capture_quality(
    state, tracker, now, active_receiver_cycle, stable_fix,
  ) is not None


def durable_quality_after_cache_result(
  result: NavigationAssistanceCacheResult,
  receiver_fingerprint: str,
  trusted_now: datetime | None = None,
) -> NavigationQuality | None:
  if not navigation_cache_phase_completed(result) or trusted_now is None:
    return None
  try:
    selection, _ = NavigationCacheStore(
      GPS_ASSISTANCE_CACHE_PATH, loader=load_cache,
    ).select_best(receiver_fingerprint, trusted_now)
  except Exception:
    cloudlog.exception(
      "Failed to resolve selected GPS navigation cache generation",
    )
    return None
  return None if selection is None else selection.cache.quality


@dataclass(frozen=True)
class NavigationAssistanceRestoreResult:
  status: NavigationAssistanceRestoreStatus
  total_frame_count: int
  accepted_frame_count: int
  initially_rejected_indexes: tuple[int, ...] = ()
  initially_timed_out_indexes: tuple[int, ...] = ()
  retry_accepted_indexes: tuple[int, ...] = ()
  permanently_rejected_indexes: tuple[int, ...] = ()
  permanently_timed_out_indexes: tuple[int, ...] = ()
  failure_phase: NavigationAssistanceRestoreFailurePhase | None = None
  position_assistance_attempted: bool = False
  position_assistance_succeeded: bool = False
  position_assistance_message_id: int | None = None
  position_assistance_message_type: int | None = None
  position_assistance_write_status: PositionAssistanceWriteStatus = PositionAssistanceWriteStatus.NOT_ATTEMPTED
  position_assistance_ack_status: PositionAssistanceAckStatus = PositionAssistanceAckStatus.NOT_ATTEMPTED
  position_assistance_ack_info_code: int | None = None
  position_assistance_error_type: str | None = None
  position_assistance_error: str | None = None
  cache_saved_at_utc: datetime | None = None
  restored_cache_generation: str | None = None
  restored_cache_selection_reason: str | None = None
  restored_cache_database_digest: str | None = None
  restored_cache_age_seconds: float | None = None
  restored_cache_maximum_age_seconds: float | None = None
  restored_cache_expires_at_utc: datetime | None = None
  restored_cache_age_evidence: str | None = None
  restored_cache_age_verified: bool = False
  captured_gps_ephemeris_available: int | None = None
  captured_glonass_ephemeris_available: int | None = None
  captured_gps_startup_ready: bool | None = None
  restored_gps_ephemeris_fresh: bool | None = None
  restored_glonass_ephemeris_fresh: bool | None = None
  restored_quality_expiration_reasons: tuple[str, ...] = ()
  restored_navigation_quality: RestoredNavigationQuality | None = None
  restored_gps_almanac_available: int | None = None
  restored_glonass_almanac_available: int | None = None
  restored_gps_ephemeris_available: int | None = None
  restored_glonass_ephemeris_available: int | None = None
  restored_satellites_used: int | None = None
  restored_gps_startup_ready: bool | None = None
  restored_gps_almanac_satellite_ids: tuple[int, ...] | None = None
  captured_gps_almanac_available: int | None = None
  captured_glonass_almanac_available: int | None = None
  captured_satellites_used: int | None = None
  captured_gps_almanac_satellite_ids: tuple[int, ...] | None = None
  database_restore_disposition: NavigationDatabaseRestoreDisposition | None = None
  database_frames_attempted_count: int = 0
  database_restore_boot_id: str | None = None
  database_restore_state_error: str | None = None
  database_restore_recovered_interrupted_attempt: bool = False
  database_restore_candidate_identities: tuple[str, ...] = ()
  database_restore_initial_failure_kinds: tuple[str, ...] = ()
  database_restore_permanent_failure_kinds: tuple[str, ...] = ()
  database_restore_execution_error: str | None = None
  database_restore_runtime_phase: str | None = None
  database_restore_transfer_budget_seconds: float | None = None
  database_restore_transfer_started_at: float | None = None
  database_restore_transfer_completed_at: float | None = None
  database_restore_transfer_deadline: float | None = None
  database_restore_transfer_elapsed_seconds: float | None = None
  database_restore_first_failed_frame_index: int | None = None
  database_restore_first_failed_attempt: int | None = None
  database_restore_first_failure_kind: str | None = None
  database_restore_first_failure_error: str | None = None
  database_trusted_time_wait_allowed: bool = False
  database_network_available: bool = False
  database_trusted_time_wait_started_at: float | None = None
  database_trusted_time_wait_completed_at: float | None = None
  database_trusted_time_wait_deadline: float | None = None
  database_trusted_time_wait_elapsed_seconds: float | None = None
  database_trusted_time_wait_error_type: str | None = None
  database_trusted_time_wait_error: str | None = None

  @property
  def usable(self) -> bool:
    if self.database_restore_disposition is not None:
      return self.database_restore_disposition.database_available
    return self.status in (
      NavigationAssistanceRestoreStatus.COMPLETE,
      NavigationAssistanceRestoreStatus.PARTIAL,
    )


def format_navigation_assistance_restore_summary(
  result: NavigationAssistanceRestoreResult | None,
  *,
  attempted: bool,
  time_assistance_source: str | None,
  diagnostic_context: str | None = None,
) -> str:
  if result is None:
    fields = (
      "GPS navigation assistance restore result",
      f"restore_attempted={attempted}",
      "total_frames=0",
      "accepted_frames=0",
      "rejected_frames=0",
      "retry_attempts=0",
      "timeout_events=0",
      "failure_phase=none",
      "terminal_result=not_attempted",
      "database_restore_disposition=not_attempted",
      "database_frames_attempted=0",
      "database_terminal_ack_count_matched=not_applicable_per_frame_restore",
      "position_assistance_attempted=false",
      "position_assistance_message_id=none",
      "position_assistance_message_type=none",
      "position_assistance_write_status=not_attempted",
      "position_assistance_ack_status=not_attempted",
      "position_assistance_ack_info_code=none",
      "position_assistance_error_type=none",
      "position_assistance_error=none",
      f"time_assistance_source={time_assistance_source or 'none'}",
    )
  else:
    fields = (
    "GPS navigation assistance restore result",
    f"restore_attempted={attempted}",
    f"restore_status={result.status.value}",
    f"total_frames={result.total_frame_count}",
    f"accepted_frames={result.accepted_frame_count}",
    f"total_frame_count={result.total_frame_count}",
    f"accepted_frame_count={result.accepted_frame_count}",
    f"rejected_frames={len(result.permanently_rejected_indexes)}",
    f"retry_attempts={len(result.initially_rejected_indexes) + len(result.initially_timed_out_indexes)}",
    f"timeout_events={len(result.initially_timed_out_indexes) + len(result.permanently_timed_out_indexes)}",
    f"failure_phase={result.failure_phase.value if result.failure_phase is not None else 'none'}",
    f"terminal_result={result.status.value}",
    "database_restore_disposition="
    + (
      result.database_restore_disposition.value
      if result.database_restore_disposition is not None
      else "legacy"
    ),
    f"database_frames_attempted={result.database_frames_attempted_count}",
    f"database_restore_boot_id={result.database_restore_boot_id or 'none'}",
    f"database_restore_state_error={result.database_restore_state_error or 'none'}",
    "database_restore_recovered_interrupted_attempt="
    + str(result.database_restore_recovered_interrupted_attempt).lower(),
    "database_restore_candidate_identities="
    + str(list(result.database_restore_candidate_identities)),
    "database_restore_initial_failure_kinds="
    + str(list(result.database_restore_initial_failure_kinds)),
    "database_restore_permanent_failure_kinds="
    + str(list(result.database_restore_permanent_failure_kinds)),
    "database_restore_execution_error="
    + (result.database_restore_execution_error or "none"),
    "database_restore_runtime_phase="
    + (result.database_restore_runtime_phase or "none"),
    "database_restore_transfer_budget_seconds="
    + str(result.database_restore_transfer_budget_seconds),
    "database_restore_transfer_started_at="
    + str(result.database_restore_transfer_started_at),
    "database_restore_transfer_completed_at="
    + str(result.database_restore_transfer_completed_at),
    "database_restore_transfer_deadline="
    + str(result.database_restore_transfer_deadline),
    "database_restore_transfer_elapsed_seconds="
    + str(result.database_restore_transfer_elapsed_seconds),
    "database_restore_first_failed_frame_index="
    + str(result.database_restore_first_failed_frame_index),
    "database_restore_first_failed_attempt="
    + str(result.database_restore_first_failed_attempt),
    "database_restore_first_failure_kind="
    + (result.database_restore_first_failure_kind or "none"),
    "database_restore_first_failure_error="
    + (result.database_restore_first_failure_error or "none"),
    "database_trusted_time_wait_allowed="
    + str(result.database_trusted_time_wait_allowed).lower(),
    "database_network_available="
    + str(result.database_network_available).lower(),
    "database_trusted_time_wait_started_at="
    + str(result.database_trusted_time_wait_started_at),
    "database_trusted_time_wait_completed_at="
    + str(result.database_trusted_time_wait_completed_at),
    "database_trusted_time_wait_deadline="
    + str(result.database_trusted_time_wait_deadline),
    "database_trusted_time_wait_elapsed_seconds="
    + str(result.database_trusted_time_wait_elapsed_seconds),
    "database_trusted_time_wait_error_type="
    + (result.database_trusted_time_wait_error_type or "none"),
    "database_trusted_time_wait_error="
    + (result.database_trusted_time_wait_error or "none"),
    "database_terminal_ack_count_matched=not_applicable_per_frame_restore",
    "position_assistance_attempted="
    + str(result.position_assistance_attempted).lower(),
    "position_assistance_succeeded="
    + str(result.position_assistance_succeeded).lower(),
    "position_assistance_message_id="
    + (
      f"0x{result.position_assistance_message_id:02X}"
      if result.position_assistance_message_id is not None
      else "none"
    ),
    "position_assistance_message_type="
    + (
      f"0x{result.position_assistance_message_type:02X}"
      if result.position_assistance_message_type is not None
      else "none"
    ),
    "position_assistance_write_status="
    + result.position_assistance_write_status.value,
    "position_assistance_ack_status="
    + result.position_assistance_ack_status.value,
    "position_assistance_ack_info_code="
    + (
      str(result.position_assistance_ack_info_code)
      if result.position_assistance_ack_info_code is not None
      else "none"
    ),
    "position_assistance_error_type="
    + (result.position_assistance_error_type or "none"),
    "position_assistance_error="
    + (result.position_assistance_error or "none"),
    f"time_assistance_source={time_assistance_source or 'unknown'}",
    f"restored_cache_generation={result.restored_cache_generation or 'none'}",
    f"restored_cache_selection_reason={result.restored_cache_selection_reason or 'none'}",
    f"restored_cache_database_digest={result.restored_cache_database_digest or 'none'}",
    f"restored_cache_age_seconds={result.restored_cache_age_seconds}",
    f"restored_cache_maximum_age_seconds={result.restored_cache_maximum_age_seconds}",
    "restored_cache_expires_at_utc="
    + (
      result.restored_cache_expires_at_utc.isoformat()
      if result.restored_cache_expires_at_utc is not None
      else "none"
    ),
    "quality_evaluation_stage=restore_result",
    f"restored_cache_age_evidence={result.restored_cache_age_evidence or 'none'}",
    f"restored_cache_age_verified={str(result.restored_cache_age_verified).lower()}",
    f"captured_gps_ephemeris_available={result.captured_gps_ephemeris_available}",
    f"captured_glonass_ephemeris_available={result.captured_glonass_ephemeris_available}",
    f"captured_gps_startup_ready={result.captured_gps_startup_ready}",
    f"captured_gps_almanac_available={result.captured_gps_almanac_available}",
    f"captured_glonass_almanac_available={result.captured_glonass_almanac_available}",
    f"captured_satellites_used={result.captured_satellites_used}",
    f"captured_gps_almanac_satellite_ids={result.captured_gps_almanac_satellite_ids}",
    f"restored_gps_ephemeris_fresh={result.restored_gps_ephemeris_fresh}",
    f"restored_glonass_ephemeris_fresh={result.restored_glonass_ephemeris_fresh}",
    f"restored_quality_expiration_reasons={list(result.restored_quality_expiration_reasons)}",
    f"restored_gps_almanac_available={result.restored_gps_almanac_available}",
    f"restored_glonass_almanac_available={result.restored_glonass_almanac_available}",
    f"restored_gps_ephemeris_available={result.restored_gps_ephemeris_available}",
    f"restored_glonass_ephemeris_available={result.restored_glonass_ephemeris_available}",
    f"effective_gps_ephemeris_available={result.restored_gps_ephemeris_available}",
    f"effective_glonass_ephemeris_available={result.restored_glonass_ephemeris_available}",
    f"restored_satellites_used={result.restored_satellites_used}",
    f"restored_gps_startup_ready={result.restored_gps_startup_ready}",
    f"effective_gps_startup_ready={result.restored_gps_startup_ready}",
    f"restored_gps_almanac_satellite_ids={result.restored_gps_almanac_satellite_ids}",
    f"initially_rejected_indexes={list(result.initially_rejected_indexes)}",
    f"initially_timed_out_indexes={list(result.initially_timed_out_indexes)}",
    f"retry_accepted_indexes={list(result.retry_accepted_indexes)}",
    f"permanently_rejected_indexes={list(result.permanently_rejected_indexes)}",
    f"permanently_timed_out_indexes={list(result.permanently_timed_out_indexes)}",
    )
  message = ", ".join(fields)
  if diagnostic_context is not None:
    message += f", {diagnostic_context}"
  return message


def log_navigation_assistance_restore_result(
  result: NavigationAssistanceRestoreResult,
  diagnostic_context: str | None,
  time_assistance_source: str | None = None,
) -> None:
  message = format_navigation_assistance_restore_summary(
    result,
    attempted=True,
    time_assistance_source=time_assistance_source,
    diagnostic_context=diagnostic_context,
  )

  if result.status is NavigationAssistanceRestoreStatus.FAILED:
    cloudlog.error(message)
  elif result.status is NavigationAssistanceRestoreStatus.PARTIAL:
    cloudlog.warning(message)
  else:
    cloudlog.info(message)


def _position_assistance_policy_skipped(
  execution: NavigationDatabaseRestoreExecution,
) -> bool:
  return (
    execution.position_assistance_attempted
    and execution.position_assistance_failure_kind in (
      PositionAssistanceFailureKind.AGE_UNVERIFIED,
      PositionAssistanceFailureKind.UNCERTAINTY_UNREPRESENTABLE,
    )
  )


def _position_assistance_failure_phase(
  execution: NavigationDatabaseRestoreExecution,
) -> NavigationAssistanceRestoreFailurePhase:
  mapping = {
    PositionAssistanceFailureKind.BUILD: (
      NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_BUILD
    ),
    PositionAssistanceFailureKind.WRITE: (
      NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_WRITE
    ),
    PositionAssistanceFailureKind.ACK_REJECTED: (
      NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_REJECTED
    ),
    PositionAssistanceFailureKind.ACK_TIMEOUT: (
      NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_TIMEOUT
    ),
    PositionAssistanceFailureKind.ACK_OBSERVATION_FAILED: (
      NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_OBSERVATION_FAILED
    ),
    PositionAssistanceFailureKind.AGE_UNVERIFIED: (
      NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_BUILD
    ),
    PositionAssistanceFailureKind.UNCERTAINTY_UNREPRESENTABLE: (
      NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_BUILD
    ),
  }
  return mapping.get(
    execution.position_assistance_failure_kind,
    NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_WRITE,
  )


def navigation_assistance_result_from_database_execution(
  execution: NavigationDatabaseRestoreExecution,
) -> NavigationAssistanceRestoreResult:
  disposition = execution.disposition
  position_policy_skipped = _position_assistance_policy_skipped(execution)
  position_satisfied = (
    execution.position_assistance_succeeded or position_policy_skipped
  )
  if disposition.database_available:
    status = (
      NavigationAssistanceRestoreStatus.COMPLETE
      if position_satisfied
      else NavigationAssistanceRestoreStatus.PARTIAL
    )
    failure_phase = (
      None
      if position_satisfied
      else _position_assistance_failure_phase(execution)
    )
  elif execution.position_assistance_succeeded:
    status = NavigationAssistanceRestoreStatus.PARTIAL
    failure_phase = (
      NavigationAssistanceRestoreFailurePhase.DATABASE_FRAME_RESTORE
      if disposition.write_failed
      else None
    )
  else:
    status = NavigationAssistanceRestoreStatus.FAILED
    failure_phase = (
      _position_assistance_failure_phase(execution)
      if execution.position_assistance_attempted and not position_policy_skipped
      else NavigationAssistanceRestoreFailurePhase.CACHE_LOAD
    )

  evaluated_quality = execution.effective_quality
  restored_quality = (
    evaluated_quality if disposition.database_available else None
  )
  captured_quality = execution.captured_quality
  first_failure = execution.first_failure
  return NavigationAssistanceRestoreResult(
    status=status,
    total_frame_count=execution.total_frame_count,
    accepted_frame_count=execution.accepted_frame_count,
    initially_rejected_indexes=execution.initial_indexes(
      NavigationDatabaseRestoreFrameFailureKind.REJECTED,
      NavigationDatabaseRestoreFrameFailureKind.VALIDATION_ERROR,
    ),
    initially_timed_out_indexes=execution.initial_indexes(
      NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT,
    ),
    retry_accepted_indexes=execution.retry_accepted_indexes,
    permanently_rejected_indexes=execution.permanent_indexes(
      NavigationDatabaseRestoreFrameFailureKind.REJECTED,
      NavigationDatabaseRestoreFrameFailureKind.VALIDATION_ERROR,
    ),
    permanently_timed_out_indexes=execution.permanent_indexes(
      NavigationDatabaseRestoreFrameFailureKind.TIMED_OUT,
    ),
    failure_phase=failure_phase,
    position_assistance_attempted=(
      execution.position_assistance_attempted
    ),
    position_assistance_succeeded=(
      execution.position_assistance_succeeded
    ),
    position_assistance_message_id=(
      execution.position_assistance_message_id
    ),
    position_assistance_message_type=(
      execution.position_assistance_message_type
    ),
    position_assistance_write_status=(
      execution.position_assistance_write_status
    ),
    position_assistance_ack_status=(
      execution.position_assistance_ack_status
    ),
    position_assistance_ack_info_code=(
      execution.position_assistance_ack_info_code
    ),
    position_assistance_error_type=(
      execution.position_assistance_error_type
    ),
    position_assistance_error=execution.position_assistance_error,
    cache_saved_at_utc=execution.cache_saved_at_utc,
    restored_cache_generation=execution.cache_generation,
    restored_cache_selection_reason=execution.cache_selection_reason,
    restored_cache_database_digest=execution.cache_database_digest,
    restored_cache_age_seconds=execution.cache_age_seconds,
    restored_cache_maximum_age_seconds=(
      execution.cache_maximum_age_seconds
    ),
    restored_cache_expires_at_utc=execution.cache_expires_at_utc,
    restored_cache_age_evidence=(
      evaluated_quality.age_evidence.value
      if evaluated_quality is not None
      else None
    ),
    restored_cache_age_verified=(
      evaluated_quality.age_verified
      if evaluated_quality is not None
      else False
    ),
    captured_gps_ephemeris_available=(
      evaluated_quality.captured_gps_ephemeris_available
      if evaluated_quality is not None
      else None
    ),
    captured_glonass_ephemeris_available=(
      evaluated_quality.captured_glonass_ephemeris_available
      if evaluated_quality is not None
      else None
    ),
    captured_gps_startup_ready=(
      evaluated_quality.captured_gps_startup_ready
      if evaluated_quality is not None
      else None
    ),
    captured_gps_almanac_available=(
      getattr(captured_quality, "gps_almanac_available", None)
      if captured_quality is not None
      else None
    ),
    captured_glonass_almanac_available=(
      getattr(captured_quality, "glonass_almanac_available", None)
      if captured_quality is not None
      else None
    ),
    captured_satellites_used=(
      getattr(captured_quality, "satellites_used", None)
      if captured_quality is not None
      else None
    ),
    captured_gps_almanac_satellite_ids=(
      getattr(captured_quality, "gps_almanac_satellite_ids", None)
      if captured_quality is not None
      else None
    ),
    restored_gps_ephemeris_fresh=(
      restored_quality.gps_ephemeris_fresh
      if restored_quality is not None
      else None
    ),
    restored_glonass_ephemeris_fresh=(
      restored_quality.glonass_ephemeris_fresh
      if restored_quality is not None
      else None
    ),
    restored_quality_expiration_reasons=(
      evaluated_quality.expiration_reasons
      if evaluated_quality is not None
      else ()
    ),
    restored_navigation_quality=restored_quality,
    restored_gps_almanac_available=(
      getattr(captured_quality, "gps_almanac_available", None)
      if restored_quality is not None and captured_quality is not None
      else None
    ),
    restored_glonass_almanac_available=(
      getattr(captured_quality, "glonass_almanac_available", None)
      if restored_quality is not None and captured_quality is not None
      else None
    ),
    restored_gps_ephemeris_available=(
      restored_quality.effective_gps_ephemeris_available
      if restored_quality is not None
      else None
    ),
    restored_glonass_ephemeris_available=(
      restored_quality.effective_glonass_ephemeris_available
      if restored_quality is not None
      else None
    ),
    restored_satellites_used=(
      getattr(captured_quality, "satellites_used", None)
      if restored_quality is not None and captured_quality is not None
      else None
    ),
    restored_gps_startup_ready=(
      restored_quality.effective_gps_startup_ready
      if restored_quality is not None
      else None
    ),
    restored_gps_almanac_satellite_ids=(
      getattr(captured_quality, "gps_almanac_satellite_ids", None)
      if restored_quality is not None and captured_quality is not None
      else None
    ),
    database_restore_disposition=disposition,
    database_frames_attempted_count=(
      execution.database_write_attempt_count
    ),
    database_restore_boot_id=execution.boot_id,
    database_restore_state_error=execution.state_persistence_error,
    database_restore_recovered_interrupted_attempt=(
      execution.recovered_interrupted_attempt
    ),
    database_restore_candidate_identities=tuple(
      ":".join((
        identity.generation,
        identity.saved_at_utc.isoformat(),
        identity.database_digest,
      ))
      for identity in execution.candidate_identities
    ),
    database_restore_initial_failure_kinds=tuple(
      f"{failure.frame_index}:{failure.kind.value}"
      for failure in execution.initial_failures
    ),
    database_restore_permanent_failure_kinds=tuple(
      f"{failure.frame_index}:{failure.kind.value}"
      for failure in execution.permanent_failures
    ),
    database_restore_execution_error=execution.execution_error,
    database_restore_runtime_phase=execution.failure_phase,
    database_restore_transfer_budget_seconds=(
      execution.transfer_budget_seconds
    ),
    database_restore_transfer_started_at=execution.transfer_started_at,
    database_restore_transfer_completed_at=(
      execution.transfer_completed_at
    ),
    database_restore_transfer_deadline=execution.transfer_deadline,
    database_restore_transfer_elapsed_seconds=(
      execution.transfer_elapsed_seconds
    ),
    database_restore_first_failed_frame_index=(
      first_failure.frame_index if first_failure is not None else None
    ),
    database_restore_first_failed_attempt=(
      first_failure.attempt if first_failure is not None else None
    ),
    database_restore_first_failure_kind=(
      first_failure.kind.value if first_failure is not None else None
    ),
    database_restore_first_failure_error=(
      first_failure.error if first_failure is not None else None
    ),
  )


def restore_navigation_assistance(
  pigeon: TTYPigeon,
  receiver_fingerprint: str,
  diagnostic_context: str | None = None,
  time_assistance_source: str | None = None,
  trusted_now: datetime | None = None,
  *,
  navigation_database_runtime: NavigationDatabaseRestoreRuntime | None = None,
  authorized_time: AuthorizedTime | None = None,
  database_trusted_time_wait_allowed: bool = False,
  database_network_available: bool = False,
  database_trusted_time_wait_started_at: float | None = None,
  database_trusted_time_wait_completed_at: float | None = None,
  database_trusted_time_wait_deadline: float | None = None,
  database_trusted_time_wait_error_type: str | None = None,
  database_trusted_time_wait_error: str | None = None,
  pre_start_deadline: float | None = None,
  allow_legacy_direct_restore: bool = False,
) -> NavigationAssistanceRestoreResult:
  if navigation_database_runtime is not None:
    def send_database_frame(
      message: bytes,
      frame_index: int,
      mark_write_attempt: Callable[[], None],
    ) -> None:
      def before_send() -> None:
        navigation_database_runtime.validate_database_write_boundary(
          frame_index
        )
        mark_write_attempt()

      send_mga_with_strict_ack(
        pigeon,
        message,
        timeout=min(
          GPS_ASSISTANCE_ACK_TIMEOUT,
          navigation_database_runtime.remaining_transfer_seconds(
            frame_index
          ),
        ),
        database_frame_index=frame_index,
        before_send=before_send,
      )

    navigation_database_runtime.prepare()
    execution = navigation_database_runtime.evaluate(
      authorized_time=authorized_time,
      reliable_fix_available=False,
      yuma_already_sent=False,
      send_database_message=send_database_frame,
      pre_start_deadline=pre_start_deadline,
    )
    # Position assistance eligibility is independent of DBD disposition and of
    # whether GNSS acquisition has already started. send_position_once() enforces
    # snapshot presence, validation, and one-shot claim semantics.
    position_timeout = (
      min(
        GPS_ASSISTANCE_ACK_TIMEOUT,
        pre_start_remaining_seconds(pre_start_deadline),
      )
      if pre_start_deadline is not None
      else GPS_ASSISTANCE_ACK_TIMEOUT
    )
    if position_timeout > 0.0:
      navigation_database_runtime.send_position_once(
        lambda message: send_mga_with_strict_ack(
          pigeon,
          message,
          timeout=position_timeout,
        )
      )
      execution = navigation_database_runtime.execution
    result = navigation_assistance_result_from_database_execution(execution)
    wait_elapsed_seconds = (
      max(
        0.0,
        database_trusted_time_wait_completed_at
        - database_trusted_time_wait_started_at,
      )
      if (
        database_trusted_time_wait_started_at is not None
        and database_trusted_time_wait_completed_at is not None
      )
      else None
    )
    result = replace(
      result,
      database_trusted_time_wait_allowed=(
        database_trusted_time_wait_allowed
      ),
      database_network_available=database_network_available,
      database_trusted_time_wait_started_at=(
        database_trusted_time_wait_started_at
      ),
      database_trusted_time_wait_completed_at=(
        database_trusted_time_wait_completed_at
      ),
      database_trusted_time_wait_deadline=(
        database_trusted_time_wait_deadline
      ),
      database_trusted_time_wait_elapsed_seconds=wait_elapsed_seconds,
      database_trusted_time_wait_error_type=(
        database_trusted_time_wait_error_type
      ),
      database_trusted_time_wait_error=(
        database_trusted_time_wait_error
      ),
    )
    log_navigation_assistance_restore_result(
      result,
      diagnostic_context,
      time_assistance_source,
    )
    return result

  if not allow_legacy_direct_restore:
    raise RuntimeError(
      "live navigation assistance restore requires a boot-scoped runtime"
    )

  if trusted_now is None:
    host_time = read_host_time_observation()
    if host_time is not None and host_time.independent:
      trusted_now = host_time.utc
      if time_assistance_source is None:
        time_assistance_source = host_time.source.value

  store = NavigationCacheStore(GPS_ASSISTANCE_CACHE_PATH, loader=load_cache)
  cleanup_failure = store.remove_stale_candidate()
  if cleanup_failure is not None:
    cloudlog.warning(f"GPS stale cache candidate cleanup failed: reason={cleanup_failure}")
  normalized_time_source = (
    time_assistance_source or ""
  ).casefold().replace("-", "_")

  if trusted_now is None:
    cache_age_evidence = CacheAgeEvidence.UNVERIFIED
  elif normalized_time_source == "rtc_estimate":
    cache_age_evidence = CacheAgeEvidence.RTC_ESTIMATE
  else:
    cache_age_evidence = CacheAgeEvidence.TRUSTED_UTC

  selection, inventory = store.select_best(
    receiver_fingerprint,
    trusted_now,
    age_evidence=cache_age_evidence,
  )
  cloudlog.info(", ".join((
    "GPS navigation cache startup generation selection",
    f"primary_status={inventory.primary.state.name.lower()}",
    f"previous_status={inventory.previous.state.name.lower()}",
    f"selected_generation={selection.generation if selection is not None else 'none'}",
    f"selection_reason={selection.reason if selection is not None else 'no_eligible_cache'}",
    f"age_evidence={cache_age_evidence.value}",
    f"age_verified={str(cache_age_evidence.verified).lower()}",
    f"trusted_time_source={time_assistance_source or 'unavailable'}",
    f"primary_quality={getattr(inventory.primary.cache, 'quality', None)}",
    f"previous_quality={getattr(inventory.previous.cache, 'quality', None)}",
  )))
  if selection is None:
    cloudlog.info(
      "GPS assistance cache load rejected: no eligible primary or previous cache"
    )
    result = NavigationAssistanceRestoreResult(
      status=NavigationAssistanceRestoreStatus.FAILED,
      total_frame_count=0,
      accepted_frame_count=0,
      failure_phase=NavigationAssistanceRestoreFailurePhase.CACHE_LOAD,
    )
    log_navigation_assistance_restore_result(
      result,
      diagnostic_context,
      time_assistance_source,
    )
    return result
  cache = selection.cache
  restored_quality = getattr(cache, "quality", None)
  effective_restored_quality = effective_restored_navigation_quality(
    restored_quality,
    cache.saved_at_utc,
    trusted_now,
    cache_age_evidence,
  )

  cloudlog.info(
    ", ".join((
      f"GPS assistance cache loaded: saved_at_utc={cache.saved_at_utc.isoformat()}",
      f"generation={selection.generation}",
      f"rtc_anchor_present={cache.rtc_counter_seconds is not None}",
      f"database_messages={len(cache.database_frames)}",
    ))
  )

  total_frame_count = len(cache.database_frames)
  accepted_indexes: set[int] = set()
  initially_rejected_indexes: list[int] = []
  initially_timed_out_indexes: list[int] = []
  retry_accepted_indexes: list[int] = []
  permanently_rejected_indexes: list[int] = []
  permanently_timed_out_indexes: list[int] = []
  failed_frames: list[tuple[int, bytes]] = []
  active_phase = NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_BUILD
  failure_phase = None

  try:
    # Position age is independent of DBD age evidence. Only TRUSTED_UTC may
    # authorize a verified age; RTC estimates and unverified clocks skip.
    age_seconds: float | None = None
    age_verified = False
    if (
      trusted_now is not None
      and cache_age_evidence is CacheAgeEvidence.TRUSTED_UTC
    ):
      age_seconds = (trusted_now - cache.saved_at_utc).total_seconds()
      age_verified = True
    accuracy_cm, accuracy_reason = age_safe_restore_position_accuracy_cm(
      cache.position_accuracy_cm,
      age_seconds=age_seconds,
      age_verified=age_verified,
    )
    if accuracy_cm is None:
      try:
        cloudlog.info(
          ", ".join((
            "GPS legacy position assistance skipped",
            f"reason={accuracy_reason}",
          ))
        )
      except Exception:
        # Observability must never block DBD restore after a policy skip.
        pass
    else:
      position_message = build_position_assistance_message(
        latitude_e7=cache.latitude_e7,
        longitude_e7=cache.longitude_e7,
        altitude_cm=cache.altitude_cm,
        position_accuracy_cm=accuracy_cm,
      )

      active_phase = NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_WRITE
      send_mga_with_strict_ack(
        pigeon,
        position_message,
      )

    active_phase = NavigationAssistanceRestoreFailurePhase.DATABASE_FRAME_RESTORE
    for database_frame_index, database_message in enumerate(
      cache.database_frames
    ):
      try:
        send_mga_with_strict_ack(
          pigeon,
          database_message,
          database_frame_index=database_frame_index,
        )
        accepted_indexes.add(database_frame_index)
      except CacheValidationError as exc:
        initially_rejected_indexes.append(
          database_frame_index
        )
        failed_frames.append((
          database_frame_index,
          database_message,
        ))
        cloudlog.warning(
          f"GPS navigation database frame rejected on initial pass: {exc}"
        )
      except (TimeoutError, MgaTransactionError) as exc:
        initially_timed_out_indexes.append(
          database_frame_index
        )
        failed_frames.append((
          database_frame_index,
          database_message,
        ))
        cloudlog.warning(
          f"GPS navigation database frame timed out on initial pass: database_frame_index={database_frame_index}, {exc}"
        )

    if failed_frames:
      time.sleep(GPS_ASSISTANCE_FRAME_RETRY_DELAY)

    for database_frame_index, database_message in failed_frames:
      try:
        send_mga_with_strict_ack(
          pigeon,
          database_message,
          database_frame_index=database_frame_index,
        )
        accepted_indexes.add(database_frame_index)
        retry_accepted_indexes.append(database_frame_index)
      except CacheValidationError as exc:
        permanently_rejected_indexes.append(
          database_frame_index
        )
        cloudlog.warning(
          f"GPS navigation database frame rejected on retry: {exc}"
        )
      except (TimeoutError, MgaTransactionError) as exc:
        permanently_timed_out_indexes.append(
          database_frame_index
        )
        cloudlog.warning(
          f"GPS navigation database frame timed out on retry: database_frame_index={database_frame_index}, {exc}"
        )

  except (
    CacheValidationError,
    MgaTransactionError,
    MgaWriteError,
    OSError,
    ResponseTransactionError,
    TimeoutError,
  ) as exc:
    if active_phase is NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_WRITE:
      if isinstance(exc, TimeoutError):
        failure_phase = NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_TIMEOUT
      elif isinstance(exc, CacheValidationError):
        failure_phase = NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_ACK_REJECTED
      else:
        failure_phase = NavigationAssistanceRestoreFailurePhase.POSITION_ASSISTANCE_WRITE
    else:
      failure_phase = active_phase
    cloudlog.exception(
      f"Failed to restore GPS navigation assistance cache, failure_phase={failure_phase.value}"
    )
    status = NavigationAssistanceRestoreStatus.FAILED
  except Exception:
    failure_phase = active_phase
    cloudlog.exception(
      f"Unexpected failure while restoring GPS navigation assistance cache, failure_phase={failure_phase.value}"
    )
    status = NavigationAssistanceRestoreStatus.FAILED
  else:
    if not accepted_indexes:
      status = NavigationAssistanceRestoreStatus.FAILED
    elif len(accepted_indexes) == total_frame_count:
      status = NavigationAssistanceRestoreStatus.COMPLETE
    else:
      status = NavigationAssistanceRestoreStatus.PARTIAL
    if status is not NavigationAssistanceRestoreStatus.COMPLETE:
      failure_phase = NavigationAssistanceRestoreFailurePhase.DATABASE_FRAME_RESTORE

  result = NavigationAssistanceRestoreResult(
    status=status,
    total_frame_count=total_frame_count,
    accepted_frame_count=len(accepted_indexes),
    initially_rejected_indexes=tuple(
      initially_rejected_indexes
    ),
    initially_timed_out_indexes=tuple(
      initially_timed_out_indexes
    ),
    retry_accepted_indexes=tuple(retry_accepted_indexes),
    permanently_rejected_indexes=tuple(
      permanently_rejected_indexes
    ),
    permanently_timed_out_indexes=tuple(
      permanently_timed_out_indexes
    ),
    failure_phase=failure_phase,
    cache_saved_at_utc=cache.saved_at_utc,
    restored_cache_generation=selection.generation,
    restored_cache_selection_reason=selection.reason,
    restored_cache_age_seconds=(
      effective_restored_quality.cache_age_seconds
    ),
    restored_cache_age_evidence=(
      effective_restored_quality.age_evidence.value
    ),
    restored_cache_age_verified=(
      effective_restored_quality.age_verified
    ),
    captured_gps_ephemeris_available=(
      effective_restored_quality.captured_gps_ephemeris_available
    ),
    captured_glonass_ephemeris_available=(
      effective_restored_quality.captured_glonass_ephemeris_available
    ),
    captured_gps_startup_ready=(
      effective_restored_quality.captured_gps_startup_ready
    ),
    restored_gps_ephemeris_fresh=(
      effective_restored_quality.gps_ephemeris_fresh
    ),
    restored_glonass_ephemeris_fresh=(
      effective_restored_quality.glonass_ephemeris_fresh
    ),
    restored_quality_expiration_reasons=(
      effective_restored_quality.expiration_reasons
    ),
    restored_navigation_quality=effective_restored_quality,
    restored_gps_almanac_available=getattr(
      restored_quality,
      "gps_almanac_available",
      None,
    ),
    restored_glonass_almanac_available=getattr(
      restored_quality,
      "glonass_almanac_available",
      None,
    ),
    restored_gps_ephemeris_available=(
      effective_restored_quality.effective_gps_ephemeris_available
    ),
    restored_glonass_ephemeris_available=(
      effective_restored_quality.effective_glonass_ephemeris_available
    ),
    restored_satellites_used=getattr(
      restored_quality,
      "satellites_used",
      None,
    ),
    restored_gps_startup_ready=(
      effective_restored_quality.effective_gps_startup_ready
    ),
    restored_gps_almanac_satellite_ids=getattr(
      restored_quality,
      "gps_almanac_satellite_ids",
      None,
    ),
  )
  log_navigation_assistance_restore_result(
    result,
    diagnostic_context,
    time_assistance_source,
  )
  return result


def cache_promotion_trusted_now(
  receiver_utc: datetime | None,
  capture_receiver_cycle: int | None,
  active_receiver_cycle: int | None,
  *,
  receiver_utc_fresh: bool,
  synchronized_utc: datetime | None = None,
  receiver_utc_independent: bool = False,
  authorized_utc: datetime | None = None,
) -> datetime | None:
  if authorized_utc is not None:
    try:
      if (
        authorized_utc.tzinfo is None
        or authorized_utc.utcoffset() is None
      ):
        return None
      return authorized_utc.astimezone(UTC)
    except Exception:
      return None

  host_time = read_host_time_observation()
  if host_time is not None and host_time.independent:
    if synchronized_utc is None:
      return host_time.utc
    try:
      if (
        synchronized_utc.tzinfo is None
        or synchronized_utc.utcoffset() is None
      ):
        return None
      synchronized_now = synchronized_utc.astimezone(UTC)
    except Exception:
      return None
    if abs(
      (host_time.utc - synchronized_now).total_seconds()
    ) > 1.0:
      return None
    return synchronized_now

  if (
    not receiver_utc_fresh
    or not receiver_utc_independent
    or receiver_utc is None
    or receiver_utc.tzinfo is None
    or capture_receiver_cycle is None
    or active_receiver_cycle is None
    or capture_receiver_cycle != active_receiver_cycle
  ):
    return None
  try:
    if receiver_utc.utcoffset() is None:
      return None
    return receiver_utc.astimezone(UTC)
  except Exception:
    return None


def write_navigation_assistance_cache(
  receiver_fingerprint: str,
  fix: NavPvtFix,
  database_frames: tuple[bytes, ...],
  quality: NavigationQuality,
  source: str = "unknown",
  receiver_cycle: int | None = None,
  receiver_utc_now: datetime | None = None,
  active_receiver_cycle: int | None = None,
  receiver_utc_fresh: bool | None = None,
  trusted_promotion_utc: datetime | None = None,
  receiver_utc_independent: bool = False,
) -> NavigationAssistanceCacheResult:
  quality_tier = navigation_quality_tier(quality)
  if quality_tier not in (CacheQualityTier.USABLE, CacheQualityTier.QUALIFIED):
    cloudlog.warning("GPS navigation assistance candidate rejected: usable capture policy failed")
    cloudlog.warning("GPS navigation assistance cache outcome: failed")
    return NavigationAssistanceCacheResult.FAILED

  # Compatibility defaults apply only to direct callers that predate explicit
  # receiver-cycle plumbing. Production always supplies both cycle values.
  capture_cycle = 0 if receiver_cycle is None else receiver_cycle
  active_cycle = capture_cycle if active_receiver_cycle is None else active_receiver_cycle
  receiver_time = fix.utc_time if receiver_utc_now is None else receiver_utc_now
  fresh = receiver_time is not None if receiver_utc_fresh is None else receiver_utc_fresh
  normalized_promotion_utc = None
  if trusted_promotion_utc is not None:
    try:
      if trusted_promotion_utc.tzinfo is None or trusted_promotion_utc.utcoffset() is None:
        raise ValueError("Trusted promotion UTC has no UTC offset")
      normalized_promotion_utc = trusted_promotion_utc.astimezone(UTC)
    except Exception:
      cloudlog.warning("GPS assistance cache not saved because trusted promotion UTC is invalid")
      cloudlog.warning("GPS navigation assistance cache outcome: failed")
      return NavigationAssistanceCacheResult.FAILED
  trusted_now = cache_promotion_trusted_now(
    receiver_time,
    capture_cycle,
    active_cycle,
    receiver_utc_fresh=fresh,
    receiver_utc_independent=receiver_utc_independent,
    authorized_utc=normalized_promotion_utc,
  )
  if (
    trusted_now is None
    or (
      normalized_promotion_utc is not None
      and trusted_now != normalized_promotion_utc
    )
  ):
    cloudlog.warning("GPS assistance cache not saved because no trusted UTC time is available")
    cloudlog.warning("GPS navigation assistance cache outcome: failed")
    return NavigationAssistanceCacheResult.FAILED

  try:
    cache = create_cache(
      receiver_fingerprint=receiver_fingerprint,
      fix=fix,
      database_frames=database_frames,
      saved_at_utc=trusted_now,
      rtc_counter_seconds=read_rtc_counter_seconds(),
      quality=quality,
      receiver_cycle=capture_cycle,
    )
    promotion = NavigationCacheStore(
      GPS_ASSISTANCE_CACHE_PATH, loader=load_cache,
    ).promote(
      cache, receiver_fingerprint, trusted_now, active_cycle,
    )
  except Exception:
    cloudlog.exception(
      "Failed to save GPS navigation assistance cache"
    )
    cloudlog.warning("GPS navigation assistance cache outcome: failed")
    return NavigationAssistanceCacheResult.FAILED

  primary_quality = promotion.inventory.primary.cache.quality if promotion.inventory.primary.cache else None
  previous_quality = promotion.inventory.previous.cache.quality if promotion.inventory.previous.cache else None
  cloudlog.info(", ".join((
    "GPS navigation cache promotion result",
    f"source={source}",
    f"quality_tier={quality_tier.value}",
    f"receiver_cycle={capture_cycle}",
    f"generation={promotion.selected.generation if promotion.selected is not None else 'none'}",
    f"promotion_stage={promotion.stage.value}",
    f"fallback_generation={promotion.fallback_generation or 'none'}",
    f"selection_reason={promotion.selection_reason or 'none'}",
    f"terminal_result={promotion.status.name.lower()}",
    f"reason={promotion.reason}",
    f"candidate_quality={quality}",
    f"primary_quality={primary_quality}",
    f"previous_quality={previous_quality}",
  )))
  if promotion.cleanup_failure is not None:
    cloudlog.warning(", ".join((
      "GPS navigation cache candidate cleanup failed",
      f"source={source}",
      f"receiver_cycle={capture_cycle}",
      f"reason={promotion.cleanup_failure}",
    )))
  if promotion.status is CachePromotionStatus.PRESERVED_EXISTING:
    cloudlog.info("GPS navigation assistance cache outcome: existing preserved")
    return NavigationAssistanceCacheResult.PRESERVED_EXISTING
  if promotion.status is CachePromotionStatus.FAILED:
    cloudlog.warning("GPS navigation assistance cache outcome: failed")
    return NavigationAssistanceCacheResult.FAILED

  cloudlog.warning(f"Saved GPS navigation assistance cache: {len(database_frames)} database messages")
  cloudlog.info("GPS navigation assistance cache outcome: saved")
  return NavigationAssistanceCacheResult.SAVED


def is_all_zero_ublox_data(data: bytes) -> bool:
  return bool(data) and not any(data)


class ReceiverRecoveryReason(StrEnum):
  NO_DATA = "no_data_watchdog"
  ALL_ZERO_DATA = "all_zero_data"
  STALLED_ACQUISITION = "stalled_acquisition"


class UbloxDataWatchdog:
  """Shared physical receiver recovery budget for transport failures."""

  def __init__(
    self,
    timeout: float = 10.0,
    max_recoveries: int = 1,
    recovery_cooldown_seconds: float = 30.0,
    healthy_rearm_seconds: float = 60.0,
    start_time: float | None = None,
  ):
    self.timeout = timeout
    self.max_recoveries = max_recoveries
    self.recovery_cooldown_seconds = recovery_cooldown_seconds
    self.healthy_rearm_seconds = healthy_rearm_seconds
    self.last_data_time = (
      time.monotonic()
      if start_time is None
      else start_time
    )
    self.recoveries = 0
    self.last_recovery_completed_time: float | None = None
    self.last_recovery_reason: ReceiverRecoveryReason | None = None
    self.healthy_data_since: float | None = None

  def note_data(
    self,
    now: float,
    *,
    healthy: bool = True,
  ) -> bool:
    self.last_data_time = now
    if not healthy:
      self.healthy_data_since = None
      return False
    if self.recoveries == 0:
      return False
    if self.healthy_data_since is None:
      self.healthy_data_since = now
      return False
    if (
      now - self.healthy_data_since
      < self.healthy_rearm_seconds
    ):
      return False

    self.recoveries = 0
    self.last_recovery_completed_time = None
    self.last_recovery_reason = None
    self.healthy_data_since = None
    return True

  def request_recovery(
    self,
    reason: ReceiverRecoveryReason,
    now: float,
  ) -> bool:
    if not isinstance(reason, ReceiverRecoveryReason):
      raise ValueError("reason must be a ReceiverRecoveryReason")
    if (
      self.last_recovery_completed_time is not None
      and (
        now - self.last_recovery_completed_time
        < self.recovery_cooldown_seconds
      )
    ):
      return False
    if self.recoveries >= self.max_recoveries:
      raise RuntimeError(", ".join((
        "GPS receiver recovery budget exhausted",
        f"reason={reason.value}",
        f"attempts={self.recoveries}",
        f"max_attempts={self.max_recoveries}",
      )))

    self.recoveries += 1
    self.last_recovery_reason = reason
    self.healthy_data_since = None
    return True

  def check(self, now: float) -> bool:
    if now - self.last_data_time < self.timeout:
      return False

    self.healthy_data_since = None
    return self.request_recovery(
      ReceiverRecoveryReason.NO_DATA,
      now,
    )

  def recovery_completed(self, now: float) -> None:
    self.last_data_time = now
    self.last_recovery_completed_time = now
    self.healthy_data_since = None


class GpsStartupDiagnostics:
  def __init__(
    self,
    process_start_time: float,
    status_interval: float = GPS_ACQUISITION_STATUS_INTERVAL,
  ) -> None:
    self.process_start_time = process_start_time
    self.status_interval = status_interval
    self.cycle_number = 0
    self.cycle_reason = ""
    self.cycle_start_time = process_start_time
    self._reset_cycle_state(process_start_time)

  def _reset_cycle_state(self, now: float) -> None:
    self.first_nav_pvt_logged = False
    self.first_fix_ok_logged = False
    self.first_receiver_utc_logged = False
    self.reliable_fix_observed = False
    self.first_rawx_after_initialization_logged = False
    self.first_nonempty_rawx_logged = False
    self.first_valid_gps_week_logged = False
    self.first_valid_leap_second_logged = False
    self.first_gps_measurement_logged = False
    self.first_glonass_measurement_logged = False
    self.latest_fix: NavPvtFix | None = None
    self.latest_fix_time: float | None = None
    self.next_status_time = now + self.status_interval

  def _timing_fields(self, now: float) -> tuple[str, ...]:
    return (
      f"cycle={self.cycle_number}",
      f"reason={self.cycle_reason}",
      f"process_elapsed_seconds={now - self.process_start_time:.1f}",
      f"cycle_elapsed_seconds={now - self.cycle_start_time:.1f}",
    )

  def _fix_fields(self, fix: NavPvtFix) -> tuple[str, ...]:
    return (
      f"fix_ok={fix.fix_ok}",
      f"satellites={fix.satellites}",
      f"horizontal_accuracy_cm={fix.horizontal_accuracy_cm}",
      f"receiver_utc_valid={fix.utc_time is not None}",
    )

  def start_cycle(self, reason: str, now: float) -> None:
    self.cycle_number += 1
    self.cycle_reason = reason
    self.cycle_start_time = now
    self._reset_cycle_state(now)
    cloudlog.info(", ".join((
      "GPS receiver cycle started",
      *self._timing_fields(now)[:3],
    )))

  def initialization_complete(self, now: float) -> None:
    cloudlog.info(", ".join((
      "GPS receiver cycle initialization complete",
      *self._timing_fields(now)[:3],
      f"cycle_initialization_elapsed_seconds={now - self.cycle_start_time:.1f}",
    )))

  def time_assistance_context(self, now: float) -> str:
    return ", ".join(self._timing_fields(now))

  def _log_milestone(
    self,
    milestone: str,
    fix: NavPvtFix,
    now: float,
  ) -> None:
    cloudlog.info(", ".join((
      f"GPS acquisition milestone={milestone}",
      *self._timing_fields(now),
      *self._fix_fields(fix),
    )))

  def note_nav_pvt(self, fix: NavPvtFix, now: float) -> None:
    self.latest_fix = fix
    self.latest_fix_time = now

    if not self.first_nav_pvt_logged:
      self._log_milestone("first_nav_pvt", fix, now)
      self.first_nav_pvt_logged = True

    if fix.fix_ok and not self.first_fix_ok_logged:
      self._log_milestone("first_fix_ok", fix, now)
      self.first_fix_ok_logged = True

    if fix.utc_time is not None and not self.first_receiver_utc_logged:
      self._log_milestone("first_receiver_utc", fix, now)
      self.first_receiver_utc_logged = True

    if fix.reliable and not self.reliable_fix_observed:
      self._log_milestone("first_reliable_fix", fix, now)
      self.reliable_fix_observed = True

  def _log_rawx_milestone(
    self,
    milestone: str,
    rawx: Ubx.RxmRawx,
    now: float,
  ) -> None:
    measurement_counts: dict[int, int] = {}
    maximum_cno: dict[int, int] = {}
    for measurement in rawx.meas:
      gnss_id = int(measurement.gnss_id)
      measurement_counts[gnss_id] = (
        measurement_counts.get(gnss_id, 0) + 1
      )
      maximum_cno[gnss_id] = max(
        maximum_cno.get(gnss_id, 0),
        measurement.cno,
      )

    cloudlog.info(", ".join((
      f"GPS acquisition milestone={milestone}",
      *self._timing_fields(now),
      f"gps_week_valid={rawx.week != 0}",
      f"leap_second_valid={bool(rawx.rec_stat & 0x01)}",
      f"measurement_count={rawx.num_meas}",
      f"measurement_counts_by_gnss={measurement_counts}",
      f"maximum_cno_by_gnss={maximum_cno}",
    )))

  def note_rawx(self, frame: bytes, now: float) -> None:
    if frame[2:4] != b"\x02\x15":
      return

    try:
      rawx = Ubx.RxmRawx.from_bytes(frame[6:-2])
    except Exception:
      return

    if not self.first_rawx_after_initialization_logged:
      self._log_rawx_milestone(
        "first_rawx_after_initialization",
        rawx,
        now,
      )
      self.first_rawx_after_initialization_logged = True

    if rawx.num_meas > 0 and not self.first_nonempty_rawx_logged:
      self._log_rawx_milestone("first_nonempty_rawx", rawx, now)
      self.first_nonempty_rawx_logged = True

    if rawx.week != 0 and not self.first_valid_gps_week_logged:
      self._log_rawx_milestone("first_valid_gps_week", rawx, now)
      self.first_valid_gps_week_logged = True

    if (
      rawx.rec_stat & 0x01
      and not self.first_valid_leap_second_logged
    ):
      self._log_rawx_milestone(
        "first_valid_leap_second",
        rawx,
        now,
      )
      self.first_valid_leap_second_logged = True

    measurement_gnss_ids = {
      int(measurement.gnss_id) for measurement in rawx.meas
    }
    if (
      int(Ubx.GnssType.gps) in measurement_gnss_ids
      and not self.first_gps_measurement_logged
    ):
      self._log_rawx_milestone("first_gps_measurement", rawx, now)
      self.first_gps_measurement_logged = True

    if (
      int(Ubx.GnssType.glonass) in measurement_gnss_ids
      and not self.first_glonass_measurement_logged
    ):
      self._log_rawx_milestone(
        "first_glonass_measurement",
        rawx,
        now,
      )
      self.first_glonass_measurement_logged = True

  def log_acquisition_status(self, now: float) -> None:
    if (
      self.reliable_fix_observed
      or now < self.next_status_time
    ):
      return

    fields = [
      "GPS acquisition status",
      *self._timing_fields(now),
      f"nav_pvt_seen={self.latest_fix is not None}",
    ]

    if self.latest_fix is not None:
      fields.extend(self._fix_fields(self.latest_fix))

    cloudlog.info(", ".join(fields))
    self.next_status_time = now + self.status_interval


def format_position_assistance_retry_state(
  state: PositionAssistanceRetryState,
  *,
  trigger: str,
  receiver_cycle: int | None,
  gnss_start_sent_at: float | None,
  nav_pvt_observed_at: float | None,
  persistence_error: str | None,
) -> str:
  def optional(value: object | None) -> str:
    return "none" if value is None else str(value)

  return ", ".join((
    "GPS position assistance post-start retry",
    f"position_assistance_retry_trigger={trigger}",
    "position_assistance_initial_attempted="
    + str(state.initial_attempted).lower(),
    "position_assistance_initial_write_status="
    + state.initial_write_status.value,
    "position_assistance_initial_ack_status="
    + state.initial_ack_status.value,
    "position_assistance_initial_ack_info_code="
    + optional(state.initial_ack_info_code),
    "position_assistance_retry_armed="
    + str(state.retry_armed).lower(),
    "position_assistance_retry_claimed="
    + str(state.retry_claimed).lower(),
    "position_assistance_retry_completed="
    + str(state.retry_completed).lower(),
    "position_assistance_retry_result="
    + state.retry_result.value,
    "position_assistance_retry_write_status="
    + state.retry_write_status.value,
    "position_assistance_retry_ack_status="
    + state.retry_ack_status.value,
    "position_assistance_retry_ack_info_code="
    + optional(state.retry_ack_info_code),
    "position_assistance_retry_error_type="
    + optional(state.retry_error_type),
    "position_assistance_retry_error="
    + optional(state.retry_error),
    "position_assistance_retry_receiver_cycle="
    + optional(receiver_cycle),
    "position_assistance_retry_triggered_at="
    + optional(state.retry_triggered_at),
    "gnss_start_sent_at_monotonic="
    + optional(gnss_start_sent_at),
    "nav_pvt_observed_at_monotonic="
    + optional(nav_pvt_observed_at),
    "position_assistance_retry_state_error="
    + optional(persistence_error),
  ))


def log_position_assistance_retry_state(
  state: PositionAssistanceRetryState,
  *,
  trigger: str,
  receiver_cycle: int | None,
  gnss_start_sent_at: float | None,
  nav_pvt_observed_at: float | None,
  persistence_error: str | None,
) -> None:
  message = format_position_assistance_retry_state(
    state,
    trigger=trigger,
    receiver_cycle=receiver_cycle,
    gnss_start_sent_at=gnss_start_sent_at,
    nav_pvt_observed_at=nav_pvt_observed_at,
    persistence_error=persistence_error,
  )
  if state.retry_result in (
    PositionAssistanceRetryResult.REJECTED,
    PositionAssistanceRetryResult.TIMED_OUT,
    PositionAssistanceRetryResult.ACK_OBSERVATION_FAILED,
    PositionAssistanceRetryResult.WRITE_FAILED,
    PositionAssistanceRetryResult.CLAIM_PERSIST_FAILED,
  ):
    cloudlog.warning(message)
  else:
    cloudlog.info(message)


@dataclass
class PositionAssistancePostStartRetryController:
  runtime: PositionAssistanceRetryRuntime | None
  receiver_cycle: int | None = None
  gnss_start_sent_at: float | None = None
  first_post_start_nav_pvt_consumed: bool = False
  retry_ready: bool = False
  nav_pvt_observed_at: float | None = None

  def begin_receiver_cycle(
    self,
    receiver_cycle: int,
    gnss_start_sent_at: float | None,
  ) -> None:
    self.receiver_cycle = receiver_cycle
    self.gnss_start_sent_at = gnss_start_sent_at
    self.first_post_start_nav_pvt_consumed = False
    self.retry_ready = False
    self.nav_pvt_observed_at = None

  def cancel_receiver_cycle(self, now: float) -> None:
    if self.runtime is not None and self.runtime.state.pending:
      state = self.runtime.cancel(
        PositionAssistanceRetryResult.CANCELLED_RECEIVER_CYCLE_CHANGED,
        now,
      )
      log_position_assistance_retry_state(
        state,
        trigger="receiver_cycle_changed",
        receiver_cycle=self.receiver_cycle,
        gnss_start_sent_at=self.gnss_start_sent_at,
        nav_pvt_observed_at=self.nav_pvt_observed_at,
        persistence_error=self.runtime.persistence_error,
      )
    self.receiver_cycle = None
    self.gnss_start_sent_at = None
    self.first_post_start_nav_pvt_consumed = True
    self.retry_ready = False
    self.nav_pvt_observed_at = None

  def observe_frames(
    self,
    frames: list[bytes],
    observed_at: float,
    receiver_cycle: int,
  ) -> None:
    if (
      self.runtime is None
      or self.first_post_start_nav_pvt_consumed
      or not self.runtime.state.pending
      or self.receiver_cycle != receiver_cycle
      or self.gnss_start_sent_at is None
      or observed_at <= self.gnss_start_sent_at
    ):
      return

    for frame in frames:
      fix = parse_nav_pvt(frame)
      if fix is None:
        continue
      self.first_post_start_nav_pvt_consumed = True
      self.nav_pvt_observed_at = observed_at
      if not fix.fix_ok:
        self.retry_ready = True
        return
      state = self.runtime.cancel(
        PositionAssistanceRetryResult.CANCELLED_EXISTING_FIX,
        observed_at,
      )
      log_position_assistance_retry_state(
        state,
        trigger="first_post_start_nav_pvt",
        receiver_cycle=receiver_cycle,
        gnss_start_sent_at=self.gnss_start_sent_at,
        nav_pvt_observed_at=observed_at,
        persistence_error=self.runtime.persistence_error,
      )
      return

  def execute_ready(
    self,
    send_message: Callable[[bytes], object],
  ) -> None:
    if self.runtime is None or not self.retry_ready:
      return
    observed_at = self.nav_pvt_observed_at
    receiver_cycle = self.receiver_cycle
    self.retry_ready = False
    if observed_at is None or not self.runtime.state.pending:
      return
    state = self.runtime.retry_once(send_message, observed_at)
    log_position_assistance_retry_state(
      state,
      trigger="first_post_start_nav_pvt",
      receiver_cycle=receiver_cycle,
      gnss_start_sent_at=self.gnss_start_sent_at,
      nav_pvt_observed_at=observed_at,
      persistence_error=self.runtime.persistence_error,
    )

@dataclass(frozen=True)
class ReceiverCycleInitialization:
  trusted_time_assistance_sent: bool
  next_time_assistance_attempt: float
  navigation_assistance_restore_attempted: bool
  mon_ver_info: MonVerInfo | None
  ack_aiding_configuration_attempted: bool
  assistnow_autonomous_supported: bool
  assistnow_autonomous_configuration_attempted: bool
  completed_at: float
  ack_aiding_configuration_result: (
    Navx5AckAidingConfigurationResult | None
  ) = None
  navigation_assistance_restore_result: (
    NavigationAssistanceRestoreResult | None
  ) = None
  time_assistance_utc: datetime | None = None
  time_assistance_source: str | None = None
  yuma_time_anchor_utc: datetime | None = None
  yuma_time_anchor_source: str | None = None
  yuma_time_anchor_monotonic: float | None = None
  authorized_time: AuthorizedTime | None = None
  host_time_observation: HostTimeObservation | None = None
  authority_evaluation: TimeAuthorityEvaluation | None = None
  time_assistance_attempts: tuple[
    TimeAssistanceAttemptDiagnostic,
    ...,
  ] = ()
  gnss_start_sent_at: float | None = None
  poll_deferred_assistance_state: (
    Callable[[], NavigationAssistanceRestoreResult | None] | None
  ) = None


def _startup_timeline_elapsed(
  value: float | None,
  cycle_started_at: float,
) -> str:
  if value is None:
    return "none"
  return f"{value - cycle_started_at:.3f}"


def _startup_timeline_has_current_network_time(
  authorized_time: object | None,
) -> bool:
  return (
    isinstance(authorized_time, AuthorizedTime)
    and is_current_independent_network_time(authorized_time)
  )


def format_gps_startup_timeline(
  *,
  cycle: int,
  reason: str,
  cycle_started_at: float,
  trusted_time_wait_started_at: float | None,
  trusted_time_wait_completed_at: float | None,
  independent_network_time_seen_at: float | None,
  acquisition_start_claimed_at: float | None,
  gnss_start_sent_at: float | None,
  restore_result: NavigationAssistanceRestoreResult | None,
  authorized_time: AuthorizedTime | None,
  time_assistance_attempts: tuple[
    TimeAssistanceAttemptDiagnostic,
    ...,
  ] = (),
) -> str:
  wait_duration = (
    trusted_time_wait_completed_at - trusted_time_wait_started_at
    if (
      trusted_time_wait_started_at is not None
      and trusted_time_wait_completed_at is not None
    )
    else None
  )
  disposition = "not_attempted"
  cache_generation = "none"
  cache_selection_reason = "none"
  cache_age = "none"
  frames_attempted = 0
  frames_accepted = 0
  frames_rejected = 0
  timeout_events = 0

  if restore_result is not None:
    if restore_result.database_restore_disposition is not None:
      disposition = (
        restore_result.database_restore_disposition.value
      )
    cache_generation = (
      restore_result.restored_cache_generation or "none"
    )
    cache_selection_reason = (
      restore_result.restored_cache_selection_reason or "none"
    )
    if restore_result.restored_cache_age_seconds is not None:
      cache_age = str(
        restore_result.restored_cache_age_seconds
      )
    frames_attempted = (
      restore_result.database_frames_attempted_count
    )
    frames_accepted = restore_result.accepted_frame_count
    frames_rejected = len(
      restore_result.permanently_rejected_indexes
    )
    timeout_events = (
      len(restore_result.initially_timed_out_indexes)
      + len(restore_result.permanently_timed_out_indexes)
    )

  trusted_time_source = (
    authorized_time.evidence.value
    if authorized_time is not None
    else "none"
  )
  time_attempt = (
    time_assistance_attempts[-1]
    if time_assistance_attempts
    else None
  )
  time_accepted_before_start = (
    (
      time_attempt.accepted_at <= gnss_start_sent_at
      if time_attempt.accepted_at is not None
      else False
    )
    if (
      time_attempt is not None
      and gnss_start_sent_at is not None
    )
    else None
  )

  fields = (
    "GPS startup timeline",
    f"cycle={cycle}",
    f"reason={reason}",
    "trusted_time_wait_started_cycle_seconds="
    + _startup_timeline_elapsed(
      trusted_time_wait_started_at,
      cycle_started_at,
    ),
    "trusted_time_wait_completed_cycle_seconds="
    + _startup_timeline_elapsed(
      trusted_time_wait_completed_at,
      cycle_started_at,
    ),
    "trusted_time_wait_duration_seconds="
    + (
      "none"
      if wait_duration is None
      else f"{wait_duration:.3f}"
    ),
    "independent_network_time_first_seen_cycle_seconds="
    + _startup_timeline_elapsed(
      independent_network_time_seen_at,
      cycle_started_at,
    ),
    "trusted_time_available="
    + str(authorized_time is not None).lower(),
    f"trusted_time_source={trusted_time_source}",
    f"database_restore_disposition={disposition}",
    f"restored_cache_generation={cache_generation}",
    "restored_cache_selection_reason="
    + cache_selection_reason,
    f"restored_cache_age_seconds={cache_age}",
    f"database_frames_attempted={frames_attempted}",
    f"database_frames_accepted={frames_accepted}",
    f"database_frames_rejected={frames_rejected}",
    f"database_timeout_events={timeout_events}",
    "time_assistance_attempted_cycle_seconds="
    + _startup_timeline_elapsed(
      (
        time_attempt.attempted_at
        if time_attempt is not None
        else None
      ),
      cycle_started_at,
    ),
    "time_assistance_written_cycle_seconds="
    + _startup_timeline_elapsed(
      (
        time_attempt.written_at
        if time_attempt is not None
        else None
      ),
      cycle_started_at,
    ),
    "time_assistance_ack_observed_cycle_seconds="
    + _startup_timeline_elapsed(
      (
        time_attempt.ack_observed_at
        if time_attempt is not None
        else None
      ),
      cycle_started_at,
    ),
    "time_assistance_write_status="
    + (
      time_attempt.write_status.value
      if time_attempt is not None
      else TimeAssistanceWriteStatus.NOT_ATTEMPTED.value
    ),
    "time_assistance_ack_status="
    + (
      time_attempt.ack_status.value
      if time_attempt is not None
      else TimeAssistanceAckStatus.NOT_ATTEMPTED.value
    ),
    "time_assistance_ack_info_code="
    + (
      str(time_attempt.ack_info_code)
      if (
        time_attempt is not None
        and time_attempt.ack_info_code is not None
      )
      else "none"
    ),
    "time_assistance_accepted_cycle_seconds="
    + _startup_timeline_elapsed(
      (
        time_attempt.accepted_at
        if time_attempt is not None
        else None
      ),
      cycle_started_at,
    ),
    "time_assistance_accepted_before_gnss_start="
    + (
      str(time_accepted_before_start).lower()
      if time_accepted_before_start is not None
      else "unknown"
    ),
    "acquisition_start_claimed_cycle_seconds="
    + _startup_timeline_elapsed(
      acquisition_start_claimed_at,
      cycle_started_at,
    ),
    "gnss_start_sent_cycle_seconds="
    + _startup_timeline_elapsed(
      gnss_start_sent_at,
      cycle_started_at,
    ),
    "related_acquisition_milestones="
    + "first_nonempty_rawx|first_fix_ok|first_reliable_fix",
  )
  return ", ".join(fields)


def log_gps_startup_timeline(
  *,
  cycle: int,
  reason: str,
  cycle_started_at: float,
  trusted_time_wait_started_at: float | None,
  trusted_time_wait_completed_at: float | None,
  independent_network_time_seen_at: float | None,
  acquisition_start_claimed_at: float | None,
  gnss_start_sent_at: float | None,
  restore_result: NavigationAssistanceRestoreResult | None,
  authorized_time: AuthorizedTime | None,
  time_assistance_attempts: tuple[
    TimeAssistanceAttemptDiagnostic,
    ...,
  ] = (),
) -> None:
  cloudlog.info(
    format_gps_startup_timeline(
      cycle=cycle,
      reason=reason,
      cycle_started_at=cycle_started_at,
      trusted_time_wait_started_at=(
        trusted_time_wait_started_at
      ),
      trusted_time_wait_completed_at=(
        trusted_time_wait_completed_at
      ),
      independent_network_time_seen_at=(
        independent_network_time_seen_at
      ),
      acquisition_start_claimed_at=(
        acquisition_start_claimed_at
      ),
      gnss_start_sent_at=gnss_start_sent_at,
      restore_result=restore_result,
      authorized_time=authorized_time,
      time_assistance_attempts=time_assistance_attempts,
    )
  )


def drain_receiver_before_database_restore(pigeon: TTYPigeon) -> None:
  """Dispatch all buffered receiver input before the DBD restore decision."""
  drain = getattr(pigeon, "drain_before_transaction", None)
  if callable(drain):
    drain("navigation_database_post_time_wait")


def navigation_database_process_start_wait_seconds(
  cycle_started_at: float,
  now: float,
) -> float:
  return max(
    0.0,
    cycle_started_at
    + NAVIGATION_DATABASE_PROCESS_START_TIME_DEADLINE_SECONDS
    - now,
  )


def pre_start_remaining_seconds(pre_start_deadline: float) -> float:
  if (
    isinstance(pre_start_deadline, bool)
    or not isinstance(pre_start_deadline, (int, float))
    or not isfinite(float(pre_start_deadline))
  ):
    raise ValueError("pre_start_deadline must be finite")
  return max(0.0, float(pre_start_deadline) - time.monotonic())


def send_yuma_with_durable_claim(
  navigation_database_runtime: NavigationDatabaseRestoreRuntime,
  send_message: Callable[[bytes], object],
  message: bytes,
) -> None:
  if not navigation_database_runtime.claim_yuma_transmission():
    cloudlog.error(
      "GPS YUMA transmission suppressed: assistance state unavailable"
    )
    raise YumaAssistanceStateUnavailableError(
      "durable YUMA ownership claim unavailable"
    )
  send_message(message)


def initialize_receiver_cycle(
  pigeon: TTYPigeon,
  receiver_fingerprint: str,
  startup_diagnostics: GpsStartupDiagnostics,
  reason: str,
  collect_mon_ver_diagnostics: bool = False,
  time_authority: TimeAuthority | None = None,
  time_provenance: ReceiverTimeProvenanceTracker | None = None,
  navigation_database_runtime: NavigationDatabaseRestoreRuntime | None = None,
  position_assistance_retry: PositionAssistancePostStartRetryController | None = None,
  transport_mon_ver_info: MonVerInfo | None = None,
  cycle_started_at: float | None = None,
  network_available: bool | None = False,
  assistance_state_factory: Callable[[], tuple[
    NavigationDatabaseRestoreRuntime,
    PositionAssistancePostStartRetryController,
    "ReceiverAcquisitionStateGuard",
  ]] | None = None,
  assistance_state_ready_callback: Callable[[
    NavigationDatabaseRestoreRuntime,
    PositionAssistancePostStartRetryController,
    "ReceiverAcquisitionStateGuard",
  ], None] | None = None,
) -> ReceiverCycleInitialization:
  if cycle_started_at is None:
    cycle_started_at = time.monotonic()
  if network_available is not None and not isinstance(network_available, bool):
    raise ValueError("network_available must be a bool or None")
  if assistance_state_factory is not None and not callable(
    assistance_state_factory
  ):
    raise ValueError("assistance_state_factory must be callable")
  if assistance_state_ready_callback is not None and not callable(
    assistance_state_ready_callback
  ):
    raise ValueError("assistance_state_ready_callback must be callable")
  if (
    assistance_state_factory is not None
    and navigation_database_runtime is not None
  ):
    raise ValueError(
      "assistance_state_factory and navigation_database_runtime are mutually exclusive"
    )
  begin_receiver_configuration_cycle(
    pigeon,
    receiver_fingerprint,
    transport_verified=transport_mon_ver_info is not None,
  )
  startup_diagnostics.start_cycle(reason, cycle_started_at)
  provenance = time_provenance or ReceiverTimeProvenanceTracker()
  cycle_id = getattr(
    startup_diagnostics,
    "cycle_number",
    provenance.cycle_id + 1,
  )
  if type(cycle_id) is not int or cycle_id < 1:
    cycle_id = provenance.cycle_id + 1
  provenance.start_cycle(
    cycle_id,
    cycle_started_at,
    observations_enabled=False,
  )
  try:
    pigeon.time_provenance = provenance
  except (AttributeError, TypeError):
    pass

  authority = time_authority or TimeAuthority()
  # Trusted time is deliberately sampled once inside the pre-acquisition
  # callback, after mandatory receiver configuration and NAVX5 ACK-aiding.
  host_time_observation: HostTimeObservation | None = None
  authority_evaluation: TimeAuthorityEvaluation | None = None
  authorized_time: AuthorizedTime | None = None
  independent_network_time_seen_at: float | None = None
  trusted_time_wait_started_at: float | None = None
  trusted_time_wait_completed_at: float | None = None
  trusted_time_wait_deadline: float | None = None
  trusted_time_wait_error_type: str | None = None
  trusted_time_wait_error: str | None = None
  pre_start_deadline = (
    cycle_started_at
    + NAVIGATION_DATABASE_PROCESS_START_TIME_DEADLINE_SECONDS
  )
  acquisition_start_claimed_at: float | None = None
  database_runtime = navigation_database_runtime
  mon_ver_info: MonVerInfo | None = None
  ack_aiding_configuration_attempted = False
  ack_aiding_configuration_result: (
    Navx5AckAidingConfigurationResult | None
  ) = None
  trusted_time_assistance_sent = False
  time_assistance_source = None
  time_assistance_utc = None
  yuma_time_anchor_utc = None
  yuma_time_anchor_source = None
  yuma_time_anchor_monotonic = None
  diagnostic_context = None
  navigation_assistance_restore_result: (
    NavigationAssistanceRestoreResult | None
  ) = None
  navigation_assistance_restore_attempted = False
  time_assistance_attempts: list[
    TimeAssistanceAttemptDiagnostic
  ] = []
  acquisition_start_claimed = False
  pre_start_assistance_deferred = False
  next_time_assistance_attempt = (
    cycle_started_at + TIME_SYNC_CHECK_INTERVAL
  )

  assistance_state_failure_logged = False
  assistance_state_task_complete = Event()
  assistance_state_task_started = False
  assistance_state_task_result: tuple[
    NavigationDatabaseRestoreRuntime,
    PositionAssistancePostStartRetryController,
    ReceiverAcquisitionStateGuard,
  ] | None = None
  assistance_state_task_error: Exception | None = None
  deferred_assistance_state_finalized = False
  gnss_start_observed_at: float | None = None

  def note_time_assistance_attempt(
    diagnostic: TimeAssistanceAttemptDiagnostic,
  ) -> None:
    time_assistance_attempts.append(diagnostic)
    log_time_assistance_attempt_diagnostic(diagnostic)

  def note_assistance_state_unavailable(operation: str) -> None:
    nonlocal assistance_state_failure_logged
    if position_assistance_retry is not None:
      position_assistance_retry.runtime = None
    if assistance_state_failure_logged:
      return
    assistance_state_failure_logged = True
    cloudlog.error(
      ", ".join((
        "GPS assistance state unavailable",
        f"operation={operation}",
        "error="
        + (
          database_runtime.execution.state_persistence_error
          or "unknown"
        ),
        "dbd_disabled=true",
        "position_assistance_disabled=true",
        "yuma_disabled=true",
        "position_retry_disabled=true",
        "gnss_start_continues=true",
      ))
    )

  def activate_assistance_state(
    runtime: NavigationDatabaseRestoreRuntime,
    retry: PositionAssistancePostStartRetryController,
    guard: ReceiverAcquisitionStateGuard | None,
  ) -> None:
    nonlocal database_runtime
    nonlocal position_assistance_retry
    database_runtime = runtime
    position_assistance_retry = retry
    if assistance_state_ready_callback is not None and guard is not None:
      assistance_state_ready_callback(runtime, retry, guard)

  def unavailable_assistance_state(exc: Exception) -> tuple[
    NavigationDatabaseRestoreRuntime,
    PositionAssistancePostStartRetryController,
    ReceiverAcquisitionStateGuard,
  ]:
    cloudlog.error(
      "GPS assistance state unavailable; assistance disabled while "
      + "GNSS START continues, error_type="
      + type(exc).__name__
      + ", error="
      + str(exc)[:512]
    )
    retry = (
      position_assistance_retry
      or PositionAssistancePostStartRetryController(None)
    )
    retry.runtime = None
    return (
      NavigationDatabaseRestoreUnavailableRuntime(
        receiver_fingerprint,
        str(exc),
      ),
      retry,
      ReceiverAcquisitionStateGuard(),
    )

  def run_assistance_state_task() -> None:
    nonlocal assistance_state_task_result
    nonlocal assistance_state_task_error
    try:
      if assistance_state_factory is None:
        runtime = NavigationDatabaseRestoreRuntime(receiver_fingerprint)
        retry = (
          position_assistance_retry
          or PositionAssistancePostStartRetryController(None)
        )
        guard = ReceiverAcquisitionStateGuard()
      else:
        runtime, retry, guard = assistance_state_factory()
      runtime.prepare()
      assistance_state_task_result = (runtime, retry, guard)
    except Exception as exc:
      assistance_state_task_error = exc
    finally:
      assistance_state_task_complete.set()

  def start_assistance_state_task() -> None:
    nonlocal assistance_state_task_started
    if assistance_state_task_started:
      return
    assistance_state_task_started = True
    thread = Thread(
      target=run_assistance_state_task,
      name="pigeond-assistance-state",
      daemon=True,
    )
    thread.start()

  def adopt_completed_assistance_state() -> bool:
    if not assistance_state_task_complete.is_set():
      return False
    if assistance_state_task_error is not None:
      runtime, retry, guard = unavailable_assistance_state(
        assistance_state_task_error
      )
    else:
      assert assistance_state_task_result is not None
      runtime, retry, guard = assistance_state_task_result
    activate_assistance_state(runtime, retry, guard)
    return True

  def prepare_assistance_state_before_start(
    *,
    current_network_time: bool,
  ) -> bool:
    nonlocal database_runtime
    if database_runtime is not None:
      try:
        database_runtime.prepare()
      except Exception as exc:
        runtime, retry, guard = unavailable_assistance_state(exc)
        activate_assistance_state(runtime, retry, guard)
      return True

    # Production factory/cache work is useful pre-START only when current
    # trusted time makes DBD restore eligible. Never start it merely to close
    # an unusable DBD window.
    if not current_network_time:
      return False
    setup_deadline = (
      pre_start_deadline
      - PRE_START_ASSISTANCE_SETUP_RESERVE_SECONDS
    )
    remaining = setup_deadline - time.monotonic()
    if remaining <= 0.0:
      return False
    start_assistance_state_task()
    if not assistance_state_task_complete.wait(timeout=remaining):
      return False
    if time.monotonic() > setup_deadline:
      return False
    return adopt_completed_assistance_state()

  def start_assistance_state_after_start() -> None:
    if database_runtime is None:
      start_assistance_state_task()

  def pre_acquisition_initialization() -> None:
    nonlocal database_runtime
    nonlocal position_assistance_retry
    nonlocal mon_ver_info
    nonlocal ack_aiding_configuration_attempted
    nonlocal ack_aiding_configuration_result
    nonlocal trusted_time_assistance_sent
    nonlocal time_assistance_source
    nonlocal time_assistance_utc
    nonlocal yuma_time_anchor_utc
    nonlocal yuma_time_anchor_source
    nonlocal yuma_time_anchor_monotonic
    nonlocal diagnostic_context
    nonlocal navigation_assistance_restore_result
    nonlocal navigation_assistance_restore_attempted
    nonlocal acquisition_start_claimed
    nonlocal next_time_assistance_attempt
    nonlocal host_time_observation
    nonlocal authority_evaluation
    nonlocal authorized_time
    nonlocal independent_network_time_seen_at
    nonlocal trusted_time_wait_started_at
    nonlocal trusted_time_wait_completed_at
    nonlocal trusted_time_wait_deadline
    nonlocal trusted_time_wait_error_type
    nonlocal trusted_time_wait_error
    nonlocal acquisition_start_claimed_at
    nonlocal pre_start_assistance_deferred

    mon_ver_info = resolve_pre_acquisition_mon_ver(
      pigeon,
      initialization.transport_mon_ver_info,
      collect_mon_ver_diagnostics,
    )
    if mon_ver_info is not None:
      initialization.transport_mon_ver_info = mon_ver_info
    log_navx5_ack_aiding_support(mon_ver_info)
    try:
      ack_aiding_configuration_result = configure_navx5_ack_aiding(
        pigeon,
        mon_ver_info,
        pre_start_deadline=pre_start_deadline,
      )
    except TypeError as exc:
      if "unexpected keyword argument 'pre_start_deadline'" not in str(exc):
        raise
      ack_aiding_configuration_result = configure_navx5_ack_aiding(
        pigeon,
        mon_ver_info,
      )
    ack_aiding_configuration_attempted = True
    initialization.navx5_ack_aiding_result = (
      ack_aiding_configuration_result
    )
    authority_evaluated_at = time.monotonic()
    if authority_evaluated_at < pre_start_deadline:
      host_time_observation = read_host_time_observation()
      authority_evaluation = evaluate_time_authority(
        authority,
        host_time_observation,
      )
      authorized_time = authority_evaluation.authorized_time
      authority_evaluated_at = time.monotonic()
    else:
      cloudlog.info(
        "GPS trusted time check skipped: pre-START deadline exhausted"
      )
    current_network_time = (
      authorized_time is not None
      and is_current_independent_network_time(authorized_time)
    )
    if current_network_time:
      independent_network_time_seen_at = authority_evaluated_at

    assistance_state_ready = prepare_assistance_state_before_start(
      current_network_time=current_network_time,
    )
    if not assistance_state_ready:
      pre_start_assistance_deferred = True
      cloudlog.info(
        "GPS pre-START assistance deferred: no safe bounded setup budget"
      )
      return
    assert database_runtime is not None
    if (
      transport_mon_ver_info is not None
      and hasattr(pigeon, "dispatch_pending_frames")
    ):
      pigeon.dispatch_pending_frames()

    if database_runtime.controller.pending and not current_network_time:
      operation = "close_restore_window_no_trusted_time"
      close_result = (
        database_runtime.close_restore_window_no_trusted_time()
      )
      if not close_result:
        note_assistance_state_unavailable(operation)

    if (
      database_runtime.controller.pending
      and current_network_time
    ):
      assert authorized_time is not None
      try:
        drain_receiver_before_database_restore(pigeon)
      except Exception as exc:
        cloudlog.exception(
          "GPS DBD pre-restore input drain failed; restore suppressed"
        )
        database_runtime.record_pre_restore_transport_error(
          authorized_time=authorized_time,
          error=exc,
          phase="pre_restore_drain",
        )
    attempt_started_at = time.monotonic()
    if authorized_time is not None:
      yuma_time_anchor_utc = authorized_time.utc
      yuma_time_anchor_source = authorized_time.evidence.value
      yuma_time_anchor_monotonic = time.monotonic()
      diagnostic_context = startup_diagnostics.time_assistance_context(
        yuma_time_anchor_monotonic
      )

    navigation_assistance_restore_result = restore_navigation_assistance(
      pigeon,
      receiver_fingerprint,
      diagnostic_context=diagnostic_context,
      time_assistance_source=(
        authorized_time.evidence.value
        if authorized_time is not None
        else None
      ),
      trusted_now=(
        authorized_time.utc if authorized_time is not None else None
      ),
      navigation_database_runtime=database_runtime,
      authorized_time=authorized_time,
      database_trusted_time_wait_allowed=(
        False
      ),
      database_network_available=(network_available is True),
      database_trusted_time_wait_started_at=(
        trusted_time_wait_started_at
      ),
      database_trusted_time_wait_completed_at=(
        trusted_time_wait_completed_at
      ),
      database_trusted_time_wait_deadline=(
        trusted_time_wait_deadline
      ),
      database_trusted_time_wait_error_type=(
        trusted_time_wait_error_type
      ),
      database_trusted_time_wait_error=trusted_time_wait_error,
      pre_start_deadline=pre_start_deadline,
    )
    navigation_assistance_restore_attempted = True

    if not database_runtime.state_available:
      note_assistance_state_unavailable(
        "navigation_assistance_restore"
      )

    if (
      position_assistance_retry is not None
      and database_runtime.state_available
    ):
      retry_runtime = position_assistance_retry.runtime
      if retry_runtime is not None:
        retry_runtime.arm_from_initial(
          database_runtime.execution,
          database_runtime.position_assistance_message,
        )
        if retry_runtime.state.pending:
          initialization.require_pre_gnss_start_drain()
        if (
          retry_runtime.state.pending
          or retry_runtime.state.retry_result
          is PositionAssistanceRetryResult.CLAIM_PERSIST_FAILED
          or retry_runtime.persistence_error is not None
        ):
          log_position_assistance_retry_state(
            retry_runtime.state,
            trigger="armed_initial_info_code_5",
            receiver_cycle=getattr(pigeon, "receiver_cycle", 0),
            gnss_start_sent_at=None,
            nav_pvt_observed_at=None,
            persistence_error=retry_runtime.persistence_error,
          )

    if authorized_time is not None:
      time_assistance_utc = authorized_time.utc
      time_assistance_timeout = min(
        GPS_ASSISTANCE_ACK_TIMEOUT,
        pre_start_remaining_seconds(pre_start_deadline),
      )
      if time_assistance_timeout > 0.0:
        trusted_time_assistance_sent = send_time_assistance(
          pigeon,
          assistance_time=authorized_time.utc,
          accuracy_seconds=authorized_time.mga_accuracy_seconds,
          source=authorized_time.evidence.value,
          diagnostic_context=diagnostic_context,
          ack_timeout=time_assistance_timeout,
          time_provenance=provenance,
          assistance_boottime_seconds=getattr(
            authorized_time,
            "observed_boottime_seconds",
            None,
          ),
          independent=authorized_time.independent,
          source_provenance=authorized_time.provenance,
          diagnostic_callback=note_time_assistance_attempt,
        )
      else:
        cloudlog.info(
          "GPS time assistance skipped: pre-START deadline exhausted"
        )
      if trusted_time_assistance_sent:
        time_assistance_source = authorized_time.evidence.value
      next_time_assistance_attempt = (
        attempt_started_at + TIME_ASSISTANCE_RETRY_INTERVAL
      )
    else:
      next_time_assistance_attempt = (
        attempt_started_at + TIME_SYNC_CHECK_INTERVAL
      )

    if not acquisition_start_claimed:
      if not database_runtime.claim_acquisition_start():
        note_assistance_state_unavailable(
          "claim_acquisition_start"
        )
      acquisition_start_claimed = True
      acquisition_start_claimed_at = time.monotonic()
  def poll_deferred_assistance_state(
  ) -> NavigationAssistanceRestoreResult | None:
    nonlocal acquisition_start_claimed
    nonlocal acquisition_start_claimed_at
    nonlocal deferred_assistance_state_finalized
    nonlocal navigation_assistance_restore_attempted
    nonlocal navigation_assistance_restore_result
    if deferred_assistance_state_finalized:
      return None
    if not pre_start_assistance_deferred:
      deferred_assistance_state_finalized = True
      return None
    if database_runtime is None:
      if not assistance_state_task_complete.is_set():
        return None
      if not adopt_completed_assistance_state():
        return None
    assert database_runtime is not None
    if database_runtime.controller.pending:
      if authorized_time is None:
        terminalized = (
          database_runtime.close_restore_window_no_trusted_time()
        )
        operation = "post_start_close_restore_window_no_trusted_time"
      else:
        terminalized = (
          database_runtime.close_restore_window_for_early_acquisition()
        )
        operation = "post_start_close_restore_window_for_early_acquisition"
      if not terminalized:
        note_assistance_state_unavailable(operation)
    if not acquisition_start_claimed:
      if not database_runtime.note_acquisition_started():
        note_assistance_state_unavailable(
          "post_start_note_acquisition_started"
        )
      acquisition_start_claimed = True
      acquisition_start_claimed_at = (
        gnss_start_observed_at or time.monotonic()
      )
    if navigation_assistance_restore_result is None:
      # DBD may already be terminal (e.g. skipped_no_trusted_time) and
      # acquisition may already be claimed post-START. Position assistance still
      # runs from the independent snapshot via restore_navigation_assistance().
      navigation_assistance_restore_result = restore_navigation_assistance(
        pigeon,
        receiver_fingerprint,
        diagnostic_context=diagnostic_context,
        time_assistance_source=(
          authorized_time.evidence.value
          if authorized_time is not None
          else None
        ),
        trusted_now=(
          authorized_time.utc if authorized_time is not None else None
        ),
        navigation_database_runtime=database_runtime,
        authorized_time=authorized_time,
        database_trusted_time_wait_allowed=False,
        database_network_available=(network_available is True),
      )
      navigation_assistance_restore_attempted = True
      if (
        position_assistance_retry is not None
        and database_runtime.state_available
      ):
        retry_runtime = position_assistance_retry.runtime
        if retry_runtime is not None:
          retry_runtime.arm_from_initial(
            database_runtime.execution,
            database_runtime.position_assistance_message,
          )
          if (
            retry_runtime.state.pending
            or retry_runtime.state.retry_result
            is PositionAssistanceRetryResult.CLAIM_PERSIST_FAILED
            or retry_runtime.persistence_error is not None
          ):
            log_position_assistance_retry_state(
              retry_runtime.state,
              trigger="armed_initial_info_code_5",
              receiver_cycle=getattr(pigeon, "receiver_cycle", 0),
              gnss_start_sent_at=gnss_start_observed_at,
              nav_pvt_observed_at=None,
              persistence_error=retry_runtime.persistence_error,
            )
    if (
      position_assistance_retry is not None
      and gnss_start_observed_at is not None
    ):
      position_assistance_retry.begin_receiver_cycle(
        getattr(pigeon, "receiver_cycle", 0),
        gnss_start_observed_at,
      )
    deferred_assistance_state_finalized = True
    return navigation_assistance_restore_result

  def note_gnss_start_sent(now: float) -> None:
    nonlocal gnss_start_observed_at
    gnss_start_observed_at = now
    if database_runtime is None:
      start_assistance_state_after_start()
      return
    if position_assistance_retry is not None:
      position_assistance_retry.begin_receiver_cycle(
        getattr(pigeon, "receiver_cycle", 0),
        now,
      )

  with install_pre_acquisition_initialization(
    pre_acquisition_initialization,
    note_gnss_start_sent,
    transport_already_started=transport_mon_ver_info is not None,
    transport_mon_ver_info=transport_mon_ver_info,
    pre_start_deadline=pre_start_deadline,
    receiver_fingerprint=receiver_fingerprint,
  ) as initialization:
    init(pigeon)

  if not initialization.executed:
    # A custom initializer did not execute the controlled pre-acquisition
    # hook. Production init() always executes it before GNSS START. This
    # compatibility path sends no controlled START itself, so close the
    # in-process DBD window before running the callback without treating
    # unavailable test storage as a receiver-action persistence failure.
    initialization.run()
    if database_runtime is None:
      start_assistance_state_after_start()
    else:
      if not database_runtime.note_acquisition_started():
        note_assistance_state_unavailable(
          "compatibility_note_acquisition_started"
        )
      acquisition_start_claimed = True
  try:
    log_gps_startup_timeline(
      cycle=cycle_id,
      reason=reason,
      cycle_started_at=cycle_started_at,
      trusted_time_wait_started_at=(
        trusted_time_wait_started_at
      ),
      trusted_time_wait_completed_at=(
        trusted_time_wait_completed_at
      ),
      independent_network_time_seen_at=(
        independent_network_time_seen_at
      ),
      acquisition_start_claimed_at=(
        acquisition_start_claimed_at
      ),
      gnss_start_sent_at=initialization.gnss_start_sent_at,
      restore_result=navigation_assistance_restore_result,
      authorized_time=authorized_time,
      time_assistance_attempts=tuple(time_assistance_attempts),
    )
  except Exception:
    cloudlog.exception("GPS startup timeline logging failed")
  provenance.enable_receiver_observations(time.monotonic())

  assistnow_autonomous_configuration_attempted = False
  assistnow_autonomous_supported = log_assistnow_autonomous_support(
    mon_ver_info
  )
  if not assistnow_autonomous_supported:
    configure_assistnow_autonomous(pigeon, mon_ver_info)
    assistnow_autonomous_configuration_attempted = True
  elif authorized_time is not None:
    configure_assistnow_autonomous(pigeon, mon_ver_info)
    assistnow_autonomous_configuration_attempted = True
  else:
    cloudlog.info(
      "GPS AssistNow Autonomous configuration deferred: "
      + "reason=absolute_time_unavailable"
    )

  if collect_mon_ver_diagnostics:
    try:
      log_acquisition_configuration_diagnostics(pigeon, mon_ver_info)
    except Exception:
      cloudlog.exception(
        "GPS acquisition configuration diagnostics failed"
      )

  return ReceiverCycleInitialization(
    trusted_time_assistance_sent=trusted_time_assistance_sent,
    next_time_assistance_attempt=next_time_assistance_attempt,
    navigation_assistance_restore_attempted=(
      navigation_assistance_restore_attempted
    ),
    mon_ver_info=mon_ver_info,
    ack_aiding_configuration_attempted=ack_aiding_configuration_attempted,
    assistnow_autonomous_supported=assistnow_autonomous_supported,
    assistnow_autonomous_configuration_attempted=(
      assistnow_autonomous_configuration_attempted
    ),
    completed_at=time.monotonic(),
    ack_aiding_configuration_result=(
      ack_aiding_configuration_result
    ),
    navigation_assistance_restore_result=(
      navigation_assistance_restore_result
    ),
    time_assistance_utc=(
      time_assistance_utc if trusted_time_assistance_sent else None
    ),
    time_assistance_source=(
      time_assistance_source if trusted_time_assistance_sent else None
    ),
    yuma_time_anchor_utc=yuma_time_anchor_utc,
    yuma_time_anchor_source=yuma_time_anchor_source,
    yuma_time_anchor_monotonic=yuma_time_anchor_monotonic,
    authorized_time=authorized_time,
    host_time_observation=host_time_observation,
    authority_evaluation=authority_evaluation,
    time_assistance_attempts=tuple(time_assistance_attempts),
    gnss_start_sent_at=initialization.gnss_start_sent_at,
    poll_deferred_assistance_state=(
      poll_deferred_assistance_state
      if pre_start_assistance_deferred
      else None
    ),
  )


def publish_ublox_raw(pm: messaging.PubMaster, data: bytes) -> None:
  message = messaging.new_message(
    "ubloxRaw",
    len(data),
    valid=True,
  )
  message.ubloxRaw = data
  pm.send("ubloxRaw", message)


def receiver_frames_show_gnss_acquisition(
  frames: list[bytes],
) -> bool:
  for frame in frames:
    if len(frame) >= 8 and frame[2:4] == b"\x02\x15":
      try:
        if Ubx.RxmRawx.from_bytes(frame[6:-2]).num_meas > 0:
          return True
      except Exception:
        pass
    fix = parse_nav_pvt(frame)
    if fix is not None and fix.fix_ok:
      return True
    nav_sat = parse_nav_sat(frame)
    if nav_sat is not None and nav_sat.satellites_used > 0:
      return True
  return False


def process_receiver_frames(
  frames: list[bytes],
  frame_time: float,
  startup_diagnostics: GpsStartupDiagnostics,
  fix_tracker: ReliableFixTracker,
  capture_quality_tracker: CaptureQualityTracker,
  autonomous_orbit_diagnostics: AutonomousOrbitDiagnostics,
  dump_collector: NavigationDatabaseDumpCollector,
  capture_state: NavigationCaptureState,
  time_provenance: ReceiverTimeProvenanceTracker | None = None,
) -> tuple[bytes, ...] | None:
  completed_database = None
  for frame in frames:
    startup_diagnostics.note_rawx(frame, frame_time)
    if time_provenance is not None:
      time_provenance.note_rawx(frame, frame_time)
    fix = parse_nav_pvt(frame)

    if fix is not None:
      startup_diagnostics.note_nav_pvt(fix, frame_time)
      if time_provenance is not None:
        time_provenance.note_nav_pvt(fix, frame_time)
      fix_tracker.update(fix, frame_time)
      reset_reason = capture_quality_tracker.update_fix(fix, frame_time)
      if reset_reason is not None:
        cloudlog.info(f"GPS navigation assistance quality gate reset: reason={reset_reason}")

    nav_sat = parse_nav_sat(frame)
    if nav_sat is not None:
      autonomous_orbit_diagnostics.note_nav_sat(nav_sat)
      orbit_was_eligible = capture_quality_tracker.orbit_eligible(frame_time)
      reset_reason = capture_quality_tracker.update_nav_sat(nav_sat, frame_time)
      if reset_reason is not None:
        cloudlog.info(f"GPS navigation assistance quality gate reset: reason={reset_reason}")
      if not orbit_was_eligible and capture_quality_tracker.orbit_eligible(frame_time):
        cloudlog.info("GPS navigation assistance orbit-quality gate eligible")

    if dump_collector.active:
      try:
        result = dump_collector.feed(frame)
        if result is not None:
          completed_database = result
      except CacheValidationError:
        cloudlog.exception("GPS navigation database capture failed")
        dump_collector.cancel()
        capture_state.fail(frame_time)

  return completed_database


def yuma_database_restore_state(
  result: NavigationAssistanceRestoreResult | None,
) -> YumaDatabaseRestoreState:
  if result is None:
    return YumaDatabaseRestoreState.FAILED
  disposition = result.database_restore_disposition
  if disposition is None:
    try:
      return YumaDatabaseRestoreState(result.status.value)
    except (AttributeError, ValueError):
      return YumaDatabaseRestoreState.FAILED
  if disposition is NavigationDatabaseRestoreDisposition.PENDING:
    return YumaDatabaseRestoreState.PENDING
  if disposition.database_available:
    return YumaDatabaseRestoreState.COMPLETE
  mapping = {
    NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL: (
      YumaDatabaseRestoreState.PARTIAL
    ),
    NavigationDatabaseRestoreDisposition.RESTORE_REJECTED: (
      YumaDatabaseRestoreState.REJECTED
    ),
    NavigationDatabaseRestoreDisposition.RESTORE_RESPONSE_TIMEOUT: (
      YumaDatabaseRestoreState.RESPONSE_TIMEOUT
    ),
    NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE: (
      YumaDatabaseRestoreState.TRANSFER_DEADLINE
    ),
    NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR: (
      YumaDatabaseRestoreState.TRANSPORT_ERROR
    ),
    NavigationDatabaseRestoreDisposition.RESTORE_CACHE_EXPIRED: (
      YumaDatabaseRestoreState.EXPIRED
    ),
    NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED: (
      YumaDatabaseRestoreState.EXPIRED
    ),
  }
  if disposition.intentionally_skipped:
    return YumaDatabaseRestoreState.SKIPPED
  return mapping.get(disposition, YumaDatabaseRestoreState.FAILED)


def log_cross_boot_rtc_observation(
  observation: CrossBootRtcObservation,
) -> None:
  candidate = observation.candidate
  fields = [
    "GPS cross-boot RTC observation",
    f"state={observation.state.value}",
    f"reason={observation.reason.value}",
    f"authorized={str(observation.authorized).lower()}",
    f"operational={str(observation.operational).lower()}",
    (
      "candidate_utc="
      + (
        candidate.candidate_utc.isoformat()
        if candidate is not None
        else "none"
      )
    ),
    (
      "candidate_uncertainty_seconds="
      + (
        str(candidate.uncertainty_seconds)
        if candidate is not None
        else "none"
      )
    ),
    (
      "anchor_generation="
      + (
        candidate.anchor_generation
        if candidate is not None
        else "none"
      )
    ),
    (
      "anchor_sequence="
      + (
        str(candidate.anchor_sequence)
        if candidate is not None
        else "none"
      )
    ),
    (
      "anchor_boot_id="
      + (
        candidate.anchor_boot_id
        if candidate is not None
        else "none"
      )
    ),
    (
      "current_boot_id="
      + (
        candidate.current_boot_id
        if candidate is not None
        else "none"
      )
    ),
    (
      "anchor_rtc_epoch_seconds="
      + (
        str(candidate.anchor_rtc_epoch_seconds)
        if candidate is not None
        else "none"
      )
    ),
    (
      "current_rtc_epoch_seconds="
      + (
        str(candidate.current_rtc_epoch_seconds)
        if candidate is not None
        else "none"
      )
    ),
    (
      "rtc_elapsed_seconds="
      + (
        str(candidate.rtc_elapsed_seconds)
        if candidate is not None
        else "none"
      )
    ),
    (
      "current_boottime_seconds="
      + (
        str(candidate.current_boottime_seconds)
        if candidate is not None
        else "none"
      )
    ),
    (
      "elapsed_covers_uptime="
      + (
        str(candidate.elapsed_covers_uptime).lower()
        if candidate is not None
        else "false"
      )
    ),
    (
      "rtc_voltage_status_supported="
      + (
        str(
          candidate.rtc_voltage_status_supported
        ).lower()
        if candidate is not None
        else "false"
      )
    ),
    (
      "rtc_voltage_status_flags="
      + (
        str(candidate.rtc_voltage_status_flags)
        if (
          candidate is not None
          and candidate.rtc_voltage_status_flags is not None
        )
        else "none"
      )
    ),
    (
      "rtc_tick_delta_seconds="
      + (
        str(observation.rtc_tick_delta_seconds)
        if observation.rtc_tick_delta_seconds is not None
        else "none"
      )
    ),
    (
      "boottime_tick_delta_seconds="
      + (
        str(observation.boottime_tick_delta_seconds)
        if (
          observation.boottime_tick_delta_seconds
          is not None
        )
        else "none"
      )
    ),
    (
      "tick_consistent="
      + (
        str(observation.tick_consistent).lower()
        if observation.tick_consistent is not None
        else "unknown"
      )
    ),
  ]
  message = ", ".join(fields)
  if observation.state is RtcObservationState.REJECTED:
    cloudlog.warning(message)
  else:
    cloudlog.info(message)


def log_receiver_utc_observation(
  observation: ReceiverUtcObservation,
) -> None:
  fields = (
    "GPS receiver UTC provenance",
    f"cycle={observation.cycle_id}",
    f"classification={observation.classification.value}",
    f"reason={observation.reason}",
    f"independent={str(observation.independent).lower()}",
    (
      "time_assistance_written="
      + str(observation.time_assistance_written).lower()
    ),
    (
      "time_assistance_source="
      + (
        observation.time_assistance_source
        if observation.time_assistance_source is not None
        else "none"
      )
    ),
    (
      "time_accuracy_ns="
      + (
        str(observation.time_accuracy_ns)
        if observation.time_accuracy_ns is not None
        else "none"
      )
    ),
    (
      "rawx_measurement_count="
      + str(observation.rawx_measurement_count)
    ),
    (
      "gps_week_valid="
      + str(observation.gps_week_valid).lower()
    ),
    (
      "leap_second_valid="
      + str(observation.leap_second_valid).lower()
    ),
  )
  message = ", ".join(fields)
  if observation.classification is (
    ReceiverUtcClassification.UNASSISTED_GNSS
  ):
    cloudlog.info(message)
  else:
    cloudlog.warning(message)


@dataclass(frozen=True)
class HostTimeProcessingState:
  generation: str | None = None
  source: HostTimeSource | None = None
  persistence_complete: bool = True
  next_retry_at: float = 0.0


def host_time_persistence_complete(
  evaluation: TimeAuthorityEvaluation,
) -> bool:
  return evaluation.anchor_write_status in {
    AnchorWriteStatus.SAVED,
    AnchorWriteStatus.PRESERVED_CURRENT_BOOT,
  }


def host_time_processing_state(
  observation: HostTimeObservation | None,
  evaluation: TimeAuthorityEvaluation | None,
  *,
  now: float,
) -> HostTimeProcessingState:
  if observation is None:
    return HostTimeProcessingState()
  persistence_complete = (
    not observation.independent
    or (
      evaluation is not None
      and host_time_persistence_complete(evaluation)
    )
  )
  return HostTimeProcessingState(
    generation=observation.generation,
    source=observation.source,
    persistence_complete=persistence_complete,
    next_retry_at=(
      now
      if persistence_complete
      else now + HOST_TIME_PERSISTENCE_RETRY_INTERVAL
    ),
  )


def host_time_requires_processing(
  state: HostTimeProcessingState,
  observation: HostTimeObservation | None,
  *,
  now: float,
) -> bool:
  if observation is None:
    return False
  if observation.generation != state.generation:
    return True
  return (
    observation.independent
    and not state.persistence_complete
    and now >= state.next_retry_at
  )


def independent_time_observation(
  authorized: AuthorizedTime | object | None,
) -> IndependentTimeObservation | None:
  if (
    authorized is None
    or getattr(authorized, "independent", False) is not True
  ):
    return None
  boottime = getattr(
    authorized,
    "observed_boottime_seconds",
    None,
  )
  if boottime is None:
    return None
  try:
    return IndependentTimeObservation(
      utc=authorized.utc,
      observed_boottime_seconds=boottime,
      uncertainty_seconds=authorized.uncertainty_seconds,
      source=authorized.source,
      provenance=authorized.provenance,
    )
  except (AttributeError, TypeError, ValueError):
    return None


def authorize_independent_receiver_utc(
  time_authority: TimeAuthority,
  observation: ReceiverUtcObservation,
  *,
  now: float | None = None,
) -> TimeAuthorityEvaluation | None:
  if (
    observation.classification
    is not ReceiverUtcClassification.UNASSISTED_GNSS
    or not observation.independent
    or observation.utc is None
    or observation.observed_at is None
    or observation.time_accuracy_ns is None
  ):
    return None
  current_monotonic = time.monotonic() if now is None else now
  if (
    type(current_monotonic) not in (int, float)
    or isinstance(current_monotonic, bool)
    or not isfinite(current_monotonic)
    or current_monotonic < observation.observed_at
  ):
    cloudlog.warning(
      "GPS independent receiver UTC rejected: reason=observation_time_invalid"
    )
    return None
  observed_boottime = read_boottime_seconds()
  if observed_boottime is None:
    cloudlog.warning(
      "GPS independent receiver UTC rejected: reason=boottime_unavailable"
    )
    return None
  receiver_utc_at_boottime = (
    observation.utc
    + timedelta(
      seconds=float(current_monotonic) - observation.observed_at
    )
  )
  evaluation = time_authority.observe_independent_time(
    utc=receiver_utc_at_boottime,
    uncertainty_seconds=(
      observation.time_accuracy_ns / 1_000_000_000
    ),
    source=(
      TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
    ),
    provenance=TimeProvenance.GNSS_INDEPENDENT,
    observed_boottime_seconds=observed_boottime,
  )
  authorized = evaluation.authorized_time
  fields = (
    "GPS independent receiver UTC authority",
    f"authorized={str(authorized is not None).lower()}",
    (
      "anchor_write_status="
      + evaluation.anchor_write_status.value
    ),
    (
      "anchor_write_reason="
      + (
        evaluation.anchor_write_reason.value
        if evaluation.anchor_write_reason is not None
        else "none"
      )
    ),
    (
      "anchor_error_seconds="
      + (
        str(evaluation.anchor_comparison.error_seconds)
        if evaluation.anchor_comparison is not None
        else "none"
      )
    ),
  )
  message = ", ".join(fields)
  if authorized is not None:
    cloudlog.info(message)
  else:
    cloudlog.warning(message)
  return evaluation


def log_cross_boot_rtc_validation(
  validation: CrossBootRtcValidation,
) -> None:
  fields = (
    "GPS cross-boot RTC independent validation",
    f"status={validation.status.value}",
    f"reason={validation.reason}",
    f"authorized={str(validation.authorized).lower()}",
    f"operational={str(validation.operational).lower()}",
    f"validation_source={validation.validation_source.value}",
    (
      "candidate_error_seconds="
      + (
        str(validation.candidate_error_seconds)
        if validation.candidate_error_seconds is not None
        else "none"
      )
    ),
    (
      "allowed_error_seconds="
      + (
        str(validation.allowed_error_seconds)
        if validation.allowed_error_seconds is not None
        else "none"
      )
    ),
    (
      "anchor_generation="
      + (
        validation.anchor_generation
        if validation.anchor_generation is not None
        else "none"
      )
    ),
    (
      "anchor_sequence="
      + str(validation.anchor_sequence)
    ),
    (
      "rtc_elapsed_seconds="
      + str(validation.rtc_elapsed_seconds)
    ),
    (
      "current_uptime_seconds="
      + str(validation.current_uptime_seconds)
    ),
    (
      "tick_consistent="
      + (
        str(validation.tick_consistent).lower()
        if validation.tick_consistent is not None
        else "unknown"
      )
    ),
  )
  message = ", ".join(fields)
  if validation.status is (
    CrossBootRtcValidationStatus.DISAGREES
  ):
    cloudlog.warning(message)
  else:
    cloudlog.info(message)


def validate_observed_cross_boot_rtc(
  observation: CrossBootRtcObservation | None,
  independent: IndependentTimeObservation,
) -> CrossBootRtcValidation | None:
  if (
    observation is None
    or observation.state is not RtcObservationState.OBSERVED
  ):
    return None
  validation = validate_cross_boot_rtc(
    observation,
    independent,
  )
  log_cross_boot_rtc_validation(validation)
  return validation


def log_receiver_correction_decision(
  decision: ReceiverCorrectionDecision,
  *,
  write_observed: bool,
  ack_accepted: bool,
) -> None:
  fields = (
    "GPS receiver UTC correction",
    f"cycle={decision.receiver_cycle}",
    f"decision={decision.reason.value}",
    f"should_correct={str(decision.should_correct).lower()}",
    f"source={decision.source.value}",
    (
      "delta_seconds="
      + (
        str(decision.delta_seconds)
        if decision.delta_seconds is not None
        else "none"
      )
    ),
    (
      "minimum_delta_seconds="
      + str(decision.minimum_delta_seconds)
    ),
    (
      "materially_better="
      + str(decision.materially_better).lower()
    ),
    f"write_observed={str(write_observed).lower()}",
    f"ack_accepted={str(ack_accepted).lower()}",
  )
  message = ", ".join(fields)
  if decision.should_correct and not write_observed:
    cloudlog.warning(message)
  else:
    cloudlog.info(message)


def maybe_send_receiver_time_correction(
  pigeon: TTYPigeon,
  time_provenance: ReceiverTimeProvenanceTracker,
  independent: IndependentTimeObservation,
  *,
  diagnostic_context: str | None = None,
) -> tuple[ReceiverCorrectionDecision, bool]:
  decision = evaluate_receiver_correction(
    time_provenance.time_assistance_observation,
    independent,
  )
  if not decision.should_correct:
    log_receiver_correction_decision(
      decision,
      write_observed=False,
      ack_accepted=False,
    )
    return decision, False

  accepted = send_time_assistance(
    pigeon,
    assistance_time=independent.utc,
    accuracy_seconds=min(
      65_535,
      max(0, ceil(independent.uncertainty_seconds)),
    ),
    source=independent.source.value,
    diagnostic_context=diagnostic_context,
    time_provenance=time_provenance,
    assistance_boottime_seconds=(
      independent.observed_boottime_seconds
    ),
    independent=True,
    source_provenance=independent.provenance,
    correction=True,
    diagnostic_callback=log_time_assistance_attempt_diagnostic,
  )
  write_observed = time_provenance.correction_written
  log_receiver_correction_decision(
    decision,
    write_observed=write_observed,
    ack_accepted=accepted,
  )
  return decision, accepted


def fresh_receiver_utc_time_anchor(
  diagnostics: GpsStartupDiagnostics,
  now: float,
) -> tuple[datetime, float] | None:
  fix = getattr(diagnostics, "latest_fix", None)
  observed_at = getattr(diagnostics, "latest_fix_time", None)
  utc_time = getattr(fix, "utc_time", None)
  if (
    utc_time is None
    or observed_at is None
    or now < observed_at
    or now - observed_at > MAXIMUM_NAV_PVT_GAP_SECONDS
  ):
    return None
  return utc_time, observed_at


def fresh_independent_receiver_utc_time_anchor(
  time_provenance: ReceiverTimeProvenanceTracker,
  now: float,
) -> tuple[datetime, float] | None:
  observation = time_provenance.current_observation(now)
  if (
    observation.classification
    is not ReceiverUtcClassification.UNASSISTED_GNSS
    or not observation.independent
    or observation.utc is None
    or observation.observed_at is None
  ):
    return None
  return observation.utc, observation.observed_at


def create_yuma_supplementation_runtime(
  initialization: ReceiverCycleInitialization,
  *,
  started_at: float | None = None,
  time_anchor_utc: datetime | None = None,
  time_anchor_monotonic: float | None = None,
  time_anchor_source: str | None = None,
) -> YumaSupplementationRuntime:
  restore_result = getattr(
    initialization,
    "navigation_assistance_restore_result",
    None,
  )
  completed_at = getattr(
    initialization,
    "completed_at",
    time.monotonic(),
  )
  runtime_started_at = (
    completed_at
    if started_at is None
    else started_at
  )
  anchor_utc = (
    getattr(
      initialization,
      "yuma_time_anchor_utc",
      getattr(initialization, "time_assistance_utc", None),
    )
    if time_anchor_utc is None
    else time_anchor_utc
  )
  anchor_monotonic = (
    getattr(
      initialization,
      "yuma_time_anchor_monotonic",
      completed_at,
    )
    if time_anchor_monotonic is None
    else time_anchor_monotonic
  )
  if anchor_monotonic is None:
    anchor_monotonic = completed_at
  anchor_source = (
    getattr(
      initialization,
      "yuma_time_anchor_source",
      getattr(initialization, "time_assistance_source", None),
    )
    if time_anchor_source is None
    else time_anchor_source
  )
  return YumaSupplementationRuntime(
    database_state=yuma_database_restore_state(
      restore_result
    ),
    database_saved_at_utc=(
      getattr(restore_result, "cache_saved_at_utc", None)
    ),
    restored_cache_generation=(
      getattr(
        restore_result,
        "restored_cache_generation",
        None,
      )
    ),
    restored_cache_selection_reason=(
      getattr(
        restore_result,
        "restored_cache_selection_reason",
        None,
      )
    ),
    restored_gps_almanac_available=(
      getattr(
        restore_result,
        "restored_gps_almanac_available",
        None,
      )
    ),
    restored_glonass_almanac_available=(
      getattr(
        restore_result,
        "restored_glonass_almanac_available",
        None,
      )
    ),
    restored_gps_ephemeris_available=(
      getattr(
        restore_result,
        "restored_gps_ephemeris_available",
        None,
      )
    ),
    restored_glonass_ephemeris_available=(
      getattr(
        restore_result,
        "restored_glonass_ephemeris_available",
        None,
      )
    ),
    restored_satellites_used=(
      getattr(
        restore_result,
        "restored_satellites_used",
        None,
      )
    ),
    restored_gps_startup_ready=(
      getattr(
        restore_result,
        "restored_gps_startup_ready",
        None,
      )
    ),
    restored_gps_almanac_satellite_ids=(
      getattr(
        restore_result,
        "restored_gps_almanac_satellite_ids",
        None,
      )
    ),
    restored_navigation_quality=(
      getattr(
        restore_result,
        "restored_navigation_quality",
        None,
      )
    ),
    started_at=runtime_started_at,
    time_anchor_utc=anchor_utc,
    time_anchor_source=anchor_source,
    time_anchor_monotonic=anchor_monotonic,
  )


def yuma_assistance_state_unavailable_outcome(
  outcome: object | None,
) -> bool:
  result = getattr(outcome, "transmit_result", None)
  return (
    getattr(result, "assistance_state_unavailable", False)
    is True
  )


class YumaSupplementationFeature:
  def __init__(
    self,
    params: Params,
    initialization: ReceiverCycleInitialization,
    receiver_cycle: int,
  ) -> None:
    if (
      isinstance(receiver_cycle, bool)
      or not isinstance(receiver_cycle, int)
      or receiver_cycle < 0
    ):
      raise ValueError(
        "receiver_cycle must be a non-negative integer"
      )
    self._params = params
    self._receiver_cycle = receiver_cycle
    self._enabled: bool | None = None
    self._next_param_check = 0.0
    self._runtime: YumaSupplementationRuntime | None = None
    self._cycle_injection_consumed = False
    self._provisional_reference: ProvisionalYumaReferenceTime | None = None
    self._provisional_reference_used: ProvisionalYumaReferenceTime | None = None
    self._provisional_attempted = False
    self._current_boot_id = read_boot_id()
    disable_state = load_provisional_yuma_boot_disable_state(
      self._current_boot_id
    )
    self._provisional_disabled_for_boot = disable_state.disabled
    cloudlog.info(", ".join((
      "GPS provisional YUMA boot disable state",
      f"disabled={str(disable_state.disabled).lower()}",
      "current_boot_id=" + (disable_state.current_boot_id or "none"),
      "stored_boot_id=" + (disable_state.stored_boot_id or "none"),
      "reason=" + (disable_state.reason or "none"),
      "error=" + (disable_state.error or "none"),
    )))
    self._pending_outcomes: deque[YumaSupplementationRuntimeOutcome] = deque()
    self._initialization = initialization
    self._time_anchor_utc: datetime | None = None
    self._time_anchor_source: str | None = None
    self._time_anchor_monotonic = getattr(
      initialization,
      "completed_at",
      time.monotonic(),
    )
    self.reset_receiver_cycle(
      initialization,
      receiver_cycle,
    )

  @property
  def runtime_active(self) -> bool:
    return self._runtime is not None

  @property
  def time_anchor_source(self) -> str | None:
    return self._time_anchor_source

  @property
  def cycle_injection_consumed(self) -> bool:
    return self._cycle_injection_consumed

  def update_navigation_assistance_restore_result(
    self,
    result: NavigationAssistanceRestoreResult,
    now: float,
  ) -> bool:
    self._initialization = replace(
      self._initialization,
      navigation_assistance_restore_result=result,
      navigation_assistance_restore_attempted=True,
    )
    if self._cycle_injection_consumed:
      return False
    self._runtime = None
    self._next_param_check = 0.0
    self._refresh_enabled(now, force=True)
    return True

  def persist_provisional_telemetry(
    self,
    event: str,
    *,
    now: float,
    observation: object | None = None,
    authority: object | None = None,
    decision: object | None = None,
    accepted: bool | None = None,
    outcome: object | None = None,
    validation: object | None = None,
  ) -> None:
    try:
      if (
        not isinstance(self._current_boot_id, str)
        or not self._current_boot_id.strip()
      ):
        raise ValueError("current Linux boot ID is unavailable")
      store_provisional_yuma_decision_event(
        event,
        current_boot_id=self._current_boot_id,
        receiver_cycle=self._receiver_cycle,
        observed_at=now,
        observation=observation,
        authority=authority,
        decision=decision,
        accepted=accepted,
        outcome=outcome,
        validation=validation,
      )
    except Exception:
      cloudlog.exception(
        "Failed to persist provisional YUMA decision telemetry"
      )

  def _contextualize_outcome(
    self,
    outcome: YumaSupplementationRuntimeOutcome,
  ) -> YumaSupplementationRuntimeOutcome:
    gnss_start_sent_at = getattr(
      self._initialization,
      "gnss_start_sent_at",
      None,
    )
    completed_before_gnss_start = (
      outcome.completion_monotonic <= gnss_start_sent_at
      if (
        outcome.completion_monotonic is not None
        and gnss_start_sent_at is not None
      )
      else None
    )
    return replace(
      outcome,
      receiver_cycle=self._receiver_cycle,
      feature_enabled=(
        outcome.plan.reason
        is not YumaSupplementationReason.FEATURE_DISABLED
      ),
      gnss_start_sent_at_monotonic=gnss_start_sent_at,
      completed_before_gnss_start=completed_before_gnss_start,
    )

  def _queue_cancellation(
    self,
    now: float,
    reason: YumaSupplementationReason,
  ) -> None:
    if self._runtime is None:
      return
    outcome = self._runtime.cancel(
      now=now,
      reason=reason,
    )
    if outcome is not None:
      self._pending_outcomes.append(
        self._contextualize_outcome(outcome)
      )

  def reset_receiver_cycle(
    self,
    initialization: ReceiverCycleInitialization,
    receiver_cycle: int,
  ) -> None:
    if (
      isinstance(receiver_cycle, bool)
      or not isinstance(receiver_cycle, int)
      or receiver_cycle < 0
    ):
      raise ValueError(
        "receiver_cycle must be a non-negative integer"
      )
    completed_at = getattr(
      initialization,
      "completed_at",
      time.monotonic(),
    )
    self._queue_cancellation(
      completed_at,
      YumaSupplementationReason.RECEIVER_CYCLE_RESET,
    )
    self._receiver_cycle = receiver_cycle
    self._initialization = initialization
    self._runtime = None
    self._cycle_injection_consumed = False
    self._provisional_reference = None
    self._provisional_reference_used = None
    self._provisional_attempted = False
    self._time_anchor_utc = getattr(
      initialization,
      "yuma_time_anchor_utc",
      getattr(initialization, "time_assistance_utc", None),
    )
    self._time_anchor_source = getattr(
      initialization,
      "yuma_time_anchor_source",
      getattr(initialization, "time_assistance_source", None),
    )
    self._time_anchor_monotonic = getattr(
      initialization,
      "yuma_time_anchor_monotonic",
      completed_at,
    )
    if self._time_anchor_monotonic is None:
      self._time_anchor_monotonic = completed_at
    self._next_param_check = 0.0
    self._refresh_enabled(completed_at, force=True)

  def set_time_anchor(
    self,
    anchor_utc: datetime,
    anchor_monotonic: float,
    source: str,
  ) -> None:
    if not isinstance(source, str) or not source.strip():
      raise ValueError("source must be a non-empty string")
    self._time_anchor_utc = anchor_utc
    self._time_anchor_source = source.strip()
    self._time_anchor_monotonic = anchor_monotonic
    if not self._provisional_attempted:
      self._provisional_reference = None
    if self._runtime is not None:
      self._runtime.set_time_anchor(
        anchor_utc,
        anchor_monotonic,
        self._time_anchor_source,
      )

  def set_receiver_time_anchor(
    self,
    anchor_utc: datetime,
    anchor_monotonic: float,
    source: str = "receiver_utc",
  ) -> bool:
    # Preserve an RTC or synchronized host anchor. Receiver UTC is a fallback
    # only for a cycle that began without usable absolute time.
    if self._time_anchor_utc is not None:
      return False
    self.set_time_anchor(
      anchor_utc,
      anchor_monotonic,
      source,
    )
    return True

  def set_provisional_reference(
    self,
    reference: ProvisionalYumaReferenceTime,
  ) -> bool:
    if not isinstance(reference, ProvisionalYumaReferenceTime):
      raise ValueError(
        "reference must be a ProvisionalYumaReferenceTime"
      )
    if (
      self._provisional_disabled_for_boot
      or self._provisional_attempted
      or self._cycle_injection_consumed
      or self._time_anchor_utc is not None
      or reference.receiver_cycle != self._receiver_cycle
      or reference.current_boot_id != self._current_boot_id
    ):
      return False
    self._provisional_reference = reference
    return True

  def note_cross_boot_validation(
    self,
    validation: CrossBootRtcValidation | None,
  ) -> None:
    if validation is None or self._provisional_reference_used is None:
      return
    if validation.status is CrossBootRtcValidationStatus.DISAGREES:
      self._provisional_disabled_for_boot = True
      self._provisional_reference = None
      try:
        current_boot_id = self._current_boot_id
        if not isinstance(current_boot_id, str) or not current_boot_id.strip():
          raise ValueError("current Linux boot ID is unavailable")
        store_provisional_yuma_boot_disable_state(
          current_boot_id,
          PROVISIONAL_YUMA_DISABLE_REASON_VALIDATION_DISAGREES,
        )
      except Exception:
        cloudlog.exception(
          "Failed to persist provisional YUMA boot disable state"
        )
    self.persist_provisional_telemetry(
      "independent_validation",
      now=time.monotonic(),
      validation=validation,
    )
    cloudlog.info(", ".join((
      "GPS provisional YUMA independent validation",
      f"status={validation.status.value}",
      f"reason={validation.reason}",
      f"validation_source={validation.validation_source.value}",
      "candidate_error_seconds="
      + (
        str(validation.candidate_error_seconds)
        if validation.candidate_error_seconds is not None
        else "none"
      ),
      "allowed_error_seconds="
      + (
        str(validation.allowed_error_seconds)
        if validation.allowed_error_seconds is not None
        else "none"
      ),
      "disabled_for_boot="
      + str(self._provisional_disabled_for_boot).lower(),
    )))

  def evaluate_provisional(
    self,
    send_message: Callable[[bytes], None],
    *,
    now: float,
    reliable_fix_available: bool,
    database_restore_pending: bool = False,
  ) -> ProvisionalYumaTransmissionOutcome | None:
    if not isinstance(reliable_fix_available, bool):
      raise ValueError(
        "reliable_fix_available must be a bool"
      )
    if not isinstance(database_restore_pending, bool):
      raise ValueError("database_restore_pending must be a bool")
    enabled = self._refresh_enabled(now)
    reference = self._provisional_reference
    if reliable_fix_available and reference is not None:
      self._provisional_attempted = True
      self._provisional_reference = None
      return None
    if (
      not enabled
      or reference is None
      or self._provisional_disabled_for_boot
      or self._provisional_attempted
      or self._cycle_injection_consumed
      or self._time_anchor_utc is not None
      or database_restore_pending
    ):
      return None

    self._provisional_attempted = True
    self._provisional_reference = None
    outcome = transmit_provisional_yuma_reference(
      reference,
      send_message,
    )
    if yuma_assistance_state_unavailable_outcome(outcome):
      if outcome.receiver_write_attempted:
        self._provisional_reference_used = reference
      self._cycle_injection_consumed = True
      self._runtime = None
    elif outcome.receiver_write_attempted:
      self._provisional_reference_used = reference
      self._cycle_injection_consumed = True
      self._runtime = None
    return outcome

  def _refresh_enabled(
    self,
    now: float,
    *,
    force: bool = False,
  ) -> bool:
    if (
      not force
      and now < self._next_param_check
    ):
      return (
        self._enabled is True
        and self._runtime is not None
      )

    enabled = public_yuma_almanac_enabled(self._params)
    self._next_param_check = (
      now + PUBLIC_YUMA_ALMANAC_PARAM_POLL_SECONDS
    )

    if enabled != self._enabled:
      cloudlog.info(f"GPS public YUMA feature, enabled={str(enabled).lower()}")

    self._enabled = enabled

    if not enabled:
      self._queue_cancellation(
        now,
        YumaSupplementationReason.FEATURE_DISABLED,
      )
      self._runtime = None
      return False

    if self._cycle_injection_consumed and self._runtime is None:
      return False

    if self._runtime is None:
      self._runtime = create_yuma_supplementation_runtime(
        self._initialization,
        started_at=now,
        time_anchor_utc=self._time_anchor_utc,
        time_anchor_monotonic=(
          self._time_anchor_monotonic
        ),
        time_anchor_source=self._time_anchor_source,
      )

    return True

  def evaluate(
    self,
    send_message: Callable[[bytes], None],
    *,
    now: float,
    nav_sat: NavSatQuality | None,
    nav_sat_time: float | None,
    reliable_fix_available: bool,
  ) -> YumaSupplementationRuntimeOutcome | None:
    enabled = self._refresh_enabled(now)
    if self._pending_outcomes:
      return self._pending_outcomes.popleft()
    if not enabled:
      return None

    assert self._runtime is not None
    outcome = self._runtime.evaluate(
      send_message,
      now=now,
      nav_sat=nav_sat,
      nav_sat_time=nav_sat_time,
      reliable_fix_available=reliable_fix_available,
    )
    if outcome is None:
      return None
    if outcome.transmission_attempt > 0:
      self._cycle_injection_consumed = True
    contextualized = self._contextualize_outcome(outcome)
    if yuma_assistance_state_unavailable_outcome(contextualized):
      self._cycle_injection_consumed = True
      self._runtime = None
      return replace(
        contextualized,
        terminal=True,
        retry_pending=False,
      )
    return contextualized


def log_provisional_yuma_reference_decision(
  decision: ProvisionalYumaReferenceDecision,
  *,
  accepted: bool,
) -> None:
  reference = decision.reference
  cloudlog.info(", ".join((
    "GPS provisional YUMA reference",
    f"eligible={str(decision.eligible).lower()}",
    f"accepted={str(accepted).lower()}",
    "rejection="
    + (
      decision.rejection.value
      if decision.rejection is not None
      else "none"
    ),
    "receiver_cycle="
    + (str(reference.receiver_cycle) if reference is not None else "none"),
    "reference_utc="
    + (reference.utc.isoformat() if reference is not None else "none"),
    "uncertainty_seconds="
    + (str(reference.uncertainty_seconds) if reference is not None else "none"),
    "anchor_generation="
    + (reference.anchor_generation if reference is not None else "none"),
    "anchor_sequence="
    + (str(reference.anchor_sequence) if reference is not None else "none"),
    "use=yuma_reference_only",
    "time_assistance_written=false",
    "cache_quality_changed=false",
  )))


def log_provisional_yuma_outcome(
  outcome: ProvisionalYumaTransmissionOutcome,
) -> None:
  result = outcome.transmit_result
  fields = [
    "GPS provisional YUMA transmission",
    f"receiver_cycle={outcome.reference.receiver_cycle}",
    f"reference_utc={outcome.reference.utc.isoformat()}",
    f"uncertainty_seconds={outcome.reference.uncertainty_seconds}",
    f"anchor_generation={outcome.reference.anchor_generation}",
    f"anchor_sequence={outcome.reference.anchor_sequence}",
    f"rtc_elapsed_seconds={outcome.reference.rtc_elapsed_seconds}",
    "selected_prns=" + _format_yuma_prns(outcome.satellite_ids),
    "snapshot_sha256=" + (outcome.snapshot_sha256 or "none"),
    "validated_reference_utc="
    + (
      outcome.validated_reference_utc.isoformat()
      if outcome.validated_reference_utc is not None
      else "none"
    ),
    f"elapsed_ms={outcome.elapsed_ms}",
    "receiver_write_attempted="
    + str(outcome.receiver_write_attempted).lower(),
    "error=" + (outcome.error or "none"),
    f"time_assistance_written={str(outcome.time_assistance_written).lower()}",
    f"cache_quality_changed={str(outcome.cache_quality_changed).lower()}",
    f"anchor_written={str(outcome.anchor_written).lower()}",
    f"system_clock_changed={str(outcome.system_clock_changed).lower()}",
    f"receiver_reset={str(outcome.receiver_reset).lower()}",
  ]
  if result is not None:
    fields.extend((
      f"transmit_status={result.status.value}",
      "requested_prns=" + _format_yuma_prns(result.requested_satellite_ids),
      "attempted_prns=" + _format_yuma_prns(result.attempted_satellite_ids),
      "accepted_prns=" + _format_yuma_prns(result.accepted_satellite_ids),
      "failed_prns=" + _format_yuma_prns(result.failed_satellite_ids),
      "rejected_prns=" + _format_yuma_prns(result.rejected_satellite_ids),
      "timed_out_prns=" + _format_yuma_prns(result.timed_out_satellite_ids),
      "deferred_prns=" + _format_yuma_prns(result.deferred_satellite_ids),
    ))
  cloudlog.info(", ".join(fields))


def _format_yuma_prns(
  satellite_ids: tuple[int, ...] | frozenset[int],
) -> str:
  return (
    ",".join(str(value) for value in satellite_ids)
    or "none"
  )


def log_yuma_supplementation_outcome(
  outcome: YumaSupplementationRuntimeOutcome,
) -> None:
  def optional(value: object | None) -> str:
    return "none" if value is None else str(value)

  def timestamp(value: datetime | None) -> str:
    return "none" if value is None else value.isoformat()

  database_state = (
    outcome.database_state.value
    if outcome.database_state is not None
    else "unknown"
  )
  fields = [
    "GPS public YUMA supplementation",
    "quality_evaluation_stage=yuma_runtime",
    f"enabled={str(outcome.feature_enabled).lower()}",
    f"terminal={str(outcome.terminal).lower()}",
    f"retry_pending={str(outcome.retry_pending).lower()}",
    "time_anchor_source=" + optional(outcome.time_anchor_source),
    f"trusted_now_utc={timestamp(outcome.trusted_now_utc)}",
    "trusted_time_wait_expired="
    + str(outcome.trusted_time_wait_expired).lower(),
    "cache_wait_expired="
    + str(outcome.cache_wait_expired).lower(),
    "nav_sat_observation_expired="
    + str(outcome.nav_sat_observation_expired).lower(),
    f"dbd_state={database_state}",
    f"dbd_age_seconds={optional(outcome.database_age_seconds)}",
    "restored_cache_age_evidence="
    + optional(outcome.restored_cache_age_evidence),
    "restored_cache_age_verified="
    + optional(outcome.restored_cache_age_verified),
    "captured_gps_ephemeris_available="
    + optional(outcome.captured_gps_ephemeris_available),
    "captured_glonass_ephemeris_available="
    + optional(outcome.captured_glonass_ephemeris_available),
    "captured_gps_startup_ready="
    + optional(outcome.captured_gps_startup_ready),
    "restored_cache_generation="
    + optional(outcome.restored_cache_generation),
    "restored_cache_selection_reason="
    + optional(outcome.restored_cache_selection_reason),
    "restored_gps_almanac_available="
    + optional(outcome.restored_gps_almanac_available),
    "restored_glonass_almanac_available="
    + optional(outcome.restored_glonass_almanac_available),
    "restored_gps_ephemeris_available="
    + optional(outcome.restored_gps_ephemeris_available),
    "restored_glonass_ephemeris_available="
    + optional(outcome.restored_glonass_ephemeris_available),
    "effective_gps_ephemeris_available="
    + optional(outcome.restored_gps_ephemeris_available),
    "effective_glonass_ephemeris_available="
    + optional(outcome.restored_glonass_ephemeris_available),
    "restored_satellites_used="
    + optional(outcome.restored_satellites_used),
    "restored_gps_startup_ready="
    + optional(outcome.restored_gps_startup_ready),
    "effective_gps_startup_ready="
    + optional(outcome.restored_gps_startup_ready),
    "restored_gps_almanac_satellite_ids="
    + optional(outcome.restored_gps_almanac_satellite_ids),
    "runtime_elapsed_seconds="
    + optional(outcome.runtime_elapsed_seconds),
    "time_anchor_elapsed_seconds="
    + optional(outcome.time_anchor_elapsed_seconds),
    "decision_ready_elapsed_seconds="
    + optional(outcome.decision_ready_elapsed_seconds),
    "nav_sat_observed_elapsed_seconds="
    + optional(outcome.nav_sat_observed_elapsed_seconds),
    "nav_sat_wait_seconds="
    + optional(outcome.nav_sat_wait_seconds),
    "completion_elapsed_seconds="
    + optional(outcome.completion_elapsed_seconds),
    "completion_monotonic="
    + optional(outcome.completion_monotonic),
    "completion_utc=" + timestamp(outcome.completion_utc),
    "gnss_start_sent_at_monotonic="
    + optional(outcome.gnss_start_sent_at_monotonic),
    "completed_before_gnss_start="
    + optional(outcome.completed_before_gnss_start),
    "yuma_snapshot_sha256="
    + optional(outcome.yuma_snapshot_sha256),
    "cancellation_reason="
    + optional(outcome.cancellation_reason),
    f"action={outcome.plan.action.value}",
    f"reason={outcome.plan.reason.value}",
    "selected_prns="
    + _format_yuma_prns(outcome.plan.satellite_ids),
    "unavailable_plan_prns="
    + _format_yuma_prns(
      outcome.plan.unavailable_satellite_ids
    ),
    f"yuma_reference_utc={timestamp(outcome.yuma_reference_utc)}",
    "yuma_reference_age_seconds="
    + optional(outcome.yuma_reference_age_seconds),
    f"downloaded_at_utc={timestamp(outcome.downloaded_at_utc)}",
    f"cache_error={optional(outcome.cache_error)}",
    f"transmission_attempt={outcome.transmission_attempt}",
    f"attempt_history_count={len(outcome.attempt_history)}",
    "transmission_elapsed_ms="
    + optional(outcome.transmission_elapsed_ms),
  ]
  if outcome.transmit_result is not None:
    result = outcome.transmit_result
    fields.extend((
      f"transmit_status={result.status.value}",
      "requested_prns="
      + _format_yuma_prns(
        getattr(result, "requested_satellite_ids", ())
      ),
      "attempted_prns="
      + _format_yuma_prns(
        result.attempted_satellite_ids
      ),
      "accepted_prns="
      + _format_yuma_prns(
        result.accepted_satellite_ids
      ),
      "failed_prns="
      + _format_yuma_prns(
        result.failed_satellite_ids
      ),
      "rejected_prns="
      + _format_yuma_prns(
        getattr(result, "rejected_satellite_ids", ())
      ),
      "timed_out_prns="
      + _format_yuma_prns(
        getattr(result, "timed_out_satellite_ids", ())
      ),
      "deferred_prns="
      + _format_yuma_prns(
        result.deferred_satellite_ids
      ),
      "unavailable_cache_prns="
      + _format_yuma_prns(
        result.unavailable_satellite_ids
      ),
    ))
  if outcome.error is not None:
    fields.append(f"error={outcome.error}")
  cloudlog.info(", ".join(fields))


def read_param_text_compat(
  params: Params,
  key: str,
) -> str | None:
  try:
    value = params.get(key)
  except TypeError:
    value = params.get(key, "utf-8")
  if value is None:
    return None
  if isinstance(value, bytes):
    return value.decode("utf-8")
  return str(value)


def persist_yuma_supplementation_outcome(
  outcome: YumaSupplementationRuntimeOutcome,
  params: Params,
) -> None:
  try:
    commit = read_param_text_compat(params, "GitCommit")
  except Exception:
    cloudlog.exception(
      "GPS public YUMA commit metadata unavailable"
    )
    commit = None

  try:
    save_yuma_supplementation_outcome(
      YUMA_LAST_OUTCOME_PATH,
      outcome,
      commit=commit,
      receiver_cycle=outcome.receiver_cycle,
      recorded_at_utc=(
        outcome.completion_utc or outcome.trusted_now_utc
      ),
    )
  except Exception:
    cloudlog.exception(
      "GPS public YUMA outcome persistence failed"
    )


class ReceiverCyclePersistenceSupersededError(RuntimeError):
  pass


class ReceiverCyclePersistenceOwnership:
  """Prevent superseded receiver cycles from committing shared assistance state."""

  def __init__(self) -> None:
    self._generation = 0
    self._commit_lock = Lock()

  def begin_cycle(self) -> int:
    # The lock only serializes ownership changes against the short final commit.
    # Slow JSON serialization/fsync happens outside this lock on a private path.
    with self._commit_lock:
      self._generation += 1
      return self._generation

  def guarded_state_storer(
    self,
    generation: int,
    state_storer: Callable[[Any, Path], None],
  ) -> Callable[[Any, Path], None]:
    if not callable(state_storer):
      raise ValueError("state_storer must be callable")

    def store_if_current(state: Any, path: Path) -> None:
      staging_path = path.with_name(
        f".{path.name}.receiver-cycle-{generation}.staged"
      )
      try:
        # Potentially slow serialization/fsync goes only to this cycle's
        # private staging path and never holds the ownership lock.
        state_storer(state, staging_path)
        with self._commit_lock:
          if generation != self._generation:
            raise ReceiverCyclePersistenceSupersededError(
              f"receiver cycle {generation} was superseded by {self._generation}"
            )
          os.replace(staging_path, path)
          directory_descriptor = os.open(path.parent, os.O_RDONLY)
          try:
            os.fsync(directory_descriptor)
          finally:
            os.close(directory_descriptor)
      finally:
        staging_path.unlink(missing_ok=True)

    return store_if_current

  def guarded_state_quarantiner(
    self,
    generation: int,
    state_quarantiner: Callable[[Path, str], Path],
  ) -> Callable[[Path, str], Path]:
    if not callable(state_quarantiner):
      raise ValueError("state_quarantiner must be callable")

    def quarantine_if_current(path: Path, boot_id: str) -> Path:
      # Quarantine directly renames the live file, so serialize the ownership
      # check with that short rename/fsync operation.
      with self._commit_lock:
        if generation != self._generation:
          raise ReceiverCyclePersistenceSupersededError(
            f"receiver cycle {generation} was superseded by {self._generation}"
          )
        return state_quarantiner(path, boot_id)

    return quarantine_if_current


@dataclass
class ReceiverAcquisitionStateGuard:
  handled: bool = False

  def note_once(
    self,
    navigation_database_runtime: NavigationDatabaseRestoreRuntime,
  ) -> bool | None:
    if self.handled:
      return None
    self.handled = True
    if navigation_database_runtime.acquisition_started:
      return True
    if navigation_database_runtime.database_restore_pending:
      return navigation_database_runtime.note_early_acquisition_started()
    return navigation_database_runtime.note_acquisition_started()


def handle_receiver_acquisition_state(
  navigation_database_runtime: NavigationDatabaseRestoreRuntime,
  position_assistance_retry: PositionAssistancePostStartRetryController,
  guard: ReceiverAcquisitionStateGuard,
) -> None:
  result = guard.note_once(navigation_database_runtime)
  if result is not False:
    return
  cloudlog.error(
    "GPS acquisition latch persistence failed; assistance "
    + "disabled while receiver processing continues"
  )
  position_assistance_retry.runtime = None


def create_receiver_cycle_navigation_state(
  receiver_fingerprint: str,
  *,
  state_storer: Callable[[Any, Path], None] | None = None,
  state_quarantiner: Callable[[Path, str], Path] | None = None,
) -> NavigationDatabaseRestoreRuntime:
  try:
    runtime_kwargs: dict[str, Any] = {}
    if state_storer is not None:
      runtime_kwargs["state_storer"] = state_storer
    if state_quarantiner is not None:
      runtime_kwargs["state_quarantiner"] = state_quarantiner
    return NavigationDatabaseRestoreRuntime(
      receiver_fingerprint,
      new_receiver_cycle=True,
      **runtime_kwargs,
    )
  except NavigationDatabaseRestoreInitializationError as exc:
    cloudlog.exception(
      "GPS assistance state unavailable; assistance disabled while "
      + "GNSS START continues"
    )
    return NavigationDatabaseRestoreUnavailableRuntime(
      receiver_fingerprint,
      str(exc),
    )

def device_network_available(
  sm: messaging.SubMaster,
) -> bool | None:
  try:
    sm.update(0)
    if not sm.alive["deviceState"] or not sm.valid["deviceState"]:
      return None
    return (
      sm["deviceState"].networkType
      != log.DeviceState.NetworkType.none
    )
  except Exception:
    return None


def run_receiving(duration: int = 0):
  diagnostic_process_start_time = time.monotonic()
  startup_diagnostics = GpsStartupDiagnostics(
    diagnostic_process_start_time
  )
  pm = messaging.PubMaster(['ubloxRaw'])
  sm = messaging.SubMaster(['deviceState'])

  params = Params()
  fingerprint_holder = {
    "value": gps_assistance_receiver_fingerprint(params, None),
  }
  receiver_fingerprint = fingerprint_holder["value"]

  receiver_cycle_persistence = ReceiverCyclePersistenceOwnership()

  def create_receiver_cycle_assistance_state(
    persistence_generation: int,
  ) -> tuple[
    NavigationDatabaseRestoreRuntime,
    PositionAssistancePostStartRetryController,
    ReceiverAcquisitionStateGuard,
  ]:
    active_init = _ACTIVE_PRE_ACQUISITION_INITIALIZATION
    mon_ver = (
      active_init.transport_mon_ver_info
      if active_init is not None
      else None
    )
    if mon_ver is not None:
      active_fingerprint = gps_assistance_receiver_fingerprint(params, mon_ver)
      fingerprint_holder["value"] = active_fingerprint
    else:
      active_fingerprint = fingerprint_holder["value"]
    navigation_database_runtime = create_receiver_cycle_navigation_state(
      active_fingerprint,
      state_storer=receiver_cycle_persistence.guarded_state_storer(
        persistence_generation,
        store_navigation_database_restore_boot_state,
      ),
      state_quarantiner=receiver_cycle_persistence.guarded_state_quarantiner(
        persistence_generation,
        quarantine_navigation_database_restore_boot_state,
      ),
    )
    if navigation_database_runtime.state_available:
      try:
        position_assistance_retry_runtime = PositionAssistanceRetryRuntime(
          active_fingerprint,
          state_storer=receiver_cycle_persistence.guarded_state_storer(
            persistence_generation,
            store_position_assistance_retry_state,
          ),
          new_receiver_cycle=True,
        )
      except Exception:
        cloudlog.exception(
          "GPS position assistance retry state unavailable"
        )
        position_assistance_retry_runtime = None
    else:
      position_assistance_retry_runtime = None
    return (
      navigation_database_runtime,
      PositionAssistancePostStartRetryController(
        position_assistance_retry_runtime
      ),
      ReceiverAcquisitionStateGuard(),
    )

  def new_receiver_cycle_assistance_state_factory() -> Callable[[], tuple[
    NavigationDatabaseRestoreRuntime,
    PositionAssistancePostStartRetryController,
    ReceiverAcquisitionStateGuard,
  ]]:
    # Allocate ownership synchronously on the main receiver thread. A worker
    # that was queued by an older cycle can never become the new owner later.
    persistence_generation = receiver_cycle_persistence.begin_cycle()
    return partial(
      create_receiver_cycle_assistance_state,
      persistence_generation,
    )

  fix_tracker = ReliableFixTracker()
  capture_quality_tracker = CaptureQualityTracker()
  dump_collector = NavigationDatabaseDumpCollector()
  # Automatic UPD-SOS backup creation is disabled for this receiver.
  # The M8 HPG 1.40 ROV firmware consistently rejects the command,
  # including after a controlled GNSS stop. Startup restore-status
  # polling remains unchanged.
  autonomous_orbit_diagnostics = AutonomousOrbitDiagnostics()
  capture_state = NavigationCaptureState()
  completed_databases: deque[tuple[bytes, ...]] = deque()
  receiver_time_provenance = (
    ReceiverTimeProvenanceTracker()
  )
  process_start_cycle_started_at = time.monotonic()
  active_frame_dispatcher: (
    Callable[[list[bytes]], None] | None
  ) = None

  def dispatch_receiver_frames(frames: list[bytes]) -> None:
    if active_frame_dispatcher is not None:
      active_frame_dispatcher(frames)

  pigeon = TTYPigeon(
    lambda data: publish_ublox_raw(pm, data),
    dispatch_receiver_frames,
  )
  process_start_transport_bootstrap_supported = (
    supports_process_start_transport_bootstrap(pigeon)
  )
  if process_start_transport_bootstrap_supported:
    pigeon.set_frame_dispatcher(None)
  process_start_mon_ver_info = (
    bootstrap_process_start_transport(pigeon)
    if process_start_transport_bootstrap_supported
    else None
  )
  fingerprint_holder["value"] = gps_assistance_receiver_fingerprint(
    params,
    process_start_mon_ver_info,
  )
  receiver_fingerprint = fingerprint_holder["value"]
  navigation_database_runtime: (
    NavigationDatabaseRestoreRuntime | None
  ) = None
  position_assistance_retry = (
    PositionAssistancePostStartRetryController(None)
  )
  acquisition_state_guard = ReceiverAcquisitionStateGuard()

  def activate_receiver_cycle_assistance_state(
    runtime: NavigationDatabaseRestoreRuntime,
    retry: PositionAssistancePostStartRetryController,
    guard: ReceiverAcquisitionStateGuard,
  ) -> None:
    nonlocal navigation_database_runtime
    nonlocal position_assistance_retry
    nonlocal acquisition_state_guard
    navigation_database_runtime = runtime
    position_assistance_retry = retry
    acquisition_state_guard = guard

  def dispatch_frames(frames: list[bytes]) -> None:
    if (
      navigation_database_runtime is not None
      and receiver_frames_show_gnss_acquisition(frames)
    ):
      handle_receiver_acquisition_state(
        navigation_database_runtime,
        position_assistance_retry,
        acquisition_state_guard,
      )
    frame_time = time.monotonic()
    completed = process_receiver_frames(
      frames,
      frame_time,
      startup_diagnostics,
      fix_tracker,
      capture_quality_tracker,
      autonomous_orbit_diagnostics,
      dump_collector,
      capture_state,
      receiver_time_provenance,
    )
    if completed is not None:
      completed_databases.append(completed)
    position_assistance_retry.observe_frames(
      frames,
      frame_time,
      getattr(pigeon, "receiver_cycle", 0),
    )

  if process_start_transport_bootstrap_supported:
    pigeon.set_frame_dispatcher(dispatch_frames)
  else:
    active_frame_dispatcher = dispatch_frames

  def execute_position_assistance_retry() -> None:
    position_assistance_retry.execute_ready(
      lambda message: send_mga_with_strict_ack(pigeon, message),
    )


  def send_yuma_message(message: bytes) -> None:
    if navigation_database_runtime is None:
      raise RuntimeError("receiver assistance state is not initialized")
    send_yuma_with_durable_claim(
      navigation_database_runtime,
      lambda claimed_message: send_mga_with_strict_ack(
        pigeon, claimed_message
      ),
      message,
    )
  def reject_live_database_write(
    _message: bytes,
    _frame_index: int,
    _mark_write_attempt: Callable[[], None],
  ) -> None:
    raise RuntimeError("DBD restore is restricted to pre-acquisition initialization")
  time_authority = TimeAuthority()
  rtc_observer = (
    time_authority.create_cross_boot_rtc_observer()
  )
  cycle_initialization = initialize_receiver_cycle(
    pigeon,
    receiver_fingerprint,
    startup_diagnostics,
    "process_start",
    collect_mon_ver_diagnostics=True,
    time_authority=time_authority,
    time_provenance=receiver_time_provenance,
    position_assistance_retry=position_assistance_retry,
    transport_mon_ver_info=process_start_mon_ver_info,
    cycle_started_at=process_start_cycle_started_at,
    network_available=device_network_available(sm),
    assistance_state_factory=new_receiver_cycle_assistance_state_factory(),
    assistance_state_ready_callback=(
      activate_receiver_cycle_assistance_state
    ),
  )
  if cycle_initialization.mon_ver_info is not None:
    fingerprint_holder["value"] = gps_assistance_receiver_fingerprint(
      params,
      cycle_initialization.mon_ver_info,
    )
    receiver_fingerprint = fingerprint_holder["value"]
  deferred_assistance_poll = (
    cycle_initialization.poll_deferred_assistance_state
  )
  execute_position_assistance_retry()
  startup_diagnostics.initialization_complete(
    cycle_initialization.completed_at
  )
  yuma_feature = YumaSupplementationFeature(
    params,
    cycle_initialization,
    getattr(pigeon, "receiver_cycle", 0),
  )

  stream_parser = UbxStreamParser()
  started_state: bool | None = None

  start_time = time.monotonic()
  next_time_assistance_attempt = (
    cycle_initialization.next_time_assistance_attempt
  )
  trusted_time_assistance_sent = (
    cycle_initialization.trusted_time_assistance_sent
  )
  latest_cross_boot_rtc_observation: (
    CrossBootRtcObservation | None
  ) = None
  latest_independent_time = independent_time_observation(
    getattr(cycle_initialization, "authorized_time", None)
  )
  host_time_state = host_time_processing_state(
    getattr(
      cycle_initialization,
      "host_time_observation",
      None,
    ),
    getattr(
      cycle_initialization,
      "authority_evaluation",
      None,
    ),
    now=start_time,
  )
  latest_authority_evaluation: TimeAuthorityEvaluation | None = getattr(
    cycle_initialization,
    "authority_evaluation",
    None,
  )
  receiver_self_time_cycle: int | None = None
  mon_ver_info = cycle_initialization.mon_ver_info
  assistnow_autonomous_configuration_attempted = (
    cycle_initialization.assistnow_autonomous_configuration_attempted
  )
  assistnow_autonomous_supported = (
    cycle_initialization.assistnow_autonomous_supported
  )
  data_watchdog = UbloxDataWatchdog()

  def recover_receiver(
    reason: ReceiverRecoveryReason,
    requested_at: float,
  ) -> None:
    nonlocal navigation_database_runtime
    nonlocal position_assistance_retry
    nonlocal acquisition_state_guard
    nonlocal trusted_time_assistance_sent
    nonlocal next_time_assistance_attempt
    nonlocal mon_ver_info
    nonlocal assistnow_autonomous_configuration_attempted
    nonlocal assistnow_autonomous_supported
    nonlocal latest_independent_time
    nonlocal host_time_state
    nonlocal latest_authority_evaluation
    nonlocal deferred_assistance_poll
    nonlocal receiver_fingerprint

    reason_value = reason.value
    attempt = data_watchdog.recoveries
    cloudlog.warning(", ".join((
      "GPS receiver recovery started",
      f"reason={reason_value}",
      f"attempt={attempt}",
      f"max_attempts={data_watchdog.max_recoveries}",
      (
        "cooldown_seconds="
        + f"{data_watchdog.recovery_cooldown_seconds:.1f}"
      ),
      (
        "healthy_rearm_seconds="
        + f"{data_watchdog.healthy_rearm_seconds:.1f}"
      ),
    )))

    position_assistance_retry.cancel_receiver_cycle(
      requested_at
    )
    prepare_receiver_cycle_response_state(pigeon)
    navigation_database_runtime = None
    position_assistance_retry = (
      PositionAssistancePostStartRetryController(None)
    )
    acquisition_state_guard = ReceiverAcquisitionStateGuard()
    cycle_initialization = initialize_receiver_cycle(
      pigeon,
      receiver_fingerprint,
      startup_diagnostics,
      reason_value,
      time_authority=time_authority,
      time_provenance=receiver_time_provenance,
      position_assistance_retry=position_assistance_retry,
      assistance_state_factory=new_receiver_cycle_assistance_state_factory(),
      assistance_state_ready_callback=(
        activate_receiver_cycle_assistance_state
      ),
    )
    deferred_assistance_poll = (
      cycle_initialization.poll_deferred_assistance_state
    )
    execute_position_assistance_retry()
    initialization_completed_at = (
      cycle_initialization.completed_at
    )

    stream_parser.reset()
    fix_tracker.reset()
    capture_quality_tracker.reset()
    dump_collector.cancel()
    capture_state.reset_receiver_cycle()

    trusted_time_assistance_sent = (
      cycle_initialization.trusted_time_assistance_sent
    )
    next_time_assistance_attempt = (
      cycle_initialization.next_time_assistance_attempt
    )
    mon_ver_info = cycle_initialization.mon_ver_info
    if mon_ver_info is not None:
      fingerprint_holder["value"] = gps_assistance_receiver_fingerprint(
        params,
        mon_ver_info,
      )
      receiver_fingerprint = fingerprint_holder["value"]
    assistnow_autonomous_configuration_attempted = (
      cycle_initialization.assistnow_autonomous_configuration_attempted
    )
    assistnow_autonomous_supported = (
      cycle_initialization.assistnow_autonomous_supported
    )
    autonomous_orbit_diagnostics.logged_state_mask = 0
    data_watchdog.recovery_completed(time.monotonic())
    startup_diagnostics.initialization_complete(
      initialization_completed_at
    )
    yuma_feature.reset_receiver_cycle(
      cycle_initialization,
      getattr(pigeon, "receiver_cycle", 0),
    )
    cycle_independent_time = independent_time_observation(
      getattr(cycle_initialization, "authorized_time", None)
    )
    if cycle_independent_time is not None:
      latest_independent_time = cycle_independent_time
    host_time_state = host_time_processing_state(
      getattr(
        cycle_initialization,
        "host_time_observation",
        None,
      ),
      getattr(
        cycle_initialization,
        "authority_evaluation",
        None,
      ),
      now=time.monotonic(),
    )
    latest_authority_evaluation = getattr(
      cycle_initialization,
      "authority_evaluation",
      None,
    )
    cloudlog.info(", ".join((
      "GPS receiver recovery completed",
      f"reason={reason_value}",
      f"attempt={attempt}",
      (
        "receiver_cycle="
        + str(getattr(pigeon, "receiver_cycle", 0))
      ),
    )))

  cloudlog.info(", ".join((
    "GPS navigation assistance quality policy",
    f"reliable_fix_seconds={MINIMUM_RELIABLE_FIX_SECONDS:.0f}",
    f"gps_ephemeris={MINIMUM_GPS_EPHEMERIS}",
    f"glonass_ephemeris={MINIMUM_GLONASS_EPHEMERIS}",
    f"total_ephemeris={MINIMUM_TOTAL_EPHEMERIS}",
    f"satellites_used={MINIMUM_SATELLITES_USED}",
    f"orbit_quality_seconds={MINIMUM_ORBIT_QUALITY_SECONDS:.0f}",
    f"nav_pvt_max_gap_seconds={MAXIMUM_NAV_PVT_GAP_SECONDS:.0f}",
    f"nav_sat_max_age_seconds={MAXIMUM_NAV_SAT_AGE_SECONDS:.0f}",
  )))

  while (
    duration == 0
    or time.monotonic() - start_time < duration
  ):
    now = time.monotonic()
    if deferred_assistance_poll is not None:
      deferred_result = deferred_assistance_poll()
      if deferred_result is not None:
        deferred_assistance_poll = None
        yuma_feature.update_navigation_assistance_restore_result(
          deferred_result,
          now,
        )
        execute_position_assistance_retry()
    authority_evaluation_for_loop: (
      TimeAuthorityEvaluation | None
    ) = None
    changed_rtc_observation = (
      rtc_observer.changed_observation(now)
    )
    if changed_rtc_observation is not None:
      log_cross_boot_rtc_observation(
        changed_rtc_observation
      )
      yuma_feature.persist_provisional_telemetry(
        "rtc_observation",
        now=now,
        observation=changed_rtc_observation,
        authority=latest_authority_evaluation,
      )
      if (
        changed_rtc_observation.state
        is RtcObservationState.OBSERVED
      ):
        latest_cross_boot_rtc_observation = (
          changed_rtc_observation
        )
        if latest_independent_time is not None:
          validation = validate_observed_cross_boot_rtc(
            latest_cross_boot_rtc_observation,
            latest_independent_time,
          )
          yuma_feature.note_cross_boot_validation(validation)
        authority_for_provisional = (
          latest_authority_evaluation
          or evaluate_time_authority(
            time_authority,
            read_host_time_observation(),
          )
        )
        latest_authority_evaluation = authority_for_provisional
        provisional_decision = evaluate_provisional_yuma_reference(
          latest_cross_boot_rtc_observation,
          authority_for_provisional,
          receiver_cycle=getattr(pigeon, "receiver_cycle", 0),
        )
        provisional_accepted = (
          provisional_decision.reference is not None
          and yuma_feature.set_provisional_reference(
            provisional_decision.reference
          )
        )
        log_provisional_yuma_reference_decision(
          provisional_decision,
          accepted=provisional_accepted,
        )
        yuma_feature.persist_provisional_telemetry(
          "reference_decision",
          now=now,
          observation=changed_rtc_observation,
          authority=authority_for_provisional,
          decision=provisional_decision,
          accepted=provisional_accepted,
        )
    sm.update(0)

    host_time_observation = read_host_time_observation()
    if host_time_requires_processing(
      host_time_state,
      host_time_observation,
      now=now,
    ):
      authority_evaluation_for_loop = evaluate_time_authority(
        time_authority,
        host_time_observation,
      )
      latest_authority_evaluation = authority_evaluation_for_loop
      host_time_state = host_time_processing_state(
        host_time_observation,
        authority_evaluation_for_loop,
        now=now,
      )
      host_authorized = (
        authority_evaluation_for_loop.authorized_time
      )
      host_independent = (
        independent_time_observation(host_authorized)
        if (
          host_time_observation is not None
          and host_time_observation.independent
        )
        else None
      )
      if host_independent is not None:
        latest_independent_time = host_independent
        yuma_feature.set_time_anchor(
          host_independent.utc,
          now,
          host_independent.source.value,
        )
        validation = validate_observed_cross_boot_rtc(
          latest_cross_boot_rtc_observation,
          host_independent,
        )
        yuma_feature.note_cross_boot_validation(validation)
        correction_context = (
          startup_diagnostics.time_assistance_context(
            now
          )
        )
        correction_decision, correction_accepted = (
          maybe_send_receiver_time_correction(
            pigeon,
            receiver_time_provenance,
            host_independent,
            diagnostic_context=correction_context,
          )
        )
        if (
          correction_decision.should_correct
          and receiver_time_provenance.correction_written
        ):
          trusted_time_assistance_sent = True
          next_time_assistance_attempt = (
            now + TIME_ASSISTANCE_RETRY_INTERVAL
          )
          if (
            correction_accepted
            and not (
              assistnow_autonomous_configuration_attempted
            )
          ):
            configure_assistnow_autonomous(
              pigeon,
              mon_ver_info,
            )
            assistnow_autonomous_configuration_attempted = True

    if sm.updated['deviceState']:
      current_started = sm['deviceState'].started

      if started_state is None:
        started_state = current_started

      elif current_started != started_state:
        if current_started and dump_collector.active:
          dump_collector.cancel()
        capture_state.road_state_changed(current_started)
        if current_started:
          cloudlog.info(
            "GPS assistance drive tracking started"
          )
        else:
          cloudlog.info(
            "GPS assistance post-drive refresh requested"
          )
          readiness_message = capture_state.drive_end_readiness_message()
          if readiness_message is not None:
            cloudlog.info(readiness_message)

        started_state = current_started

    if (
      not trusted_time_assistance_sent
      and now >= next_time_assistance_attempt
    ):
      authority_evaluation = (
        authority_evaluation_for_loop
        or evaluate_time_authority(
          time_authority,
          read_host_time_observation(),
        )
      )
      latest_authority_evaluation = authority_evaluation
      authorized_time = authority_evaluation.authorized_time
      if authorized_time is not None:
        receiver_self_source = (
          receiver_self_time_cycle
          == receiver_time_provenance.cycle_id
          and authorized_time.source
          is TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS
          and authorized_time.evidence.value
          == "same_boot_boottime"
        )
        if receiver_self_source:
          cloudlog.info(
            "GPS time assistance suppressed: reason=receiver_self_resolved_utc"
          )
          next_time_assistance_attempt = (
            now + TIME_SYNC_CHECK_INTERVAL
          )
        else:
          diagnostic_context = (
            startup_diagnostics.time_assistance_context(
              time.monotonic()
            )
          )
          anchor_monotonic = now
          yuma_feature.set_time_anchor(
            authorized_time.utc,
            anchor_monotonic,
            authorized_time.evidence.value,
          )
          trusted_time_assistance_sent = send_time_assistance(
            pigeon,
            assistance_time=authorized_time.utc,
            accuracy_seconds=(
              authorized_time.mga_accuracy_seconds
            ),
            source=authorized_time.evidence.value,
            diagnostic_context=diagnostic_context,
            time_provenance=receiver_time_provenance,
            assistance_boottime_seconds=getattr(
              authorized_time,
              "observed_boottime_seconds",
              None,
            ),
            independent=authorized_time.independent,
            source_provenance=authorized_time.provenance,
            diagnostic_callback=(
              log_time_assistance_attempt_diagnostic
            ),
          )
          if (
            trusted_time_assistance_sent
            and not assistnow_autonomous_configuration_attempted
          ):
            configure_assistnow_autonomous(
              pigeon,
              mon_ver_info,
            )
            assistnow_autonomous_configuration_attempted = True
          next_time_assistance_attempt = (
            now + TIME_ASSISTANCE_RETRY_INTERVAL
          )
      else:
        next_time_assistance_attempt = (
          now + TIME_SYNC_CHECK_INTERVAL
        )

    startup_diagnostics.log_acquisition_status(
      time.monotonic()
    )

    raw_published_by_pigeon = hasattr(pigeon, "_stream_parser")
    try:
      if raw_published_by_pigeon:
        data, received_frames = pigeon.receive_normal()
      else:
        data = pigeon.receive()
        received_frames = stream_parser.feed(data)
    except RawPublicationError as exc:
      cloudlog.error(f"GPS raw publication deferred: {exc}")
      time.sleep(0.001)
      continue

    if not data and not received_frames:
      if not data_watchdog.check(now):
        time.sleep(0.001)
        continue

      recover_receiver(
        ReceiverRecoveryReason.NO_DATA,
        now,
      )
      continue

    all_zero_data = is_all_zero_ublox_data(data)
    if data_watchdog.note_data(
      now,
      healthy=not all_zero_data,
    ):
      cloudlog.info(", ".join((
        "GPS receiver recovery budget rearmed",
        (
          "healthy_rearm_seconds="
          + f"{data_watchdog.healthy_rearm_seconds:.1f}"
        ),
        f"max_attempts={data_watchdog.max_recoveries}",
      )))

    if data:
      if all_zero_data:
        if not data_watchdog.request_recovery(
          ReceiverRecoveryReason.ALL_ZERO_DATA,
          now,
        ):
          time.sleep(0.001)
          continue

        recover_receiver(
          ReceiverRecoveryReason.ALL_ZERO_DATA,
          now,
        )
        continue

      if not raw_published_by_pigeon:
        publish_ublox_raw(pm, data)

    dispatch_frames(received_frames)
    execute_position_assistance_retry()
    completed_database = (
      completed_databases.popleft()
      if completed_databases
      else None
    )

    now = time.monotonic()
    changed_receiver_utc = (
      receiver_time_provenance.changed_observation(now)
    )
    if changed_receiver_utc is not None:
      log_receiver_utc_observation(
        changed_receiver_utc
      )
      receiver_evaluation = (
        authorize_independent_receiver_utc(
          time_authority,
          changed_receiver_utc,
          now=now,
        )
      )
      if receiver_evaluation is not None:
        latest_authority_evaluation = receiver_evaluation
      if (
        receiver_evaluation is not None
        and receiver_evaluation.authorized_time is not None
      ):
        receiver_independent = independent_time_observation(
          receiver_evaluation.authorized_time
        )
        if receiver_independent is not None:
          latest_independent_time = receiver_independent
          receiver_self_time_cycle = (
            changed_receiver_utc.cycle_id
          )
          yuma_feature.set_receiver_time_anchor(
            receiver_independent.utc,
            now,
            source=receiver_independent.source.value,
          )
          validation = validate_observed_cross_boot_rtc(
            latest_cross_boot_rtc_observation,
            receiver_independent,
          )
          yuma_feature.note_cross_boot_validation(validation)

    stable_fix = fix_tracker.stable_fix(now)
    previous_database_disposition = (
      navigation_database_runtime.controller.disposition
      if navigation_database_runtime is not None
      else None
    )
    database_execution = (
      navigation_database_runtime.evaluate(
        authorized_time=(
          latest_authority_evaluation.authorized_time
          if latest_authority_evaluation is not None
          else None
        ),
        reliable_fix_available=stable_fix is not None,
        yuma_already_sent=yuma_feature.cycle_injection_consumed,
        send_database_message=reject_live_database_write,
      )
      if navigation_database_runtime is not None
      else None
    )
    if (
      navigation_database_runtime is not None
      and database_execution is not None
      and
      navigation_database_runtime.controller.disposition
      is not previous_database_disposition
    ):
      database_result = (
        navigation_assistance_result_from_database_execution(
          database_execution
        )
      )
      log_navigation_assistance_restore_result(
        database_result,
        startup_diagnostics.time_assistance_context(now),
        (
          latest_authority_evaluation.authorized_time.evidence.value
          if (
            latest_authority_evaluation is not None
            and latest_authority_evaluation.authorized_time is not None
          )
          else None
        ),
      )
      yuma_feature.update_navigation_assistance_restore_result(
        database_result,
        now,
      )

    # Provisional YUMA has first YUMA priority only after the durable DBD
    # decision is terminal; the shared wrapper claims ownership before frame 0.
    provisional_yuma_outcome = (
      yuma_feature.evaluate_provisional(
        send_yuma_message,
        now=now,
        reliable_fix_available=stable_fix is not None,
        database_restore_pending=(
          navigation_database_runtime.database_restore_pending
        ),
      )
      if navigation_database_runtime is not None
      else None
    )
    if provisional_yuma_outcome is not None:
      log_provisional_yuma_outcome(provisional_yuma_outcome)
      yuma_feature.persist_provisional_telemetry(
        "transmission",
        now=now,
        outcome=provisional_yuma_outcome,
      )

    yuma_outcome = (
      yuma_feature.evaluate(
        send_yuma_message,
        now=now,
        nav_sat=capture_quality_tracker.latest_nav_sat,
        nav_sat_time=(
          capture_quality_tracker.latest_nav_sat_time
        ),
        reliable_fix_available=stable_fix is not None,
      )
      if navigation_database_runtime is not None
      else None
    )
    if yuma_outcome is not None:
      log_yuma_supplementation_outcome(yuma_outcome)
      persist_yuma_supplementation_outcome(
        yuma_outcome,
        params,
      )

    if dump_collector.expired(now):
      cloudlog.warning(
        "GPS navigation database capture timed out"
      )
      dump_collector.cancel()
      capture_state.fail(now)

    if (
      completed_database is not None
      and capture_state.capture_fix is not None
      and capture_state.capture_quality is not None
      and capture_state.capture_reason is not None
    ):
      finalized_quality = finalized_capture_quality(
        capture_state,
        capture_quality_tracker,
        now,
        getattr(pigeon, "receiver_cycle", 0),
        stable_fix,
      )
      capture_still_valid = finalized_quality is not None
      receiver_utc_observation = (
        receiver_time_provenance.current_observation(now)
      )
      promotion_authority = (
        time_authority.current_authorized_time(
          host_time_observation=(
            read_host_time_observation()
          ),
        )
      )
      authorized_promotion_utc = (
        promotion_authority.authorized_time.utc
        if promotion_authority.authorized_time is not None
        else None
      )
      trusted_promotion_utc = cache_promotion_trusted_now(
        (
          capture_quality_tracker.latest_fix.utc_time
          if capture_quality_tracker.latest_fix is not None
          else None
        ),
        capture_state.capture_receiver_cycle,
        getattr(pigeon, "receiver_cycle", 0),
        receiver_utc_fresh=capture_still_valid,
        receiver_utc_independent=(
          receiver_utc_observation.independent
        ),
        authorized_utc=authorized_promotion_utc,
      )
      if not capture_still_valid:
        cloudlog.warning("GPS navigation assistance candidate discarded because quality degraded during dump")
        result = NavigationAssistanceCacheResult.FAILED
      elif trusted_promotion_utc is None:
        cloudlog.warning("GPS navigation assistance candidate discarded because trusted promotion UTC is unavailable")
        result = NavigationAssistanceCacheResult.FAILED
      else:
        assert finalized_quality is not None
        source = {
          "onroad": "onroad_first",
          "onroad_refresh": "onroad_refresh",
          "post_drive": "postdrive",
        }[capture_state.capture_reason]
        result = write_navigation_assistance_cache(
          receiver_fingerprint,
          capture_state.capture_fix,
          completed_database,
          finalized_quality,
          source=source,
          receiver_cycle=capture_state.capture_receiver_cycle,
          receiver_utc_now=(
            capture_quality_tracker.latest_fix.utc_time
            if capture_quality_tracker.latest_fix is not None
            else None
          ),
          active_receiver_cycle=getattr(pigeon, "receiver_cycle", 0),
          receiver_utc_fresh=capture_still_valid,
          receiver_utc_independent=(
            receiver_utc_observation.independent
          ),
          trusted_promotion_utc=trusted_promotion_utc,
        )

      durable_quality = durable_quality_after_cache_result(
        result,
        receiver_fingerprint,
        trusted_promotion_utc,
      )
      readiness_message = capture_state.complete(
        result,
        now,
        durable_quality,
        finalized_quality,
      )
      if readiness_message is not None:
        cloudlog.info(readiness_message)

    if capture_state.request(
      now,
      started_state,
      dump_collector.active,
      capture_quality_tracker,
      getattr(pigeon, "receiver_cycle", 0),
      stable_fix,
    ):
      trigger_source = {
        "onroad": "onroad_first",
        "onroad_refresh": "onroad_refresh",
        "post_drive": "postdrive",
      }[capture_state.capture_reason]
      cloudlog.info(", ".join((
        "GPS navigation cache capture trigger",
        f"source={trigger_source}",
        f"quality_tier={navigation_quality_tier(capture_state.capture_quality).value}",
        f"receiver_cycle={capture_state.capture_receiver_cycle}",
        f"quality={capture_state.capture_quality}",
      )))
      try:
        request_navigation_database_capture(
          pigeon,
          dump_collector,
          capture_state,
          now,
          assistnow_autonomous_supported,
        )

      except Exception:
        cloudlog.exception(
          "Failed to request GPS navigation database"
        )
        dump_collector.cancel()
        capture_state.fail(now)


    if not data:
      time.sleep(0.001)


def main():
  assert TICI, "unsupported hardware for pigeond"
  run_receiving()

if __name__ == "__main__":
  main()
