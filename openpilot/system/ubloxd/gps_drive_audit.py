#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import traceback
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpilot.system.ubloxd import gps_acquisition_report

DEFAULT_REALDATA_ROOT = Path("/data/media/0/realdata")
DEFAULT_OUTPUT_ROOT = Path("/data")
DEFAULT_REPOSITORY_ROOT = Path("/data/openpilot")
DEFAULT_ASSISTANCE_ROOT = Path("/data/gps_assistance")
BOOT_ID_PATH = Path("/proc/sys/kernel/random/boot_id")
SEGMENT_PATTERN = re.compile(r"^(?P<route>.+--[0-9a-fA-F]+)--(?P<segment>[0-9]+)$")
LOG_NAMES = ("rlog.zst", "rlog.bz2", "rlog", "qlog.zst", "qlog.bz2", "qlog")
PARAM_KEYS = ("GitCommit", "GitBranch", "Version", "PublicYumaAlmanacEnabled", "IsOnroad", "IsOffroad")
STATE_FILES = (
  "navigation_cache.json",
  "navigation_cache_previous.json",
  "public_yuma_almanac.json",
  "public_yuma_last_outcome.json",
  "provisional_yuma_last_decision.json",
  "trusted_time_anchor.json",
  "trusted_time_anchor_previous.json",
)
EVENT_KEYWORDS = (
  "gps acquisition milestone",
  "gps acquisition status",
  "gps receiver cycle",
  "gps receiver utc provenance",
  "gps rf observability",
  "gps startup timeline",
  "time assistance",
  "trusted time",
  "navigation assistance restore",
  "receiver cycle initialization",
  "gps public yuma",
  "requested gps navigation database",
  "saved gps navigation assistance cache",
  "watchdog",
  "receiver reset",
  "pigeond",
  "ubloxd",
  "gpsard",
  "position assistance",
  "dbd",
  "yuma",
  "measurement",
  "mga",
)


def safe_get(obj: object, name: str, default: Any = None) -> Any:
  try:
    return getattr(obj, name)
  except Exception:
    return default


def as_float(value: object, default: float | None = None) -> float | None:
  if value is None:
    return default
  if isinstance(value, bool):
    return float(value)
  if isinstance(value, (int, float)):
    return float(value)
  if isinstance(value, str):
    try:
      return float(value)
    except ValueError:
      return default
  if isinstance(value, (bytes, bytearray)):
    try:
      return float(value.decode())
    except (ValueError, UnicodeDecodeError):
      return default
  return default


def as_int(value: object, default: int | None = None) -> int | None:
  if value is None:
    return default
  if isinstance(value, bool):
    return int(value)
  if isinstance(value, int):
    return value
  if isinstance(value, float):
    return int(value)
  if isinstance(value, str):
    try:
      return int(value)
    except ValueError:
      return default
  if isinstance(value, (bytes, bytearray)):
    try:
      return int(value.decode())
    except (ValueError, UnicodeDecodeError):
      return default
  return default


def decode_text(value: object) -> str:
  if isinstance(value, bytes):
    return value.decode("utf-8", errors="replace")
  return str(value)


def extract_event_message(text: str) -> str:
  try:
    payload = json.loads(text)
  except (json.JSONDecodeError, TypeError):
    return text
  if isinstance(payload, dict) and isinstance(payload.get("msg"), str):
    return payload["msg"]
  return text


def is_independent_receiver_utc_event(text: str) -> bool:
  message = extract_event_message(text).lower()
  return "gps receiver utc provenance" in message and "classification=receiver_utc_unassisted_gnss" in message and "independent=true" in message


def haversine_m(latitude_1: float, longitude_1: float, latitude_2: float, longitude_2: float) -> float:
  radius = 6_371_000.0
  phi_1 = math.radians(latitude_1)
  phi_2 = math.radians(latitude_2)
  delta_phi = math.radians(latitude_2 - latitude_1)
  delta_lambda = math.radians(longitude_2 - longitude_1)
  value = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi_1) * math.cos(phi_2) * math.sin(delta_lambda / 2.0) ** 2
  return 2.0 * radius * math.asin(min(1.0, math.sqrt(value)))


@dataclass(frozen=True)
class RouteSelection:
  route: str
  segments: tuple[tuple[int, Path], ...]
  newest_mtime: float


@dataclass
class RouteMetrics:
  route: str
  route_start: float | None = None
  route_end: float | None = None
  message_count: int = 0
  service_counts: Counter[str] = field(default_factory=Counter)
  errors: list[str] = field(default_factory=list)
  events: list[tuple[float, str]] = field(default_factory=list)
  used_logs: list[str] = field(default_factory=list)
  gps_samples: int = 0
  fix_samples: int = 0
  positive_timestamp_samples: int = 0
  rawx_reports: int = 0
  first_rawx: float | None = None
  first_nonempty_rawx: float | None = None
  first_gps_measurement: float | None = None
  first_glonass_measurement: float | None = None
  first_valid_gps_week: float | None = None
  first_valid_leap_second: float | None = None
  first_receiver_utc: float | None = None
  first_fix: float | None = None
  first_25m: float | None = None
  first_10m: float | None = None
  first_5m: float | None = None
  last_fix: float | None = None
  best_accuracy: float | None = None
  max_satellites: int = 0
  gps_distance_m: float = 0.0
  vehicle_distance_m: float = 0.0
  previous_gps: tuple[float, float, float] | None = None
  previous_car: tuple[float, float] | None = None
  has_gps_source_state: bool = False
  has_measurement_mono_ns: bool = False
  has_sat_report: bool = False
  has_rf_observability_logs: bool = False
  has_qcom_gps: bool = False
  first_nav_pvt: float | None = None
  first_fix_ok: float | None = None
  first_reliable_fix: float | None = None
  first_tracked_sv: float | None = None
  first_code_lock: float | None = None
  first_ephemeris: float | None = None
  first_3_used: float | None = None
  first_4_used: float | None = None
  first_fix_ublox: float | None = None
  first_fix_qcom: float | None = None
  first_authoritative: float | None = None
  measurement_mono_samples_with_stamp: int = 0
  measurement_mono_first_positive: float | None = None
  measurement_mono_max_age_s: float | None = None
  gps_source_first_selected: str | None = None
  gps_source_first_authoritative: str | None = None
  gps_source_transitions: list[dict[str, Any]] = field(default_factory=list)
  max_failover_count: int = 0
  max_recovery_count: int = 0
  no_healthy_interval_count: int = 0
  no_healthy_intervals: list[dict[str, Any]] = field(default_factory=list)
  startup_no_healthy_interval_count: int = 0
  runtime_no_healthy_interval_count: int = 0
  startup_no_healthy_intervals: list[dict[str, Any]] = field(default_factory=list)
  runtime_no_healthy_intervals: list[dict[str, Any]] = field(default_factory=list)
  source_authority_intervals: list[dict[str, Any]] = field(default_factory=list)
  source_epoch_warnings: list[str] = field(default_factory=list)
  _no_healthy_active: bool = False
  _no_healthy_start: float | None = None
  _no_healthy_phase: str | None = None
  gnss_start_mono: float | None = None
  cycle_start_mono: float | None = None
  reference_policy: str = "route_start"
  classification: str = "INSUFFICIENT_TELEMETRY"
  start_type: str = "UNKNOWN_START_TYPE"
  missing_telemetry: list[str] = field(default_factory=list)
  max_sat_signal_count: int = 0
  max_sat_code_locked: int = 0
  max_sat_used: int = 0
  max_sat_ephemeris: int = 0
  max_rf_jam_indicator: int | None = None
  sat_report_count: int = 0
  sat_reports_with_zero_signals: int = 0
  sat_reports_with_code_lock_and_zero_eph: int = 0
  rf_observability_zero_signal_count: int = 0
  dbd_assisted_evidence: bool = False
  dbd_restore_disposition: str = "none"
  assistance_late_evidence: bool = False
  configuration_failure_evidence: bool = False
  warm_start_evidence: bool = False
  short_stop_dbd_evidence: bool = False
  overnight_offline_evidence: bool = False
  overnight_online_evidence: bool = False
  trusted_time_available: bool | None = None
  yuma_attempted: float | None = None
  yuma_completed: float | None = None
  yuma_failed: float | None = None
  yuma_disposition: str | None = None
  _previous_gps_source: str | None = None
  _previous_gps_source_generation: int | None = None
  _previous_transition_mono_ns: int | None = None

  def note_time(self, monotonic_time: float) -> None:
    if self.route_start is None or monotonic_time < self.route_start:
      self.route_start = monotonic_time
    if self.route_end is None or monotonic_time > self.route_end:
      self.route_end = monotonic_time

  def _note_milestone(self, milestone: str, monotonic_time: float) -> None:
    field_map = {
      "first_nav_pvt": "first_nav_pvt",
      "first_fix_ok": "first_fix_ok",
      "first_reliable_fix": "first_reliable_fix",
      "first_receiver_utc": "first_receiver_utc",
      "first_nonempty_rawx": "first_nonempty_rawx",
      "first_valid_gps_week": "first_valid_gps_week",
      "first_valid_leap_second": "first_valid_leap_second",
      "first_gps_measurement": "first_gps_measurement",
      "first_glonass_measurement": "first_glonass_measurement",
      "first_rawx_after_initialization": "first_rawx",
    }
    attribute = field_map.get(milestone)
    if attribute is not None and getattr(self, attribute) is None:
      setattr(self, attribute, monotonic_time)

  def _apply_dbd_restore_disposition(self, disposition: str) -> None:
    normalized = gps_acquisition_report.normalize_dbd_restore_disposition(disposition)
    if normalized == "none":
      return
    self.dbd_restore_disposition = normalized
    if normalized == "success":
      self.dbd_assisted_evidence = True

  def _apply_short_stop_evidence(self, fields: dict[str, str]) -> None:
    if not self.dbd_assisted_evidence:
      return
    start_scenario = fields.get("start_scenario", "").lower()
    if start_scenario in ("short_stop_dbd", "short_stop"):
      self.short_stop_dbd_evidence = True
      return
    if fields.get("short_stop", "").lower() == "true":
      self.short_stop_dbd_evidence = True
      return
    cache_age = as_float(fields.get("cache_age_seconds") or fields.get("dbd_age_seconds"), None)
    short_stop_max = as_float(fields.get("short_stop_max_age_seconds"), None)
    if cache_age is not None and short_stop_max is not None and cache_age <= short_stop_max:
      self.short_stop_dbd_evidence = True
      return
    age_evidence = fields.get("restored_cache_age_evidence", "").lower()
    if age_evidence in ("short_stop", "short_stop_dbd"):
      self.short_stop_dbd_evidence = True

  def _apply_start_scenario_fields(self, fields: dict[str, str]) -> None:
    start_scenario = fields.get("start_scenario", "").lower()
    if start_scenario in ("overnight_offline_cold_start", "overnight_offline"):
      self.overnight_offline_evidence = True
    elif start_scenario in ("overnight_online_start", "overnight_online"):
      self.overnight_online_evidence = True
    elif start_scenario in ("warm_start",):
      self.warm_start_evidence = True
    elif start_scenario in ("short_stop_dbd", "short_stop"):
      self._apply_short_stop_evidence(fields)

  def _parse_startup_timeline(self, monotonic_time: float, text: str) -> None:
    fields = gps_acquisition_report.parse_log_fields(text)
    gnss_start_cycle = as_float(fields.get("gnss_start_sent_cycle_seconds"), None)
    if gnss_start_cycle is not None and self.cycle_start_mono is not None:
      candidate = self.cycle_start_mono + gnss_start_cycle
      if self.gnss_start_mono is None or candidate < self.gnss_start_mono:
        self.gnss_start_mono = candidate

    self._apply_dbd_restore_disposition(fields.get("database_restore_disposition", ""))
    self._apply_start_scenario_fields(fields)
    self._apply_short_stop_evidence(fields)

    trusted_time = fields.get("trusted_time_available", "").lower()
    if trusted_time == "true":
      self.trusted_time_available = True
    elif trusted_time == "false":
      self.trusted_time_available = False

    accepted_before = fields.get("time_assistance_accepted_before_gnss_start", "").lower()
    if accepted_before == "false":
      self.assistance_late_evidence = True

  def _parse_yuma_log(self, monotonic_time: float, text: str) -> None:
    fields = gps_acquisition_report.parse_log_fields(text)
    if self.yuma_attempted is None:
      if fields.get("transmission_attempt") or fields.get("action"):
        self.yuma_attempted = monotonic_time
    transmit_status = fields.get("transmit_status", "").lower()
    terminal = fields.get("terminal", "").lower() == "true"
    cancellation = fields.get("cancellation_reason", "").lower()
    action = fields.get("action", "").lower()
    success_statuses = ("complete", "success", "accepted")
    failure_tokens = ("fail", "reject", "error", "timeout", "cancel")

    # Historical and current terminal-success statuses are unambiguous without terminal=true.
    if transmit_status in success_statuses or (terminal and (transmit_status in success_statuses or action in ("transmit", "send_all"))):
      if self.yuma_completed is None:
        self.yuma_completed = monotonic_time
      self.yuma_disposition = transmit_status or action or "complete"
      return

    failed = any(token in transmit_status for token in failure_tokens) or (cancellation not in ("", "none"))
    if failed or (terminal and failed):
      if self.yuma_failed is None:
        self.yuma_failed = monotonic_time
      self.yuma_disposition = transmit_status or cancellation or action
      return

    if terminal and any(token in transmit_status for token in failure_tokens):
      if self.yuma_failed is None:
        self.yuma_failed = monotonic_time
      self.yuma_disposition = transmit_status or cancellation or action

  def process_log_message(self, monotonic_time: float, value: object) -> None:
    text = decode_text(value)
    lowered = text.lower()
    if any(keyword in lowered for keyword in EVENT_KEYWORDS):
      self.events.append((monotonic_time, text))

    if "gps rf observability" in lowered:
      self.has_rf_observability_logs = True
      if "signal_satellites=0" in lowered:
        self.rf_observability_zero_signal_count += 1

    if "gps receiver cycle started" in lowered:
      self.cycle_start_mono = monotonic_time
      fields = gps_acquisition_report.parse_log_fields(text)
      self._apply_start_scenario_fields(fields)

    if "gps startup timeline" in lowered:
      self._parse_startup_timeline(monotonic_time, text)

    if "navigation assistance restore" in lowered:
      fields = gps_acquisition_report.parse_log_fields(text)
      self._apply_dbd_restore_disposition(fields.get("database_restore_disposition", ""))
      self._apply_start_scenario_fields(fields)
      self._apply_short_stop_evidence(fields)

    if "gps public yuma" in lowered:
      self._parse_yuma_log(monotonic_time, text)

    if "configuration failure" in lowered or "strict configuration failure" in lowered:
      self.configuration_failure_evidence = True

    milestone, _fields = gps_acquisition_report.parse_milestone_log(text)
    if milestone is not None:
      self._note_milestone(milestone, monotonic_time)

    if self.first_receiver_utc is None and is_independent_receiver_utc_event(text):
      self.first_receiver_utc = monotonic_time

  def process_car_state(self, monotonic_time: float, car_state: object) -> None:
    speed = abs(float(safe_get(car_state, "vEgo", 0.0)))
    if self.previous_car is not None:
      previous_time, previous_speed = self.previous_car
      delta = monotonic_time - previous_time
      if 0.0 < delta <= 2.0:
        self.vehicle_distance_m += ((previous_speed + speed) / 2.0) * delta
    self.previous_car = (monotonic_time, speed)

  def process_gps(self, monotonic_time: float, gps: object, source: str = "ublox") -> None:
    self.gps_samples += 1
    if source == "qcom":
      self.has_qcom_gps = True

    measurement_mono_ns = as_int(safe_get(gps, "measurementMonoNs", 0), 0) or 0
    if measurement_mono_ns > 0:
      self.has_measurement_mono_ns = True
      self.measurement_mono_samples_with_stamp += 1
      measurement_mono_s = measurement_mono_ns * 1e-9
      if self.measurement_mono_first_positive is None:
        self.measurement_mono_first_positive = measurement_mono_s
      age_s = monotonic_time - measurement_mono_s
      if age_s >= 0.0:
        if self.measurement_mono_max_age_s is None or age_s > self.measurement_mono_max_age_s:
          self.measurement_mono_max_age_s = age_s

    flags = as_int(safe_get(gps, "flags", 0), 0) or 0
    has_fix = bool(flags & 1) or bool(safe_get(gps, "hasFix", False))
    timestamp_ms = as_int(safe_get(gps, "unixTimestampMillis", 0), 0) or 0
    if timestamp_ms > 0:
      self.positive_timestamp_samples += 1

    satellites = as_int(safe_get(gps, "satelliteCount", 0), 0) or 0
    self.max_satellites = max(self.max_satellites, satellites)

    accuracy = as_float(safe_get(gps, "horizontalAccuracy", None), None)
    latitude = as_float(safe_get(gps, "latitude", None), None)
    longitude = as_float(safe_get(gps, "longitude", None), None)

    if not has_fix:
      return

    self.fix_samples += 1
    self.last_fix = monotonic_time
    if self.first_fix is None:
      self.first_fix = monotonic_time
    if source == "qcom" and self.first_fix_qcom is None:
      self.first_fix_qcom = monotonic_time
    if source == "ublox" and self.first_fix_ublox is None:
      self.first_fix_ublox = monotonic_time

    if accuracy is not None and math.isfinite(accuracy) and accuracy >= 0.0:
      if self.best_accuracy is None or accuracy < self.best_accuracy:
        self.best_accuracy = accuracy
      if accuracy <= 25.0 and self.first_25m is None:
        self.first_25m = monotonic_time
      if accuracy <= 10.0 and self.first_10m is None:
        self.first_10m = monotonic_time
      if accuracy <= 5.0 and self.first_5m is None:
        self.first_5m = monotonic_time

    valid_position = (
      latitude is not None
      and longitude is not None
      and math.isfinite(latitude)
      and math.isfinite(longitude)
      and -90.0 <= latitude <= 90.0
      and -180.0 <= longitude <= 180.0
      and not (latitude == 0.0 and longitude == 0.0)
    )
    if not valid_position:
      return

    assert latitude is not None
    assert longitude is not None
    if self.previous_gps is not None:
      previous_time, previous_latitude, previous_longitude = self.previous_gps
      delta = monotonic_time - previous_time
      if 0.0 < delta <= 5.0:
        distance = haversine_m(previous_latitude, previous_longitude, latitude, longitude)
        if distance / delta <= 100.0:
          self.gps_distance_m += distance
    self.previous_gps = (monotonic_time, latitude, longitude)

  def _close_no_healthy_interval(self, end_t: float | None) -> None:
    if not self._no_healthy_active or self._no_healthy_start is None:
      self._no_healthy_active = False
      self._no_healthy_start = None
      self._no_healthy_phase = None
      return
    duration_s = None if end_t is None else end_t - self._no_healthy_start
    interval = {
      "phase": self._no_healthy_phase or "startup",
      "start_t": self._no_healthy_start,
      "end_t": end_t,
      "duration_s": duration_s,
    }
    self.no_healthy_intervals.append(interval)
    if interval["phase"] == "runtime":
      self.runtime_no_healthy_intervals.append(interval)
      self.runtime_no_healthy_interval_count = len(self.runtime_no_healthy_intervals)
    else:
      self.startup_no_healthy_intervals.append(interval)
      self.startup_no_healthy_interval_count = len(self.startup_no_healthy_intervals)
    self.no_healthy_interval_count = len(self.no_healthy_intervals)
    self._no_healthy_active = False
    self._no_healthy_start = None
    self._no_healthy_phase = None

  def process_gps_source_state(self, monotonic_time: float, state: object) -> None:
    self.has_gps_source_state = True
    selected = gps_acquisition_report.normalize_source_name(safe_get(state, "selected", None))
    generation = as_int(safe_get(state, "generation", None), None)
    transition_mono_ns = as_int(safe_get(state, "transitionMonoNs", None), None)
    reason = decode_text(safe_get(state, "transitionReason", ""))

    decision = gps_acquisition_report.evaluate_gps_source_epoch(
      selected=selected,
      generation=generation,
      transition_mono_ns=transition_mono_ns,
      previous_selected=self._previous_gps_source,
      previous_generation=self._previous_gps_source_generation,
      previous_transition_mono_ns=self._previous_transition_mono_ns,
      event_mono_s=monotonic_time,
    )
    if decision in ("reject_regressing", "reject_inconsistent", "reject_future"):
      warning = (
        f"gpsSourceState epoch {decision}: selected={selected} generation={generation} " + f"transitionMonoNs={transition_mono_ns} event_t={monotonic_time}"
      )
      self.source_epoch_warnings.append(warning)
      self.errors.append(warning)
      return

    transition_t = gps_acquisition_report.authority_transition_time_s(
      transition_mono_ns=transition_mono_ns,
      event_mono_s=monotonic_time,
    )

    if self.gps_source_first_selected is None and selected is not None:
      self.gps_source_first_selected = selected
    if self.gps_source_first_authoritative is None and selected in (
      gps_acquisition_report.SOURCE_UBLOX,
      gps_acquisition_report.SOURCE_QCOM,
    ):
      self.gps_source_first_authoritative = selected
      if self.first_authoritative is None:
        self.first_authoritative = transition_t

    failover_count = as_int(safe_get(state, "failoverCount", 0), 0) or 0
    recovery_count = as_int(safe_get(state, "recoveryCount", 0), 0) or 0
    self.max_failover_count = max(self.max_failover_count, failover_count)
    self.max_recovery_count = max(self.max_recovery_count, recovery_count)

    if selected == gps_acquisition_report.SOURCE_NO_HEALTHY:
      if not self._no_healthy_active:
        self._no_healthy_active = True
        self._no_healthy_start = transition_t
        self._no_healthy_phase = "runtime" if self.gps_source_first_authoritative is not None else "startup"
    elif self._no_healthy_active:
      self._close_no_healthy_interval(transition_t)

    if decision == "transition" and selected is not None:
      self.gps_source_transitions.append(
        {
          "t": transition_t,
          "selected": selected,
          "generation": generation,
          "transitionMonoNs": transition_mono_ns,
          "reason": reason,
        }
      )
      self._previous_gps_source = selected
      self._previous_gps_source_generation = generation
      if transition_mono_ns is not None:
        self._previous_transition_mono_ns = transition_mono_ns
    elif decision == "refresh":
      # Same epoch identity: keep authority pointers unchanged.
      if self._previous_gps_source is None and selected is not None:
        self._previous_gps_source = selected
        self._previous_gps_source_generation = generation
        if transition_mono_ns is not None:
          self._previous_transition_mono_ns = transition_mono_ns

  def process_sat_report(self, monotonic_time: float, report: object) -> None:
    self.has_sat_report = True
    svs = tuple(safe_get(report, "svs", ()) or ())
    if not svs:
      return

    signal_count = 0
    code_locked_count = 0
    used_count = 0
    ephemeris_count = 0
    for sv in svs:
      cno = as_int(safe_get(sv, "cno", 0), 0) or 0
      flags = as_int(safe_get(sv, "flagsBitfield", 0), 0) or 0
      analysis = gps_acquisition_report.analyze_sat_flags(flags)
      if cno > 0:
        signal_count += 1
      if analysis["code_locked"]:
        code_locked_count += 1
      if analysis["used"]:
        used_count += 1
      if analysis["ephemeris"]:
        ephemeris_count += 1

    self.sat_report_count += 1
    if signal_count == 0:
      self.sat_reports_with_zero_signals += 1
    if code_locked_count > 0 and ephemeris_count == 0:
      self.sat_reports_with_code_lock_and_zero_eph += 1

    self.max_sat_signal_count = max(self.max_sat_signal_count, signal_count)
    self.max_sat_code_locked = max(self.max_sat_code_locked, code_locked_count)
    self.max_sat_used = max(self.max_sat_used, used_count)
    self.max_sat_ephemeris = max(self.max_sat_ephemeris, ephemeris_count)

    if self.first_ephemeris is None and ephemeris_count > 0:
      self.first_ephemeris = monotonic_time

    if self.first_tracked_sv is None and len(svs) > 0:
      self.first_tracked_sv = monotonic_time
    if self.first_code_lock is None and code_locked_count > 0:
      self.first_code_lock = monotonic_time
    if self.first_3_used is None and used_count >= 3:
      self.first_3_used = monotonic_time
    if self.first_4_used is None and used_count >= 4:
      self.first_4_used = monotonic_time

  def process_hw_status(self, status: object) -> None:
    jam_ind = as_int(safe_get(status, "jamInd", None), None)
    if jam_ind is not None:
      if self.max_rf_jam_indicator is None or jam_ind > self.max_rf_jam_indicator:
        self.max_rf_jam_indicator = jam_ind

  def process_rawx(self, monotonic_time: float, report: object) -> None:
    self.rawx_reports += 1
    if self.first_rawx is None:
      self.first_rawx = monotonic_time

    measurements = tuple(safe_get(report, "measurements", ()) or ())
    if not measurements:
      return

    if self.first_nonempty_rawx is None:
      self.first_nonempty_rawx = monotonic_time

    gnss_ids = {as_int(safe_get(measurement, "gnssId", None), None) for measurement in measurements}
    if 0 in gnss_ids and self.first_gps_measurement is None:
      self.first_gps_measurement = monotonic_time
    if 6 in gnss_ids and self.first_glonass_measurement is None:
      self.first_glonass_measurement = monotonic_time

    gps_week = as_int(safe_get(report, "gpsWeek", None), None)
    if gps_week is not None and gps_week > 0 and self.first_valid_gps_week is None:
      self.first_valid_gps_week = monotonic_time

    leap_seconds = as_int(safe_get(report, "leapSeconds", None), None)
    if leap_seconds is not None and leap_seconds != 0 and self.first_valid_leap_second is None:
      self.first_valid_leap_second = monotonic_time

  def finalize(self) -> None:
    if self._no_healthy_active:
      self._close_no_healthy_interval(self.route_end)

    self.source_authority_intervals = gps_acquisition_report.build_source_authority_intervals(self)
    self.reference_policy = gps_acquisition_report.resolve_reference_policy(self)
    self.missing_telemetry = gps_acquisition_report.compute_missing_telemetry(self)
    self.classification = gps_acquisition_report.classify_acquisition(self)
    self.start_type = gps_acquisition_report.infer_start_type(self)

  def relative(self, value: float | None) -> float | None:
    return gps_acquisition_report.relative_seconds(self, value)

  def to_machine_report(self) -> dict[str, Any]:
    return gps_acquisition_report.to_machine_report(self)

  def ttff_report_lines(self) -> list[str]:
    return gps_acquisition_report.ttff_report_lines(self)

  def summary_lines(self, segment_count: int) -> list[str]:
    duration = None if self.route_start is None or self.route_end is None else self.route_end - self.route_start
    fix_span = None
    if self.first_fix is not None and self.last_fix is not None:
      fix_span = max(0.0, self.last_fix - self.first_fix)

    lines = [
      f"route={self.route}",
      f"segments={segment_count}",
      f"log_files={len(self.used_logs)}",
      f"duration_seconds={duration}",
      f"estimated_vehicle_distance_miles={self.vehicle_distance_m / 1609.344:.4f}",
      f"gps_fixed_distance_miles={self.gps_distance_m / 1609.344:.4f}",
      f"fix_span_seconds={fix_span}",
      f"message_count={self.message_count}",
      f"gps_samples={self.gps_samples}",
      f"fix_samples={self.fix_samples}",
      f"positive_timestamp_samples={self.positive_timestamp_samples}",
      f"rawx_reports={self.rawx_reports}",
      f"first_rawx_seconds={self.relative(self.first_rawx)}",
      f"first_nonempty_rawx_seconds={self.relative(self.first_nonempty_rawx)}",
      f"first_gps_measurement_seconds={self.relative(self.first_gps_measurement)}",
      f"first_glonass_measurement_seconds={self.relative(self.first_glonass_measurement)}",
      f"first_valid_gps_week_seconds={self.relative(self.first_valid_gps_week)}",
      f"first_valid_leap_second_seconds={self.relative(self.first_valid_leap_second)}",
      f"first_receiver_utc_seconds={self.relative(self.first_receiver_utc)}",
      f"first_fix_seconds={self.relative(self.first_fix)}",
      f"first_25m_seconds={self.relative(self.first_25m)}",
      f"first_10m_seconds={self.relative(self.first_10m)}",
      f"first_5m_seconds={self.relative(self.first_5m)}",
      f"best_accuracy_m={self.best_accuracy}",
      f"max_satellites={self.max_satellites}",
      "",
      *self.ttff_report_lines(),
      "",
      "===== LOG FILES =====",
      *self.used_logs,
      "",
      "===== SERVICE COUNTS =====",
    ]
    lines.extend(f"{service}={count}" for service, count in sorted(self.service_counts.items()))
    lines.extend(("", "===== ERRORS ====="))
    lines.extend(self.errors if self.errors else ["none"])
    return lines


def choose_log(segment_dir: Path) -> Path | None:
  for name in LOG_NAMES:
    candidate = segment_dir / name
    if candidate.is_file():
      return candidate
  return None


def discover_routes(realdata_root: Path) -> list[RouteSelection]:
  grouped: dict[str, list[tuple[int, Path]]] = defaultdict(list)
  if not realdata_root.is_dir():
    return []

  for entry in realdata_root.iterdir():
    if not entry.is_dir():
      continue
    match = SEGMENT_PATTERN.match(entry.name)
    if match is None:
      continue
    grouped[match.group("route")].append((int(match.group("segment")), entry))

  selections = []
  for route, segments in grouped.items():
    segments.sort(key=lambda item: item[0])
    newest = max(path.stat().st_mtime for _, path in segments)
    selections.append(RouteSelection(route, tuple(segments), newest))
  return sorted(selections, key=lambda selection: selection.newest_mtime, reverse=True)


def select_routes(discovered: Iterable[RouteSelection], route: str | None, latest: int | None) -> list[RouteSelection]:
  available = list(discovered)
  if route is not None:
    matches = [selection for selection in available if selection.route == route]
    if not matches:
      raise RuntimeError(f"Requested route not found: {route}")
    return matches
  count = latest if latest is not None else 1
  if count < 1:
    raise RuntimeError("--latest must be at least 1")
  selected = available[:count]
  if not selected:
    raise RuntimeError("No segmented routes were found")
  return selected


def _load_log_reader() -> Any:
  from openpilot.tools.lib.logreader import LogReader

  return LogReader


def analyze_route(selection: RouteSelection, output_root: Path) -> list[str]:
  log_reader = _load_log_reader()
  metrics = RouteMetrics(selection.route)

  for segment_number, segment_dir in selection.segments:
    log_path = choose_log(segment_dir)
    if log_path is None:
      metrics.errors.append(f"segment={segment_number}: no rlog/qlog found in {segment_dir}")
      continue
    metrics.used_logs.append(str(log_path))

    try:
      for message in log_reader(str(log_path)):
        metrics.message_count += 1
        monotonic_time = as_float(safe_get(message, "logMonoTime", None), None)
        if monotonic_time is None:
          continue
        monotonic_time *= 1e-9
        metrics.note_time(monotonic_time)

        try:
          service = message.which()
        except Exception:
          continue
        metrics.service_counts[service] += 1

        try:
          if service == "logMessage":
            metrics.process_log_message(monotonic_time, message.logMessage)
          elif service == "carState":
            metrics.process_car_state(monotonic_time, message.carState)
          elif service == "gpsLocationExternal":
            metrics.process_gps(monotonic_time, message.gpsLocationExternal, source="ublox")
          elif service == "gpsLocation":
            metrics.process_gps(monotonic_time, message.gpsLocation, source="qcom")
          elif service == "gpsSourceState":
            metrics.process_gps_source_state(monotonic_time, message.gpsSourceState)
          elif service == "ubloxGnss":
            ublox = message.ubloxGnss
            ublox_kind = ublox.which()
            if ublox_kind == "measurementReport":
              metrics.process_rawx(monotonic_time, ublox.measurementReport)
            elif ublox_kind == "satReport":
              metrics.process_sat_report(monotonic_time, ublox.satReport)
            elif ublox_kind == "hwStatus":
              metrics.process_hw_status(ublox.hwStatus)
            elif ublox_kind == "hwStatus2":
              metrics.process_hw_status(ublox.hwStatus2)
        except Exception as exc:
          metrics.errors.append(f"{service} parse: {type(exc).__name__}: {exc}")
    except Exception as exc:
      metrics.errors.append(f"{log_path}: {type(exc).__name__}: {exc}\n{traceback.format_exc(limit=3)}")

  metrics.finalize()
  route_dir = output_root / "routes" / selection.route
  route_dir.mkdir(parents=True, exist_ok=True)
  (route_dir / "acquisition_report.json").write_text(
    json.dumps(metrics.to_machine_report(), indent=2) + "\n",
    encoding="utf-8",
  )
  summary = metrics.summary_lines(len(selection.segments))
  (route_dir / "summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")

  event_lines = []
  for event_time, text in sorted(metrics.events):
    elapsed = event_time - metrics.route_start if metrics.route_start is not None else 0.0
    event_lines.append(f"[{elapsed:.3f}s] {text}")
  (route_dir / "assistance_events.txt").write_text(
    "\n".join(event_lines) + ("\n" if event_lines else ""),
    encoding="utf-8",
  )
  return summary


def decode_param(value: object) -> str:
  if value is None:
    return "<missing>"
  if isinstance(value, bytes):
    return value.decode("utf-8", errors="replace")
  return str(value)


def collect_params(destination: Path) -> None:
  from openpilot.common.params import Params

  params = Params()
  lines = []
  for key in PARAM_KEYS:
    try:
      lines.append(f"{key}={decode_param(params.get(key))}")
    except Exception as exc:
      raise RuntimeError(f"Params read failed for {key}: {type(exc).__name__}: {exc}") from exc
  destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_capture(*args: str, cwd: Path | None = None) -> str:
  try:
    result = subprocess.run(args, cwd=cwd, check=False, capture_output=True, text=True, timeout=30)
  except Exception as exc:
    return f"ERROR:{type(exc).__name__}:{exc}\n"
  output = result.stdout
  if result.stderr:
    output += result.stderr
  if result.returncode != 0:
    output += f"returncode={result.returncode}\n"
  return output


def collect_git_state(repository_root: Path, destination: Path) -> None:
  sections = (
    ("HEAD", ("git", "rev-parse", "HEAD")),
    ("BRANCH", ("git", "branch", "--show-current")),
    ("STATUS", ("git", "status", "--short", "--branch")),
    ("RECENT GPS COMMITS", ("git", "log", "-20", "--oneline", "--decorate")),
  )
  lines = []
  for title, command in sections:
    lines.extend((f"===== {title} =====", run_capture(*command, cwd=repository_root).rstrip(), ""))
  destination.write_text("\n".join(lines), encoding="utf-8")


def read_boot_id() -> str:
  try:
    value = BOOT_ID_PATH.read_text(encoding="utf-8").strip()
  except OSError as exc:
    return f"ERROR:{type(exc).__name__}:{exc}"
  return value or "<empty>"


def copy_state_files(assistance_root: Path, destination: Path, current_boot_id: str) -> None:
  destination.mkdir(parents=True, exist_ok=True)
  lines = [
    "State files are current-device snapshots, not route-contained evidence.",
    f"capture_boot_id={current_boot_id}",
    f"captured_at_utc={datetime.now(UTC).isoformat()}",
    "",
  ]

  for name in STATE_FILES:
    source = assistance_root / name
    target = destination / name
    if not source.is_file():
      lines.append(f"{name}: missing")
      continue
    shutil.copy2(source, target)
    lines.append(f"{name}: copied bytes={target.stat().st_size}")

  (destination / "STATE_SCOPE.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_system_snapshot(destination: Path, current_boot_id: str) -> None:
  lines = [
    f"captured_at_utc={datetime.now(UTC).isoformat()}",
    f"current_boot_id={current_boot_id}",
    "",
    "===== UNAME =====",
    run_capture("uname", "-a").rstrip(),
    "",
    "===== UPTIME =====",
    run_capture("cat", "/proc/uptime").rstrip(),
    "",
    "===== PIGEOND/MANAGER PROCESSES =====",
    run_capture("pgrep", "-af", "system\\.ubloxd\\.pigeond|manager\\.py").rstrip(),
  ]
  destination.write_text("\n".join(lines) + "\n", encoding="utf-8")


def generate_checksums(bundle_root: Path) -> None:
  checksum_path = bundle_root / "SHA256SUMS.txt"
  entries = []
  for path in sorted(bundle_root.rglob("*")):
    if not path.is_file() or path == checksum_path:
      continue
    relative = path.relative_to(bundle_root).as_posix()
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    entries.append(f"{digest}  {relative}")
  checksum_path.write_text("\n".join(entries) + "\n", encoding="utf-8")


def verify_checksums(bundle_root: Path) -> None:
  checksum_path = bundle_root / "SHA256SUMS.txt"
  if not checksum_path.is_file():
    raise RuntimeError("SHA256SUMS.txt is missing")
  for line in checksum_path.read_text(encoding="utf-8").splitlines():
    digest, separator, relative = line.partition("  ")
    if not separator or not relative:
      raise RuntimeError(f"Invalid checksum entry: {line!r}")
    if relative.startswith("/") or "/data/" in relative:
      raise RuntimeError(f"Absolute checksum path is forbidden: {relative}")
    path = bundle_root / relative
    if not path.is_file():
      raise RuntimeError(f"Checksummed file is missing: {relative}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != digest:
      raise RuntimeError(f"Checksum mismatch for {relative}")


def validate_bundle(bundle_root: Path, selected_routes: Iterable[RouteSelection]) -> None:
  summary_path = bundle_root / "LATEST_ROUTE_SUMMARY.txt"
  params_path = bundle_root / "selected_params.txt"
  if not summary_path.is_file():
    raise RuntimeError("LATEST_ROUTE_SUMMARY.txt is missing")
  summary = summary_path.read_text(encoding="utf-8")
  for selection in selected_routes:
    if f"route={selection.route}" not in summary:
      raise RuntimeError(f"Requested route missing from summary: {selection.route}")
  if not params_path.is_file() or "ERROR:" in params_path.read_text(encoding="utf-8"):
    raise RuntimeError("Params collection failed")
  verify_checksums(bundle_root)


def build_bundle(
  selected_routes: list[RouteSelection],
  output_root: Path,
  repository_root: Path,
  assistance_root: Path,
) -> tuple[Path, Path]:
  timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
  final_directory = output_root / f"gps_drive_audit_{timestamp}"
  final_bundle = output_root / f"gps_drive_audit_{timestamp}.tar.gz"
  if final_directory.exists() or final_bundle.exists():
    raise RuntimeError(f"Audit output already exists for timestamp {timestamp}")

  output_root.mkdir(parents=True, exist_ok=True)
  with tempfile.TemporaryDirectory(prefix="gps_drive_audit_", dir=output_root) as temporary:
    bundle_root = Path(temporary) / final_directory.name
    bundle_root.mkdir()

    combined = [
      "================================================================",
      "LATEST ROUTE GPS SUMMARY",
      "================================================================",
      f"requested_routes={','.join(selection.route for selection in selected_routes)}",
      f"discovered_route_count={len(selected_routes)}",
      "",
    ]
    for selection in selected_routes:
      combined.extend(analyze_route(selection, bundle_root))
      combined.extend(("", "----------------------------------------------------------------", ""))
    (bundle_root / "LATEST_ROUTE_SUMMARY.txt").write_text("\n".join(combined) + "\n", encoding="utf-8")

    current_boot_id = read_boot_id()
    (bundle_root / "current_boot_id.txt").write_text(current_boot_id + "\n", encoding="utf-8")
    (bundle_root / "state_capture_utc.txt").write_text(datetime.now(UTC).isoformat() + "\n", encoding="utf-8")
    evidence_scope = "\n".join(
      (
        "Route summaries and assistance_events are derived from selected route logs.",
        "Files under state/current_boot are snapshots from the boot active when collection ran.",
        "A current state file must not be attributed to a route unless its boot identity is independently matched.",
      )
    )
    (bundle_root / "EVIDENCE_SCOPE.txt").write_text(evidence_scope + "\n", encoding="utf-8")
    collect_params(bundle_root / "selected_params.txt")
    collect_git_state(repository_root, bundle_root / "git_state.txt")
    write_system_snapshot(bundle_root / "system_snapshot.txt", current_boot_id)
    copy_state_files(assistance_root, bundle_root / "state" / "current_boot", current_boot_id)
    shutil.copy2(Path(__file__), bundle_root / "gps_drive_audit.py")

    generate_checksums(bundle_root)
    validate_bundle(bundle_root, selected_routes)
    shutil.move(str(bundle_root), final_directory)

  with tarfile.open(final_bundle, "w:gz") as archive:
    archive.add(final_directory, arcname=final_directory.name)
  return final_directory, final_bundle


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(description="Collect a boot-aware GPS cold-start route audit bundle")
  selection = parser.add_mutually_exclusive_group(required=True)
  selection.add_argument("--route", help="Exact route name, for example 00000093--a1ef00c9c2")
  selection.add_argument("--latest", type=int, help="Collect the newest N locally recorded routes")
  parser.add_argument("--realdata-root", type=Path, default=DEFAULT_REALDATA_ROOT)
  parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
  parser.add_argument("--repository-root", type=Path, default=DEFAULT_REPOSITORY_ROOT)
  parser.add_argument("--assistance-root", type=Path, default=DEFAULT_ASSISTANCE_ROOT)
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = parse_args(argv)
  try:
    discovered = discover_routes(args.realdata_root)
    selected = select_routes(discovered, args.route, args.latest)
    directory, bundle = build_bundle(selected, args.output_root, args.repository_root, args.assistance_root)
  except Exception as exc:
    print(f"RESULT: GPS_DRIVE_AUDIT_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1

  print("================================================================")
  print("RESULT: GPS_DRIVE_AUDIT_BUNDLE_CREATED")
  print("================================================================")
  print(f"Directory: {directory}")
  print(f"Bundle:    {bundle}")
  print("Routes:")
  for selection in selected:
    print(selection.route)
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
