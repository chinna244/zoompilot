import ast
from pathlib import Path


PIGEOND = Path("openpilot/system/ubloxd/pigeond.py")
RUNTIME = Path("openpilot/system/ubloxd/navigation_database_restore_runtime.py")


def source_tree(path: Path) -> tuple[str, ast.Module]:
  source = path.read_text(encoding="utf-8")
  return source, ast.parse(source)


def named_node(tree: ast.Module, name: str) -> ast.AST:
  matches = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name == name]
  assert len(matches) == 1
  return matches[0]


def calls(node: ast.AST, name: str) -> list[ast.Call]:
  return [
    item
    for item in ast.walk(node)
    if isinstance(item, ast.Call)
    and (isinstance(item.func, ast.Name) and item.func.id == name or isinstance(item.func, ast.Attribute) and item.func.attr == name)
  ]


def test_initialize_receiver_cycle_uses_receiver_cycle_runtime_adapter() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "initialize_receiver_cycle")
  restore_calls = calls(node, "restore_navigation_assistance")
  # One pre-START restore path and one deferred post-START path that still
  # evaluates independent position assistance after DBD is terminalized.
  assert len(restore_calls) == 2
  for restore_call in restore_calls:
    keywords = {keyword.arg for keyword in restore_call.keywords}
    assert "navigation_database_runtime" in keywords
    assert "authorized_time" in keywords
  segment = ast.get_source_segment(source, node)
  assert segment is not None
  assert "allow_legacy_direct_restore" not in segment
  assert "poll_deferred_assistance_state" in segment
  deferred = segment.index("def poll_deferred_assistance_state(")
  deferred_restore = segment.index("restore_navigation_assistance(", deferred)
  assert deferred < deferred_restore


def test_live_loop_creates_fresh_state_for_each_receiver_cycle() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "run_receiving")
  segment = ast.get_source_segment(source, node)
  assert segment is not None
  assert not calls(node, "create_receiver_cycle_assistance_state")
  assert len(calls(node, "prepare_receiver_cycle_response_state")) == 1
  initialize_calls = calls(node, "initialize_receiver_cycle")
  assert len(initialize_calls) == 2
  for call in initialize_calls:
    keywords = {keyword.arg for keyword in call.keywords}
    assert "navigation_database_runtime" not in keywords
    assert "assistance_state_factory" in keywords
    assert "assistance_state_ready_callback" in keywords
    assert "response_state_prepared" not in keywords

  navigation_factory = named_node(
    tree,
    "create_receiver_cycle_navigation_state",
  )
  navigation_calls = calls(
    navigation_factory,
    "NavigationDatabaseRestoreRuntime",
  )
  assert len(navigation_calls) == 1
  assert any(
    keyword.arg == "new_receiver_cycle" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in navigation_calls[0].keywords
  )

  assistance_factories = [item for item in node.body if (isinstance(item, ast.FunctionDef) and item.name == "create_receiver_cycle_assistance_state")]
  assert len(assistance_factories) == 1
  retry_calls = calls(
    assistance_factories[0],
    "PositionAssistanceRetryRuntime",
  )
  assert len(retry_calls) == 1
  assert any(
    keyword.arg == "new_receiver_cycle" and isinstance(keyword.value, ast.Constant) and keyword.value.value is True for keyword in retry_calls[0].keywords
  )


def test_process_start_never_installs_a_pre_start_network_wait() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "run_receiving")
  segment = ast.get_source_segment(source, node)
  assert segment is not None
  assert "network_available=device_network_available(sm)" in segment
  assert "allow_database_trusted_time_wait" not in segment
  assert "network_available_reader" not in segment
  assert "wait_for_current_independent_network_time" not in source
  assert "get_assistnow_messages" not in source
  assert "AssistNowToken" not in source
  assert "online-live2.services.u-blox.com" not in source
  assert "GetOnlineData.ashx" not in source


def test_deferred_assistance_worker_is_polled_without_unbounded_wait() -> None:
  source, tree = source_tree(PIGEOND)
  initialize = named_node(tree, "initialize_receiver_cycle")
  wait_calls = calls(initialize, "wait")
  assert len(wait_calls) == 1
  assert any(keyword.arg == "timeout" for keyword in wait_calls[0].keywords)
  initialize_segment = ast.get_source_segment(source, initialize)
  assert initialize_segment is not None
  assert "assistance_state_task_complete.is_set()" in initialize_segment
  assert "assistance_state_task_complete.wait()" not in initialize_segment
  assert "if assistance_state_task_started:" in initialize_segment

  receiving = named_node(tree, "run_receiving")
  receiving_segment = ast.get_source_segment(source, receiving)
  assert receiving_segment is not None
  loop = receiving_segment.index("while (")
  poll = receiving_segment.index(
    "deferred_result = deferred_assistance_poll()",
    loop,
  )
  receive = receiving_segment.index("pigeon.receive()", loop)
  assert loop < poll < receive
  assert receiving_segment.count("if navigation_database_runtime is not None") >= 3


def test_late_network_time_remains_a_post_start_assistance_path() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "run_receiving")
  segment = ast.get_source_segment(source, node)
  assert segment is not None
  initialization = segment.index("cycle_initialization = initialize_receiver_cycle(")
  receiving_loop = segment.index("authority_evaluation_for_loop:", initialization)
  authority_refresh = segment.index("evaluate_time_authority(", receiving_loop)
  late_time_assistance = segment.index("send_time_assistance(", authority_refresh)
  assert initialization < receiving_loop < authority_refresh < late_time_assistance


def test_acquisition_latch_is_updated_before_receiver_processing() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "run_receiving")
  segment = ast.get_source_segment(source, node)
  assert segment is not None
  assert segment.index("receiver_frames_show_gnss_acquisition(frames)") < segment.index("process_receiver_frames(")
  assert "handle_receiver_acquisition_state(" in segment


def test_database_decision_runs_before_yuma_transmission() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "run_receiving")
  segment = ast.get_source_segment(source, node)
  assert segment is not None

  database_decision = segment.index("navigation_database_runtime.evaluate(")
  assert database_decision < segment.index("yuma_feature.evaluate_provisional(")
  assert database_decision < segment.index("yuma_feature.evaluate(")

  assert "provisional_yuma_outcome = (" in segment
  assert "yuma_feature.evaluate_provisional(" in segment
  assert "database_restore_pending=(" in segment
  assert "navigation_database_runtime.database_restore_pending" in segment
  assert "yuma_outcome = (" in segment
  assert "yuma_feature.evaluate(" in segment
  assert segment.count("if navigation_database_runtime is not None") >= 3
  assert "navigation_database_runtime.note_yuma_sent()" not in segment

  helper = named_node(tree, "send_yuma_with_durable_claim")
  helper_segment = ast.get_source_segment(source, helper)
  assert helper_segment is not None
  assert helper_segment.index("navigation_database_runtime.claim_yuma_transmission()") < helper_segment.index("send_message(message)")

  runtime_node = named_node(
    source_tree(RUNTIME)[1],
    "NavigationDatabaseRestoreRuntime",
  )
  runtime_source = RUNTIME.read_text(encoding="utf-8")
  runtime_segment = ast.get_source_segment(runtime_source, runtime_node)
  assert runtime_segment is not None
  claim = runtime_segment.index("def claim_yuma_transmission")
  pending = runtime_segment.index("self.database_restore_pending", claim)
  persist = runtime_segment.index("self._persist_state()", claim)
  assert claim < pending < persist
  assert "raise YumaAssistanceStateUnavailableError(" in helper_segment
  assert "return False" not in helper_segment


def test_skipped_database_never_populates_restored_quality_fields() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(
    tree,
    "navigation_assistance_result_from_database_execution",
  )
  segment = ast.get_source_segment(source, node)
  assert segment is not None
  assert "evaluated_quality if disposition.database_available else None" in segment
  assert "restored_navigation_quality=restored_quality" in segment
  assert "captured_gps_almanac_available=" in segment
  assert "captured_gps_almanac_satellite_ids=" in segment


def test_yuma_database_state_preserves_pending_disposition() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "yuma_database_restore_state")
  segment = ast.get_source_segment(source, node)
  assert segment is not None
  assert "disposition = result.database_restore_disposition" in segment
  assert "if disposition is NavigationDatabaseRestoreDisposition.PENDING" in segment
  assert "return YumaDatabaseRestoreState.PENDING" in segment
  assert "if disposition.database_available" in segment


def test_mandatory_configuration_precedes_assistance_and_optional_work() -> None:
  source, tree = source_tree(PIGEOND)
  init_node = named_node(tree, "init")
  init_segment = ast.get_source_segment(source, init_node)
  assert init_segment is not None
  assert init_segment.index("start_pigeon_transport(pigeon)") < init_segment.index("initialization.run()")
  mandatory = init_segment.index("finish_pigeon_initialization(pigeon)")
  assistance = init_segment.index("initialization.run()")
  optional = init_segment.index("finish_post_start_receiver_configuration(pigeon)")
  legacy = init_segment.index("run_post_start_legacy_assistance(pigeon)")
  assert mandatory < assistance < optional < legacy

  node = named_node(tree, "initialize_receiver_cycle")
  segment = ast.get_source_segment(source, node)
  assert segment is not None
  restore = segment.index("restore_navigation_assistance(")
  time_assistance = segment.index("send_time_assistance(")
  observations = segment.index("provenance.enable_receiver_observations(")
  assert restore < time_assistance < observations
  assert not calls(node, "wait_for_current_independent_network_time")
  assert "install_pre_acquisition_initialization(" in segment


def test_process_start_transport_precedes_pr66_state_creation() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "run_receiving")
  segment = ast.get_source_segment(source, node)
  assert segment is not None
  disable_dispatch = segment.index("pigeon.set_frame_dispatcher(None)")
  bootstrap = segment.index("bootstrap_process_start_transport(pigeon)")
  enable_dispatch = segment.index("pigeon.set_frame_dispatcher(dispatch_frames)")
  initialize = segment.index("initialize_receiver_cycle(")
  factory = segment.index(
    "assistance_state_factory=new_receiver_cycle_assistance_state_factory()",
    initialize,
  )
  assert disable_dispatch < bootstrap < enable_dispatch < initialize < factory


def test_bootstrap_frames_dispatch_only_after_mandatory_configuration() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "initialize_receiver_cycle")
  callback = next(item for item in node.body if isinstance(item, ast.FunctionDef) and item.name == "pre_acquisition_initialization")
  segment = ast.get_source_segment(source, callback)
  assert segment is not None
  navx5 = segment.index("configure_navx5_ack_aiding(")
  trusted_time = segment.index("read_host_time_observation()")
  prepare = segment.index("prepare_assistance_state_before_start(")
  dispatch = segment.index("pigeon.dispatch_pending_frames()")
  restore = segment.index("restore_navigation_assistance(")
  assert navx5 < trusted_time < prepare < dispatch < restore


def test_pre_database_setup_does_not_ignore_acquisition_frames() -> None:
  source, tree = source_tree(PIGEOND)
  initialize = named_node(tree, "initialize_receiver_cycle")
  segment = ast.get_source_segment(source, initialize)
  assert segment is not None
  assert "resolve_pre_acquisition_mon_ver(" in segment
  assert "initialization.transport_mon_ver_info" in segment
  assert "configure_navx5_ack_aiding(" in segment
  assert "pre_start_deadline=pre_start_deadline" in segment

  unrelated = named_node(tree, "_queue_unrelated_frames")
  unrelated_segment = ast.get_source_segment(source, unrelated)
  assert unrelated_segment is not None
  assert "pigeon.queue_pending_frames(" in unrelated_segment
  assert "pigeon.dispatch_pending_frames()" in unrelated_segment


def test_new_runtime_does_not_power_cycle_or_reinitialize_receiver() -> None:
  _source, tree = source_tree(RUNTIME)
  assert not calls(tree, "set_power")
  assert not calls(tree, "initialize_receiver_cycle")


def test_runtime_persists_linux_boot_identity_and_terminal_state() -> None:
  source, tree = source_tree(RUNTIME)
  runtime = named_node(tree, "NavigationDatabaseRestoreRuntime")
  segment = ast.get_source_segment(source, runtime)
  assert segment is not None
  assert "boot_id_reader" in segment
  assert "_restore_persisted_state" in segment
  assert "recovered_interrupted_attempt" in segment
  assert "position_assistance_claimed" in source
  assert "acquisition_started" in source
  assert "yuma_sent" in source


def test_pigeond_change_does_not_add_direct_power_cycle_call() -> None:
  _source, tree = source_tree(PIGEOND)
  for name in (
    "initialize_receiver_cycle",
    "navigation_assistance_result_from_database_execution",
    "receiver_frames_show_gnss_acquisition",
    "run_receiving",
  ):
    assert not calls(named_node(tree, name), "set_power")


def test_controlled_stop_start_brackets_pre_database_window() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "paused_gnss_acquisition")
  segment = ast.get_source_segment(source, node)
  assert segment is not None

  stop = segment.index("pigeon.send(CONTROLLED_GNSS_STOP_MESSAGE)")
  yielded = segment.index("yield")
  failure_path = segment.index("except BaseException as exc:")
  finalizer = segment.index("finally:")
  start = segment.index("pigeon.send(CONTROLLED_GNSS_START_MESSAGE)")

  assert stop < yielded < failure_path < finalizer < start
  assert "body_error = exc" in segment[failure_path:finalizer]
  assert "if body_error is not None:" in segment[start:]


def test_power_on_stop_precedes_baud_transition_and_transactions() -> None:
  source, tree = source_tree(PIGEOND)
  baud = named_node(tree, "init_baudrate")
  segment = ast.get_source_segment(source, baud)
  assert segment is not None
  power_on_baud = segment.index("pigeon.set_baud(9600)")
  stop = segment.index("pigeon.send(CONTROLLED_GNSS_STOP_MESSAGE)")
  baud_transition = segment.index(r'pigeon.send(b"\x24\x50\x55\x42\x58')
  assert power_on_baud < stop < baud_transition

  initialize = named_node(tree, "initialize_receiver_cycle")
  initialize_segment = ast.get_source_segment(source, initialize)
  assert initialize_segment is not None
  assert initialize_segment.index("restore_navigation_assistance(") < (initialize_segment.index("send_time_assistance("))


def test_structured_telemetry_keeps_assistance_command_order() -> None:
  source, tree = source_tree(PIGEOND)

  paused = named_node(tree, "paused_gnss_acquisition")
  paused_segment = ast.get_source_segment(source, paused)
  assert paused_segment is not None
  assert paused_segment.index("pigeon.send(CONTROLLED_GNSS_STOP_MESSAGE)") < paused_segment.index("yield")
  assert paused_segment.index("yield") < paused_segment.index("pigeon.send(CONTROLLED_GNSS_START_MESSAGE)")

  initialize = named_node(tree, "initialize_receiver_cycle")
  initialize_segment = ast.get_source_segment(source, initialize)
  assert initialize_segment is not None
  restore = initialize_segment.index("restore_navigation_assistance(")
  time_assistance = initialize_segment.index("send_time_assistance(")
  acquisition_claim = initialize_segment.index("claim_acquisition_start(")
  assert restore < time_assistance < acquisition_claim

  restore_helper = named_node(
    tree,
    "restore_navigation_assistance",
  )
  restore_segment = ast.get_source_segment(
    source,
    restore_helper,
  )
  assert restore_segment is not None
  database_restore = restore_segment.index("navigation_database_runtime.evaluate(")
  position_restore = restore_segment.index("navigation_database_runtime.send_position_once(")
  assert database_restore < position_restore

  run_receiving = named_node(tree, "run_receiving")
  run_segment = ast.get_source_segment(source, run_receiving)
  assert run_segment is not None
  receiver_start = run_segment.index("initialize_receiver_cycle(")
  runtime_yuma = run_segment.index("yuma_feature.evaluate(")
  assert receiver_start < runtime_yuma


# COMMIT9_DBD_RUNTIME_BEFORE_RECEIVER_TEST


def test_dbd_runtime_initialization_is_deferred_until_after_configuration() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "run_receiving")
  segment = ast.get_source_segment(source, node)
  assert segment is not None

  pigeon = segment.index("pigeon = TTYPigeon(")
  bootstrap = segment.index("bootstrap_process_start_transport(pigeon)")
  first_cycle = segment.index("initialize_receiver_cycle(", bootstrap)
  assistance_factory = segment.index(
    "assistance_state_factory=new_receiver_cycle_assistance_state_factory()",
    first_cycle,
  )
  assert pigeon < bootstrap < first_cycle < assistance_factory

  recovery = segment.index("def recover_receiver(")
  recovery_cycle = segment.index("initialize_receiver_cycle(", recovery)
  recovery_factory = segment.index(
    "assistance_state_factory=new_receiver_cycle_assistance_state_factory()",
    recovery_cycle,
  )
  assert recovery < recovery_cycle < recovery_factory

  initialize = named_node(tree, "initialize_receiver_cycle")
  initialize_segment = ast.get_source_segment(source, initialize)
  assert initialize_segment is not None
  callbacks = [item for item in initialize.body if isinstance(item, ast.FunctionDef) and item.name == "pre_acquisition_initialization"]
  assert len(callbacks) == 1
  callback_segment = ast.get_source_segment(source, callbacks[0])
  assert callback_segment is not None
  navx5 = callback_segment.index("configure_navx5_ack_aiding(")
  trusted_time = callback_segment.index("read_host_time_observation()")
  assistance_state = callback_segment.index("prepare_assistance_state_before_start(")
  assert navx5 < trusted_time < assistance_state


def test_recovery_paths_share_one_bounded_receiver_reset_helper() -> None:
  source, tree = source_tree(PIGEOND)
  node = named_node(tree, "run_receiving")
  segment = ast.get_source_segment(source, node)
  assert segment is not None

  recovery_helpers = [item for item in node.body if (isinstance(item, ast.FunctionDef) and item.name == "recover_receiver")]
  assert len(recovery_helpers) == 1
  helper = recovery_helpers[0]
  helper_segment = ast.get_source_segment(source, helper)
  assert helper_segment is not None

  cancel = helper_segment.index("position_assistance_retry.cancel_receiver_cycle(")
  response_reset = helper_segment.index("prepare_receiver_cycle_response_state(pigeon)")
  receiver_cycle = helper_segment.index("initialize_receiver_cycle(")
  fresh_state = helper_segment.index(
    "assistance_state_factory=new_receiver_cycle_assistance_state_factory()",
    receiver_cycle,
  )
  completed = helper_segment.index("data_watchdog.recovery_completed(")
  assert cancel < response_reset < receiver_cycle < fresh_state < completed
  assert not calls(helper, "create_receiver_cycle_assistance_state")
  assert "GPS receiver recovery started" in helper_segment
  assert "GPS receiver recovery completed" in helper_segment
  assert 'f"reason={reason_value}"' in helper_segment
  assert 'f"attempt={attempt}"' in helper_segment

  assert segment.count("recover_receiver(\n        ReceiverRecoveryReason.NO_DATA,") == 1
  assert segment.count("recover_receiver(\n          ReceiverRecoveryReason.ALL_ZERO_DATA,") == 1
  assert "data_watchdog.request_recovery(" in segment


def test_yuma_assistance_state_unavailable_is_terminal_without_retry() -> None:
  source, tree = source_tree(PIGEOND)
  helper = named_node(
    tree,
    "yuma_assistance_state_unavailable_outcome",
  )
  helper_segment = ast.get_source_segment(source, helper)
  assert helper_segment is not None
  assert 'getattr(result, "assistance_state_unavailable", False)' in helper_segment
  assert "is True" in helper_segment
  assert "YumaAlmanacTransmitStatus" not in helper_segment
  assert "attempted_satellite_ids" not in helper_segment
  assert "accepted_satellite_ids" not in helper_segment
  assert "unavailable_satellite_ids" not in helper_segment

  feature = named_node(tree, "YumaSupplementationFeature")
  feature_segment = ast.get_source_segment(source, feature)
  assert feature_segment is not None
  assert feature_segment.count("yuma_assistance_state_unavailable_outcome(") >= 2
  assert "self._cycle_injection_consumed = True" in feature_segment
  assert "self._runtime = None" in feature_segment
  assert "terminal=True" in feature_segment
  assert "retry_pending=False" in feature_segment
  assert "if outcome.receiver_write_attempted:" in feature_segment
  assert "self._provisional_reference_used = reference" in feature_segment


def test_configuration_summary_persistence_is_strictly_post_start() -> None:
  source, tree = source_tree(PIGEOND)
  strict_configuration = named_node(tree, "init_pigeon")
  strict_segment = ast.get_source_segment(source, strict_configuration)
  assert strict_segment is not None
  assert "persist_receiver_configuration_summary(" not in strict_segment

  start_boundary = named_node(tree, "paused_gnss_acquisition")
  start_segment = ast.get_source_segment(source, start_boundary)
  assert start_segment is not None
  attempted = start_segment.index("initialization.note_gnss_start_attempted()")
  start_write = start_segment.index("pigeon.send(CONTROLLED_GNSS_START_MESSAGE)")
  persisted = start_segment.index("persist_receiver_configuration_summary(")
  assert attempted < start_write < persisted


def test_obsolete_whole_sequence_configuration_retry_is_removed() -> None:
  source, tree = source_tree(PIGEOND)
  strict_configuration = named_node(tree, "init_pigeon")

  assert "for _ in range(10):" not in source
  assert "try initializing a few times" not in source
  assert "removed legacy CFG-PRT-3 sequence" not in source
  assert len(calls(strict_configuration, "run_receiver_configuration_item")) >= 2


def test_strict_configuration_uses_one_absolute_transaction_deadline() -> None:
  source, tree = source_tree(PIGEOND)

  transaction = named_node(tree, "_begin_response_transaction")
  transaction_segment = ast.get_source_segment(source, transaction)
  assert transaction_segment is not None
  assert "deadline=deadline" in transaction_segment

  poll = named_node(tree, "_poll_cfg")
  poll_segment = ast.get_source_segment(source, poll)
  assert poll_segment is not None
  assert "deadline=deadline" in poll_segment
  assert "time.monotonic() >= deadline" in poll_segment

  strict_configuration = named_node(tree, "init_pigeon")
  strict_segment = ast.get_source_segment(source, strict_configuration)
  assert strict_segment is not None
  assert "deadline=pre_start_deadline" in strict_segment
