# Post-PR82 C4 field validation plan

**PLAN ONLY — do not modify/install on C4 during PR82.**

Goal: prove the entire final GPS stack on hardware after PR82 merges to `develop`.

## Setup checklist

- [ ] Device on merged `develop` containing PR81 + PR82
- [ ] Both GPS daemons enabled per `process_config` (u-blox HW present)
- [ ] Fresh route recording with rlog available

## Prove each item (and which log fields show it)

| # | Proof goal | How to verify | Route evidence |
|---|------------|---------------|----------------|
| 1 | Both GPS daemons running | `pgrep`/manager process list; no crash loops | `logMessage` watchdog/restart absence; process uptime |
| 2 | QCOM DIAG works on C4 | qcomgpsd alive; DIAG logs accepted | `qcomGnss` service count > 0 |
| 3 | QCOM publishes `gpsLocation` | | `gpsLocation` messages present |
| 4 | u-blox publishes expected streams | | `ubloxRaw`, `gpsLocationExternal`, `ubloxGnss` |
| 5 | gpsard publishes `gpsSourceState` | ~1 Hz | `gpsSourceState.selected/generation/...` |
| 6 | First sustained reliable fix wins | Cold/warm start | First authoritative `gpsSourceState.selected` after startup confirmation |
| 7 | If QCOM fixes first, locationd uses QCOM | Force/observe QCOM-first | `selected=qcomFallback` then `liveLocationKalman` accepts QCOM path (no exclusive ublox fuse) |
| 8 | timed does **not** use QCOM for clock | | timed policy + system clock changes only under `ubloxPrimary` |
| 9 | u-blox recovers after hysteresis | After QCOM won | `selected` transitions qcom→ublox; `recoveryCount` increments; `transitionReason` |
| 10 | Source transition epochs work | | `transitionMonoNs` increases; locationd rejects pre-transition GPS |
| 11 | `measurementMonoNs` on both live producers | | `gpsLocation.measurementMonoNs > 0` and `gpsLocationExternal.measurementMonoNs > 0` |
| 12 | No excessive stale/future GPS rejection | | locationd GPS input stats: `stale`/`future` not dominating `accepted` |
| 13 | PR81 covariance gets finite H/V | | accepted GPS with finite `horizontalAccuracy`/`verticalAccuracy` |
| 14 | No `GNSS_PWR_EN` conflict | | pigeond owns rail; qcomgpsd skips enable when ublox HW present |
| 15 | No process crash loops | | manager restart counts stable for gpsard/ubloxd/qcomgpsd/pigeond/locationd |

## Suggested route matrix

1. Overnight offline cold start (u-blox likely slow; observe QCOM-first if any)
2. Short-stop warm / DBD-available start
3. Runtime u-blox loss simulation if safe (antenna disconnect) → QCOM failover → recovery

## Offline analysis

Run `gps_drive_audit.py --route <id>` on the collected route and confirm:

- `acquisition_report.json` populated
- `gpsSourceState` presence true
- `measurementMonoNs` presence true
- classification/start_type not crashing on missing fields
