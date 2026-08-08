from __future__ import annotations

import struct
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite

from openpilot.system.ubloxd.gps_assistance import (
  MAXIMUM_NAV_PVT_GAP_SECONDS,
  NavPvtFix,
  validate_ubx_frame,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
)

MAX_RECEIVER_UTC_TIME_ACCURACY_NS = 1_000_000_000


class ReceiverUtcClassification(StrEnum):
  UNAVAILABLE = "receiver_utc_unavailable"
  ASSISTED = "receiver_utc_assisted"
  UNASSISTED_UNCONFIRMED = (
    "receiver_utc_unassisted_unconfirmed"
  )
  UNASSISTED_GNSS = "receiver_utc_unassisted_gnss"


@dataclass(frozen=True)
class ReceiverUtcObservation:
  classification: ReceiverUtcClassification
  reason: str
  cycle_id: int
  utc: datetime | None
  observed_at: float | None
  time_accuracy_ns: int | None
  independent: bool
  time_assistance_written: bool
  time_assistance_source: str | None
  rawx_observed_at: float | None
  rawx_measurement_count: int
  gps_week_valid: bool
  leap_second_valid: bool


@dataclass(frozen=True)
class ReceiverTimeAssistanceObservation:
  cycle_id: int
  written: bool
  source: str | None
  utc: datetime | None
  uncertainty_seconds: float | None
  written_at: float | None
  written_boottime_seconds: float | None
  independent: bool | None
  provenance: TimeProvenance | None
  correction_written: bool


def _valid_monotonic(value: float) -> bool:
  return (
    type(value) in (int, float)
    and not isinstance(value, bool)
    and isfinite(value)
    and value >= 0.0
  )


class ReceiverTimeProvenanceTracker:
  def __init__(
    self,
    *,
    maximum_observation_age_seconds: float = (
      MAXIMUM_NAV_PVT_GAP_SECONDS
    ),
    maximum_time_accuracy_ns: int = (
      MAX_RECEIVER_UTC_TIME_ACCURACY_NS
    ),
  ) -> None:
    if (
      not _valid_monotonic(maximum_observation_age_seconds)
    ):
      raise ValueError(
        "Receiver UTC observation age limit is invalid"
      )
    if (
      type(maximum_time_accuracy_ns) is not int
      or maximum_time_accuracy_ns < 1
    ):
      raise ValueError(
        "Receiver UTC accuracy limit is invalid"
      )
    self.maximum_observation_age_seconds = float(
      maximum_observation_age_seconds
    )
    self.maximum_time_accuracy_ns = (
      maximum_time_accuracy_ns
    )
    self._cycle_id = 0
    self._cycle_started_at = 0.0
    self._observations_enabled = False
    self._time_assistance_written = False
    self._time_assistance_source: str | None = None
    self._time_assistance_utc: datetime | None = None
    self._time_assistance_uncertainty_seconds: float | None = None
    self._time_assistance_written_at: float | None = None
    self._time_assistance_written_boottime_seconds: float | None = None
    self._time_assistance_independent: bool | None = None
    self._time_assistance_provenance: TimeProvenance | None = None
    self._correction_written = False
    self._latest_rawx_time: float | None = None
    self._rawx_measurement_count = 0
    self._gps_week_valid = False
    self._leap_second_valid = False
    self._latest_fix: NavPvtFix | None = None
    self._latest_fix_time: float | None = None
    self._last_report_key: tuple[object, ...] | None = None

  @property
  def cycle_id(self) -> int:
    return self._cycle_id

  @property
  def time_assistance_written(self) -> bool:
    return self._time_assistance_written

  @property
  def correction_written(self) -> bool:
    return self._correction_written

  @property
  def time_assistance_observation(
    self,
  ) -> ReceiverTimeAssistanceObservation:
    return ReceiverTimeAssistanceObservation(
      cycle_id=self._cycle_id,
      written=self._time_assistance_written,
      source=self._time_assistance_source,
      utc=self._time_assistance_utc,
      uncertainty_seconds=(
        self._time_assistance_uncertainty_seconds
      ),
      written_at=self._time_assistance_written_at,
      written_boottime_seconds=(
        self._time_assistance_written_boottime_seconds
      ),
      independent=self._time_assistance_independent,
      provenance=self._time_assistance_provenance,
      correction_written=self._correction_written,
    )

  def start_cycle(
    self,
    cycle_id: int,
    now: float,
    *,
    observations_enabled: bool = True,
  ) -> None:
    if type(cycle_id) is not int or cycle_id < 1:
      raise ValueError("Receiver time cycle ID is invalid")
    if not _valid_monotonic(now):
      raise ValueError("Receiver time cycle timestamp is invalid")
    if type(observations_enabled) is not bool:
      raise ValueError(
        "Receiver observation state is invalid"
      )
    self._cycle_id = cycle_id
    self._cycle_started_at = float(now)
    self._observations_enabled = observations_enabled
    self._time_assistance_written = False
    self._time_assistance_source = None
    self._time_assistance_utc = None
    self._time_assistance_uncertainty_seconds = None
    self._time_assistance_written_at = None
    self._time_assistance_written_boottime_seconds = None
    self._time_assistance_independent = None
    self._time_assistance_provenance = None
    self._correction_written = False
    self._latest_rawx_time = None
    self._rawx_measurement_count = 0
    self._gps_week_valid = False
    self._leap_second_valid = False
    self._latest_fix = None
    self._latest_fix_time = None
    self._last_report_key = None

  def enable_receiver_observations(
    self,
    now: float,
  ) -> None:
    if self._cycle_id < 1:
      raise ValueError("Receiver time cycle is uninitialized")
    if (
      not _valid_monotonic(now)
      or now < self._cycle_started_at
    ):
      raise ValueError(
        "Receiver initialization timestamp is invalid"
      )
    self._observations_enabled = True

  def note_time_assistance_written(
    self,
    *,
    source: str,
    assistance_utc: datetime | None,
    uncertainty_seconds: float | None,
    now: float,
    written_boottime_seconds: float | None = None,
    independent: bool | None = None,
    provenance: TimeProvenance | None = None,
    correction: bool = False,
  ) -> None:
    if self._cycle_id < 1:
      return
    self._time_assistance_written = True
    if correction:
      self._correction_written = True
    update_record = (
      self._time_assistance_source is None
      or correction
    )
    if not update_record:
      return

    self._time_assistance_source = (
      source.strip()
      if type(source) is str and source.strip()
      else "unknown"
    )
    self._time_assistance_utc = None
    if (
      isinstance(assistance_utc, datetime)
      and assistance_utc.tzinfo is not None
    ):
      try:
        if assistance_utc.utcoffset() is not None:
          self._time_assistance_utc = (
            assistance_utc.astimezone(UTC)
          )
      except Exception:
        self._time_assistance_utc = None

    self._time_assistance_uncertainty_seconds = None
    if (
      type(uncertainty_seconds) in (int, float)
      and not isinstance(uncertainty_seconds, bool)
      and isfinite(uncertainty_seconds)
      and uncertainty_seconds >= 0.0
    ):
      self._time_assistance_uncertainty_seconds = float(
        uncertainty_seconds
      )

    self._time_assistance_written_at = (
      float(now)
      if _valid_monotonic(now)
      else None
    )
    self._time_assistance_written_boottime_seconds = (
      float(written_boottime_seconds)
      if _valid_monotonic(written_boottime_seconds)
      else None
    )
    self._time_assistance_independent = (
      independent
      if type(independent) is bool
      else None
    )
    self._time_assistance_provenance = (
      provenance
      if isinstance(provenance, TimeProvenance)
      else None
    )

  def note_rawx(self, frame: bytes, now: float) -> None:
    if (
      self._cycle_id < 1
      or not self._observations_enabled
      or not _valid_monotonic(now)
      or now < self._cycle_started_at
      or len(frame) < 8
      or frame[2:4] != b"\x02\x15"
    ):
      return
    if not validate_ubx_frame(frame):
      return
    payload = frame[6:-2]
    if len(payload) < 16:
      return
    measurement_count = payload[11]
    if (
      measurement_count <= 0
      or len(payload) != 16 + measurement_count * 32
    ):
      return
    week = struct.unpack_from("<H", payload, 8)[0]
    receiver_status = payload[12]
    self._latest_rawx_time = float(now)
    self._rawx_measurement_count = measurement_count
    self._gps_week_valid = week != 0
    self._leap_second_valid = bool(
      receiver_status & 0x01
    )

  def note_nav_pvt(self, fix: NavPvtFix, now: float) -> None:
    if (
      self._cycle_id < 1
      or not self._observations_enabled
      or not isinstance(fix, NavPvtFix)
      or not _valid_monotonic(now)
      or now < self._cycle_started_at
    ):
      return
    self._latest_fix = fix
    self._latest_fix_time = float(now)

  def _fresh(
    self,
    observed_at: float | None,
    now: float,
  ) -> bool:
    return (
      observed_at is not None
      and now >= observed_at
      and now - observed_at
      <= self.maximum_observation_age_seconds
    )

  def current_observation(
    self,
    now: float,
  ) -> ReceiverUtcObservation:
    now_valid = _valid_monotonic(now)
    fix = self._latest_fix
    fix_time = self._latest_fix_time
    utc = fix.utc_time if fix is not None else None
    time_accuracy_ns = (
      fix.time_accuracy_ns
      if fix is not None
      else None
    )

    classification = ReceiverUtcClassification.UNAVAILABLE
    reason = "nav_pvt_utc_unavailable"
    independent = False

    if self._cycle_id < 1:
      reason = "receiver_cycle_uninitialized"
    elif not now_valid:
      reason = "observation_time_invalid"
    elif float(now) < self._cycle_started_at:
      reason = "observation_before_cycle_start"
    elif fix is None or fix_time is None:
      reason = "nav_pvt_unavailable"
    elif not self._fresh(fix_time, float(now)):
      reason = "nav_pvt_stale"
    elif utc is None:
      reason = "nav_pvt_utc_invalid"
    elif self._time_assistance_written:
      classification = ReceiverUtcClassification.ASSISTED
      reason = "time_assistance_written_in_cycle"
    else:
      classification = (
        ReceiverUtcClassification.UNASSISTED_UNCONFIRMED
      )
      if not fix.valid_date:
        reason = "valid_date_false"
      elif not fix.valid_time:
        reason = "valid_time_false"
      elif not fix.fully_resolved:
        reason = "fully_resolved_false"
      elif (
        type(time_accuracy_ns) is not int
        or time_accuracy_ns < 0
      ):
        reason = "time_accuracy_unavailable"
      elif (
        time_accuracy_ns
        > self.maximum_time_accuracy_ns
      ):
        reason = "time_accuracy_above_limit"
      elif self._latest_rawx_time is None:
        reason = "nonempty_rawx_unavailable"
      elif not self._fresh(
        self._latest_rawx_time,
        float(now),
      ):
        reason = "nonempty_rawx_stale"
      elif not self._gps_week_valid:
        reason = "gps_week_invalid"
      elif not self._leap_second_valid:
        reason = "leap_second_invalid"
      else:
        classification = (
          ReceiverUtcClassification.UNASSISTED_GNSS
        )
        reason = "fresh_gnss_time_evidence"
        independent = True

    return ReceiverUtcObservation(
      classification=classification,
      reason=reason,
      cycle_id=self._cycle_id,
      utc=utc,
      observed_at=fix_time,
      time_accuracy_ns=time_accuracy_ns,
      independent=independent,
      time_assistance_written=(
        self._time_assistance_written
      ),
      time_assistance_source=(
        self._time_assistance_source
      ),
      rawx_observed_at=self._latest_rawx_time,
      rawx_measurement_count=(
        self._rawx_measurement_count
      ),
      gps_week_valid=self._gps_week_valid,
      leap_second_valid=self._leap_second_valid,
    )

  def changed_observation(
    self,
    now: float,
  ) -> ReceiverUtcObservation | None:
    observation = self.current_observation(now)
    key = (
      observation.cycle_id,
      observation.classification,
      observation.reason,
      observation.time_assistance_written,
      observation.gps_week_valid,
      observation.leap_second_valid,
    )
    if key == self._last_report_key:
      return None
    self._last_report_key = key
    return observation


def is_mga_time_assistance_message(message: bytes) -> bool:
  if (
    type(message) is not bytes
    or len(message) < 9
    or message[:4] != b"\xB5\x62\x13\x40"
  ):
    return False
  payload_length = int.from_bytes(
    message[4:6],
    "little",
  )
  return (
    payload_length >= 1
    and len(message) == 6 + payload_length + 2
    and validate_ubx_frame(message)
    and message[6] == 0x10
  )
