"""Deterministic PR80 GPS source arbitration tests (first reliable fix + blockers)."""

from __future__ import annotations

import math

import pytest

from openpilot.common.gps_source_arbiter import (
  GpsSample,
  GpsSourceArbiter,
  SelectedSource,
  SourceHealth,
  STARTUP_HEALTH_CONFIRM_SECONDS,
  UBLOX_FRESH_SECONDS,
  UBLOX_HEALTHY_BEFORE_RECOVERY_SECONDS,
  UBLOX_INITIAL_OUTPUT_GRACE_SECONDS,
  UBLOX_UNHEALTHY_BEFORE_FAILOVER_SECONDS,
  QCOM_FALLBACK_MIN_DWELL_SECONDS,
  qcom_sample_is_valid_fix,
  ublox_sample_is_valid_fix,
)


def _valid_ublox(t: float, **kwargs) -> GpsSample:
  meas_ns = kwargs.get("measurement_mono_ns", int(t * 1e9))
  return GpsSample(
    recv_mono=t,
    has_fix=kwargs.get("has_fix", True),
    latitude=kwargs.get("latitude", 37.0),
    longitude=kwargs.get("longitude", -122.0),
    horizontal_accuracy=kwargs.get("horizontal_accuracy", 2.0),
    vertical_accuracy=kwargs.get("vertical_accuracy", 3.0),
    unix_timestamp_millis=kwargs.get("unix_timestamp_millis", 1.7e12),
    altitude=kwargs.get("altitude", 10.0),
    speed_accuracy=kwargs.get("speed_accuracy", 0.5),
    bearing_accuracy_deg=kwargs.get("bearing_accuracy_deg", 5.0),
    v_ned=kwargs.get("v_ned", (0.0, 0.0, 0.0)),
    measurement_mono_ns=meas_ns,
  )


def _valid_qcom(t: float, **kwargs) -> GpsSample:
  meas_ns = kwargs.get("measurement_mono_ns", int(t * 1e9))
  return GpsSample(
    recv_mono=t,
    has_fix=kwargs.get("has_fix", True),
    latitude=kwargs.get("latitude", 37.1),
    longitude=kwargs.get("longitude", -122.1),
    horizontal_accuracy=kwargs.get("horizontal_accuracy", 5.0),
    vertical_accuracy=kwargs.get("vertical_accuracy", 8.0),
    unix_timestamp_millis=kwargs.get("unix_timestamp_millis", 1.7e12),
    altitude=kwargs.get("altitude", 12.0),
    speed_accuracy=kwargs.get("speed_accuracy", 0.5),
    bearing_accuracy_deg=kwargs.get("bearing_accuracy_deg", 5.0),
    v_ned=kwargs.get("v_ned", (0.0, 0.0, 0.0)),
    measurement_mono_ns=meas_ns,
  )


def _arbiter(**kwargs) -> GpsSourceArbiter:
  return GpsSourceArbiter(ublox_hardware_available=True, **kwargs)


def _feed_ublox_healthy(a: GpsSourceArbiter, t0: float, duration: float, step: float = 0.1) -> float:
  t = t0
  end = t0 + duration
  while t <= end + 1e-9:
    a.observe_ublox(_valid_ublox(t), now_mono=t)
    a.step(now_mono=t)
    t += step
  return end


def _feed_qcom_healthy(a: GpsSourceArbiter, t0: float, duration: float, step: float = 0.25) -> float:
  t = t0
  end = t0 + duration
  while t <= end + 1e-9:
    a.observe_qcom(_valid_qcom(t), now_mono=t)
    a.step(now_mono=t)
    t += step
  return end


def _establish_ublox_primary(a: GpsSourceArbiter, t0: float = 1.0) -> float:
  end = _feed_ublox_healthy(a, t0, STARTUP_HEALTH_CONFIRM_SECONDS + 0.05)
  assert a.state.selected == SelectedSource.UBLOX_PRIMARY
  return end


def _establish_qcom_startup(a: GpsSourceArbiter, t0: float = 1.0) -> float:
  end = _feed_qcom_healthy(a, t0, STARTUP_HEALTH_CONFIRM_SECONDS + 0.05)
  assert a.state.selected == SelectedSource.QCOM_FALLBACK
  return end


class TestRepeatedValidStartupConfirmation:
  """Bug A / issue 1: startup needs repeated continuous valid fixes, not one stale valid."""

  def test_A_one_qcom_valid_then_invalids_through_1s_must_not_win(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_qcom(_valid_qcom(0.0), now_mono=0.0)
    a.step(now_mono=0.0)
    for t in (0.5, 1.0, 1.5):
      a.observe_qcom(_valid_qcom(t, has_fix=False), now_mono=t)
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_one_qcom_valid_then_no_samples_over_1s_not_qualified(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_qcom(_valid_qcom(1.0), now_mono=1.0)
    a.step(now_mono=1.0)
    a.step(now_mono=2.5)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_one_qcom_valid_then_hasfix_false_not_qualified(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_qcom(_valid_qcom(1.0), now_mono=1.0)
    a.step(now_mono=1.0)
    a.observe_qcom(_valid_qcom(1.5, has_fix=False), now_mono=1.5)
    a.step(now_mono=1.5)
    a.step(now_mono=2.5)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_qcom_valid_invalid_valid_restarts_confirmation(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_qcom(_valid_qcom(1.0), now_mono=1.0)
    a.step(now_mono=1.0)
    a.observe_qcom(_valid_qcom(1.5, has_fix=False), now_mono=1.5)
    a.step(now_mono=1.5)
    a.observe_qcom(_valid_qcom(2.0), now_mono=2.0)
    a.step(now_mono=2.0)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE
    # Need a second valid spanning >=1s after the restart.
    a.observe_qcom(_valid_qcom(3.0), now_mono=3.0)
    a.step(now_mono=3.0)
    assert a.state.selected == SelectedSource.QCOM_FALLBACK

  def test_two_qcom_valids_spanning_1s_qualified(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_qcom(_valid_qcom(1.0), now_mono=1.0)
    a.step(now_mono=1.0)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE
    a.observe_qcom(_valid_qcom(2.0), now_mono=2.0)
    a.step(now_mono=2.0)
    assert a.state.selected == SelectedSource.QCOM_FALLBACK

  def test_one_ublox_valid_then_invalids_cannot_win_startup(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_ublox(_valid_ublox(1.0), now_mono=1.0)
    a.step(now_mono=1.0)
    for t in (1.1, 1.2, 1.5, 2.0, 2.5):
      a.observe_ublox(_valid_ublox(t, has_fix=False), now_mono=t)
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_two_ublox_valids_spanning_1s_qualified(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_ublox(_valid_ublox(1.0), now_mono=1.0)
    a.step(now_mono=1.0)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE
    a.observe_ublox(_valid_ublox(2.0), now_mono=2.0)
    a.step(now_mono=2.0)
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY


class TestFirstReliableFixStartup:
  def test_neither_has_fix_no_healthy_source(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    for t in range(30):
      a.step(now_mono=float(t))
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_ublox_reliable_first_primary(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    _establish_ublox_primary(a, 1.0)
    assert a.state.transition_reason == "startup_ublox_first_reliable_fix"

  def test_qcom_reliable_first_fallback(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    _establish_qcom_startup(a, 1.0)
    assert a.state.transition_reason == "startup_qcom_first_reliable_fix"

  def test_qcom_wins_while_ublox_acquiring(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t = 0.0
    end = STARTUP_HEALTH_CONFIRM_SECONDS + 0.5
    while t <= end:
      a.observe_ublox(_valid_ublox(t, has_fix=False), now_mono=t)
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
      t += 0.25
    assert a.state.ublox.health == SourceHealth.ACQUIRING
    assert a.state.selected == SelectedSource.QCOM_FALLBACK

  def test_qcom_at_20s_with_zero_ublox_output_no_wait_for_60s(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t = 20.0
    end = 20.0 + STARTUP_HEALTH_CONFIRM_SECONDS + 0.1
    while t <= end:
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
      t += 0.25
    assert a.state.selected == SelectedSource.QCOM_FALLBACK
    assert end < UBLOX_INITIAL_OUTPUT_GRACE_SECONDS

  def test_same_cycle_tie_prefers_ublox(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    # Build both continuous valid sequences, then select once both qualify.
    t = 1.0
    while t < 1.0 + STARTUP_HEALTH_CONFIRM_SECONDS:
      a.observe_ublox(_valid_ublox(t), now_mono=t)
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.observe_ublox(None, now_mono=t)
      a.observe_qcom(None, now_mono=t)
      t += 0.1
    a.observe_ublox(_valid_ublox(t), now_mono=t)
    a.observe_qcom(_valid_qcom(t), now_mono=t)
    a.step(now_mono=t)
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY
    assert a.state.transition_reason == "startup_tie_ublox"

  def test_one_transient_ublox_fix_not_qualified(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_ublox(_valid_ublox(1.0), now_mono=1.0)
    a.step(now_mono=1.0)
    assert a.state.ublox.health == SourceHealth.HEALTHY
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_one_transient_qcom_fix_not_qualified(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_qcom(_valid_qcom(1.0), now_mono=1.0)
    a.step(now_mono=1.0)
    assert a.state.qcom.health == SourceHealth.HEALTHY
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_continuous_health_for_confirm_qualifies(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    _feed_ublox_healthy(a, 1.0, STARTUP_HEALTH_CONFIRM_SECONDS + 0.05)
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY

  def test_qcom_wins_then_one_ublox_fix_remains_qcom(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_qcom_startup(a, 1.0)
    t = t0 + 0.5
    a.observe_ublox(_valid_ublox(t), now_mono=t)
    a.observe_qcom(_valid_qcom(t), now_mono=t)
    a.step(now_mono=t)
    assert a.state.selected == SelectedSource.QCOM_FALLBACK
    assert a.state.recovery_count == 0

  def test_qcom_wins_then_ublox_sustained_recovers(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_qcom_startup(a, 1.0)
    t = t0 + QCOM_FALLBACK_MIN_DWELL_SECONDS + 0.1
    end = t + UBLOX_HEALTHY_BEFORE_RECOVERY_SECONDS + 0.5
    while t <= end:
      a.observe_ublox(_valid_ublox(t), now_mono=t)
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
      t += 0.25
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY
    assert a.state.recovery_count == 1

  def test_ublox_wins_healthy_qcom_never_steals(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    t = t0
    end = t0 + 20.0
    while t <= end:
      a.observe_ublox(_valid_ublox(t), now_mono=t)
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
      t += 0.5
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY
    assert a.state.failover_count == 0

  def test_selected_fails_alternate_unhealthy_no_source(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    t = t0
    end = t0 + 3.0 + UBLOX_UNHEALTHY_BEFORE_FAILOVER_SECONDS + 0.5
    while t <= end:
      t += 0.5
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_selected_fails_alternate_healthy_failover(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    t = t0
    end = t0 + 3.0 + UBLOX_UNHEALTHY_BEFORE_FAILOVER_SECONDS + 0.5
    while t <= end:
      t += 0.5
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.QCOM_FALLBACK
    assert a.state.failover_count == 1

  def test_arbiter_restart_fresh_startup_race(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    epoch0 = a.state.transition_mono
    a.reset(now_mono=100.0)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE
    assert a.state.generation == 0
    assert a.state.transition_mono == 100.0
    assert not a.message_is_authoritative(source=SelectedSource.UBLOX_PRIMARY, recv_mono=t0)
    _establish_qcom_startup(a, 100.5)
    assert a.state.transition_mono > 100.0
    assert a.state.transition_mono != epoch0


class TestNoOutputGrace:
  def test_zero_samples_within_grace_unknown_no_authority(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    for t in range(int(UBLOX_INITIAL_OUTPUT_GRACE_SECONDS) - 1):
      a.step(now_mono=float(t))
    assert a.state.ublox.health == SourceHealth.UNKNOWN
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_zero_samples_beyond_deadline_unhealthy(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t = UBLOX_INITIAL_OUTPUT_GRACE_SECONDS + 0.1
    a.step(now_mono=t)
    assert a.state.ublox.health == SourceHealth.UNHEALTHY

  def test_no_output_deadline_does_not_gate_qcom(self):
    # Explicit: QCOM may win well before the 60s ublox no-output mark.
    a = _arbiter()
    a.reset(now_mono=0.0)
    end = _feed_qcom_healthy(a, 5.0, STARTUP_HEALTH_CONFIRM_SECONDS + 0.1)
    assert a.state.selected == SelectedSource.QCOM_FALLBACK
    assert end < 30.0


class TestNoFixedNoFixTimeout:
  def test_fresh_nofix_at_121s_still_acquiring(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t = 121.0
    a.observe_ublox(_valid_ublox(t, has_fix=False), now_mono=t)
    a.step(now_mono=t)
    assert a.state.ublox.health == SourceHealth.ACQUIRING
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_fresh_nofix_at_210s_still_acquiring(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t = 210.0
    a.observe_ublox(_valid_ublox(t, has_fix=False), now_mono=t)
    a.step(now_mono=t)
    assert a.state.ublox.health == SourceHealth.ACQUIRING

  def test_active_slow_acquisition_no_ttff_timeout(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    for t in [50.0, 100.0, 150.0, 200.0, 250.0, 300.0]:
      a.observe_ublox(_valid_ublox(t, has_fix=False), now_mono=t)
      a.step(now_mono=t)
    assert a.state.ublox.health == SourceHealth.ACQUIRING
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_stale_ublox_stream_unhealthy(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_ublox(_valid_ublox(1.0, has_fix=False), now_mono=1.0)
    a.step(now_mono=1.0)
    assert a.state.ublox.health == SourceHealth.ACQUIRING
    a.step(now_mono=1.0 + 5.0)
    assert a.state.ublox.health == SourceHealth.UNHEALTHY


class TestAntiFlap:
  def test_qcom_startup_oscillating_ublox_stays_qcom(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_qcom_startup(a, 1.0)
    t = t0
    for i in range(40):
      t += 0.25
      if i % 2 == 0:
        a.observe_ublox(_valid_ublox(t), now_mono=t)
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.QCOM_FALLBACK
    assert a.state.recovery_count == 0

  def test_after_ublox_recovery_qcom_healthy_stays_ublox(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_qcom_startup(a, 1.0)
    t = t0 + QCOM_FALLBACK_MIN_DWELL_SECONDS + 0.1
    end = t + UBLOX_HEALTHY_BEFORE_RECOVERY_SECONDS + 0.5
    while t <= end:
      a.observe_ublox(_valid_ublox(t), now_mono=t)
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
      t += 0.25
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY
    # Continue with both healthy — no steal back.
    end2 = t + 10.0
    while t <= end2:
      a.observe_ublox(_valid_ublox(t), now_mono=t)
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
      t += 0.5
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY
    assert a.state.failover_count == 0


class TestFailoverFromUbloxPrimary:
  def test_one_missed_ublox_sample_no_switch(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    t = t0 + 1.5
    a.observe_qcom(_valid_qcom(t), now_mono=t)
    a.step(now_mono=t)
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY

  def test_one_bad_fix_remains_ublox(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    t = t0 + 0.2
    a.observe_ublox(_valid_ublox(t, has_fix=False), now_mono=t)
    a.observe_qcom(_valid_qcom(t), now_mono=t)
    a.step(now_mono=t)
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY

  def test_B_post_fix_nofix_beyond_fresh_plus_debounce_fails_over(self):
    """Bug B: after valid fix, sustained no-fix is LOST_FIX, not indefinite ACQUIRING."""
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    t = t0
    # Continuous fresh hasFix=false past valid-fix freshness + failover debounce.
    end = t0 + UBLOX_FRESH_SECONDS + UBLOX_UNHEALTHY_BEFORE_FAILOVER_SECONDS + 0.5
    while t < end:
      t += 0.2
      a.observe_ublox(_valid_ublox(t, has_fix=False), now_mono=t)
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
    assert a.state.ublox.health == SourceHealth.UNHEALTHY
    assert a.state.selected == SelectedSource.QCOM_FALLBACK
    assert a.state.failover_count == 1

  def test_post_fix_nofix_during_debounce_remains_ublox(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    t = t0
    # Past freshness into UNHEALTHY, but before 5s debounce completes.
    end = t0 + UBLOX_FRESH_SECONDS + 1.0
    while t < end:
      t += 0.2
      a.observe_ublox(_valid_ublox(t, has_fix=False), now_mono=t)
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
    assert a.state.ublox.health == SourceHealth.UNHEALTHY
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY

  def test_post_fix_nofix_unhealthy_qcom_goes_no_source(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    t = t0
    end = t0 + UBLOX_FRESH_SECONDS + UBLOX_UNHEALTHY_BEFORE_FAILOVER_SECONDS + 0.5
    while t < end:
      t += 0.2
      a.observe_ublox(_valid_ublox(t, has_fix=False), now_mono=t)
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_initial_cold_acquisition_stays_acquiring_not_ttff(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    for t in (10.0, 50.0, 120.0, 200.0):
      a.observe_ublox(_valid_ublox(t, has_fix=False), now_mono=t)
      a.step(now_mono=t)
      assert a.state.ublox.health == SourceHealth.ACQUIRING
      assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_sustained_ublox_stale_with_healthy_qcom_switches(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    t = t0
    end = t0 + 3.0 + UBLOX_UNHEALTHY_BEFORE_FAILOVER_SECONDS + 0.5
    while t < end:
      t += 0.5
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.QCOM_FALLBACK


class TestStartupRaceOneTimeOnly:
  """Bug C / issue 3: runtime NO_HEALTHY_SOURCE must not re-enter 1s startup race."""

  def test_C_qcom_winner_ublox_brief_qcom_lost_stays_no_source(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_qcom_startup(a, 1.0)
    assert a.state.startup_complete
    # u-blox healthy for only ~2s (<10s recovery), then QCOM dies.
    t = t0
    while t < t0 + 2.0:
      t += 0.25
      a.observe_ublox(_valid_ublox(t), now_mono=t)
      a.observe_qcom(_valid_qcom(t), now_mono=t)
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.QCOM_FALLBACK
    last_qcom = t
    # Expire QCOM valid-fix freshness while keeping brief ublox health (<10s total).
    while t < last_qcom + 3.5:
      t += 0.25
      a.observe_ublox(_valid_ublox(t), now_mono=t)
      a.observe_qcom(_valid_qcom(t, has_fix=False), now_mono=t)
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE
    # Continue briefly — must NOT jump via startup 1s rule.
    end = t + 2.0
    while t < end:
      t += 0.25
      a.observe_ublox(_valid_ublox(t), now_mono=t)
      a.step(now_mono=t)
      assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE
    # After full 10s ublox healthy since continuous start (~t0), recover.
    while t < t0 + UBLOX_HEALTHY_BEFORE_RECOVERY_SECONDS + 1.0:
      t += 0.25
      a.observe_ublox(_valid_ublox(t), now_mono=t)
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY
    assert a.state.transition_reason == "runtime_ublox_recovery_from_none"

  def test_qcom_winner_no_source_qcom_returns_runtime(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_qcom_startup(a, 1.0)
    # Expire QCOM without alternate.
    t = t0 + 4.0
    a.step(now_mono=t)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE
    assert a.state.startup_complete
    # QCOM returns — runtime restore, not startup race.
    a.observe_qcom(_valid_qcom(t + 0.1), now_mono=t + 0.1)
    a.step(now_mono=t + 0.1)
    assert a.state.selected == SelectedSource.QCOM_FALLBACK
    assert a.state.transition_reason == "runtime_qcom_restored_from_none"

  def test_ublox_winner_failure_no_source_recovery_runtime(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    t = t0
    end = t0 + UBLOX_FRESH_SECONDS + UBLOX_UNHEALTHY_BEFORE_FAILOVER_SECONDS + 0.5
    while t < end:
      t += 0.5
      a.step(now_mono=t)
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE
    assert a.state.startup_complete
    # Restore ublox via runtime path (immediate when last was ublox and healthy).
    t += 0.1
    a.observe_ublox(_valid_ublox(t), now_mono=t)
    a.step(now_mono=t)
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY
    assert a.state.transition_reason == "runtime_ublox_restored_from_none"

  def test_arbiter_reset_allows_startup_race_again(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    _establish_qcom_startup(a, 1.0)
    assert a.state.startup_complete
    a.reset(now_mono=50.0)
    assert not a.state.startup_complete
    assert a.state.last_authoritative_source is None
    _establish_ublox_primary(a, 50.5)
    assert a.state.transition_reason == "startup_ublox_first_reliable_fix"


class TestQcomValidation:
  @pytest.mark.parametrize(
    "sample_kwargs",
    [
      {"has_fix": False},
      {"latitude": float("nan")},
      {"horizontal_accuracy": -1.0},
      {"vertical_accuracy": float("nan")},
    ],
  )
  def test_invalid_qcom_not_eligible(self, sample_kwargs):
    assert not qcom_sample_is_valid_fix(_valid_qcom(1.0, **sample_kwargs))


class TestSourceEpoch:
  def test_startup_winner_creates_epoch(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    t0 = _establish_ublox_primary(a, 1.0)
    assert a.state.generation == 1
    assert not a.message_is_authoritative(source=SelectedSource.UBLOX_PRIMARY, recv_mono=a.state.transition_mono)
    assert a.message_is_authoritative(source=SelectedSource.UBLOX_PRIMARY, recv_mono=a.state.transition_mono + 0.01)
    assert t0 >= a.state.transition_mono


class TestFailClosed:
  def test_nan_accuracy_not_healthy(self):
    assert not ublox_sample_is_valid_fix(_valid_ublox(1.0, horizontal_accuracy=float("nan")))

  def test_reset_discards_evidence(self):
    a = _arbiter()
    a.reset(now_mono=0.0)
    _establish_ublox_primary(a, 1.0)
    a.reset(now_mono=100.0)
    assert a.state.ublox.health == SourceHealth.UNKNOWN
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE
    assert a.state.generation == 0


class TestProcessConfig:
  def test_qcomgps_runs_when_ublox_available(self, monkeypatch):
    from openpilot.system.manager import process_config

    monkeypatch.setattr(process_config, "ublox_available", lambda: True)
    assert process_config.qcomgps(True, None, None) is True
    assert process_config.qcomgps(False, None, None) is False

  def test_qcomgpsd_restart_if_crash(self):
    from openpilot.system.manager import process_config

    assert process_config.managed_processes["qcomgpsd"].restart_if_crash is True
    assert process_config.managed_processes["ubloxd"].restart_if_crash is True
    assert process_config.managed_processes["gpsard"].restart_if_crash is True


class TestCoordinationHelpers:
  def test_selected_source_to_service(self):
    from openpilot.common.gps import selected_source_to_service

    assert selected_source_to_service("ubloxPrimary") == "gpsLocationExternal"
    assert selected_source_to_service("qcomFallback") == "gpsLocation"
    assert selected_source_to_service("noHealthySource") is None

  def test_gps_source_state_freshness(self):
    from openpilot.common.gps import gps_source_state_is_fresh

    assert not gps_source_state_is_fresh(now_mono=1.0, last_state_recv_mono=None)
    assert gps_source_state_is_fresh(now_mono=3.5, last_state_recv_mono=1.0)
    assert not gps_source_state_is_fresh(now_mono=4.1, last_state_recv_mono=1.0)

  def test_accept_gps_source_epoch_monotonic(self):
    from openpilot.common.gps import accept_gps_source_epoch

    assert accept_gps_source_epoch(
      transition_mono_ns=100,
      generation=1,
      selected="qcomFallback",
      recv_mono_ns=1000,
      last_transition_mono_ns=None,
      last_generation=None,
      last_selected=None,
    )
    assert not accept_gps_source_epoch(
      transition_mono_ns=50,
      generation=2,
      selected="ubloxPrimary",
      recv_mono_ns=2000,
      last_transition_mono_ns=100,
      last_generation=1,
      last_selected="qcomFallback",
    )
    assert not accept_gps_source_epoch(
      transition_mono_ns=100,
      generation=2,
      selected="ubloxPrimary",
      recv_mono_ns=2000,
      last_transition_mono_ns=100,
      last_generation=1,
      last_selected="qcomFallback",
    )
    assert accept_gps_source_epoch(
      transition_mono_ns=200,
      generation=0,
      selected="ubloxPrimary",
      recv_mono_ns=2000,
      last_transition_mono_ns=100,
      last_generation=1,
      last_selected="qcomFallback",
    )
    assert not accept_gps_source_epoch(
      transition_mono_ns=9000,
      generation=1,
      selected="ubloxPrimary",
      recv_mono_ns=2000,
      last_transition_mono_ns=100,
      last_generation=1,
      last_selected="qcomFallback",
    )

  def test_startup_confirm_is_anti_glitch_not_ttff(self):
    assert STARTUP_HEALTH_CONFIRM_SECONDS == 1.0
    assert STARTUP_HEALTH_CONFIRM_SECONDS < 5.0
    assert math.isfinite(UBLOX_INITIAL_OUTPUT_GRACE_SECONDS)


class TestLocationdUsabilityContract:
  """gpsard HEALTHY must match locationd handle_gps field usability."""

  def test_sane_uncertainty_constant_aligned(self):
    from pathlib import Path

    from openpilot.common.gps_measurement import (
      GPS_LOCATIOND_ALTITUDE_SANITY_M,
      GPS_LOCATIOND_MAX_FILTER_REWIND_SECONDS,
      GPS_LOCATIOND_SANE_UNCERTAINTY_M,
      GPS_LOCATIOND_TRANS_SANITY_MPS,
    )

    assert GPS_LOCATIOND_SANE_UNCERTAINTY_M == 1500.0
    assert GPS_LOCATIOND_MAX_FILTER_REWIND_SECONDS == 0.8
    assert GPS_LOCATIOND_ALTITUDE_SANITY_M == 10000.0
    assert GPS_LOCATIOND_TRANS_SANITY_MPS == 200.0
    # Mirrored C++ constants must not silently drift.
    loc = (Path(__file__).resolve().parents[2] / "sunnypilot" / "selfdrive" / "locationd" / "locationd.cc").read_text()
    assert "const double SANE_GPS_UNCERTAINTY = 1500.0" in loc
    assert "const double MAX_FILTER_REWIND_TIME = 0.8" in loc
    assert "const double ALTITUDE_SANITY_CHECK = 10000" in loc
    assert "const double TRANS_SANITY_CHECK = 200.0" in loc

  def test_qcom_excessive_uncertainty_not_healthy(self):
    # hAcc=2000, vAcc=500 → hypot ≈ 2061 > 1500; locationd rejects.
    a = _arbiter()
    a.reset(now_mono=0.0)
    t = 1.0
    for _ in range(5):
      a.observe_qcom(
        _valid_qcom(t, horizontal_accuracy=2000.0, vertical_accuracy=500.0),
        now_mono=t,
      )
      a.step(now_mono=t)
      t += 0.25
    assert a.state.qcom.health != SourceHealth.HEALTHY
    assert a.state.selected == SelectedSource.NO_HEALTHY_SOURCE

  def test_invalid_altitude_not_qualified(self):
    assert not qcom_sample_is_valid_fix(_valid_qcom(1.0, altitude=20_000.0))
    assert not ublox_sample_is_valid_fix(_valid_ublox(1.0, altitude=-20_000.0))

  def test_invalid_speed_accuracy_not_qualified(self):
    assert not qcom_sample_is_valid_fix(_valid_qcom(1.0, speed_accuracy=0.0))
    assert not ublox_sample_is_valid_fix(_valid_ublox(1.0, speed_accuracy=-1.0))

  def test_invalid_bearing_accuracy_not_qualified(self):
    assert not qcom_sample_is_valid_fix(_valid_qcom(1.0, bearing_accuracy_deg=0.0))
    assert not ublox_sample_is_valid_fix(_valid_ublox(1.0, bearing_accuracy_deg=float("nan")))

  def test_invalid_vned_not_qualified(self):
    assert not qcom_sample_is_valid_fix(_valid_qcom(1.0, v_ned=(300.0, 0.0, 0.0)))
    assert not ublox_sample_is_valid_fix(_valid_ublox(1.0, v_ned=(float("nan"), 0.0, 0.0)))

  def test_stale_measurement_mono_with_fresh_event_not_qualified(self):
    # Event at t=10, measurement stamped 2s earlier → beyond 0.8s rewind.
    s = _valid_qcom(10.0, measurement_mono_ns=int(8.0 * 1e9))
    assert not qcom_sample_is_valid_fix(s)
    a = _arbiter()
    a.reset(now_mono=0.0)
    a.observe_qcom(s, now_mono=10.0)
    a.step(now_mono=10.0)
    assert a.state.qcom.health != SourceHealth.HEALTHY

  def test_valid_qcom_and_ublox_remain_eligible(self):
    assert qcom_sample_is_valid_fix(_valid_qcom(1.0))
    assert ublox_sample_is_valid_fix(_valid_ublox(1.0))
    a = _arbiter()
    a.reset(now_mono=0.0)
    _establish_qcom_startup(a, 1.0)
    assert a.state.selected == SelectedSource.QCOM_FALLBACK
    a.reset(now_mono=50.0)
    _establish_ublox_primary(a, 50.5)
    assert a.state.selected == SelectedSource.UBLOX_PRIMARY


class TestGpsardTelemetryFailOpen:
  def test_safe_cloudlog_swallows_logger_exceptions(self):
    from openpilot.system import gpsard
    import openpilot.system.gpsard as gpsard_mod

    class Boom:
      def info(self, *_a, **_k):
        raise RuntimeError("info boom")

      def warning(self, *_a, **_k):
        raise RuntimeError("warning boom")

      def exception(self, *_a, **_k):
        raise RuntimeError("exception boom")

    original = gpsard_mod.cloudlog
    gpsard_mod.cloudlog = Boom()  # ty: ignore[invalid-assignment]  # test double
    try:
      gpsard.safe_cloudlog("info", "startup")
      gpsard.safe_cloudlog("warning", "transition")
      gpsard.safe_cloudlog("exception", "publish_failed")
    finally:
      gpsard_mod.cloudlog = original

  def test_publish_failure_does_not_corrupt_state(self):
    from openpilot.system import gpsard
    import openpilot.system.gpsard as gpsard_mod

    a = _arbiter()
    a.reset(now_mono=0.0)
    _establish_ublox_primary(a, 1.0)
    before = (a.state.selected, a.state.generation, a.state.failover_count)

    class FakePM:
      def send(self, *_a, **_k):
        raise RuntimeError("send failed")

    class Boom:
      def exception(self, *_a, **_k):
        raise RuntimeError("log boom")

    original = gpsard_mod.cloudlog
    gpsard_mod.cloudlog = Boom()  # ty: ignore[invalid-assignment]  # test double
    try:
      ok = gpsard._publish_state(FakePM(), a, 2.0)  # ty: ignore[invalid-argument-type]
    finally:
      gpsard_mod.cloudlog = original
    assert ok is False
    assert (a.state.selected, a.state.generation, a.state.failover_count) == before
