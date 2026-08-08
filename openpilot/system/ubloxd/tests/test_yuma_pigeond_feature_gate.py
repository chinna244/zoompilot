from datetime import UTC, datetime
from types import SimpleNamespace

from openpilot.system.ubloxd import pigeond
from openpilot.system.ubloxd.yuma_almanac_config import (
  PUBLIC_YUMA_ALMANAC_ENABLED_PARAM,
)
from openpilot.system.ubloxd.yuma_almanac_plan import (
  YumaSupplementationAction,
  YumaSupplementationPlan,
  YumaSupplementationReason,
)
from openpilot.system.ubloxd.yuma_almanac_runtime import (
  YumaSupplementationRuntimeOutcome,
)


class FakeParams:
  def __init__(self, enabled: bool) -> None:
    self.enabled = enabled

  def get_bool(self, key: str) -> bool:
    assert key == PUBLIC_YUMA_ALMANAC_ENABLED_PARAM
    return self.enabled


def fake_outcome(
  reason=YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME,
):
  return YumaSupplementationRuntimeOutcome(
    plan=YumaSupplementationPlan(
      (
        YumaSupplementationAction.WAIT
        if reason
        is YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME
        else YumaSupplementationAction.SKIP
      ),
      reason,
    ),
    terminal=(
      reason
      is not YumaSupplementationReason.WAITING_FOR_TRUSTED_TIME
    ),
  )


class FakeRuntime:
  def __init__(self, outcome: YumaSupplementationRuntimeOutcome) -> None:
    self.outcome = outcome
    self.anchors = []
    self.evaluations = []
    self.cancellations = []

  def set_time_anchor(
    self,
    anchor_utc: datetime,
    anchor_monotonic: float,
    source: str,
  ) -> None:
    self.anchors.append(
      (anchor_utc, anchor_monotonic, source)
    )

  def cancel(self, *, now: float, reason):
    self.cancellations.append((now, reason))
    return fake_outcome(reason)

  def evaluate(self, send_message, **kwargs):
    self.evaluations.append(
      (send_message, kwargs)
    )
    return self.outcome


def initialization(
  completed_at: float,
  time_assistance_utc=None,
  time_assistance_source=None,
):
  return SimpleNamespace(
    navigation_assistance_restore_result=None,
    completed_at=completed_at,
    time_assistance_utc=time_assistance_utc,
    time_assistance_source=time_assistance_source,
  )


def evaluate(feature, now: float):
  return feature.evaluate(
    lambda _message: True,
    now=now,
    nav_sat=None,
    nav_sat_time=None,
    reliable_fix_available=False,
  )


def test_disabled_feature_does_not_construct_or_evaluate(
  monkeypatch,
):
  created = []

  def create_runtime(*args, **kwargs):
    created.append((args, kwargs))
    return FakeRuntime(fake_outcome())

  monkeypatch.setattr(
    pigeond,
    "create_yuma_supplementation_runtime",
    create_runtime,
  )

  feature = pigeond.YumaSupplementationFeature(
    FakeParams(False),
    initialization(100.0),
    0,
  )

  assert not feature.runtime_active
  assert evaluate(feature, 100.0) is None
  assert created == []


def test_runtime_is_created_enabled_and_disposed_disabled(
  monkeypatch,
):
  params = FakeParams(False)
  created = []

  def create_runtime(*args, **kwargs):
    runtime = FakeRuntime(fake_outcome())
    created.append((args, kwargs, runtime))
    return runtime

  monkeypatch.setattr(
    pigeond,
    "create_yuma_supplementation_runtime",
    create_runtime,
  )

  feature = pigeond.YumaSupplementationFeature(
    params,
    initialization(100.0),
    0,
  )
  assert not feature.runtime_active

  params.enabled = True
  first_outcome = evaluate(feature, 101.1)

  assert first_outcome is not None
  assert first_outcome.receiver_cycle == 0
  assert first_outcome.feature_enabled
  assert feature.runtime_active
  assert created[0][1]["started_at"] == 101.1

  anchor_utc = datetime(
    2026,
    7,
    21,
    18,
    tzinfo=UTC,
  )
  feature.set_time_anchor(
    anchor_utc,
    101.2,
    "synchronized",
  )
  assert created[0][2].anchors == [
    (anchor_utc, 101.2, "synchronized")
  ]

  params.enabled = False
  disabled_outcome = evaluate(feature, 102.2)
  assert disabled_outcome is not None
  assert disabled_outcome.receiver_cycle == 0
  assert not disabled_outcome.feature_enabled
  assert disabled_outcome.plan.reason is (
    YumaSupplementationReason.FEATURE_DISABLED
  )
  assert not feature.runtime_active

  feature.set_time_anchor(
    anchor_utc,
    102.5,
    "synchronized",
  )

  params.enabled = True
  second_outcome = evaluate(feature, 103.3)

  assert len(created) == 2
  assert second_outcome is not None
  assert second_outcome.receiver_cycle == 0
  assert second_outcome.feature_enabled
  assert created[1][1]["started_at"] == 103.3
  assert (
    created[1][1]["time_anchor_utc"]
    == anchor_utc
  )
  assert (
    created[1][1]["time_anchor_monotonic"]
    == 102.5
  )
  assert created[1][1]["time_anchor_source"] == "synchronized"


def test_receiver_utc_anchor_only_fills_missing_anchor(
  monkeypatch,
):
  created = []

  def create_runtime(*args, **kwargs):
    runtime = FakeRuntime(fake_outcome())
    created.append(runtime)
    return runtime

  monkeypatch.setattr(
    pigeond,
    "create_yuma_supplementation_runtime",
    create_runtime,
  )

  feature = pigeond.YumaSupplementationFeature(
    FakeParams(True),
    initialization(100.0),
    0,
  )
  receiver_utc = datetime(2026, 7, 22, 12, tzinfo=UTC)
  later_receiver_utc = receiver_utc.replace(minute=1)
  synchronized_utc = receiver_utc.replace(minute=2)

  assert feature.set_receiver_time_anchor(receiver_utc, 257.0)
  assert not feature.set_receiver_time_anchor(
    later_receiver_utc,
    258.0,
  )
  feature.set_time_anchor(
    synchronized_utc,
    300.0,
    "synchronized",
  )
  assert not feature.set_receiver_time_anchor(
    later_receiver_utc,
    301.0,
  )

  assert created[0].anchors == [
    (receiver_utc, 257.0, "receiver_utc"),
    (synchronized_utc, 300.0, "synchronized"),
  ]
  assert feature.time_anchor_source == "synchronized"


def test_receiver_cycle_reset_rebuilds_only_when_enabled(
  monkeypatch,
):
  params = FakeParams(True)
  created = []

  def create_runtime(*args, **kwargs):
    runtime = FakeRuntime(fake_outcome())
    created.append((args, kwargs, runtime))
    return runtime

  monkeypatch.setattr(
    pigeond,
    "create_yuma_supplementation_runtime",
    create_runtime,
  )

  feature = pigeond.YumaSupplementationFeature(
    params,
    initialization(100.0),
    0,
  )
  assert len(created) == 1

  first_runtime = created[0][2]
  feature.reset_receiver_cycle(
    initialization(200.0),
    1,
  )
  assert len(created) == 2
  assert feature.runtime_active
  assert first_runtime.cancellations == [(
    200.0,
    pigeond.YumaSupplementationReason.RECEIVER_CYCLE_RESET,
  )]
  reset_outcome = evaluate(feature, 200.1)
  assert reset_outcome is not None
  assert reset_outcome.receiver_cycle == 0
  assert reset_outcome.feature_enabled
  assert reset_outcome.plan.reason is (
    YumaSupplementationReason.RECEIVER_CYCLE_RESET
  )

  new_cycle_outcome = evaluate(feature, 200.2)
  assert new_cycle_outcome is not None
  assert new_cycle_outcome.receiver_cycle == 1

  params.enabled = False
  feature.reset_receiver_cycle(
    initialization(300.0),
    2,
  )
  assert len(created) == 2
  assert not feature.runtime_active


def test_feature_off_on_same_cycle_does_not_inject_twice(
  monkeypatch,
):
  params = FakeParams(True)
  created = []
  send_plan = YumaSupplementationRuntimeOutcome(
    plan=YumaSupplementationPlan(
      YumaSupplementationAction.SEND_ALL,
      YumaSupplementationReason.DATABASE_RESTORE_INCOMPLETE,
      satellite_ids=frozenset((1,)),
    ),
    transmission_attempt=1,
    terminal=True,
  )

  def create_runtime(*args, **kwargs):
    runtime = FakeRuntime(send_plan)
    created.append(runtime)
    return runtime

  monkeypatch.setattr(
    pigeond,
    "create_yuma_supplementation_runtime",
    create_runtime,
  )

  feature = pigeond.YumaSupplementationFeature(
    params,
    initialization(100.0),
    0,
  )

  first = evaluate(feature, 100.0)
  assert first is not None
  assert first.transmission_attempt == 1
  assert feature.cycle_injection_consumed

  params.enabled = False
  evaluate(feature, 101.1)
  params.enabled = True

  assert evaluate(feature, 102.2) is None
  # The next call is still inside the parameter polling interval. A blocked
  # re-enable must remain disabled instead of claiming a missing runtime is
  # active and tripping the evaluate assertion.
  assert evaluate(feature, 102.3) is None
  assert len(created) == 1
  assert not feature.runtime_active

  feature.reset_receiver_cycle(
    initialization(200.0),
    1,
  )
  assert len(created) == 2
  assert not feature.cycle_injection_consumed


def test_cycle_injection_guard_keeps_pending_runtime_retry(
  monkeypatch,
):
  params = FakeParams(True)
  first = YumaSupplementationRuntimeOutcome(
    plan=YumaSupplementationPlan(
      YumaSupplementationAction.SEND_ALL,
      YumaSupplementationReason.DATABASE_RESTORE_INCOMPLETE,
      satellite_ids=frozenset((1,)),
    ),
    transmission_attempt=1,
    terminal=False,
    retry_pending=True,
  )
  second = YumaSupplementationRuntimeOutcome(
    plan=first.plan,
    transmission_attempt=2,
    terminal=True,
  )

  class RetryRuntime(FakeRuntime):
    def __init__(self) -> None:
      super().__init__(first)
      self.outcomes = iter((first, second))

    def evaluate(self, send_message, **kwargs):
      self.evaluations.append((send_message, kwargs))
      return next(self.outcomes)

  runtime = RetryRuntime()
  monkeypatch.setattr(
    pigeond,
    "create_yuma_supplementation_runtime",
    lambda *args, **kwargs: runtime,
  )

  feature = pigeond.YumaSupplementationFeature(
    params,
    initialization(100.0),
    0,
  )

  first_outcome = evaluate(feature, 100.0)
  second_outcome = evaluate(feature, 101.1)

  assert first_outcome is not None
  assert first_outcome.retry_pending
  assert feature.cycle_injection_consumed
  assert second_outcome is not None
  assert second_outcome.transmission_attempt == 2
  assert second_outcome.terminal
  assert len(runtime.evaluations) == 2
