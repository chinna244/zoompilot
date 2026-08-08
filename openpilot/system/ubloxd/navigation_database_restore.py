from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite

from openpilot.system.ubloxd.trusted_time_anchor import (
  TimeProvenance,
  TrustedTimeSource,
)
from openpilot.system.ubloxd.trusted_time_authority import (
  AuthorizedTime,
  TimeAuthorizationEvidence,
)


# MGA-DBD is opaque and cannot be filtered by constellation or record age.
# This restore foundation therefore authorizes only whole caches no older than
# one hour.  It intentionally does not make a roughly two-hour d8 -> d9 cache
# eligible; that field case needs a separate safe cache-freshness mechanism.
NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS = 60.0 * 60.0


@dataclass(frozen=True)
class NavigationDatabaseRestoreAgePolicy:
  maximum_age_seconds: float = NAVIGATION_DATABASE_RESTORE_MAX_AGE_SECONDS

  def __post_init__(self) -> None:
    value = self.maximum_age_seconds
    if (
      isinstance(value, bool)
      or not isinstance(value, (int, float))
      or not isfinite(float(value))
      or float(value) < 0.0
    ):
      raise ValueError("maximum cache age is invalid")
    object.__setattr__(self, "maximum_age_seconds", float(value))

  def accepts(self, cache_age_seconds: float | None) -> bool:
    return (
      not isinstance(cache_age_seconds, bool)
      and isinstance(cache_age_seconds, (int, float))
      and isfinite(float(cache_age_seconds))
      and 0.0 <= float(cache_age_seconds) <= self.maximum_age_seconds
    )


DEFAULT_NAVIGATION_DATABASE_RESTORE_AGE_POLICY = (
  NavigationDatabaseRestoreAgePolicy()
)


class NavigationDatabaseRestoreDisposition(StrEnum):
  """Boot-scoped outcome for opaque MGA-DBD restoration."""

  PENDING = "pending"
  RESTORED = "restored"
  RESTORE_PARTIAL = "restore_partial"
  RESTORE_REJECTED = "restore_rejected"
  RESTORE_RESPONSE_TIMEOUT = "restore_response_timeout"
  RESTORE_TRANSFER_DEADLINE = "restore_transfer_deadline"
  RESTORE_TRANSPORT_ERROR = "restore_transport_error"
  RESTORE_CACHE_EXPIRED = "restore_cache_expired"
  SKIPPED_EXPIRED = "skipped_expired"
  SKIPPED_UNVERIFIED = "skipped_unverified"
  SKIPPED_NO_TRUSTED_TIME = "skipped_no_trusted_time"
  SKIPPED_WAIT_TIMEOUT = "skipped_wait_timeout"
  SKIPPED_WAIT_ERROR = "skipped_wait_error"
  SKIPPED_STATE_UNAVAILABLE = "skipped_state_unavailable"
  SKIPPED_EARLY_ACQUISITION = "skipped_early_acquisition"
  SKIPPED_LATE_RECEIVER_TIME = "skipped_late_receiver_time"
  SKIPPED_ACQUISITION_ALREADY_STARTED = "skipped_acquisition_already_started"
  SKIPPED_RELIABLE_FIX = "skipped_reliable_fix"
  SKIPPED_YUMA_ALREADY_SENT = "skipped_yuma_already_sent"
  SKIPPED_NO_CACHE = "skipped_no_cache"
  SKIPPED_CACHE_UNQUALIFIED = "skipped_cache_unqualified"
  SKIPPED_NO_USABLE_CACHE = "skipped_no_usable_cache"
  WRITE_FAILED = "write_failed"

  @property
  def terminal(self) -> bool:
    return self is not NavigationDatabaseRestoreDisposition.PENDING

  @property
  def database_available(self) -> bool:
    return self is NavigationDatabaseRestoreDisposition.RESTORED

  @property
  def intentionally_skipped(self) -> bool:
    return self.name.startswith("SKIPPED_")

  @property
  def write_failed(self) -> bool:
    return self in (
      NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
      NavigationDatabaseRestoreDisposition.RESTORE_REJECTED,
      NavigationDatabaseRestoreDisposition.RESTORE_RESPONSE_TIMEOUT,
      NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE,
      NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR,
      NavigationDatabaseRestoreDisposition.RESTORE_CACHE_EXPIRED,
      NavigationDatabaseRestoreDisposition.WRITE_FAILED,
    )


class NavigationDatabaseRestoreDecisionAction(StrEnum):
  WAIT = "wait"
  RESTORE = "restore"
  SKIP = "skip"


@dataclass(frozen=True)
class NavigationDatabaseRestoreDecision:
  action: NavigationDatabaseRestoreDecisionAction
  skip_disposition: NavigationDatabaseRestoreDisposition | None = None

  def __post_init__(self) -> None:
    if not isinstance(self.action, NavigationDatabaseRestoreDecisionAction):
      raise ValueError("decision action is invalid")
    if self.action is NavigationDatabaseRestoreDecisionAction.SKIP:
      if self.skip_disposition is None or not self.skip_disposition.intentionally_skipped:
        raise ValueError("skip decision requires an intentional skip disposition")
    elif self.skip_disposition is not None:
      raise ValueError("wait and restore decisions cannot include a skip disposition")

  @property
  def waiting(self) -> bool:
    return self.action is NavigationDatabaseRestoreDecisionAction.WAIT

  @property
  def should_restore(self) -> bool:
    return self.action is NavigationDatabaseRestoreDecisionAction.RESTORE

  @property
  def should_skip(self) -> bool:
    return self.action is NavigationDatabaseRestoreDecisionAction.SKIP


class NavigationDatabaseRestoreBootController:
  def __init__(self) -> None:
    self._disposition = NavigationDatabaseRestoreDisposition.PENDING
    self._restore_attempted = False
    self._position_assistance_claimed = False
    self._acquisition_started = False

  @property
  def disposition(self) -> NavigationDatabaseRestoreDisposition:
    return self._disposition

  @property
  def pending(self) -> bool:
    return self._disposition is NavigationDatabaseRestoreDisposition.PENDING

  @property
  def terminal(self) -> bool:
    return self._disposition.terminal

  @property
  def restore_attempted(self) -> bool:
    return self._restore_attempted

  @property
  def acquisition_started(self) -> bool:
    return self._acquisition_started

  def claim_position_assistance(self) -> bool:
    if self._position_assistance_claimed:
      return False
    self._position_assistance_claimed = True
    return True

  def note_acquisition_started(self) -> None:
    self._acquisition_started = True

  def apply_decision(
    self,
    decision: NavigationDatabaseRestoreDecision,
  ) -> bool:
    if not isinstance(decision, NavigationDatabaseRestoreDecision):
      raise ValueError("restore decision is invalid")
    if self.terminal or self._restore_attempted:
      return False
    if decision.waiting:
      return False
    if decision.should_restore:
      return self.begin_restore_attempt()
    assert decision.skip_disposition is not None
    return self.skip(decision.skip_disposition)

  def begin_restore_attempt(self) -> bool:
    if self.terminal or self._restore_attempted:
      return False
    self._restore_attempted = True
    return True

  def finish_restore(self, disposition: NavigationDatabaseRestoreDisposition) -> bool:
    if disposition not in (
      NavigationDatabaseRestoreDisposition.RESTORED,
      NavigationDatabaseRestoreDisposition.RESTORE_PARTIAL,
      NavigationDatabaseRestoreDisposition.RESTORE_REJECTED,
      NavigationDatabaseRestoreDisposition.RESTORE_RESPONSE_TIMEOUT,
      NavigationDatabaseRestoreDisposition.RESTORE_TRANSFER_DEADLINE,
      NavigationDatabaseRestoreDisposition.RESTORE_TRANSPORT_ERROR,
      NavigationDatabaseRestoreDisposition.RESTORE_CACHE_EXPIRED,
      NavigationDatabaseRestoreDisposition.WRITE_FAILED,
    ):
      raise ValueError("restore completion disposition is invalid")
    if self.terminal or not self._restore_attempted:
      return False
    self._disposition = disposition
    return True

  def skip(self, disposition: NavigationDatabaseRestoreDisposition) -> bool:
    if not disposition.intentionally_skipped:
      raise ValueError("skip disposition must be intentional")
    if self.terminal or self._restore_attempted:
      return False
    self._disposition = disposition
    return True


def is_current_independent_network_time(
  authorized_time: AuthorizedTime,
) -> bool:
  return (
    authorized_time.independent
    and authorized_time.source is TrustedTimeSource.SYSTEM_SYNCHRONIZED
    and authorized_time.provenance is TimeProvenance.NETWORK_INDEPENDENT
    and authorized_time.evidence is TimeAuthorizationEvidence.SYSTEM_SYNCHRONIZED
  )


def evaluate_navigation_database_restore(
  *,
  reliable_fix_available: bool,
  yuma_already_sent: bool,
  authorized_time: AuthorizedTime | None,
  cache_age_seconds: float | None,
  gnss_acquisition_started: bool,
  age_policy: NavigationDatabaseRestoreAgePolicy = DEFAULT_NAVIGATION_DATABASE_RESTORE_AGE_POLICY,
) -> NavigationDatabaseRestoreDecision:
  for name, value in (
    ("reliable_fix_available", reliable_fix_available),
    ("yuma_already_sent", yuma_already_sent),
    ("gnss_acquisition_started", gnss_acquisition_started),
  ):
    if not isinstance(value, bool):
      raise ValueError(f"{name} must be a bool")
  if authorized_time is not None and not isinstance(authorized_time, AuthorizedTime):
    raise ValueError("authorized_time is invalid")
  if not isinstance(age_policy, NavigationDatabaseRestoreAgePolicy):
    raise ValueError("age_policy is invalid")

  if reliable_fix_available:
    return NavigationDatabaseRestoreDecision(
      NavigationDatabaseRestoreDecisionAction.SKIP,
      NavigationDatabaseRestoreDisposition.SKIPPED_RELIABLE_FIX,
    )
  if yuma_already_sent:
    return NavigationDatabaseRestoreDecision(
      NavigationDatabaseRestoreDecisionAction.SKIP,
      NavigationDatabaseRestoreDisposition.SKIPPED_YUMA_ALREADY_SENT,
    )
  if authorized_time is not None and authorized_time.source is TrustedTimeSource.RECEIVER_UTC_UNASSISTED_GNSS:
    return NavigationDatabaseRestoreDecision(
      NavigationDatabaseRestoreDecisionAction.SKIP,
      NavigationDatabaseRestoreDisposition.SKIPPED_LATE_RECEIVER_TIME,
    )
  if gnss_acquisition_started:
    return NavigationDatabaseRestoreDecision(
      NavigationDatabaseRestoreDecisionAction.SKIP,
      NavigationDatabaseRestoreDisposition.SKIPPED_ACQUISITION_ALREADY_STARTED,
    )
  if authorized_time is None or not is_current_independent_network_time(authorized_time):
    return NavigationDatabaseRestoreDecision(NavigationDatabaseRestoreDecisionAction.WAIT)
  if (
    isinstance(cache_age_seconds, bool)
    or not isinstance(cache_age_seconds, (int, float))
    or not isfinite(float(cache_age_seconds))
    or float(cache_age_seconds) < 0.0
  ):
    return NavigationDatabaseRestoreDecision(
      NavigationDatabaseRestoreDecisionAction.SKIP,
      NavigationDatabaseRestoreDisposition.SKIPPED_UNVERIFIED,
    )
  if not age_policy.accepts(cache_age_seconds):
    return NavigationDatabaseRestoreDecision(
      NavigationDatabaseRestoreDecisionAction.SKIP,
      NavigationDatabaseRestoreDisposition.SKIPPED_EXPIRED,
    )
  return NavigationDatabaseRestoreDecision(NavigationDatabaseRestoreDecisionAction.RESTORE)
