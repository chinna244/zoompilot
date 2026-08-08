"""Authoritative GPS source arbitration for ublox primary / QCOM fallback.

Single authority for locationd and timed. Runtime-only (not persisted).

Startup: first sustained health-qualified fix wins (tie-break: ublox), once.
Long-term: ublox preferred; runtime hysteresis after first award.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum, auto


class SelectedSource(Enum):
  UBLOX_PRIMARY = auto()
  QCOM_FALLBACK = auto()
  NO_HEALTHY_SOURCE = auto()


class SourceHealth(Enum):
  UNKNOWN = auto()
  ACQUIRING = auto()
  HEALTHY = auto()
  UNHEALTHY = auto()


# Message cadences (Hz) from cereal/services.py.
UBLOX_GPS_HZ = 10.0
QCOM_GPS_HZ = 1.0

# Freshness: allow ~2 missed periods before declaring stale.
UBLOX_FRESH_SECONDS = 2.0 / UBLOX_GPS_HZ * 10.0  # 2.0s
QCOM_FRESH_SECONDS = 3.0  # 1 Hz + margin

# gpsSourceState publishes at 1 Hz — three missed updates = loss of authority.
GPS_SOURCE_STATE_FRESH_SECONDS = 3.0

# Initial u-blox *output* grace (not TTFF): GNSS START process-start deadline is
# 45s (pigeond). Detects dead output path only — does NOT gate QCOM startup.
UBLOX_GNSS_START_DEADLINE_SECONDS = 45.0
UBLOX_POST_START_OUTPUT_MARGIN_SECONDS = 15.0
UBLOX_INITIAL_OUTPUT_GRACE_SECONDS = UBLOX_GNSS_START_DEADLINE_SECONDS + UBLOX_POST_START_OUTPUT_MARGIN_SECONDS  # 60s

# Startup anti-glitch: continuous valid-fix sequence span (not TTFF).
STARTUP_HEALTH_CONFIRM_SECONDS = 1.0

# Sustained unhealthy before failover (not one missed sample).
UBLOX_UNHEALTHY_BEFORE_FAILOVER_SECONDS = 5.0

# Stricter recovery + anti-flap (QCOM -> ublox).
UBLOX_HEALTHY_BEFORE_RECOVERY_SECONDS = 10.0
QCOM_FALLBACK_MIN_DWELL_SECONDS = 5.0


@dataclass(frozen=True)
class GpsSample:
  """Normalized position sample for health evaluation (locationd-usable fields)."""

  recv_mono: float
  has_fix: bool
  latitude: float
  longitude: float
  horizontal_accuracy: float
  vertical_accuracy: float | None = None
  unix_timestamp_millis: float | None = None
  altitude: float = 0.0
  speed_accuracy: float = 1.0
  bearing_accuracy_deg: float = 1.0
  v_ned: tuple[float, float, float] = (0.0, 0.0, 0.0)
  measurement_mono_ns: int | None = None


@dataclass
class SourceHealthTrack:
  health: SourceHealth = SourceHealth.UNKNOWN
  last_sample_mono: float | None = None
  last_healthy_mono: float | None = None
  first_seen_mono: float | None = None
  unhealthy_since_mono: float | None = None
  healthy_since_mono: float | None = None
  # Continuous valid-fix sequence (startup confirmation; broken by invalid).
  continuous_valid_since_mono: float | None = None
  last_valid_sample_mono: float | None = None
  last_sample_was_valid: bool = False
  valid_sample_count_in_sequence: int = 0
  # After first valid fix this arbiter lifetime, no-fix is LOST_FIX not ACQUIRING.
  ever_had_valid_fix: bool = False


@dataclass
class ArbiterState:
  selected: SelectedSource = SelectedSource.NO_HEALTHY_SOURCE
  generation: int = 0
  transition_mono: float = 0.0
  transition_reason: str = "boot"
  ublox: SourceHealthTrack = field(default_factory=SourceHealthTrack)
  qcom: SourceHealthTrack = field(default_factory=SourceHealthTrack)
  failover_count: int = 0
  recovery_count: int = 0
  ublox_hardware_available: bool = True
  # First authority award ends the one-time startup race for this process.
  startup_complete: bool = False
  last_authoritative_source: SelectedSource | None = None


def _finite_number(value: object) -> bool:
  if isinstance(value, bool) or not isinstance(value, (int, float)):
    return False
  return math.isfinite(value)


def sample_is_locationd_usable(sample: GpsSample) -> bool:
  """Health-qualified fix must be usable by locationd handle_gps field checks."""
  from openpilot.common.gps_measurement import locationd_position_fields_usable

  vert = sample.vertical_accuracy
  if vert is None:
    return False
  return locationd_position_fields_usable(
    has_fix=sample.has_fix,
    latitude=sample.latitude,
    longitude=sample.longitude,
    altitude=sample.altitude,
    horizontal_accuracy=sample.horizontal_accuracy,
    vertical_accuracy=vert,
    speed_accuracy=sample.speed_accuracy,
    bearing_accuracy_deg=sample.bearing_accuracy_deg,
    v_ned=sample.v_ned,
    measurement_mono_ns=sample.measurement_mono_ns,
    event_mono_s=sample.recv_mono,
    require_measurement_mono=True,
  )


def ublox_sample_is_valid_fix(sample: GpsSample) -> bool:
  """Affirmative valid u-blox fix evidence usable by locationd (fail closed)."""
  if sample.unix_timestamp_millis is not None and not _finite_number(sample.unix_timestamp_millis):
    return False
  return sample_is_locationd_usable(sample)


def qcom_sample_is_valid_fix(sample: GpsSample) -> bool:
  """PR79/PR81 QCOM fix usable by locationd (fail closed)."""
  if sample.unix_timestamp_millis is not None and not _finite_number(sample.unix_timestamp_millis):
    return False
  return sample_is_locationd_usable(sample)


def _age(now_mono: float, then: float | None) -> float | None:
  if then is None:
    return None
  if not math.isfinite(now_mono) or not math.isfinite(then):
    return None
  age = now_mono - then
  if age < 0.0:
    return None
  return age


class GpsSourceArbiter:
  """Monotonic, fail-closed GPS source state machine."""

  def __init__(
    self,
    *,
    ublox_hardware_available: bool = True,
    ublox_initial_output_grace_seconds: float = UBLOX_INITIAL_OUTPUT_GRACE_SECONDS,
    startup_health_confirm_seconds: float = STARTUP_HEALTH_CONFIRM_SECONDS,
    ublox_unhealthy_before_failover_seconds: float = UBLOX_UNHEALTHY_BEFORE_FAILOVER_SECONDS,
    ublox_healthy_before_recovery_seconds: float = UBLOX_HEALTHY_BEFORE_RECOVERY_SECONDS,
    qcom_fallback_min_dwell_seconds: float = QCOM_FALLBACK_MIN_DWELL_SECONDS,
    ublox_fresh_seconds: float = UBLOX_FRESH_SECONDS,
    qcom_fresh_seconds: float = QCOM_FRESH_SECONDS,
  ) -> None:
    self.ublox_initial_output_grace_seconds = ublox_initial_output_grace_seconds
    self.startup_health_confirm_seconds = startup_health_confirm_seconds
    self.ublox_unhealthy_before_failover_seconds = ublox_unhealthy_before_failover_seconds
    self.ublox_healthy_before_recovery_seconds = ublox_healthy_before_recovery_seconds
    self.qcom_fallback_min_dwell_seconds = qcom_fallback_min_dwell_seconds
    self.ublox_fresh_seconds = ublox_fresh_seconds
    self.qcom_fresh_seconds = qcom_fresh_seconds
    self.state = ArbiterState(ublox_hardware_available=ublox_hardware_available)
    self._boot_mono: float | None = None

  def reset(self, *, now_mono: float, ublox_hardware_available: bool | None = None) -> None:
    """Discard all health evidence (process boot / arbiter restart)."""
    hw = self.state.ublox_hardware_available if ublox_hardware_available is None else ublox_hardware_available
    self.state = ArbiterState(
      selected=SelectedSource.NO_HEALTHY_SOURCE,
      generation=0,
      transition_mono=now_mono,
      transition_reason="arbiter_reset",
      ublox_hardware_available=hw,
      startup_complete=False,
      last_authoritative_source=None,
    )
    self._boot_mono = now_mono

  def observe_ublox(self, sample: GpsSample | None, *, now_mono: float) -> None:
    self._observe(
      track=self.state.ublox,
      sample=sample,
      now_mono=now_mono,
      fresh_seconds=self.ublox_fresh_seconds,
      valid_fix=ublox_sample_is_valid_fix if sample is not None else None,
      acquiring_ok=True,
      enforce_initial_output_grace=True,
    )

  def observe_qcom(self, sample: GpsSample | None, *, now_mono: float) -> None:
    self._observe(
      track=self.state.qcom,
      sample=sample,
      now_mono=now_mono,
      fresh_seconds=self.qcom_fresh_seconds,
      valid_fix=qcom_sample_is_valid_fix if sample is not None else None,
      acquiring_ok=False,
      enforce_initial_output_grace=False,
    )

  def _break_valid_sequence(self, track: SourceHealthTrack) -> None:
    track.continuous_valid_since_mono = None
    track.valid_sample_count_in_sequence = 0
    track.last_sample_was_valid = False
    track.healthy_since_mono = None

  def _observe(
    self,
    *,
    track: SourceHealthTrack,
    sample: GpsSample | None,
    now_mono: float,
    fresh_seconds: float,
    valid_fix,
    acquiring_ok: bool,
    enforce_initial_output_grace: bool,
  ) -> None:
    if self._boot_mono is None:
      self._boot_mono = now_mono
    if not math.isfinite(now_mono):
      track.health = SourceHealth.UNHEALTHY
      return

    if sample is not None:
      if not math.isfinite(sample.recv_mono) or sample.recv_mono > now_mono + 1e-3:
        track.health = SourceHealth.UNHEALTHY
        track.unhealthy_since_mono = now_mono if track.unhealthy_since_mono is None else track.unhealthy_since_mono
        self._break_valid_sequence(track)
        return
      if track.first_seen_mono is None:
        track.first_seen_mono = sample.recv_mono
      track.last_sample_mono = sample.recv_mono

      is_valid = valid_fix is not None and valid_fix(sample)
      if is_valid:
        if track.continuous_valid_since_mono is None or not track.last_sample_was_valid:
          track.continuous_valid_since_mono = sample.recv_mono
          track.valid_sample_count_in_sequence = 1
        else:
          track.valid_sample_count_in_sequence += 1
        track.last_valid_sample_mono = sample.recv_mono
        track.last_sample_was_valid = True
        track.last_healthy_mono = sample.recv_mono
        track.ever_had_valid_fix = True
      else:
        # Invalid/no-fix/malformed immediately breaks continuous-valid evidence.
        self._break_valid_sequence(track)

    valid_age = _age(now_mono, track.last_valid_sample_mono)
    valid_fresh = valid_age is not None and valid_age <= fresh_seconds
    sample_age = _age(now_mono, track.last_sample_mono)
    sample_fresh = sample_age is not None and sample_age <= fresh_seconds

    # Position health follows last valid-fix freshness. An isolated invalid
    # sample does not immediately fail over while the last valid remains fresh.
    # Startup qualification separately requires the latest sample to be valid
    # plus a repeated continuous valid-fix sequence.
    if valid_fresh and track.ever_had_valid_fix:
      track.health = SourceHealth.HEALTHY
      track.unhealthy_since_mono = None
      if track.last_sample_was_valid and track.continuous_valid_since_mono is not None:
        if track.healthy_since_mono is None:
          track.healthy_since_mono = track.continuous_valid_since_mono
      else:
        # Broken continuous-valid sequence resets recovery dwell clock.
        track.healthy_since_mono = None
      return

    # Initial acquisition only: never had a valid fix this cycle.
    if sample_fresh and acquiring_ok and not track.ever_had_valid_fix:
      track.health = SourceHealth.ACQUIRING
      track.healthy_since_mono = None
      track.unhealthy_since_mono = None
      return

    # Post-fix: last valid expired beyond healthy-fresh allowance -> LOST_FIX.
    if track.ever_had_valid_fix:
      track.health = SourceHealth.UNHEALTHY
      track.healthy_since_mono = None
      if track.unhealthy_since_mono is None:
        track.unhealthy_since_mono = now_mono
      return

    if track.last_sample_mono is None and track.last_valid_sample_mono is None:
      if enforce_initial_output_grace:
        boot_age = _age(now_mono, self._boot_mono)
        if boot_age is not None and boot_age >= self.ublox_initial_output_grace_seconds:
          track.health = SourceHealth.UNHEALTHY
          if track.unhealthy_since_mono is None:
            track.unhealthy_since_mono = now_mono
          track.healthy_since_mono = None
          return
      track.health = SourceHealth.UNKNOWN
      return

    track.health = SourceHealth.UNHEALTHY
    track.healthy_since_mono = None
    if track.unhealthy_since_mono is None:
      track.unhealthy_since_mono = now_mono

  def _startup_qualified(self, track: SourceHealthTrack, *, now_mono: float, fresh_seconds: float) -> bool:
    """Repeated continuous valid fixes spanning confirmation (not one stale valid)."""
    if track.health != SourceHealth.HEALTHY:
      return False
    if not track.last_sample_was_valid:
      return False
    if track.continuous_valid_since_mono is None or track.last_valid_sample_mono is None:
      return False
    if track.valid_sample_count_in_sequence < 2:
      return False
    valid_age = _age(now_mono, track.last_valid_sample_mono)
    if valid_age is None or valid_age > fresh_seconds:
      return False
    span = _age(track.last_valid_sample_mono, track.continuous_valid_since_mono)
    return span is not None and span >= self.startup_health_confirm_seconds

  def _award(self, selected: SelectedSource, *, now_mono: float, reason: str) -> None:
    prev = self.state.selected
    if selected == SelectedSource.QCOM_FALLBACK and prev == SelectedSource.UBLOX_PRIMARY:
      self.state.failover_count += 1
    if selected == SelectedSource.UBLOX_PRIMARY and prev == SelectedSource.QCOM_FALLBACK:
      self.state.recovery_count += 1
    self.state.selected = selected
    self.state.generation += 1
    self.state.transition_mono = now_mono
    self.state.transition_reason = reason
    if selected != SelectedSource.NO_HEALTHY_SOURCE:
      self.state.startup_complete = True
      self.state.last_authoritative_source = selected
    # NO_HEALTHY_SOURCE preserves startup_complete and last_authoritative_source.

  def step(self, *, now_mono: float) -> ArbiterState:
    """Advance selection using current health tracks."""
    if self._boot_mono is None:
      self._boot_mono = now_mono
    if not math.isfinite(now_mono):
      return self.state

    self.observe_ublox(None, now_mono=now_mono)
    self.observe_qcom(None, now_mono=now_mono)

    ublox_ok = self.state.ublox.health == SourceHealth.HEALTHY
    ublox_acquiring = self.state.ublox.health == SourceHealth.ACQUIRING
    qcom_ok = self.state.qcom.health == SourceHealth.HEALTHY

    ublox_unhealthy_sustained = False
    if self.state.ublox.health == SourceHealth.UNHEALTHY and self.state.ublox.unhealthy_since_mono is not None:
      age = _age(now_mono, self.state.ublox.unhealthy_since_mono)
      ublox_unhealthy_sustained = age is not None and age >= self.ublox_unhealthy_before_failover_seconds

    ublox_healthy_sustained = False
    if ublox_ok and self.state.ublox.healthy_since_mono is not None:
      age = _age(now_mono, self.state.ublox.healthy_since_mono)
      ublox_healthy_sustained = age is not None and age >= self.ublox_healthy_before_recovery_seconds

    ublox_startup_ok = self.state.ublox_hardware_available and self._startup_qualified(
      self.state.ublox, now_mono=now_mono, fresh_seconds=self.ublox_fresh_seconds
    )
    qcom_startup_ok = self._startup_qualified(self.state.qcom, now_mono=now_mono, fresh_seconds=self.qcom_fresh_seconds)

    selected = self.state.selected
    reason = None

    if not self.state.ublox_hardware_available:
      if not self.state.startup_complete and selected == SelectedSource.NO_HEALTHY_SOURCE:
        if qcom_startup_ok:
          selected = SelectedSource.QCOM_FALLBACK
          reason = "startup_qcom_first_reliable_fix"
      elif qcom_ok:
        selected = SelectedSource.QCOM_FALLBACK
      else:
        if selected != SelectedSource.NO_HEALTHY_SOURCE:
          reason = "qcom_only_hardware_unhealthy"
        selected = SelectedSource.NO_HEALTHY_SOURCE
    elif selected == SelectedSource.NO_HEALTHY_SOURCE and not self.state.startup_complete:
      # One-time INITIAL STARTUP race only.
      if ublox_startup_ok and qcom_startup_ok:
        selected = SelectedSource.UBLOX_PRIMARY
        reason = "startup_tie_ublox"
      elif ublox_startup_ok:
        selected = SelectedSource.UBLOX_PRIMARY
        reason = "startup_ublox_first_reliable_fix"
      elif qcom_startup_ok:
        selected = SelectedSource.QCOM_FALLBACK
        reason = "startup_qcom_first_reliable_fix"
    elif selected == SelectedSource.NO_HEALTHY_SOURCE and self.state.startup_complete:
      # RUNTIME recovery — never re-enter 1s startup race.
      last = self.state.last_authoritative_source
      if last == SelectedSource.QCOM_FALLBACK:
        if ublox_healthy_sustained:
          selected = SelectedSource.UBLOX_PRIMARY
          reason = "runtime_ublox_recovery_from_none"
        elif qcom_ok:
          selected = SelectedSource.QCOM_FALLBACK
          reason = "runtime_qcom_restored_from_none"
      elif last == SelectedSource.UBLOX_PRIMARY:
        if ublox_ok:
          selected = SelectedSource.UBLOX_PRIMARY
          reason = "runtime_ublox_restored_from_none"
        elif qcom_ok:
          selected = SelectedSource.QCOM_FALLBACK
          reason = "runtime_qcom_from_none_after_ublox"
      else:
        # Should not happen; prefer ublox if healthy else qcom.
        if ublox_ok:
          selected = SelectedSource.UBLOX_PRIMARY
          reason = "runtime_ublox_from_none"
        elif qcom_ok:
          selected = SelectedSource.QCOM_FALLBACK
          reason = "runtime_qcom_from_none"
    elif selected == SelectedSource.UBLOX_PRIMARY:
      # Post-fix ACQUIRING is no longer used; loss -> UNHEALTHY path.
      if ublox_ok or (ublox_acquiring and not self.state.ublox.ever_had_valid_fix):
        selected = SelectedSource.UBLOX_PRIMARY
      elif ublox_unhealthy_sustained and qcom_ok:
        selected = SelectedSource.QCOM_FALLBACK
        reason = "ublox_sustained_unhealthy_qcom_healthy"
      elif ublox_unhealthy_sustained and not qcom_ok:
        selected = SelectedSource.NO_HEALTHY_SOURCE
        reason = "ublox_sustained_unhealthy_qcom_unhealthy"
      else:
        selected = SelectedSource.UBLOX_PRIMARY
    elif selected == SelectedSource.QCOM_FALLBACK:
      dwell = _age(now_mono, self.state.transition_mono)
      dwell_ok = dwell is not None and dwell >= self.qcom_fallback_min_dwell_seconds
      if ublox_healthy_sustained and dwell_ok:
        selected = SelectedSource.UBLOX_PRIMARY
        reason = "ublox_sustained_recovery"
      elif qcom_ok:
        selected = SelectedSource.QCOM_FALLBACK
      else:
        selected = SelectedSource.NO_HEALTHY_SOURCE
        reason = "qcom_lost"
    else:
      selected = SelectedSource.NO_HEALTHY_SOURCE

    if selected != self.state.selected:
      self._award(selected, now_mono=now_mono, reason=reason or "transition")
    return self.state

  def message_is_authoritative(self, *, source: SelectedSource, recv_mono: float) -> bool:
    """Reject pre-transition samples and non-selected sources."""
    if source != self.state.selected:
      return False
    if self.state.selected == SelectedSource.NO_HEALTHY_SOURCE:
      return False
    if not math.isfinite(recv_mono):
      return False
    return recv_mono > self.state.transition_mono
