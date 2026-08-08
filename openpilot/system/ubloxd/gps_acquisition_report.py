"""Pure functions for GPS route acquisition classification and TTFF reporting."""

from __future__ import annotations

import re
from typing import Any

MILESTONE_PATTERN = re.compile(r"GPS acquisition milestone=(\w+)")
FIELD_PATTERN = re.compile(r"(\w+)=([^,]+)")

SOURCE_UBLOX = "ubloxPrimary"
SOURCE_QCOM = "qcomFallback"
SOURCE_NO_HEALTHY = "noHealthySource"

DBD_RESTORE_SUCCESS = frozenset({"restored", "accepted", "applied", "success", "restore_partial"})
DBD_RESTORE_SKIPPED_PREFIX = "skipped"

CLASSIFICATIONS = (
  "HEALTHY_FAST_ACQUISITION",
  "RF_LIMITED",
  "NAV_DATA_OR_EPHEMERIS_LIMITED",
  "RECEIVER_OUTPUT_FAILURE",
  "CONFIGURATION_FAILURE",
  "PARSER_OR_TRANSPORT_FAILURE",
  "ASSISTANCE_LATE",
  "DBD_ASSISTED",
  "QCOM_STARTUP_WINNER",
  "QCOM_RUNTIME_FAILOVER",
  "UBLOX_RECOVERED",
  "NO_HEALTHY_GPS_SOURCE",
  "INSUFFICIENT_TELEMETRY",
)

START_TYPES = (
  "WARM_START",
  "SHORT_STOP_DBD",
  "OVERNIGHT_OFFLINE_COLD_START",
  "OVERNIGHT_ONLINE_START",
  "UNKNOWN_START_TYPE",
)


def parse_log_fields(text: str) -> dict[str, str]:
  fields: dict[str, str] = {}
  for match in FIELD_PATTERN.finditer(text):
    fields[match.group(1)] = match.group(2).strip()
  return fields


def parse_milestone_log(text: str) -> tuple[str | None, dict[str, str]]:
  message = text
  if text.strip().startswith("{"):
    try:
      import json

      payload = json.loads(text)
      if isinstance(payload, dict) and isinstance(payload.get("msg"), str):
        message = payload["msg"]
    except (json.JSONDecodeError, TypeError):
      pass
  match = MILESTONE_PATTERN.search(message)
  if match is None:
    return None, parse_log_fields(message)
  return match.group(1), parse_log_fields(message)


def analyze_sat_flags(flags: int) -> dict[str, bool]:
  quality = flags & 0x07
  return {
    "tracked": True,
    "acquired": quality >= 2,
    "code_locked": quality >= 4,
    "used": bool(flags & (1 << 3)),
    "ephemeris": bool(flags & (1 << 11)),
    "almanac": bool(flags & (1 << 12)),
  }


def normalize_source_name(value: object) -> str | None:
  if value is None:
    return None
  text = str(value)
  if text in (SOURCE_UBLOX, SOURCE_QCOM, SOURCE_NO_HEALTHY):
    return text
  lowered = text.lower()
  if "ublox" in lowered or text in ("0", "ubloxprimary"):
    return SOURCE_UBLOX
  if "qcom" in lowered or text in ("1", "qcomfallback"):
    return SOURCE_QCOM
  if "nohealthy" in lowered or text in ("2", "nohealthysource"):
    return SOURCE_NO_HEALTHY
  return text


def normalize_dbd_restore_disposition(disposition: str) -> str:
  lowered = disposition.strip().lower()
  if not lowered:
    return "none"
  if lowered in DBD_RESTORE_SUCCESS:
    return "success"
  if lowered.startswith(DBD_RESTORE_SKIPPED_PREFIX) or "skipped" in lowered:
    return "skipped"
  if "reject" in lowered or "fail" in lowered or lowered.endswith("_failed"):
    return "rejected"
  return "unknown"


def gps_source_transition_is_new(
  *,
  selected: str | None,
  generation: int | None,
  transition_mono_ns: int | None,
  previous_selected: str | None,
  previous_generation: int | None,
  previous_transition_mono_ns: int | None,
) -> bool:
  decision = evaluate_gps_source_epoch(
    selected=selected,
    generation=generation,
    transition_mono_ns=transition_mono_ns,
    previous_selected=previous_selected,
    previous_generation=previous_generation,
    previous_transition_mono_ns=previous_transition_mono_ns,
  )
  return decision == "transition"


def evaluate_gps_source_epoch(
  *,
  selected: str | None,
  generation: int | None,
  transition_mono_ns: int | None,
  previous_selected: str | None,
  previous_generation: int | None,
  previous_transition_mono_ns: int | None,
  event_mono_s: float | None = None,
) -> str:
  """Evaluate a gpsSourceState epoch against PR80 monotonic authority rules.

  Returns one of:
    transition, refresh, reject_regressing, reject_inconsistent, reject_future
  """
  if selected is None:
    return "refresh"
  if previous_selected is None:
    if transition_mono_ns is not None and event_mono_s is not None:
      epoch_s = transition_mono_ns * 1e-9
      if epoch_s > event_mono_s + 2.0:
        return "reject_future"
    return "transition"

  # Historical states without transitionMonoNs: selected/generation compatibility.
  if transition_mono_ns is None:
    if selected != previous_selected or generation != previous_generation:
      return "transition"
    return "refresh"

  if previous_transition_mono_ns is None:
    if event_mono_s is not None and transition_mono_ns * 1e-9 > event_mono_s + 2.0:
      return "reject_future"
    return "transition"

  if transition_mono_ns < previous_transition_mono_ns:
    return "reject_regressing"
  if transition_mono_ns == previous_transition_mono_ns:
    if selected == previous_selected and generation == previous_generation:
      return "refresh"
    return "reject_inconsistent"
  # transition_mono_ns > previous_transition_mono_ns
  if event_mono_s is not None and transition_mono_ns * 1e-9 > event_mono_s + 2.0:
    return "reject_future"
  return "transition"


def authority_transition_time_s(
  *,
  transition_mono_ns: int | None,
  event_mono_s: float,
) -> float:
  """Prefer PR80 transitionMonoNs when present and not impossibly future."""
  if transition_mono_ns is None:
    return event_mono_s
  epoch_s = transition_mono_ns * 1e-9
  if epoch_s > event_mono_s + 2.0:
    return event_mono_s
  return epoch_s


def build_source_authority_intervals(metrics: Any) -> list[dict[str, Any]]:
  transitions = metrics.gps_source_transitions
  if not transitions:
    return []

  intervals: list[dict[str, Any]] = []
  route_end = metrics.route_end
  for index, transition in enumerate(transitions):
    start_t = transition.get("t")
    end_t = transitions[index + 1].get("t") if index + 1 < len(transitions) else route_end
    duration_s = None
    if start_t is not None and end_t is not None:
      duration_s = end_t - start_t
    intervals.append(
      {
        "selected": transition.get("selected"),
        "generation": transition.get("generation"),
        "transitionMonoNs": transition.get("transitionMonoNs"),
        "reason": transition.get("reason"),
        "start_t": start_t,
        "end_t": end_t,
        "duration_s": duration_s,
      }
    )
  return intervals


def compute_missing_telemetry(metrics: Any) -> list[str]:
  missing: list[str] = []
  if not metrics.has_gps_source_state:
    missing.append("gpsSourceState")
  if not metrics.has_measurement_mono_ns:
    missing.append("measurementMonoNs")
  if not metrics.has_sat_report:
    missing.append("ubloxGnss.satReport")
  if not metrics.has_rf_observability_logs:
    missing.append("gps_rf_observability_logs")
  if not metrics.has_qcom_gps:
    missing.append("gpsLocation")
  if metrics.gnss_start_mono is None:
    missing.append("gnss_start_mono")
  return missing


def _insufficient_telemetry(metrics: Any) -> bool:
  evidence_points = 0
  if metrics.gps_samples > 0:
    evidence_points += 1
  if metrics.has_gps_source_state:
    evidence_points += 1
  if metrics.rawx_reports > 0:
    evidence_points += 1
  if metrics.has_sat_report:
    evidence_points += 1
  if metrics.first_nav_pvt is not None or metrics.first_fix_ok is not None:
    evidence_points += 1
  if metrics.events:
    evidence_points += 1
  return evidence_points < 2


def _rf_limited_evidence(metrics: Any) -> bool:
  if _healthy_fast_evidence(metrics):
    return False
  if metrics.max_rf_jam_indicator is not None and metrics.max_rf_jam_indicator >= 50:
    return True
  if metrics.sat_reports_with_zero_signals >= 2 and metrics.max_sat_signal_count == 0:
    return True
  if metrics.rf_observability_zero_signal_count >= 2:
    return True
  if metrics.max_sat_signal_count > 0 and metrics.max_sat_code_locked == 0:
    return metrics.sat_report_count >= 2
  return False


def _nav_data_limited_evidence(metrics: Any) -> bool:
  if _healthy_fast_evidence(metrics):
    return False
  receiver_output_alive = metrics.first_nonempty_rawx is not None or metrics.max_sat_signal_count > 0 or metrics.rawx_reports > 0
  if not receiver_output_alive:
    return False
  if metrics.max_sat_code_locked < 2:
    return False
  if metrics.max_sat_ephemeris >= 1:
    return False
  if metrics.first_valid_gps_week is not None:
    return False
  if metrics.first_ephemeris is not None:
    return False
  if metrics.sat_reports_with_code_lock_and_zero_eph < 2:
    return False
  return True


def _receiver_output_failure_evidence(metrics: Any) -> bool:
  for _time, text in metrics.events:
    lowered = text.lower()
    if "no_data_watchdog" in lowered:
      return True
    if "required stream absent" in lowered:
      return True
    if "receiver output" in lowered or "output stream" in lowered:
      if any(token in lowered for token in ("fail", "absent", "missing", "timeout", "error", "stopped")):
        return True
    if "transport" in lowered and any(token in lowered for token in ("fail", "error", "timeout", "absent")):
      return True
  return False


def _runtime_no_healthy_count(metrics: Any) -> int:
  return int(getattr(metrics, "runtime_no_healthy_interval_count", 0) or 0)


def _startup_no_healthy_count(metrics: Any) -> int:
  return int(getattr(metrics, "startup_no_healthy_interval_count", 0) or 0)


def _healthy_fast_evidence(metrics: Any) -> bool:
  fix_time = metrics.first_reliable_fix or metrics.first_fix_ok or metrics.first_fix
  if fix_time is None:
    return False
  reference = metrics.gnss_start_mono if metrics.reference_policy == "gnss_start" else metrics.route_start
  if reference is None:
    return False
  ttff = fix_time - reference
  if ttff < 0 or ttff > 120.0:
    return False
  if metrics.gps_source_first_authoritative == SOURCE_NO_HEALTHY:
    return False
  if metrics.gps_source_first_authoritative not in (SOURCE_UBLOX, SOURCE_QCOM, None):
    return False
  # Startup NO_HEALTHY is expected; only runtime authority loss blocks healthy-fast.
  if _runtime_no_healthy_count(metrics) > 0:
    return False
  return True


def _has_failover_transition(metrics: Any) -> bool:
  for transition in metrics.gps_source_transitions:
    reason = str(transition.get("reason", "")).lower()
    selected = transition.get("selected")
    if selected == SOURCE_QCOM and "failover" in reason:
      return True
  return metrics.max_failover_count > 0


def _has_recovery_transition(metrics: Any) -> bool:
  for transition in metrics.gps_source_transitions:
    reason = str(transition.get("reason", "")).lower()
    selected = transition.get("selected")
    if selected == SOURCE_UBLOX and "recover" in reason:
      return True
  return metrics.max_recovery_count > 0


def classify_acquisition(metrics: Any) -> str:
  if _insufficient_telemetry(metrics):
    return "INSUFFICIENT_TELEMETRY"

  for _time, text in metrics.events:
    lowered = text.lower()
    if "ubloxd parser error" in lowered or "parser error" in lowered:
      return "PARSER_OR_TRANSPORT_FAILURE"

  if metrics.configuration_failure_evidence:
    return "CONFIGURATION_FAILURE"

  # Runtime authority loss after a source was established is a failure classification.
  if _runtime_no_healthy_count(metrics) > 0:
    return "NO_HEALTHY_GPS_SOURCE"

  # Route never established an authoritative source.
  if metrics.gps_source_first_authoritative is None and (_startup_no_healthy_count(metrics) > 0 or metrics.no_healthy_interval_count > 0):
    return "NO_HEALTHY_GPS_SOURCE"

  if metrics.gps_source_first_authoritative == SOURCE_QCOM:
    return "QCOM_STARTUP_WINNER"

  if _has_recovery_transition(metrics):
    return "UBLOX_RECOVERED"

  if _has_failover_transition(metrics):
    return "QCOM_RUNTIME_FAILOVER"

  if metrics.dbd_assisted_evidence:
    return "DBD_ASSISTED"

  if metrics.assistance_late_evidence:
    return "ASSISTANCE_LATE"

  healthy_fast = _healthy_fast_evidence(metrics)
  # Transient RF/NAV snapshots must not override a proven healthy-fast acquisition.
  if _rf_limited_evidence(metrics) and not healthy_fast:
    return "RF_LIMITED"

  if _nav_data_limited_evidence(metrics) and not healthy_fast:
    return "NAV_DATA_OR_EPHEMERIS_LIMITED"

  if _receiver_output_failure_evidence(metrics):
    return "RECEIVER_OUTPUT_FAILURE"

  if healthy_fast:
    return "HEALTHY_FAST_ACQUISITION"

  return "INSUFFICIENT_TELEMETRY"


def infer_start_type(metrics: Any) -> str:
  if metrics.dbd_assisted_evidence and metrics.short_stop_dbd_evidence:
    return "SHORT_STOP_DBD"
  if metrics.overnight_offline_evidence:
    return "OVERNIGHT_OFFLINE_COLD_START"
  if metrics.overnight_online_evidence:
    return "OVERNIGHT_ONLINE_START"
  if metrics.warm_start_evidence:
    return "WARM_START"
  return "UNKNOWN_START_TYPE"


def resolve_reference_policy(metrics: Any) -> str:
  if metrics.gnss_start_mono is not None:
    return "gnss_start"
  return "route_start"


def reference_mono(metrics: Any) -> float | None:
  if metrics.reference_policy == "gnss_start":
    return metrics.gnss_start_mono
  return metrics.route_start


def relative_seconds(metrics: Any, value: float | None) -> float | None:
  if value is None:
    return None
  reference = reference_mono(metrics)
  if reference is None:
    return None
  return value - reference


def _milestone_fields(metrics: Any) -> dict[str, float | None]:
  return {
    "gnss_start": relative_seconds(metrics, metrics.gnss_start_mono),
    "first_nav_pvt": relative_seconds(metrics, metrics.first_nav_pvt),
    "first_fix_ok": relative_seconds(metrics, metrics.first_fix_ok),
    "first_reliable_fix": relative_seconds(metrics, metrics.first_reliable_fix),
    "first_fix": relative_seconds(metrics, metrics.first_fix),
    "first_fix_ublox": relative_seconds(metrics, metrics.first_fix_ublox),
    "first_fix_qcom": relative_seconds(metrics, metrics.first_fix_qcom),
    "first_tracked_sv": relative_seconds(metrics, metrics.first_tracked_sv),
    "first_code_lock": relative_seconds(metrics, metrics.first_code_lock),
    "first_3_used": relative_seconds(metrics, metrics.first_3_used),
    "first_4_used": relative_seconds(metrics, metrics.first_4_used),
    "first_rawx": relative_seconds(metrics, metrics.first_rawx),
    "first_nonempty_rawx": relative_seconds(metrics, metrics.first_nonempty_rawx),
    "first_valid_gps_week": relative_seconds(metrics, metrics.first_valid_gps_week),
    "first_valid_leap_second": relative_seconds(metrics, metrics.first_valid_leap_second),
    "first_gps_measurement": relative_seconds(metrics, metrics.first_gps_measurement),
    "first_glonass_measurement": relative_seconds(metrics, metrics.first_glonass_measurement),
    "first_ephemeris": relative_seconds(metrics, metrics.first_ephemeris),
    "first_receiver_utc": relative_seconds(metrics, metrics.first_receiver_utc),
    "first_authoritative": relative_seconds(metrics, metrics.first_authoritative),
  }


def ttff_report_lines(metrics: Any) -> list[str]:
  reference = metrics.reference_policy
  milestones = _milestone_fields(metrics)
  lines = [
    "===== TTFF / ACQUISITION REPORT =====",
    f"classification={metrics.classification}",
    f"start_type={metrics.start_type}",
    f"reference_policy={reference}",
    f"missing_telemetry={','.join(metrics.missing_telemetry) if metrics.missing_telemetry else 'none'}",
    f"dbd_restore_disposition={metrics.dbd_restore_disposition}",
    f"assistance_late={str(metrics.assistance_late_evidence).lower()}",
    "",
    "----- presence flags -----",
    f"has_gps_source_state={metrics.has_gps_source_state}",
    f"has_measurement_mono_ns={metrics.has_measurement_mono_ns}",
    f"has_sat_report={metrics.has_sat_report}",
    f"has_rf_observability_logs={metrics.has_rf_observability_logs}",
    f"has_qcom_gps={metrics.has_qcom_gps}",
    f"trusted_time_available={metrics.trusted_time_available}",
    "",
    "----- milestones (seconds since reference) -----",
  ]
  for name, value in milestones.items():
    lines.append(f"{name}_s={value}")
  lines.extend(
    (
      "",
      "----- measurementMonoNs -----",
      f"samples_with_stamp={metrics.measurement_mono_samples_with_stamp}",
      f"first_positive_stamp_s={relative_seconds(metrics, metrics.measurement_mono_first_positive)}",
      f"max_meas_to_pub_age_s={metrics.measurement_mono_max_age_s}",
      "",
      "----- gpsSourceState -----",
      f"first_selected={metrics.gps_source_first_selected}",
      f"first_authoritative={metrics.gps_source_first_authoritative}",
      f"max_failover_count={metrics.max_failover_count}",
      f"max_recovery_count={metrics.max_recovery_count}",
      f"no_healthy_interval_count={metrics.no_healthy_interval_count}",
      f"startup_no_healthy_interval_count={getattr(metrics, 'startup_no_healthy_interval_count', 0)}",
      f"runtime_no_healthy_interval_count={getattr(metrics, 'runtime_no_healthy_interval_count', 0)}",
      f"transition_count={len(metrics.gps_source_transitions)}",
      "",
      "----- source authority intervals -----",
    )
  )
  if metrics.source_authority_intervals:
    for interval in metrics.source_authority_intervals:
      lines.append(
        "interval="
        + f"selected={interval.get('selected')},"
        + f"generation={interval.get('generation')},"
        + f"transitionMonoNs={interval.get('transitionMonoNs')},"
        + f"reason={interval.get('reason')},"
        + f"start_t={interval.get('start_t')},"
        + f"end_t={interval.get('end_t')},"
        + f"duration_s={interval.get('duration_s')}"
      )
  else:
    lines.append("interval=none")
  lines.extend(
    (
      "",
      "----- no healthy intervals -----",
    )
  )
  if metrics.no_healthy_intervals:
    for interval in metrics.no_healthy_intervals:
      lines.append(
        "no_healthy="
        + f"phase={interval.get('phase')},"
        + f"start_t={interval.get('start_t')},"
        + f"end_t={interval.get('end_t')},"
        + f"duration_s={interval.get('duration_s')}"
      )
  else:
    lines.append("no_healthy=none")
  if getattr(metrics, "startup_no_healthy_intervals", None):
    lines.append("----- startup no healthy intervals -----")
    for interval in metrics.startup_no_healthy_intervals:
      lines.append(
        "startup_no_healthy=" + f"start_t={interval.get('start_t')}," + f"end_t={interval.get('end_t')}," + f"duration_s={interval.get('duration_s')}"
      )
  if getattr(metrics, "runtime_no_healthy_intervals", None):
    lines.append("----- runtime no healthy intervals -----")
    for interval in metrics.runtime_no_healthy_intervals:
      lines.append(
        "runtime_no_healthy=" + f"start_t={interval.get('start_t')}," + f"end_t={interval.get('end_t')}," + f"duration_s={interval.get('duration_s')}"
      )
  lines.extend(
    (
      "",
      "----- RF / NAV-SAT peaks -----",
      f"max_sat_signal_count={metrics.max_sat_signal_count}",
      f"max_sat_code_locked={metrics.max_sat_code_locked}",
      f"max_sat_used={metrics.max_sat_used}",
      f"max_sat_ephemeris={metrics.max_sat_ephemeris}",
      f"max_rf_jam_indicator={metrics.max_rf_jam_indicator}",
      "",
      "----- YUMA -----",
      f"yuma_attempted_s={relative_seconds(metrics, metrics.yuma_attempted)}",
      f"yuma_completed_s={relative_seconds(metrics, metrics.yuma_completed)}",
      f"yuma_failed_s={relative_seconds(metrics, metrics.yuma_failed)}",
      f"yuma_disposition={metrics.yuma_disposition}",
    )
  )
  return lines


def to_machine_report(metrics: Any) -> dict[str, Any]:
  transitions = [
    {
      "t_s": relative_seconds(metrics, transition.get("t")),
      "selected": transition.get("selected"),
      "generation": transition.get("generation"),
      "transitionMonoNs": transition.get("transitionMonoNs"),
      "reason": transition.get("reason"),
    }
    for transition in metrics.gps_source_transitions
  ]
  no_healthy_intervals = [
    {
      "phase": interval.get("phase"),
      "start_t_s": relative_seconds(metrics, interval.get("start_t")),
      "end_t_s": relative_seconds(metrics, interval.get("end_t")),
      "duration_s": interval.get("duration_s"),
    }
    for interval in metrics.no_healthy_intervals
  ]
  startup_no_healthy_intervals = [
    {
      "start_t_s": relative_seconds(metrics, interval.get("start_t")),
      "end_t_s": relative_seconds(metrics, interval.get("end_t")),
      "duration_s": interval.get("duration_s"),
    }
    for interval in getattr(metrics, "startup_no_healthy_intervals", [])
  ]
  runtime_no_healthy_intervals = [
    {
      "start_t_s": relative_seconds(metrics, interval.get("start_t")),
      "end_t_s": relative_seconds(metrics, interval.get("end_t")),
      "duration_s": interval.get("duration_s"),
    }
    for interval in getattr(metrics, "runtime_no_healthy_intervals", [])
  ]
  source_authority_intervals = [
    {
      "selected": interval.get("selected"),
      "generation": interval.get("generation"),
      "transitionMonoNs": interval.get("transitionMonoNs"),
      "reason": interval.get("reason"),
      "start_t_s": relative_seconds(metrics, interval.get("start_t")),
      "end_t_s": relative_seconds(metrics, interval.get("end_t")),
      "duration_s": interval.get("duration_s"),
    }
    for interval in metrics.source_authority_intervals
  ]
  return {
    "route": metrics.route,
    "classification": metrics.classification,
    "start_type": metrics.start_type,
    "reference_policy": metrics.reference_policy,
    "missing_telemetry": list(metrics.missing_telemetry),
    "dbd_restore_disposition": metrics.dbd_restore_disposition,
    "assistance_late": metrics.assistance_late_evidence,
    "presence": {
      "has_gps_source_state": metrics.has_gps_source_state,
      "has_measurement_mono_ns": metrics.has_measurement_mono_ns,
      "has_sat_report": metrics.has_sat_report,
      "has_rf_observability_logs": metrics.has_rf_observability_logs,
      "has_qcom_gps": metrics.has_qcom_gps,
      "trusted_time_available": metrics.trusted_time_available,
    },
    "milestones_s": _milestone_fields(metrics),
    "measurement_mono_ns": {
      "samples_with_stamp": metrics.measurement_mono_samples_with_stamp,
      "first_positive_stamp_s": relative_seconds(metrics, metrics.measurement_mono_first_positive),
      "max_meas_to_pub_age_s": metrics.measurement_mono_max_age_s,
    },
    "gps_source_state": {
      "first_selected": metrics.gps_source_first_selected,
      "first_authoritative": metrics.gps_source_first_authoritative,
      "max_failover_count": metrics.max_failover_count,
      "max_recovery_count": metrics.max_recovery_count,
      "no_healthy_interval_count": metrics.no_healthy_interval_count,
      "startup_no_healthy_interval_count": getattr(metrics, "startup_no_healthy_interval_count", 0),
      "runtime_no_healthy_interval_count": getattr(metrics, "runtime_no_healthy_interval_count", 0),
      "transitions": transitions,
      "source_authority_intervals": source_authority_intervals,
      "no_healthy_intervals": no_healthy_intervals,
      "startup_no_healthy_intervals": startup_no_healthy_intervals,
      "runtime_no_healthy_intervals": runtime_no_healthy_intervals,
    },
    "rf_nav_sat_peaks": {
      "max_sat_signal_count": metrics.max_sat_signal_count,
      "max_sat_code_locked": metrics.max_sat_code_locked,
      "max_sat_used": metrics.max_sat_used,
      "max_sat_ephemeris": metrics.max_sat_ephemeris,
      "max_rf_jam_indicator": metrics.max_rf_jam_indicator,
    },
    "yuma": {
      "attempted_s": relative_seconds(metrics, metrics.yuma_attempted),
      "completed_s": relative_seconds(metrics, metrics.yuma_completed),
      "failed_s": relative_seconds(metrics, metrics.yuma_failed),
      "disposition": metrics.yuma_disposition,
    },
    "gnss_start_mono": metrics.gnss_start_mono,
    "route_start_mono": metrics.route_start,
  }
