from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from openpilot.system.ubloxd.provisional_yuma_reference import (
  ProvisionalYumaRejection,
  evaluate_provisional_yuma_reference,
)
from openpilot.system.ubloxd.rtc_time_observation import (
  CrossBootRtcObservation,
  RtcObservationCandidate,
  RtcObservationReason,
  RtcObservationState,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
  TrustedTimeSource,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AnchorWriteStatus,
  AuthorizedTime,
  TimeAuthorizationEvidence,
  TimeAuthorityEvaluation,
  TimeAuthorityRejectionReason,
)


@dataclass(frozen=True)
class RouteReplay:
  route: str
  anchor_sequence: int
  rtc_elapsed_seconds: int
  candidate_error_seconds: float
  delayed_yuma_seconds: float
  first_receiver_utc_seconds: float
  first_fix_seconds: float


COLD_START_ROUTES = (
  RouteReplay(
    route="000000a0--b8a17b30b6",
    anchor_sequence=21,
    rtc_elapsed_seconds=3_611,
    candidate_error_seconds=0.344896,
    delayed_yuma_seconds=159.6,
    first_receiver_utc_seconds=159.308,
    first_fix_seconds=598.485,
  ),
  RouteReplay(
    route="000000a3--38efb186ca",
    anchor_sequence=23,
    rtc_elapsed_seconds=10_945,
    candidate_error_seconds=0.472487,
    delayed_yuma_seconds=194.5,
    first_receiver_utc_seconds=194.327,
    first_fix_seconds=495.090,
  ),
)

ANCHOR_BOOT = "11111111-1111-1111-1111-111111111111"
CURRENT_BOOT = "22222222-2222-2222-2222-222222222222"
REFERENCE_UTC = datetime(2026, 7, 26, 2, 0, tzinfo=UTC)


def replay_observation(route: RouteReplay) -> CrossBootRtcObservation:
  candidate = RtcObservationCandidate(
    candidate_utc=REFERENCE_UTC,
    uncertainty_seconds=3.0,
    anchor_generation="primary",
    anchor_sequence=route.anchor_sequence,
    anchor_boot_id=ANCHOR_BOOT,
    current_boot_id=CURRENT_BOOT,
    anchor_trusted_utc=REFERENCE_UTC,
    anchor_rtc_epoch_seconds=1_000,
    current_rtc_epoch_seconds=1_000 + route.rtc_elapsed_seconds,
    rtc_elapsed_seconds=route.rtc_elapsed_seconds,
    current_boottime_seconds=2.0,
    rtc_advanced=True,
    elapsed_covers_uptime=True,
    rtc_voltage_status_supported=True,
    rtc_voltage_status_flags=0,
    anchor_source=TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS,
    anchor_provenance=TimeProvenance.GNSS_INDEPENDENT,
    anchor_authorized=True,
    anchor_independent=True,
  )
  return CrossBootRtcObservation(
    state=RtcObservationState.OBSERVED,
    reason=RtcObservationReason.CROSS_BOOT_CANDIDATE_OBSERVED,
    candidate=candidate,
    first_rtc_epoch_seconds=candidate.current_rtc_epoch_seconds - 2,
    second_rtc_epoch_seconds=candidate.current_rtc_epoch_seconds,
    first_boottime_seconds=0.0,
    second_boottime_seconds=2.0,
    first_observed_at=0.0,
    second_observed_at=2.0,
    tick_elapsed_seconds=2.0,
    rtc_tick_delta_seconds=2,
    boottime_tick_delta_seconds=2.0,
    tick_consistent=True,
  )


@pytest.mark.parametrize("route", COLD_START_ROUTES, ids=lambda route: route.route)
def test_slow_cold_start_routes_would_use_early_yuma_only(route):
  authority = TimeAuthorityEvaluation(
    authorized_time=None,
    rejection_reason=TimeAuthorityRejectionReason.CROSS_BOOT_CONTINUITY_UNPROVABLE,
    anchor_write_status=AnchorWriteStatus.NOT_REQUIRED,
    selected_anchor_generation="primary",
    selected_anchor_sequence=route.anchor_sequence,
  )

  decision = evaluate_provisional_yuma_reference(
    replay_observation(route),
    authority,
    receiver_cycle=0,
  )

  assert decision.eligible
  assert decision.reference is not None
  assert decision.reference.observed_at == 2.0
  assert decision.reference.observed_at < route.delayed_yuma_seconds
  assert decision.reference.observed_at < route.first_receiver_utc_seconds
  assert route.candidate_error_seconds < decision.reference.uncertainty_seconds
  assert route.first_fix_seconds - route.delayed_yuma_seconds > 300.0


def test_route_92_network_authorized_path_remains_normal():
  authority = TimeAuthorityEvaluation(
    authorized_time=AuthorizedTime(
      utc=REFERENCE_UTC,
      uncertainty_seconds=1.0,
      source=TrustedTimeSource.SYSTEM_SYNCHRONIZED,
      provenance=TimeProvenance.NETWORK_INDEPENDENT,
      independent=True,
      evidence=TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED,
    ),
    rejection_reason=None,
    anchor_write_status=AnchorWriteStatus.NOT_REQUIRED,
    selected_anchor_generation="primary",
    selected_anchor_sequence=20,
  )
  route_92_shape = RouteReplay(
    route="00000092--known-good",
    anchor_sequence=20,
    rtc_elapsed_seconds=328,
    candidate_error_seconds=0.0,
    delayed_yuma_seconds=24.8,
    first_receiver_utc_seconds=43.2,
    first_fix_seconds=88.3,
  )

  decision = evaluate_provisional_yuma_reference(
    replay_observation(route_92_shape),
    authority,
    receiver_cycle=0,
  )

  assert not decision.eligible
  assert decision.rejection is ProvisionalYumaRejection.AUTHORIZED_TIME_AVAILABLE
