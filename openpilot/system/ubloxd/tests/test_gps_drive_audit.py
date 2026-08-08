from types import SimpleNamespace

import pytest

from openpilot.system.ubloxd import gps_drive_audit, pigeond


def test_route_metrics_uses_real_gps_schema_and_trusted_provenance():
  metrics = gps_drive_audit.RouteMetrics("00000093--a1ef00c9c2")
  metrics.note_time(100.0)

  metrics.process_gps(
    100.1,
    SimpleNamespace(
      flags=0,
      hasFix=False,
      unixTimestampMillis=1_774_360_000_000,
      satelliteCount=0,
      horizontalAccuracy=41_000.0,
      latitude=0.0,
      longitude=0.0,
    ),
  )

  assert metrics.positive_timestamp_samples == 1
  assert metrics.first_receiver_utc is None

  provenance_message = b",".join(
    (
      b'{"msg":"GPS receiver UTC provenance',
      b' cycle=1',
      b' classification=receiver_utc_unassisted_gnss',
      b' reason=fresh_gnss_time_evidence',
      b' independent=true"}',
    )
  )
  metrics.process_log_message(348.95, provenance_message)
  metrics.process_gps(
    414.95,
    SimpleNamespace(
      flags=1,
      hasFix=True,
      unixTimestampMillis=1_774_898_400_000,
      satelliteCount=5,
      horizontalAccuracy=41.6,
      latitude=32.8,
      longitude=-96.8,
    ),
  )
  metrics.process_gps(
    415.36,
    SimpleNamespace(
      flags=1,
      hasFix=True,
      unixTimestampMillis=1_774_898_400_410,
      satelliteCount=6,
      horizontalAccuracy=23.69,
      latitude=32.80001,
      longitude=-96.80001,
    ),
  )

  assert metrics.relative(metrics.first_receiver_utc) == pytest.approx(248.95)
  assert metrics.relative(metrics.first_fix) == pytest.approx(314.95)
  assert metrics.relative(metrics.first_25m) == pytest.approx(315.36)
  assert metrics.best_accuracy == 23.69
  assert metrics.max_satellites == 6


def test_rawx_week_and_leap_require_nonempty_measurements():
  metrics = gps_drive_audit.RouteMetrics("route")
  metrics.note_time(10.0)

  metrics.process_rawx(
    10.1,
    SimpleNamespace(measurements=(), gpsWeek=2411, leapSeconds=18),
  )

  assert metrics.first_rawx == 10.1
  assert metrics.first_nonempty_rawx is None
  assert metrics.first_valid_gps_week is None
  assert metrics.first_valid_leap_second is None

  measurements = (
    SimpleNamespace(gnssId=0),
    SimpleNamespace(gnssId=6),
  )
  metrics.process_rawx(
    258.98,
    SimpleNamespace(
      measurements=measurements,
      gpsWeek=2411,
      leapSeconds=18,
    ),
  )

  assert metrics.relative(metrics.first_nonempty_rawx) == pytest.approx(248.98)
  assert metrics.relative(metrics.first_gps_measurement) == pytest.approx(248.98)
  assert metrics.relative(metrics.first_glonass_measurement) == pytest.approx(248.98)
  assert metrics.relative(metrics.first_valid_gps_week) == pytest.approx(248.98)
  assert metrics.relative(metrics.first_valid_leap_second) == pytest.approx(248.98)


def test_route_selection_supports_exact_and_latest(tmp_path):
  older = tmp_path / "00000092--d36d5b033c--0"
  newer = tmp_path / "00000093--a1ef00c9c2--0"
  older.mkdir()
  newer.mkdir()

  selections = [
    gps_drive_audit.RouteSelection(
      "00000093--a1ef00c9c2",
      ((0, newer),),
      200.0,
    ),
    gps_drive_audit.RouteSelection(
      "00000092--d36d5b033c",
      ((0, older),),
      100.0,
    ),
  ]

  assert [selection.route for selection in gps_drive_audit.select_routes(selections, None, 1)] == ["00000093--a1ef00c9c2"]
  assert [
    selection.route
    for selection in gps_drive_audit.select_routes(
      selections,
      "00000092--d36d5b033c",
      None,
    )
  ] == ["00000092--d36d5b033c"]


def test_checksums_are_relative_and_self_verifying(tmp_path):
  (tmp_path / "nested").mkdir()
  (tmp_path / "nested" / "evidence.txt").write_text("evidence\n", encoding="utf-8")
  (tmp_path / "summary.txt").write_text("summary\n", encoding="utf-8")

  gps_drive_audit.generate_checksums(tmp_path)
  checksum_text = (tmp_path / "SHA256SUMS.txt").read_text(encoding="utf-8")

  assert "/data/" not in checksum_text
  assert "  /" not in checksum_text
  assert "nested/evidence.txt" in checksum_text
  gps_drive_audit.verify_checksums(tmp_path)


def test_state_snapshot_labels_capture_identity(tmp_path):
  assistance_root = tmp_path / "assistance"
  destination = tmp_path / "state"
  assistance_root.mkdir()
  cache_payload = '{"version":1,"receiver_fingerprint":"test"}\n'
  (assistance_root / "navigation_cache.json").write_text(
    cache_payload,
    encoding="utf-8",
  )

  gps_drive_audit.copy_state_files(
    assistance_root,
    destination,
    "capture-boot",
  )

  scope = (destination / "STATE_SCOPE.txt").read_text(encoding="utf-8")
  assert "State files are current-device snapshots" in scope
  assert "capture_boot_id=capture-boot" in scope
  assert "navigation_cache.json: copied bytes=" in scope
  assert (destination / "navigation_cache.json").read_text(encoding="utf-8") == cache_payload


def test_decode_param_handles_bytes_without_encoding_argument():
  assert gps_drive_audit.decode_param(b"cd0e0fdc") == "cd0e0fdc"
  assert gps_drive_audit.decode_param(None) == "<missing>"


def test_yuma_commit_metadata_uses_supported_params_get(monkeypatch):
  captured = {}

  class ParamsStub:
    def get(self, key):
      assert key == "GitCommit"
      return b"cd0e0fdc4d4e18eed3ceeeb4bbed76d3a3ea9259"

  outcome = SimpleNamespace(
    receiver_cycle=2,
    completion_utc=None,
    trusted_now_utc=None,
  )

  def fake_save(path, saved_outcome, **kwargs):
    captured["path"] = path
    captured["outcome"] = saved_outcome
    captured.update(kwargs)

  monkeypatch.setattr(pigeond, "save_yuma_supplementation_outcome", fake_save)
  pigeond.persist_yuma_supplementation_outcome(outcome, ParamsStub())

  assert captured["outcome"] is outcome
  assert captured["commit"] == "cd0e0fdc4d4e18eed3ceeeb4bbed76d3a3ea9259"
  assert captured["receiver_cycle"] == 2


def test_yuma_commit_metadata_supports_legacy_params_get(monkeypatch):
  captured = {}

  class ParamsStub:
    def get(self, key, encoding):
      assert key == "GitCommit"
      assert encoding == "utf-8"
      return "legacy-commit"

  outcome = SimpleNamespace(
    receiver_cycle=3,
    completion_utc=None,
    trusted_now_utc=None,
  )

  def fake_save(path, saved_outcome, **kwargs):
    captured["path"] = path
    captured["outcome"] = saved_outcome
    captured.update(kwargs)

  monkeypatch.setattr(pigeond, "save_yuma_supplementation_outcome", fake_save)
  pigeond.persist_yuma_supplementation_outcome(outcome, ParamsStub())

  assert captured["outcome"] is outcome
  assert captured["commit"] == "legacy-commit"
  assert captured["receiver_cycle"] == 3


_SOURCE_EPOCH_NS = 0


def _source_state(
  selected: str,
  *,
  generation: int = 0,
  transition_mono_ns: int | None = None,
  reason: str = "",
  failover: int = 0,
  recovery: int = 0,
) -> SimpleNamespace:
  global _SOURCE_EPOCH_NS
  if transition_mono_ns is None:
    _SOURCE_EPOCH_NS += 1_000_000_000
    transition_mono_ns = _SOURCE_EPOCH_NS
  return SimpleNamespace(
    selected=selected,
    generation=generation,
    transitionMonoNs=transition_mono_ns,
    transitionReason=reason,
    failoverCount=failover,
    recoveryCount=recovery,
  )


def _gps_fix(
  *,
  measurement_mono_ns: int = 0,
  accuracy: float = 8.0,
  monotonic_time: float = 110.0,
) -> SimpleNamespace:
  return SimpleNamespace(
    flags=1,
    hasFix=True,
    unixTimestampMillis=1_774_898_400_000,
    satelliteCount=8,
    horizontalAccuracy=accuracy,
    latitude=32.8,
    longitude=-96.8,
    measurementMonoNs=measurement_mono_ns,
  )


def _sat(sv_id: int, gnss_id: int, cno: int, flags: int) -> SimpleNamespace:
  return SimpleNamespace(
    svId=sv_id,
    gnssId=gnss_id,
    cno=cno,
    flagsBitfield=flags,
    elevationDeg=45,
    azimuthDeg=90,
    pseudorangeResidual=0.0,
  )


def test_historical_route_degrades_without_new_telemetry():
  metrics = gps_drive_audit.RouteMetrics("legacy-route")
  metrics.note_time(100.0)
  metrics.process_gps(110.0, _gps_fix(measurement_mono_ns=0), source="ublox")
  metrics.finalize()

  assert metrics.has_gps_source_state is False
  assert metrics.has_measurement_mono_ns is False
  assert "gpsSourceState" in metrics.missing_telemetry
  assert "measurementMonoNs" in metrics.missing_telemetry
  assert metrics.classification == "INSUFFICIENT_TELEMETRY"
  assert metrics.reference_policy == "route_start"


def test_ublox_startup_winner_with_post_pr81_telemetry():
  metrics = gps_drive_audit.RouteMetrics("ublox-winner")
  metrics.note_time(100.0)
  metrics.process_gps_source_state(100.1, _source_state("ubloxPrimary", reason="startup"))
  metrics.process_gps(
    105.0,
    _gps_fix(measurement_mono_ns=104_500_000_000),
    source="ublox",
  )
  metrics.process_log_message(
    104.0,
    "GPS acquisition milestone=first_fix_ok, cycle=1, fix_ok=true",
  )
  metrics.finalize()

  assert metrics.has_gps_source_state is True
  assert metrics.has_measurement_mono_ns is True
  assert metrics.gps_source_first_authoritative == "ubloxPrimary"
  assert metrics.classification == "HEALTHY_FAST_ACQUISITION"
  assert metrics.first_fix_ok == pytest.approx(104.0)


def test_qcom_startup_winner():
  metrics = gps_drive_audit.RouteMetrics("qcom-winner")
  metrics.note_time(200.0)
  metrics.process_gps_source_state(200.1, _source_state("qcomFallback", reason="startup_qcom"))
  metrics.process_gps(205.0, _gps_fix(measurement_mono_ns=204_000_000_000), source="qcom")
  metrics.finalize()

  assert metrics.gps_source_first_authoritative == "qcomFallback"
  assert metrics.classification == "QCOM_STARTUP_WINNER"


def test_ublox_to_qcom_failover_transition():
  metrics = gps_drive_audit.RouteMetrics("failover-route")
  metrics.note_time(300.0)
  metrics.process_gps_source_state(300.1, _source_state("ubloxPrimary", generation=1))
  metrics.process_gps_source_state(
    310.0,
    _source_state("qcomFallback", generation=2, reason="ublox_failover", failover=1),
  )
  metrics.process_gps(305.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.classification == "QCOM_RUNTIME_FAILOVER"
  assert metrics.max_failover_count == 1
  assert len(metrics.gps_source_transitions) == 2


def test_qcom_to_ublox_recovery_transition():
  metrics = gps_drive_audit.RouteMetrics("recovery-route")
  metrics.note_time(400.0)
  metrics.process_gps_source_state(400.1, _source_state("ubloxPrimary", generation=1))
  metrics.process_gps_source_state(
    405.0,
    _source_state("qcomFallback", generation=2, reason="ublox_failover", failover=1),
  )
  metrics.process_gps_source_state(
    415.0,
    _source_state("ubloxPrimary", generation=3, reason="ublox_recovered", recovery=1),
  )
  metrics.process_gps(410.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.classification == "UBLOX_RECOVERED"
  assert metrics.max_recovery_count == 1


def test_no_healthy_gps_source_interval():
  metrics = gps_drive_audit.RouteMetrics("no-healthy-route")
  metrics.note_time(500.0)
  metrics.process_gps_source_state(500.1, _source_state("ubloxPrimary"))
  metrics.process_gps_source_state(501.0, _source_state("noHealthySource"))
  metrics.process_gps_source_state(502.0, _source_state("ubloxPrimary", generation=2))
  metrics.process_gps(505.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.runtime_no_healthy_interval_count == 1
  assert metrics.classification == "NO_HEALTHY_GPS_SOURCE"


def test_startup_no_healthy_then_ublox_healthy_fast():
  metrics = gps_drive_audit.RouteMetrics("startup-no-healthy-ublox")
  metrics.note_time(100.0)
  metrics.process_gps_source_state(
    100.1,
    _source_state("noHealthySource", reason="startup", transition_mono_ns=100_100_000_000),
  )
  metrics.process_gps_source_state(
    105.0,
    _source_state("ubloxPrimary", reason="startup_ublox", transition_mono_ns=105_000_000_000),
  )
  metrics.process_log_message(106.0, "GPS acquisition milestone=first_reliable_fix, cycle=1")
  metrics.process_gps(107.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.startup_no_healthy_interval_count == 1
  assert metrics.runtime_no_healthy_interval_count == 0
  assert metrics.classification == "HEALTHY_FAST_ACQUISITION"
  assert metrics.startup_no_healthy_intervals[0]["duration_s"] == pytest.approx(4.9)


def test_startup_no_healthy_then_qcom_winner():
  metrics = gps_drive_audit.RouteMetrics("startup-no-healthy-qcom")
  metrics.note_time(200.0)
  metrics.process_gps_source_state(
    200.1,
    _source_state("noHealthySource", transition_mono_ns=200_100_000_000),
  )
  metrics.process_gps_source_state(
    205.0,
    _source_state("qcomFallback", reason="startup_qcom", transition_mono_ns=205_000_000_000),
  )
  metrics.process_gps(206.0, _gps_fix(), source="qcom")
  metrics.finalize()

  assert metrics.startup_no_healthy_interval_count == 1
  assert metrics.classification == "QCOM_STARTUP_WINNER"


def test_startup_no_healthy_never_gets_source():
  metrics = gps_drive_audit.RouteMetrics("startup-no-healthy-forever")
  metrics.note_time(300.0)
  metrics.process_gps_source_state(
    300.1,
    _source_state("noHealthySource", transition_mono_ns=300_100_000_000),
  )
  metrics.process_rawx(301.0, SimpleNamespace(measurements=(SimpleNamespace(gnssId=0),), gpsWeek=0, leapSeconds=0))
  metrics.finalize()

  assert metrics.gps_source_first_authoritative is None
  assert metrics.startup_no_healthy_interval_count == 1
  assert metrics.classification == "NO_HEALTHY_GPS_SOURCE"


def test_qcom_failover_then_no_healthy_then_recovery():
  metrics = gps_drive_audit.RouteMetrics("runtime-loss-recovery")
  metrics.note_time(400.0)
  metrics.process_gps_source_state(
    400.1,
    _source_state("qcomFallback", reason="startup_qcom", transition_mono_ns=400_100_000_000),
  )
  metrics.process_gps_source_state(
    410.0,
    _source_state("noHealthySource", reason="both_unhealthy", transition_mono_ns=410_000_000_000),
  )
  metrics.process_gps_source_state(
    420.0,
    _source_state("ubloxPrimary", reason="ublox_recovered", recovery=1, transition_mono_ns=420_000_000_000),
  )
  metrics.process_gps(405.0, _gps_fix(), source="qcom")
  metrics.finalize()

  assert metrics.runtime_no_healthy_interval_count == 1
  assert metrics.classification == "NO_HEALTHY_GPS_SOURCE"


def test_rf_limited_when_signals_fail_to_code_lock():
  metrics = gps_drive_audit.RouteMetrics("rf-limited-route")
  metrics.note_time(600.0)
  acquired_flags = 2
  sat_report = SimpleNamespace(
    svs=(
      _sat(1, 0, 28, acquired_flags),
      _sat(2, 0, 26, acquired_flags),
    )
  )
  metrics.process_sat_report(601.0, sat_report)
  metrics.process_sat_report(601.5, sat_report)
  metrics.process_rawx(602.0, SimpleNamespace(measurements=(SimpleNamespace(gnssId=0),), gpsWeek=0, leapSeconds=0))
  metrics.finalize()

  assert metrics.classification == "RF_LIMITED"


def test_ephemeris_limited_requires_nav_evidence_not_long_ttff():
  metrics = gps_drive_audit.RouteMetrics("long-ttff-route")
  metrics.note_time(700.0)
  metrics.process_gps(900.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.classification != "NAV_DATA_OR_EPHEMERIS_LIMITED"

  metrics = gps_drive_audit.RouteMetrics("eph-limited-route")
  metrics.note_time(800.0)
  code_lock_flags = 4
  sat_report = SimpleNamespace(
    svs=(
      _sat(3, 0, 32, code_lock_flags),
      _sat(4, 0, 30, code_lock_flags),
    )
  )
  metrics.process_sat_report(801.0, sat_report)
  metrics.process_sat_report(801.5, sat_report)
  metrics.process_rawx(802.0, SimpleNamespace(measurements=(SimpleNamespace(gnssId=0),), gpsWeek=0, leapSeconds=0))
  # No fix: unresolved acquisition with sustained code-lock / zero-ephemeris evidence.
  metrics.finalize()

  assert metrics.max_sat_code_locked >= 2
  assert metrics.max_sat_ephemeris == 0
  assert metrics.classification == "NAV_DATA_OR_EPHEMERIS_LIMITED"


def test_fast_fix_with_zero_eph_snapshots_not_nav_limited():
  metrics = gps_drive_audit.RouteMetrics("fast-fix-zero-eph")
  metrics.note_time(100.0)
  metrics.process_gps_source_state(100.1, _source_state("ubloxPrimary"))
  code_lock_flags = 4
  sat_report = SimpleNamespace(
    svs=(
      _sat(3, 0, 32, code_lock_flags),
      _sat(4, 0, 30, code_lock_flags),
    )
  )
  metrics.process_sat_report(101.0, sat_report)
  metrics.process_sat_report(101.5, sat_report)
  metrics.process_log_message(105.0, "GPS acquisition milestone=first_reliable_fix, cycle=1")
  metrics.process_gps(106.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.max_sat_ephemeris == 0
  assert metrics.classification == "HEALTHY_FAST_ACQUISITION"
  assert metrics.classification != "NAV_DATA_OR_EPHEMERIS_LIMITED"


def test_fast_fix_with_early_no_code_lock_not_rf_limited():
  metrics = gps_drive_audit.RouteMetrics("fast-fix-early-rf")
  metrics.note_time(100.0)
  metrics.process_gps_source_state(100.1, _source_state("ubloxPrimary"))
  acquired_flags = 2
  sat_report = SimpleNamespace(
    svs=(
      _sat(1, 0, 28, acquired_flags),
      _sat(2, 0, 26, acquired_flags),
    )
  )
  metrics.process_sat_report(101.0, sat_report)
  metrics.process_sat_report(101.5, sat_report)
  metrics.process_log_message(105.0, "GPS acquisition milestone=first_reliable_fix, cycle=1")
  metrics.process_gps(106.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.classification == "HEALTHY_FAST_ACQUISITION"
  assert metrics.classification != "RF_LIMITED"


def test_machine_report_and_ttff_lines_are_deterministic():
  metrics = gps_drive_audit.RouteMetrics("report-route")
  metrics.note_time(100.0)
  metrics.process_gps_source_state(100.5, _source_state("ubloxPrimary"))
  metrics.process_gps(110.0, _gps_fix(measurement_mono_ns=109_000_000_000), source="ublox")
  metrics.finalize()

  report = metrics.to_machine_report()
  assert report["route"] == "report-route"
  assert report["classification"] == metrics.classification
  assert "milestones_s" in report
  assert "first_rawx" in report["milestones_s"]
  assert "first_ephemeris" in report["milestones_s"]
  assert "source_authority_intervals" in report["gps_source_state"]
  assert "no_healthy_intervals" in report["gps_source_state"]
  assert "dbd_restore_disposition" in report
  assert "yuma" in report
  ttff_lines = metrics.ttff_report_lines()
  assert ttff_lines[0] == "===== TTFF / ACQUISITION REPORT ====="
  assert any(line.startswith("classification=") for line in ttff_lines)
  assert any(line.startswith("first_rawx_s=") for line in ttff_lines)


def test_nonempty_rawx_code_lock_no_fix_not_receiver_output_failure():
  metrics = gps_drive_audit.RouteMetrics("output-alive-no-fix")
  metrics.note_time(100.0)
  metrics.process_gps_source_state(100.1, _source_state("ubloxPrimary"))
  metrics.process_rawx(
    101.0,
    SimpleNamespace(measurements=(SimpleNamespace(gnssId=0),), gpsWeek=2411, leapSeconds=18),
  )
  code_lock_flags = 4
  metrics.process_sat_report(
    102.0,
    SimpleNamespace(svs=(_sat(1, 0, 30, code_lock_flags), _sat(2, 0, 28, code_lock_flags))),
  )
  metrics.finalize()

  assert metrics.classification != "RECEIVER_OUTPUT_FAILURE"
  assert metrics.classification == "INSUFFICIENT_TELEMETRY"


def test_saved_cache_only_not_dbd_assisted():
  metrics = gps_drive_audit.RouteMetrics("cache-save-only")
  metrics.note_time(100.0)
  metrics.process_log_message(101.0, "saved gps navigation assistance cache, version=1")
  metrics.process_gps_source_state(102.0, _source_state("ubloxPrimary"))
  metrics.process_gps(110.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.dbd_assisted_evidence is False
  assert metrics.dbd_restore_disposition == "none"
  assert metrics.classification != "DBD_ASSISTED"


def test_restore_successful_classifies_dbd_assisted():
  metrics = gps_drive_audit.RouteMetrics("dbd-restore-success")
  metrics.note_time(100.0)
  metrics.process_log_message(
    101.0,
    "navigation assistance restore, database_restore_disposition=restored",
  )
  metrics.process_gps_source_state(102.0, _source_state("ubloxPrimary"))
  metrics.process_gps(110.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.dbd_assisted_evidence is True
  assert metrics.dbd_restore_disposition == "success"
  assert metrics.classification == "DBD_ASSISTED"


def test_restore_skipped_not_dbd_assisted():
  metrics = gps_drive_audit.RouteMetrics("dbd-restore-skipped")
  metrics.note_time(100.0)
  metrics.process_log_message(
    101.0,
    "navigation assistance restore, database_restore_disposition=skipped_no_trusted_time",
  )
  metrics.process_gps_source_state(102.0, _source_state("ubloxPrimary"))
  metrics.process_gps(110.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.dbd_assisted_evidence is False
  assert metrics.dbd_restore_disposition == "skipped"
  assert metrics.classification != "DBD_ASSISTED"


def test_trusted_time_true_alone_unknown_start_type():
  metrics = gps_drive_audit.RouteMetrics("trusted-time-true")
  metrics.note_time(100.0)
  metrics.process_log_message(
    101.0,
    "GPS startup timeline, trusted_time_available=true",
  )
  metrics.process_gps_source_state(102.0, _source_state("ubloxPrimary"))
  metrics.process_gps(110.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.trusted_time_available is True
  assert metrics.start_type == "UNKNOWN_START_TYPE"


def test_trusted_time_false_alone_unknown_start_type():
  metrics = gps_drive_audit.RouteMetrics("trusted-time-false")
  metrics.note_time(100.0)
  metrics.process_log_message(
    101.0,
    "GPS startup timeline, trusted_time_available=false",
  )
  metrics.process_gps_source_state(102.0, _source_state("ubloxPrimary"))
  metrics.process_gps(110.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.trusted_time_available is False
  assert metrics.start_type == "UNKNOWN_START_TYPE"


def test_reset_watchdog_alone_unknown_start_type():
  metrics = gps_drive_audit.RouteMetrics("reset-watchdog")
  metrics.note_time(100.0)
  metrics.process_log_message(101.0, "GPS receiver cycle started, reason=no_data_watchdog")
  metrics.process_gps_source_state(102.0, _source_state("ubloxPrimary"))
  metrics.process_gps(110.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.warm_start_evidence is False
  assert metrics.start_type == "UNKNOWN_START_TYPE"


def test_one_code_lock_ephemeris_zero_not_nav_limited():
  metrics = gps_drive_audit.RouteMetrics("single-code-lock")
  metrics.note_time(700.0)
  code_lock_flags = 4
  metrics.process_sat_report(
    701.0,
    SimpleNamespace(svs=(_sat(1, 0, 32, code_lock_flags),)),
  )
  metrics.process_rawx(702.0, SimpleNamespace(measurements=(SimpleNamespace(gnssId=0),), gpsWeek=0, leapSeconds=0))
  metrics.process_gps_source_state(703.0, _source_state("ubloxPrimary"))
  metrics.finalize()

  assert metrics.max_sat_code_locked == 1
  assert metrics.max_sat_ephemeris == 0
  assert metrics.classification != "NAV_DATA_OR_EPHEMERIS_LIMITED"


def test_same_source_generation_newer_transition_mono_ns_is_new_transition():
  metrics = gps_drive_audit.RouteMetrics("transition-epoch")
  metrics.note_time(300.0)
  metrics.process_gps_source_state(
    300.1,
    _source_state("ubloxPrimary", generation=1, transition_mono_ns=1_000_000_000),
  )
  metrics.process_gps_source_state(
    310.0,
    _source_state("ubloxPrimary", generation=1, transition_mono_ns=2_000_000_000, reason="epoch_refresh"),
  )
  metrics.process_gps(305.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert len(metrics.gps_source_transitions) == 2


def test_same_exact_epoch_refresh_not_duplicate_transition():
  metrics = gps_drive_audit.RouteMetrics("transition-duplicate")
  metrics.note_time(300.0)
  state = _source_state("ubloxPrimary", generation=1, transition_mono_ns=1_000_000_000)
  metrics.process_gps_source_state(300.1, state)
  metrics.process_gps_source_state(300.2, state)
  metrics.process_gps(305.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert len(metrics.gps_source_transitions) == 1


def test_historical_without_transition_mono_ns_still_works():
  metrics = gps_drive_audit.RouteMetrics("historical-transition")
  metrics.note_time(300.0)
  metrics.process_gps_source_state(
    300.1,
    SimpleNamespace(
      selected="ubloxPrimary",
      generation=0,
      transitionMonoNs=None,
      transitionReason="startup",
      failoverCount=0,
      recoveryCount=0,
    ),
  )
  metrics.process_gps_source_state(
    310.0,
    SimpleNamespace(
      selected="qcomFallback",
      generation=1,
      transitionMonoNs=None,
      transitionReason="failover",
      failoverCount=1,
      recoveryCount=0,
    ),
  )
  metrics.process_gps(305.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert len(metrics.gps_source_transitions) == 2


def test_no_healthy_interval_durations_reported():
  metrics = gps_drive_audit.RouteMetrics("no-healthy-intervals")
  metrics.note_time(500.0)
  metrics.process_gps_source_state(
    500.1,
    _source_state("ubloxPrimary", transition_mono_ns=500_100_000_000),
  )
  metrics.process_gps_source_state(
    501.0,
    _source_state("noHealthySource", transition_mono_ns=501_000_000_000),
  )
  metrics.process_gps_source_state(
    503.0,
    _source_state("ubloxPrimary", generation=2, transition_mono_ns=503_000_000_000),
  )
  metrics.process_gps(505.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert len(metrics.no_healthy_intervals) == 1
  interval = metrics.no_healthy_intervals[0]
  assert interval["phase"] == "runtime"
  assert interval["start_t"] == pytest.approx(501.0)
  assert interval["end_t"] == pytest.approx(503.0)
  assert interval["duration_s"] == pytest.approx(2.0)
  report = metrics.to_machine_report()
  assert len(report["gps_source_state"]["no_healthy_intervals"]) == 1
  assert report["gps_source_state"]["no_healthy_intervals"][0]["duration_s"] == pytest.approx(2.0)


def test_report_includes_first_rawx_and_first_ephemeris():
  metrics = gps_drive_audit.RouteMetrics("milestones-report")
  metrics.note_time(100.0)
  metrics.process_rawx(101.0, SimpleNamespace(measurements=(), gpsWeek=0, leapSeconds=0))
  metrics.process_rawx(
    102.0,
    SimpleNamespace(measurements=(SimpleNamespace(gnssId=0),), gpsWeek=2411, leapSeconds=18),
  )
  ephemeris_flags = 4 | (1 << 11)
  metrics.process_sat_report(
    103.0,
    SimpleNamespace(svs=(_sat(1, 0, 32, ephemeris_flags),)),
  )
  metrics.process_gps_source_state(104.0, _source_state("ubloxPrimary"))
  metrics.finalize()

  report = metrics.to_machine_report()
  assert report["milestones_s"]["first_rawx"] == pytest.approx(1.0)
  assert report["milestones_s"]["first_ephemeris"] == pytest.approx(3.0)
  ttff_lines = metrics.ttff_report_lines()
  assert any(line == "first_rawx_s=1.0" for line in ttff_lines)
  assert any(line == "first_ephemeris_s=3.0" for line in ttff_lines)


def test_old_route_missing_fields_no_crash():
  metrics = gps_drive_audit.RouteMetrics("legacy-nulls")
  metrics.note_time(100.0)
  metrics.process_gps(110.0, _gps_fix(measurement_mono_ns=0), source="ublox")
  metrics.finalize()

  report = metrics.to_machine_report()
  assert report["milestones_s"]["first_rawx"] is None
  assert report["milestones_s"]["first_ephemeris"] is None
  assert report["dbd_restore_disposition"] == "none"
  assert report["yuma"]["disposition"] is None
  assert report["presence"]["trusted_time_available"] is None


def test_historical_yuma_transmit_status_complete():
  metrics = gps_drive_audit.RouteMetrics("historical-yuma")
  metrics.note_time(100.0)
  historical_yuma_complete = (
    "GPS public YUMA supplementation, action=send_all, transmit_status=complete, " + "transmission_attempt=1, accepted_prns=1|2|3, failed_prns=none"
  )
  metrics.process_log_message(110.0, historical_yuma_complete)
  metrics.process_gps_source_state(120.0, _source_state("ubloxPrimary"))
  metrics.process_gps(125.0, _gps_fix(), source="ublox")
  metrics.finalize()

  assert metrics.yuma_attempted == pytest.approx(110.0)
  assert metrics.yuma_completed == pytest.approx(110.0)
  assert metrics.yuma_disposition == "complete"
  assert metrics.to_machine_report()["yuma"]["disposition"] == "complete"


def test_regressing_epoch_rejected():
  metrics = gps_drive_audit.RouteMetrics("regressing-epoch")
  metrics.note_time(100.0)
  metrics.process_gps_source_state(
    100.1,
    _source_state("qcomFallback", generation=1, transition_mono_ns=100_000_000_000),
  )
  metrics.process_gps_source_state(
    101.0,
    _source_state("ubloxPrimary", generation=0, transition_mono_ns=50_000_000_000),
  )
  metrics.finalize()

  assert len(metrics.gps_source_transitions) == 1
  assert metrics.gps_source_transitions[0]["selected"] == "qcomFallback"
  assert any("reject_regressing" in warning for warning in metrics.source_epoch_warnings)


def test_equal_epoch_inconsistent_selected_rejected():
  metrics = gps_drive_audit.RouteMetrics("inconsistent-epoch")
  metrics.note_time(100.0)
  metrics.process_gps_source_state(
    100.1,
    _source_state("qcomFallback", generation=1, transition_mono_ns=100_000_000_000),
  )
  metrics.process_gps_source_state(
    100.2,
    _source_state("ubloxPrimary", generation=2, transition_mono_ns=100_000_000_000),
  )
  metrics.finalize()

  assert len(metrics.gps_source_transitions) == 1
  assert any("reject_inconsistent" in warning for warning in metrics.source_epoch_warnings)


def test_gpsard_restart_same_gen_newer_epoch_accepted():
  metrics = gps_drive_audit.RouteMetrics("gpsard-restart-epoch")
  metrics.note_time(100.0)
  metrics.process_gps_source_state(
    100.1,
    _source_state("qcomFallback", generation=1, transition_mono_ns=100_000_000_000),
  )
  metrics.process_gps_source_state(
    110.0,
    _source_state("ubloxPrimary", generation=0, transition_mono_ns=109_500_000_000, reason="restart"),
  )
  metrics.finalize()

  assert len(metrics.gps_source_transitions) == 2
  assert metrics.gps_source_transitions[1]["transitionMonoNs"] == 109_500_000_000
  assert metrics.gps_source_transitions[1]["t"] == pytest.approx(109.5)


def test_transition_time_uses_transition_mono_ns_not_event_time():
  metrics = gps_drive_audit.RouteMetrics("epoch-time")
  metrics.note_time(100.0)
  metrics.process_gps_source_state(
    100.8,
    _source_state("ubloxPrimary", generation=0, transition_mono_ns=100_000_000_000, reason="startup"),
  )
  metrics.finalize()

  assert metrics.gps_source_transitions[0]["t"] == pytest.approx(100.0)
  assert metrics.first_authoritative == pytest.approx(100.0)
  assert metrics.first_authoritative != pytest.approx(100.8)
