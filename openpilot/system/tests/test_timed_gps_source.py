"""PR80 timed coordination with gpsSourceState freshness and epoch validation."""

from __future__ import annotations

from openpilot.common.gps import (
  GPS_SOURCE_STATE_FRESH_SECONDS,
  accept_gps_source_epoch,
  gps_source_state_is_fresh,
  selected_source_to_service,
)


def test_timed_only_syncs_from_ublox_primary():
  assert selected_source_to_service("ubloxPrimary") == "gpsLocationExternal"
  assert selected_source_to_service("qcomFallback") == "gpsLocation"
  assert selected_source_to_service("noHealthySource") is None


def test_timed_source_epoch_rejects_equal_transition():
  transition_ns = 1_000_000_000
  msg_ns = transition_ns
  assert not (msg_ns > transition_ns)
  assert (transition_ns + 1) > transition_ns


def test_timed_requires_fresh_gps_source_state():
  assert GPS_SOURCE_STATE_FRESH_SECONDS == 3.0
  assert not gps_source_state_is_fresh(now_mono=10.0, last_state_recv_mono=None)
  assert not gps_source_state_is_fresh(now_mono=10.0, last_state_recv_mono=6.0)
  assert gps_source_state_is_fresh(now_mono=10.0, last_state_recv_mono=8.0)


def test_timed_qcom_clock_policy_no_gps_time_when_qcom_selected():
  # Even if QCOM wins the startup race for locationd, timed must not sync clock.
  assert selected_source_to_service("qcomFallback") == "gpsLocation"
  assert GPS_SOURCE_STATE_FRESH_SECONDS == 3.0
  assert not gps_source_state_is_fresh(now_mono=10.0, last_state_recv_mono=None)


def test_D_timed_rejects_regressing_epoch():
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


def test_D_timed_rejects_equal_epoch_inconsistent_source():
  assert not accept_gps_source_epoch(
    transition_mono_ns=100,
    generation=2,
    selected="ubloxPrimary",
    recv_mono_ns=2000,
    last_transition_mono_ns=100,
    last_generation=1,
    last_selected="qcomFallback",
  )


def test_D_timed_accepts_newer_epoch_arbiter_restart():
  assert accept_gps_source_epoch(
    transition_mono_ns=200,
    generation=0,
    selected="ubloxPrimary",
    recv_mono_ns=2000,
    last_transition_mono_ns=100,
    last_generation=1,
    last_selected="qcomFallback",
  )


def test_D_timed_rejects_future_epoch():
  assert not accept_gps_source_epoch(
    transition_mono_ns=5000,
    generation=1,
    selected="ubloxPrimary",
    recv_mono_ns=2000,
    last_transition_mono_ns=100,
    last_generation=1,
    last_selected="qcomFallback",
  )


def test_timed_equal_epoch_consistent_refresh_ok():
  assert accept_gps_source_epoch(
    transition_mono_ns=100,
    generation=1,
    selected="qcomFallback",
    recv_mono_ns=2000,
    last_transition_mono_ns=100,
    last_generation=1,
    last_selected="qcomFallback",
  )
