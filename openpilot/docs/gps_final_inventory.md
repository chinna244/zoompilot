# Final GPS software inventory (PR82 closeout)

Post-PR81 architecture on `develop`. PR82 adds tooling/future-proofing only;
it does not redesign source arbitration, measurement timing, or assistance policy.

## Dataflow

```
pigeond → ubloxRaw → ubloxd → gpsLocationExternal ─┐
                                                    ├→ gpsard → gpsSourceState
qcomgpsd ─────────────────────→ gpsLocation ───────┘
                                      │                    │
                                      └──── locationd_llk ←┘  (authoritative fuse)
                                      └──── timed (ubloxPrimary only → settime)
```

## Components

| Component | Responsibility | Primary inputs | Outputs | Important health/safety policy | Establishing PR |
|-----------|----------------|----------------|---------|--------------------------------|-----------------|
| **pigeond** | u-blox power/config, MGA/YUMA/DBD/position assist, GNSS START, AssistNow Autonomous | UART, params, almanac/DBD caches | `ubloxRaw` (via pigeon), GPIO `GNSS_PWR_EN` | Owns rail when HW present; Online AssistNow retired; trusted-time gates assistance | Pre-existing; PR69–74/78 |
| **ubloxd** | Parse UBX → location/GNSS | `ubloxRaw` | `gpsLocationExternal`, `ubloxGnss` | Stamp `measurementMonoNs` at framing; NAV-PVT `hasFix`; ephemeris week era via RAWX | PR76/77/81; PR82 week era |
| **qcomgpsd** | Modem DIAG → QCOM GPS | Modem DIAG | `gpsLocation`, `qcomGnss` | Concurrent with u-blox; skip `GNSS_PWR_EN` if ublox; stamp `measurementMonoNs` at DIAG payload | PR79/80/81 |
| **gpsard** | Single source arbiter | `gpsLocationExternal`, `gpsLocation` | `gpsSourceState` | First sustained health-qualified fix; then u-blox-preferred hysteresis; usability aligned with locationd | **PR80**; usability **PR81** |
| **locationd_llk** | LiveLocationKalman GPS fusion | `gpsSourceState`, both GPS sockets, IMU, … | `liveLocationKalman` | Follow arbiter; `measurementMonoNs` epoch; event+KF rewind gates; NED→ECEF H/V cov | PR75/80/81 |
| **timed** | System clock + `clocks` | `gpsSourceState`, GPS sockets | `clocks`; system time | Only **selected ubloxPrimary** + valid NAV-PVT time flags | **PR80** |
| **gps_drive_audit** | Offline route acquisition audit | route logs, optional `/data/gps_assistance` snapshots | audit bundle + TTFF/JSON report | Consumes PR71/80/81 telemetry; degrades on historical routes | Tooling; **PR82** upgrade |
| **common/gps_time** | Week/TOW/leap/NAV-PVT helpers | week+TOW, flags | Unix ms, resolved weeks | Full vs modulo week; maintained leap table (no rollover cutoff); historical unknown leaps fail closed | PR79; **PR82** future-proof |
| **common/gps_measurement** | Timing usability + NED→ECEF cov helpers | GPS fields | usability bool, ECEF R | Aligns gpsard with locationd | **PR81** |
| **common/gps_source_arbiter** | Pure arbiter state machine | `GpsSample` | selected source / health | Startup race + hysteresis constants | **PR80** |
| **common/time_helpers** | Host time validity / sync markers | wall clock | `system_time_valid`, host observation | Floor via `MIN_DATE`/`min_date()`; ceiling = representable GPS UTC max (**PR82**) | Pre-existing; **PR82** ceiling |

## Tests (primary)

- Arbiter: `common/tests/test_gps_source_arbiter.py`
- Timing/cov: `common/tests/test_gps_measurement.py`
- GPS time/week/leap: `common/tests/test_gps_time.py`
- locationd Catch2: `sunnypilot/selfdrive/locationd/tests/test_locationd_gps_input_safety.cc`
- qcomgpsd: `system/qcomgpsd/tests/`
- ubloxd: `system/ubloxd/tests/`
- timed: `system/tests/test_timed_gps_source.py`
- audit: `system/ubloxd/tests/test_gps_drive_audit.py`
