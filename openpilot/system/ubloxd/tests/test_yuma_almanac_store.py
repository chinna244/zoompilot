import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from openpilot.system.ubloxd.yuma_almanac import (
  MINIMUM_YUMA_GPS_SATELLITES,
  convert_yuma_almanac,
)
from openpilot.system.ubloxd.yuma_almanac_store import (
  YumaAlmanacStoreError,
  load_yuma_almanac,
  save_yuma_almanac,
)


DOWNLOADED_AT = datetime(2026, 7, 21, tzinfo=UTC)


def yuma_block(satellite_id: int) -> str:
  return f"""******** Week 380 almanac for PRN-{satellite_id:02d} ********
ID:                         {satellite_id:02d}
Health:                     000
Eccentricity:               0.1829147339E-002
Time of Applicability(s):  319488.0000
Orbital Inclination(rad):   0.9571045426
Rate of Right Ascen(r/s):  -0.8114623721E-008
SQRT(A)  (m 1/2):           5153.549805
Right Ascen at Week(rad):   0.6282692456E+000
Argument of Perigee(rad):   0.193269221
Mean Anom(rad):            -0.7104690442E+000
Af0(s):                     0.2155303955E-003
Af1(s/s):                  -0.1091393642E-010
week:                        380
"""


def almanac():
  return convert_yuma_almanac("\n".join(
    yuma_block(satellite_id)
    for satellite_id in range(
      1,
      MINIMUM_YUMA_GPS_SATELLITES + 1,
    )
  ))


def read_payload(path: Path) -> dict:
  return json.loads(path.read_text(encoding="utf-8"))


def write_payload(path: Path, payload: dict) -> None:
  path.write_text(
    json.dumps(payload, separators=(",", ":")) + "\n",
    encoding="utf-8",
  )


def test_save_and_load_yuma_almanac_round_trip(tmp_path: Path):
  path = tmp_path / "public_yuma_almanac.json"
  expected = almanac()

  save_yuma_almanac(
    path,
    expected,
    downloaded_at_utc=DOWNLOADED_AT,
  )
  stored = load_yuma_almanac(path)

  assert stored.downloaded_at_utc == DOWNLOADED_AT
  assert stored.almanac == expected
  assert path.stat().st_mode & 0o777 == 0o600



def test_load_yuma_almanac_rejects_hash_mismatch(tmp_path: Path):
  path = tmp_path / "public_yuma_almanac.json"
  save_yuma_almanac(
    path,
    almanac(),
    downloaded_at_utc=DOWNLOADED_AT,
  )
  payload = read_payload(path)
  data = bytearray(base64.b64decode(payload["ubx_data_base64"]))
  data[-1] ^= 0xFF
  payload["ubx_data_base64"] = base64.b64encode(data).decode("ascii")
  write_payload(path, payload)

  with pytest.raises(
    YumaAlmanacStoreError,
    match="hash mismatch",
  ):
    load_yuma_almanac(path)


def test_load_yuma_almanac_rejects_valid_hash_with_bad_frame(
  tmp_path: Path,
):
  path = tmp_path / "public_yuma_almanac.json"
  save_yuma_almanac(
    path,
    almanac(),
    downloaded_at_utc=DOWNLOADED_AT,
  )
  payload = read_payload(path)
  data = bytearray(base64.b64decode(payload["ubx_data_base64"]))
  data[-1] ^= 0xFF
  payload["ubx_data_base64"] = base64.b64encode(data).decode("ascii")
  payload["ubx_sha256"] = hashlib.sha256(data).hexdigest()
  write_payload(path, payload)

  with pytest.raises(
    YumaAlmanacStoreError,
    match="Invalid UBX checksum",
  ):
    load_yuma_almanac(path)


def test_load_yuma_almanac_rejects_metadata_mismatch(
  tmp_path: Path,
):
  path = tmp_path / "public_yuma_almanac.json"
  save_yuma_almanac(
    path,
    almanac(),
    downloaded_at_utc=DOWNLOADED_AT,
  )
  payload = read_payload(path)
  payload["satellite_ids"][0] = 32
  payload["satellite_ids"].sort()
  write_payload(path, payload)

  with pytest.raises(
    YumaAlmanacStoreError,
    match="satellite metadata mismatch",
  ):
    load_yuma_almanac(path)



def test_save_failure_preserves_existing_file(
  tmp_path: Path,
  monkeypatch,
):
  path = tmp_path / "public_yuma_almanac.json"
  path.write_bytes(b"existing-cache\n")

  def fail_replace(source, destination):
    raise OSError("injected replace failure")

  monkeypatch.setattr(
    "openpilot.system.ubloxd.yuma_almanac_store.os.replace",
    fail_replace,
  )

  with pytest.raises(OSError, match="injected replace failure"):
    save_yuma_almanac(
      path,
      almanac(),
      downloaded_at_utc=DOWNLOADED_AT,
    )

  assert path.read_bytes() == b"existing-cache\n"
  assert list(tmp_path.iterdir()) == [path]
