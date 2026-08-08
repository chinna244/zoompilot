#!/usr/bin/env python3
"""Authoritative GPS source arbitration daemon (PR80).

Publishes gpsSourceState for locationd and timed. Does not persist selection.
"""

from __future__ import annotations

import time
from typing import NoReturn

import openpilot.cereal.messaging as messaging
from openpilot.common.gps_source_arbiter import (
  GpsSample,
  GpsSourceArbiter,
  SelectedSource,
  SourceHealth,
)
from openpilot.common.swaglog import cloudlog
from openpilot.system.manager.process_config import ublox_available


_HEALTH_TO_CEREAL = {
  SourceHealth.UNKNOWN: "unknown",
  SourceHealth.ACQUIRING: "acquiring",
  SourceHealth.HEALTHY: "healthy",
  SourceHealth.UNHEALTHY: "unhealthy",
}

_SELECTED_TO_CEREAL = {
  SelectedSource.UBLOX_PRIMARY: "ubloxPrimary",
  SelectedSource.QCOM_FALLBACK: "qcomFallback",
  SelectedSource.NO_HEALTHY_SOURCE: "noHealthySource",
}


def safe_cloudlog(method: str, message: str) -> None:
  """Fail-open diagnostic wrapper: logging exceptions must never alter arbitration."""
  try:
    getattr(cloudlog, method)(message)
  except Exception:
    pass


def _age_or_neg1(now_mono: float, then: float | None) -> float:
  if then is None:
    return -1.0
  age = now_mono - then
  if age < 0.0:
    return -1.0
  return float(age)


def _gps_msg_to_sample(msg, recv_mono: float) -> GpsSample:
  vned = tuple(float(v) for v in msg.vNED[:3]) if len(msg.vNED) >= 3 else (float("nan"), float("nan"), float("nan"))
  meas_ns = int(msg.measurementMonoNs) if hasattr(msg, "measurementMonoNs") else 0
  return GpsSample(
    recv_mono=recv_mono,
    has_fix=bool(msg.hasFix),
    latitude=float(msg.latitude),
    longitude=float(msg.longitude),
    horizontal_accuracy=float(msg.horizontalAccuracy),
    vertical_accuracy=float(msg.verticalAccuracy),
    unix_timestamp_millis=float(msg.unixTimestampMillis),
    altitude=float(msg.altitude),
    speed_accuracy=float(msg.speedAccuracy),
    bearing_accuracy_deg=float(msg.bearingAccuracyDeg),
    v_ned=(vned[0], vned[1], vned[2]),
    measurement_mono_ns=meas_ns if meas_ns > 0 else None,
  )


def _publish_state(pm: messaging.PubMaster, arbiter: GpsSourceArbiter, now_mono: float) -> bool:
  """Publish gpsSourceState. Returns False on publish failure; never mutates arbiter."""
  st = arbiter.state
  try:
    msg = messaging.new_message("gpsSourceState", valid=True)
    g = msg.gpsSourceState
    g.selected = _SELECTED_TO_CEREAL[st.selected]
    g.generation = int(st.generation)
    g.transitionMonoNs = int(max(0.0, st.transition_mono) * 1e9)
    g.transitionReason = st.transition_reason
    g.ubloxHealth = _HEALTH_TO_CEREAL[st.ublox.health]
    g.qcomHealth = _HEALTH_TO_CEREAL[st.qcom.health]
    g.ubloxAgeS = _age_or_neg1(now_mono, st.ublox.last_sample_mono)
    g.qcomAgeS = _age_or_neg1(now_mono, st.qcom.last_sample_mono)
    g.ubloxHealthyAgeS = _age_or_neg1(now_mono, st.ublox.last_healthy_mono)
    g.qcomHealthyAgeS = _age_or_neg1(now_mono, st.qcom.last_healthy_mono)
    g.failoverCount = int(st.failover_count)
    g.recoveryCount = int(st.recovery_count)
    g.ubloxHardwareAvailable = bool(st.ublox_hardware_available)
    pm.send("gpsSourceState", msg)
    return True
  except Exception:
    safe_cloudlog("exception", "gpsard.publish_failed")
    return False


def main() -> NoReturn:
  hw = ublox_available()
  arbiter = GpsSourceArbiter(ublox_hardware_available=hw)
  now0 = time.monotonic()
  arbiter.reset(now_mono=now0, ublox_hardware_available=hw)

  sm = messaging.SubMaster(["gpsLocationExternal", "gpsLocation"])
  pm = messaging.PubMaster(["gpsSourceState"])

  last_unhealthy_log = 0.0

  safe_cloudlog("info", f"gpsard start ublox_hw={hw}")

  while True:
    sm.update(100)
    now = time.monotonic()

    # Re-check hardware presence rarely (USB/persist flag).
    hw_now = ublox_available()
    if hw_now != arbiter.state.ublox_hardware_available:
      safe_cloudlog("warning", f"gpsard ublox_hw changed {arbiter.state.ublox_hardware_available} -> {hw_now}")
      arbiter.reset(now_mono=now, ublox_hardware_available=hw_now)

    if sm.updated["gpsLocationExternal"]:
      recv = sm.logMonoTime["gpsLocationExternal"] / 1e9
      arbiter.observe_ublox(_gps_msg_to_sample(sm["gpsLocationExternal"], recv), now_mono=now)
    if sm.updated["gpsLocation"]:
      recv = sm.logMonoTime["gpsLocation"] / 1e9
      arbiter.observe_qcom(_gps_msg_to_sample(sm["gpsLocation"], recv), now_mono=now)

    prev = arbiter.state.selected
    arbiter.step(now_mono=now)
    if arbiter.state.selected != prev:
      safe_cloudlog(
        "warning",
        f"gpsard transition {prev.name} -> {arbiter.state.selected.name} reason={arbiter.state.transition_reason} gen={arbiter.state.generation}",
      )

    # Rate-limit recurring unhealthy diagnostics (~1/min).
    if arbiter.state.selected == SelectedSource.NO_HEALTHY_SOURCE and (now - last_unhealthy_log) > 60.0:
      safe_cloudlog(
        "warning",
        f"gpsard no_healthy_source ublox={arbiter.state.ublox.health.name} qcom={arbiter.state.qcom.health.name}",
      )
      last_unhealthy_log = now

    _publish_state(pm, arbiter, now)


if __name__ == "__main__":
  main()
