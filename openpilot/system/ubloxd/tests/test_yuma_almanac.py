from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from openpilot.system.ubloxd.gps_assistance import validate_ubx_frame
from openpilot.system.ubloxd.yuma_almanac import (
  GPS_EPOCH_UTC,
  MINIMUM_YUMA_GPS_SATELLITES,
  YUMA_ALMANAC_MAX_REFERENCE_AGE_SECONDS,
  YUMA_ALMANAC_MAX_REFERENCE_FUTURE_SECONDS,
  YumaAlmanacError,
  convert_yuma_almanac,
  resolve_yuma_reference_time,
  split_yuma_ubx_frames,
  validate_yuma_reference_time,
)


def yuma_block(
  satellite_id: int,
  *,
  health: int = 0,
  week: int = 380,
  time_of_applicability: float = 319488.0,
) -> str:
  return f"""******** Week {week} almanac for PRN-{satellite_id:02d} ********
ID:                         {satellite_id:02d}
Health:                     {health:03d}
Eccentricity:               0.1829147339E-002
Time of Applicability(s):  {time_of_applicability:.4f}
Orbital Inclination(rad):   0.9571045426
Rate of Right Ascen(r/s):  -0.8114623721E-008
SQRT(A)  (m 1/2):           5153.549805
Right Ascen at Week(rad):   0.6282692456E+000
Argument of Perigee(rad):   0.193269221
Mean Anom(rad):            -0.7104690442E+000
Af0(s):                     0.2155303955E-003
Af1(s/s):                  -0.1091393642E-010
week:                        {week}
"""


def yuma_text(
  *,
  count: int = MINIMUM_YUMA_GPS_SATELLITES,
  unhealthy: tuple[int, ...] = (),
) -> str:
  return "\n".join(
    yuma_block(
      satellite_id,
      health=1 if satellite_id in unhealthy else 0,
    )
    for satellite_id in range(1, count + 1)
  )


def test_convert_yuma_almanac_generates_valid_sorted_gps_frames():
  almanac = convert_yuma_almanac(yuma_text(count=25, unhealthy=(13,)))

  assert almanac.gps_week_mod_1024 == 380
  assert almanac.time_of_applicability_seconds == 319488
  assert almanac.satellite_ids == tuple(satellite_id for satellite_id in range(1, 26) if satellite_id != 13)
  assert len(almanac.frames) == MINIMUM_YUMA_GPS_SATELLITES
  assert len(almanac.ubx_data) == MINIMUM_YUMA_GPS_SATELLITES * 44

  for satellite_id, frame in zip(
    almanac.satellite_ids,
    almanac.frames,
    strict=True,
  ):
    assert validate_ubx_frame(frame)
    assert frame[2:4] == b"\x13\x00"
    assert int.from_bytes(frame[4:6], "little") == 36
    assert frame[6:10] == bytes((0x02, 0, satellite_id, 0))


def test_split_yuma_ubx_frames_round_trip():
  almanac = convert_yuma_almanac(yuma_text())

  assert split_yuma_ubx_frames(almanac.ubx_data) == almanac.frames


def test_convert_yuma_almanac_rejects_duplicate_satellite():
  text = yuma_text() + "\n" + yuma_block(1)

  with pytest.raises(YumaAlmanacError, match="Duplicate GPS satellite ID"):
    convert_yuma_almanac(text)


def test_convert_yuma_almanac_rejects_mixed_week():
  text = "\n".join(
    (
      *(yuma_block(satellite_id) for satellite_id in range(1, 24)),
      yuma_block(24, week=381),
    )
  )

  with pytest.raises(YumaAlmanacError, match="Mixed GPS weeks"):
    convert_yuma_almanac(text)


def test_convert_yuma_almanac_rejects_mixed_time_of_applicability():
  text = "\n".join(
    (
      *(yuma_block(satellite_id) for satellite_id in range(1, 24)),
      yuma_block(24, time_of_applicability=323584.0),
    )
  )

  with pytest.raises(YumaAlmanacError, match="Mixed times of applicability"):
    convert_yuma_almanac(text)


def test_convert_yuma_almanac_rejects_too_few_healthy_satellites():
  with pytest.raises(
    YumaAlmanacError,
    match="Unexpected number of healthy GPS satellites",
  ):
    convert_yuma_almanac(yuma_text(unhealthy=(1,)))


def test_convert_yuma_almanac_rejects_missing_required_field():
  text = yuma_text().replace(
    "Af1(s/s):                  -0.1091393642E-010\n",
    "",
    1,
  )

  with pytest.raises(YumaAlmanacError, match="missing fields: Af1"):
    convert_yuma_almanac(text)


def test_split_yuma_ubx_frames_rejects_corrupt_checksum():
  data = bytearray(convert_yuma_almanac(yuma_text()).ubx_data)
  data[-1] ^= 0xFF

  with pytest.raises(YumaAlmanacError, match="Invalid UBX checksum"):
    split_yuma_ubx_frames(bytes(data))


def test_split_yuma_ubx_frames_rejects_duplicate_satellite():
  almanac = convert_yuma_almanac(yuma_text())
  duplicated = almanac.frames[0] + almanac.frames[0]

  with pytest.raises(YumaAlmanacError, match="Duplicate YUMA satellite ID"):
    split_yuma_ubx_frames(duplicated)


def test_resolve_yuma_reference_time_uses_nearest_rollover():
  almanac = convert_yuma_almanac(yuma_text())
  trusted_now = datetime(2026, 7, 21, tzinfo=UTC)

  assert resolve_yuma_reference_time(
    almanac,
    trusted_now,
  ) == (
    GPS_EPOCH_UTC
    + timedelta(
      weeks=2428,
      seconds=almanac.time_of_applicability_seconds,
    )
  )


def test_resolve_yuma_reference_time_handles_rollover_boundary():
  source = convert_yuma_almanac(yuma_text())
  almanac = replace(
    source,
    gps_week_mod_1024=1023,
    time_of_applicability_seconds=0,
  )
  trusted_now = GPS_EPOCH_UTC + timedelta(
    weeks=2048,
    days=1,
  )

  assert resolve_yuma_reference_time(
    almanac,
    trusted_now,
  ) == GPS_EPOCH_UTC + timedelta(weeks=2047)


def test_resolve_yuma_reference_time_ambiguous_midpoint_fail_closed():
  source = convert_yuma_almanac(yuma_text())
  almanac = replace(
    source,
    gps_week_mod_1024=0,
    time_of_applicability_seconds=0,
  )
  trusted_now = GPS_EPOCH_UTC + timedelta(weeks=512)
  with pytest.raises(YumaAlmanacError, match="ambiguous"):
    resolve_yuma_reference_time(almanac, trusted_now)


@pytest.mark.parametrize(
  ("age_seconds", "accepted"),
  (
    (YUMA_ALMANAC_MAX_REFERENCE_AGE_SECONDS, True),
    (YUMA_ALMANAC_MAX_REFERENCE_AGE_SECONDS + 1, False),
  ),
)
def test_validate_yuma_reference_time_age_boundary(
  age_seconds: int,
  accepted: bool,
):
  almanac = convert_yuma_almanac(yuma_text())
  reference_time = resolve_yuma_reference_time(
    almanac,
    datetime(2026, 7, 21, tzinfo=UTC),
  )
  trusted_now = reference_time + timedelta(
    seconds=age_seconds,
  )

  if accepted:
    assert (
      validate_yuma_reference_time(
        almanac,
        trusted_now,
      )
      == reference_time
    )
  else:
    with pytest.raises(YumaAlmanacError, match="too old"):
      validate_yuma_reference_time(
        almanac,
        trusted_now,
      )


@pytest.mark.parametrize(
  ("future_seconds", "accepted"),
  (
    (YUMA_ALMANAC_MAX_REFERENCE_FUTURE_SECONDS, True),
    (YUMA_ALMANAC_MAX_REFERENCE_FUTURE_SECONDS + 1, False),
  ),
)
def test_validate_yuma_reference_time_future_boundary(
  future_seconds: int,
  accepted: bool,
):
  almanac = convert_yuma_almanac(yuma_text())
  reference_time = resolve_yuma_reference_time(
    almanac,
    datetime(2026, 7, 21, tzinfo=UTC),
  )
  trusted_now = reference_time - timedelta(
    seconds=future_seconds,
  )

  if accepted:
    assert (
      validate_yuma_reference_time(
        almanac,
        trusted_now,
      )
      == reference_time
    )
  else:
    with pytest.raises(
      YumaAlmanacError,
      match="too far in the future",
    ):
      validate_yuma_reference_time(
        almanac,
        trusted_now,
      )
