from datetime import UTC, datetime
from typing import cast

import pytest

from openpilot.system.ubloxd.navigation_database_restore import (
  NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS,
  NavigationDatabaseRestoreAgePolicy,
  NavigationDatabaseRestoreBootController,
  NavigationDatabaseRestoreDecision,
  NavigationDatabaseRestoreDecisionAction,
  NavigationDatabaseRestoreDisposition,
  evaluate_navigation_database_restore,
  is_current_independent_network_time,
)
from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
  TrustedTimeSource,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AuthorizedTime,
  TimeAuthorizationEvidence,
)


def test_disposition_values_are_stable() -> None:
  assert {item.name: item.value for item in NavigationDatabaseRestoreDisposition} == {
    "PENDING": "pending",
    "RESTORED": "restored",
    "RESTORE_PARTIAL": "restore_partial",
    "RESTORE_REJECTED": "restore_rejected",
    "RESTORE_RESPONSE_TIMEOUT": "restore_response_timeout",
    "RESTORE_TRANSFER_DEADLINE": "restore_transfer_deadline",
    "RESTORE_TRANSPORT_ERROR": "restore_transport_error",
    "RESTORE_CACHE_EXPIRED": "restore_cache_expired",
    "SKIPPED_EXPIRED": "skipped_expired",
    "SKIPPED_UNVERIFIED": "skipped_unverified",
    "SKIPPED_NO_TRUSTED_TIME": "skipped_no_trusted_time",
    "SKIPPED_WAIT_TIMEOUT": "skipped_wait_timeout",
    "SKIPPED_WAIT_ERROR": "skipped_wait_error",
    "SKIPPED_STATE_UNAVAILABLE": "skipped_state_unavailable",
    "SKIPPED_EARLY_ACQUISITION": "skipped_early_acquisition",
    "SKIPPED_LATE_RECEIVER_TIME": "skipped_late_receiver_time",
    "SKIPPED_ACQUISITION_ALREADY_STARTED": "skipped_acquisition_already_started",
    "SKIPPED_RELIABLE_FIX": "skipped_reliable_fix",
    "SKIPPED_YUMA_ALREADY_SENT": "skipped_yuma_already_sent",
    "SKIPPED_NO_CACHE": "skipped_no_cache",
    "SKIPPED_CACHE_UNQUALIFIED": "skipped_cache_unqualified",
    "SKIPPED_NO_USABLE_CACHE": "skipped_no_usable_cache",
    "WRITE_FAILED": "write_failed",
  }


def test_only_pending_is_nonterminal() -> None:
  assert not NavigationDatabaseRestoreDisposition.PENDING.terminal
  assert all(item.terminal for item in NavigationDatabaseRestoreDisposition if item is not NavigationDatabaseRestoreDisposition.PENDING)


def test_only_restored_makes_database_available() -> None:
  assert NavigationDatabaseRestoreDisposition.RESTORED.database_available
  assert all(not item.database_available for item in NavigationDatabaseRestoreDisposition if item is not NavigationDatabaseRestoreDisposition.RESTORED)


def test_terminal_meanings_are_unambiguous() -> None:
  for item in NavigationDatabaseRestoreDisposition:
    meanings = (item.database_available, item.intentionally_skipped, item.write_failed)
    assert sum(meanings) == int(item.terminal)


def test_boot_controller_starts_pending() -> None:
  controller = NavigationDatabaseRestoreBootController()
  assert controller.pending
  assert not controller.terminal
  assert not controller.restore_attempted


def test_position_assistance_is_claimed_once() -> None:
  controller = NavigationDatabaseRestoreBootController()
  assert controller.claim_position_assistance()
  assert not controller.claim_position_assistance()


def test_acquisition_state_is_latched() -> None:
  controller = NavigationDatabaseRestoreBootController()
  controller.note_acquisition_started()
  assert controller.acquisition_started


def test_restore_attempt_is_one_shot() -> None:
  controller = NavigationDatabaseRestoreBootController()
  assert controller.begin_restore_attempt()
  assert not controller.begin_restore_attempt()
  assert controller.finish_restore(NavigationDatabaseRestoreDisposition.RESTORED)
  assert controller.terminal


def test_started_attempt_cannot_become_skip() -> None:
  controller = NavigationDatabaseRestoreBootController()
  assert controller.begin_restore_attempt()
  assert not controller.skip(NavigationDatabaseRestoreDisposition.SKIPPED_RELIABLE_FIX)
  assert controller.finish_restore(NavigationDatabaseRestoreDisposition.WRITE_FAILED)


def test_finish_restore_requires_started_attempt() -> None:
  controller = NavigationDatabaseRestoreBootController()

  assert not controller.finish_restore(NavigationDatabaseRestoreDisposition.RESTORED)
  assert controller.pending
  assert not controller.restore_attempted


def test_intentional_skip_is_terminal_and_persisted() -> None:
  controller = NavigationDatabaseRestoreBootController()

  assert controller.skip(NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED)
  assert controller.terminal
  assert controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED
  assert not controller.restore_attempted


def test_terminal_disposition_cannot_change() -> None:
  controller = NavigationDatabaseRestoreBootController()

  assert controller.skip(NavigationDatabaseRestoreDisposition.SKIPPED_RELIABLE_FIX)
  assert not controller.skip(NavigationDatabaseRestoreDisposition.SKIPPED_YUMA_ALREADY_SENT)
  assert not controller.begin_restore_attempt()
  assert not controller.finish_restore(NavigationDatabaseRestoreDisposition.RESTORED)
  assert controller.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_RELIABLE_FIX


def test_finish_restore_rejects_noncompletion_disposition() -> None:
  controller = NavigationDatabaseRestoreBootController()
  assert controller.begin_restore_attempt()

  with pytest.raises(ValueError):
    controller.finish_restore(NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED)

  assert controller.pending
  assert controller.restore_attempted
  assert controller.finish_restore(NavigationDatabaseRestoreDisposition.WRITE_FAILED)


def test_skip_rejects_nonintentional_disposition() -> None:
  controller = NavigationDatabaseRestoreBootController()

  for disposition in (
    NavigationDatabaseRestoreDisposition.PENDING,
    NavigationDatabaseRestoreDisposition.RESTORED,
    NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
    NavigationDatabaseRestoreDisposition.RESTORE_REJECTED,
    NavigationDatabaseRestoreDisposition.RESTORE_RESPONSE_TIMEOUT,
    NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE,
    NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR,
    NavigationDatabaseRestoreDisposition.RESTORE_CACHE_EXPIRED,
    NavigationDatabaseRestoreDisposition.WRITE_FAILED,
  ):
    with pytest.raises(ValueError):
      controller.skip(disposition)

  assert controller.pending
  assert not controller.restore_attempted


def test_terminal_state_remains_on_same_controller_instance() -> None:
  controller = NavigationDatabaseRestoreBootController()

  assert controller.begin_restore_attempt()
  assert controller.finish_restore(NavigationDatabaseRestoreDisposition.RESTORED)

  same_controller = controller
  assert same_controller.terminal
  assert same_controller.restore_attempted
  assert not same_controller.begin_restore_attempt()
  assert same_controller.disposition is NavigationDatabaseRestoreDisposition.RESTORED


NOW = datetime(2026, 7, 28, tzinfo=UTC)


def authorized_time(
  *,
  source: TrustedTimeSource = TrustedTimeSource.SYSTEM_SYNCHRONIZED,
  provenance: TimeProvenance = TimeProvenance.NETWORK_INDEPENDENT,
  independent: bool = True,
  evidence: TimeAuthorizationEvidence = (TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED),
) -> AuthorizedTime:
  return AuthorizedTime(
    utc=NOW,
    uncertainty_seconds=1.0,
    source=source,
    provenance=provenance,
    independent=independent,
    evidence=evidence,
  )


def evaluate(**overrides: object) -> NavigationDatabaseRestoreDecision:
  arguments: dict[str, object] = {
    "reliable_fix_available": False,
    "yuma_already_sent": False,
    "authorized_time": authorized_time(),
    "cache_age_seconds": 30.0 * 60.0,
    "gnss_acquisition_started": False,
  }
  arguments.update(overrides)
  return evaluate_navigation_database_restore(**arguments)  # type: ignore[arg-type, ty:invalid-argument-type]


def test_decision_action_values_are_stable() -> None:
  assert {item.name: item.value for item in NavigationDatabaseRestoreDecisionAction} == {
    "WAIT": "wait",
    "RESTORE": "restore",
    "SKIP": "skip",
  }


@pytest.mark.parametrize(
  ("action", "disposition"),
  (
    (
      NavigationDatabaseRestoreDecisionAction.WAIT,
      NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED,
    ),
    (
      NavigationDatabaseRestoreDecisionAction.RESTORE,
      NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED,
    ),
    (NavigationDatabaseRestoreDecisionAction.SKIP, None),
    (
      NavigationDatabaseRestoreDecisionAction.SKIP,
      NavigationDatabaseRestoreDisposition.RESTORED,
    ),
  ),
)
def test_decision_rejects_inconsistent_state(
  action: NavigationDatabaseRestoreDecisionAction,
  disposition: NavigationDatabaseRestoreDisposition | None,
) -> None:
  with pytest.raises(ValueError):
    NavigationDatabaseRestoreDecision(action, disposition)


def test_current_independent_network_time_is_explicit() -> None:
  assert is_current_independent_network_time(authorized_time())
  assert not is_current_independent_network_time(
    authorized_time(
      independent=False,
      evidence=TimeAuthorizationEvidence.SAME_BOOT_BOOTTIME,
    )
  )
  assert not is_current_independent_network_time(
    authorized_time(
      source=TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS,
      provenance=TimeProvenance.GNSS_INDEPENDENT,
      evidence=(TimeAuthorizationEvidence.RECEIVER_UTC_UNASSISTED_GNSS),
    )
  )


@pytest.mark.parametrize(
  (
    "reliable_fix_available",
    "yuma_already_sent",
    "time_kind",
    "acquisition_started",
    "cache_age_seconds",
    "expected_action",
    "expected_disposition",
  ),
  (
    (
      True,
      True,
      "receiver",
      True,
      30.0,
      "skip",
      NavigationDatabaseRestoreDisposition.SKIPPED_RELIABLE_FIX,
    ),
    (
      False,
      True,
      "receiver",
      True,
      30.0,
      "skip",
      NavigationDatabaseRestoreDisposition.SKIPPED_YUMA_ALREADY_SENT,
    ),
    (
      False,
      False,
      "receiver",
      True,
      30.0,
      "skip",
      NavigationDatabaseRestoreDisposition.SKIPPED_LATE_RECEIVER_TIME,
    ),
    (
      False,
      False,
      "none",
      True,
      None,
      "skip",
      NavigationDatabaseRestoreDisposition.SKIPPED_ACQUISITION_ALREADY_STARTED,
    ),
    (False, False, "none", False, None, "wait", None),
    (
      False,
      False,
      "same_boot",
      False,
      None,
      "wait",
      None,
    ),
    (
      False,
      False,
      "network",
      False,
      None,
      "skip",
      NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED,
    ),
    (
      False,
      False,
      "network",
      False,
      3600.001,
      "skip",
      NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED,
    ),
    (False, False, "network", False, 1800.0, "restore", None),
  ),
)
def test_policy_precedence_matrix(
  reliable_fix_available: bool,
  yuma_already_sent: bool,
  time_kind: str,
  acquisition_started: bool,
  cache_age_seconds: float | None,
  expected_action: str,
  expected_disposition: NavigationDatabaseRestoreDisposition | None,
) -> None:
  times = {
    "none": None,
    "network": authorized_time(),
    "same_boot": authorized_time(
      independent=False,
      evidence=TimeAuthorizationEvidence.SAME_BOOT_BOOTTIME,
    ),
    "receiver": authorized_time(
      source=TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS,
      provenance=TimeProvenance.GNSS_INDEPENDENT,
      evidence=(TimeAuthorizationEvidence.RECEIVER_UTC_UNASSISTED_GNSS),
    ),
  }
  decision = evaluate(
    reliable_fix_available=reliable_fix_available,
    yuma_already_sent=yuma_already_sent,
    authorized_time=times[time_kind],
    cache_age_seconds=cache_age_seconds,
    gnss_acquisition_started=acquisition_started,
  )
  assert decision.action.value == expected_action
  assert decision.skip_disposition is expected_disposition


@pytest.mark.parametrize(
  "cache_age_seconds",
  (None, -1.0, float("nan"), float("inf"), True),
)
def test_invalid_cache_age_is_unverified_skip(
  cache_age_seconds: object,
) -> None:
  decision = evaluate(cache_age_seconds=cache_age_seconds)
  assert decision.skip_disposition is NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED


def test_cache_age_boundary_is_inclusive() -> None:
  assert evaluate(cache_age_seconds=NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS).should_restore
  assert (
    evaluate(cache_age_seconds=(NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS + 0.001)).skip_disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED
  )


@pytest.mark.parametrize(
  "maximum_cache_age_seconds",
  (-1.0, float("nan"), float("inf"), True),
)
def test_invalid_maximum_age_is_rejected(
  maximum_cache_age_seconds: object,
) -> None:
  with pytest.raises(ValueError):
    NavigationDatabaseRestoreAgePolicy(
      maximum_cache_age_seconds  # type: ignore[arg-type, ty:invalid-argument-type]
    )


def test_decision_uses_supplied_candidate_age_policy() -> None:
  policy = NavigationDatabaseRestoreAgePolicy(120.0)
  assert evaluate_navigation_database_restore(
    reliable_fix_available=False,
    yuma_already_sent=False,
    authorized_time=authorized_time(),
    cache_age_seconds=120.0,
    gnss_acquisition_started=False,
    age_policy=policy,
  ).should_restore
  assert (
    evaluate_navigation_database_restore(
      reliable_fix_available=False,
      yuma_already_sent=False,
      authorized_time=authorized_time(),
      cache_age_seconds=120.001,
      gnss_acquisition_started=False,
      age_policy=policy,
    ).skip_disposition
    is NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED
  )


def test_decision_rejects_nonpolicy_age_policy() -> None:
  with pytest.raises(ValueError):
    evaluate_navigation_database_restore(
      reliable_fix_available=False,
      yuma_already_sent=False,
      authorized_time=authorized_time(),
      cache_age_seconds=30.0,
      gnss_acquisition_started=False,
      age_policy=cast(NavigationDatabaseRestoreAgePolicy, object()),
    )


@pytest.mark.parametrize(
  ("name", "value"),
  (
    ("reliable_fix_available", 1),
    ("yuma_already_sent", 1),
    ("gnss_acquisition_started", 1),
  ),
)
def test_nonboolean_flags_are_rejected(
  name: str,
  value: object,
) -> None:
  arguments: dict[str, object] = {
    "reliable_fix_available": False,
    "yuma_already_sent": False,
    "authorized_time": None,
    "cache_age_seconds": None,
    "gnss_acquisition_started": False,
  }
  arguments[name] = value
  with pytest.raises(ValueError):
    evaluate_navigation_database_restore(**arguments)  # type: ignore[arg-type, ty:invalid-argument-type]


def test_invalid_authorized_time_is_rejected() -> None:
  with pytest.raises(ValueError):
    evaluate(authorized_time="network")


def test_controller_applies_wait_restore_and_skip() -> None:
  waiting = NavigationDatabaseRestoreBootController()
  assert not waiting.apply_decision(evaluate(authorized_time=None, cache_age_seconds=None))
  assert waiting.pending

  restoring = NavigationDatabaseRestoreBootController()
  assert restoring.apply_decision(evaluate())
  assert restoring.restore_attempted
  assert restoring.pending

  skipped = NavigationDatabaseRestoreBootController()
  assert skipped.apply_decision(evaluate(cache_age_seconds=7200.0))
  assert skipped.disposition is NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED
