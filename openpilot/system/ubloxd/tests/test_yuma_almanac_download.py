from datetime import UTC, datetime
from pathlib import Path

import pytest
import requests

from openpilot.system.ubloxd.yuma_almanac import (
  MINIMUM_YUMA_GPS_SATELLITES,
  convert_yuma_almanac,
)
from openpilot.system.ubloxd.yuma_almanac_download import (
  NAVCEN_CURRENT_YUMA_URL,
  YUMA_ALMANAC_MAX_DOWNLOAD_BYTES,
  YUMA_ALMANAC_USER_AGENT,
  YumaAlmanacDownloadError,
  YumaAlmanacRefreshStatus,
  download_navcen_yuma_text,
  refresh_public_yuma_almanac,
)
from openpilot.system.ubloxd.yuma_almanac_store import (
  load_yuma_almanac,
  save_yuma_almanac,
)


NOW = datetime(2026, 7, 21, 18, tzinfo=UTC)


def yuma_block(
  satellite_id: int,
  *,
  week: int = 380,
  time_of_applicability: int = 319488,
  eccentricity: str = "0.1829147339E-002",
) -> str:
  return f"""******** Week {week} almanac for PRN-{satellite_id:02d} ********
ID:                         {satellite_id:02d}
Health:                     000
Eccentricity:               {eccentricity}
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
  week: int = 380,
  time_of_applicability: int = 319488,
  eccentricity: str = "0.1829147339E-002",
) -> str:
  return "\n".join(
    yuma_block(
      satellite_id,
      week=week,
      time_of_applicability=time_of_applicability,
      eccentricity=eccentricity,
    )
    for satellite_id in range(
      1,
      MINIMUM_YUMA_GPS_SATELLITES + 1,
    )
  )


class FakeResponse:
  def __init__(
    self,
    body: bytes,
    *,
    status_code: int = 200,
    headers: dict[str, str] | None = None,
    chunk_size: int | None = None,
  ):
    self.body = body
    self.status_code = status_code
    self.headers = headers or {}
    self.chunk_size = chunk_size or len(body) or 1
    self.closed = False

  def raise_for_status(self):
    if self.status_code >= 400:
      raise requests.HTTPError(
        f"HTTP {self.status_code}",
        response=self,
      )

  def iter_content(self, chunk_size: int):
    del chunk_size
    for offset in range(0, len(self.body), self.chunk_size):
      yield self.body[offset:offset + self.chunk_size]

  def close(self):
    self.closed = True


def response_for(text: str) -> FakeResponse:
  return FakeResponse(text.encode("utf-8"))


def test_download_navcen_yuma_text_uses_bounded_request():
  response = response_for(yuma_text())
  calls = []

  def request_get(url, **kwargs):
    calls.append((url, kwargs))
    return response

  result = download_navcen_yuma_text(
    request_get=request_get,
  )

  assert result == yuma_text()
  assert calls == [(
    NAVCEN_CURRENT_YUMA_URL,
    {
      "headers": {
        "Accept": "text/plain",
        "User-Agent": YUMA_ALMANAC_USER_AGENT,
      },
      "stream": True,
      "timeout": 10.0,
    },
  )]
  assert response.closed


def test_download_navcen_yuma_text_rejects_http_error():
  response = FakeResponse(b"error", status_code=503)

  with pytest.raises(
    YumaAlmanacDownloadError,
    match="download failed",
  ):
    download_navcen_yuma_text(
      request_get=lambda *args, **kwargs: response,
    )

  assert response.closed


def test_download_navcen_yuma_text_rejects_content_length():
  response = FakeResponse(
    b"",
    headers={
      "Content-Length": str(
        YUMA_ALMANAC_MAX_DOWNLOAD_BYTES + 1
      ),
    },
  )

  with pytest.raises(
    YumaAlmanacDownloadError,
    match="too large",
  ):
    download_navcen_yuma_text(
      request_get=lambda *args, **kwargs: response,
    )


def test_download_navcen_yuma_text_rejects_stream_overflow():
  response = FakeResponse(
    b"x" * (YUMA_ALMANAC_MAX_DOWNLOAD_BYTES + 1),
    chunk_size=1024,
  )

  with pytest.raises(
    YumaAlmanacDownloadError,
    match="exceeded",
  ):
    download_navcen_yuma_text(
      request_get=lambda *args, **kwargs: response,
    )


def test_refresh_missing_cache_downloads_and_saves(tmp_path: Path):
  path = tmp_path / "public_yuma_almanac.json"

  result = refresh_public_yuma_almanac(
    trusted_now=NOW,
    path=path,
    request_get=lambda *args, **kwargs: response_for(
      yuma_text()
    ),
  )

  assert result.status is YumaAlmanacRefreshStatus.UPDATED
  assert result.stored is not None
  assert result.stored.downloaded_at_utc == NOW
  assert load_yuma_almanac(path).almanac == result.stored.almanac


def test_refresh_same_content_does_not_rewrite_download_time(
  tmp_path: Path,
):
  path = tmp_path / "public_yuma_almanac.json"
  existing = convert_yuma_almanac(yuma_text())
  earlier_download = datetime(2026, 7, 21, 10, tzinfo=UTC)
  save_yuma_almanac(
    path,
    existing,
    downloaded_at_utc=earlier_download,
  )
  before = path.read_bytes()

  result = refresh_public_yuma_almanac(
    trusted_now=NOW,
    path=path,
    request_get=lambda *args, **kwargs: response_for(
      yuma_text()
    ),
  )

  assert result.status is YumaAlmanacRefreshStatus.UNCHANGED
  assert result.stored is not None
  assert result.stored.downloaded_at_utc == earlier_download
  assert path.read_bytes() == before


def test_refresh_newer_reference_replaces_stored_almanac(
  tmp_path: Path,
):
  path = tmp_path / "public_yuma_almanac.json"
  save_yuma_almanac(
    path,
    convert_yuma_almanac(yuma_text()),
    downloaded_at_utc=datetime(2026, 7, 21, 10, tzinfo=UTC),
  )

  result = refresh_public_yuma_almanac(
    trusted_now=NOW,
    path=path,
    request_get=lambda *args, **kwargs: response_for(
      yuma_text(time_of_applicability=323584)
    ),
  )

  assert result.status is YumaAlmanacRefreshStatus.UPDATED
  assert load_yuma_almanac(
    path
  ).almanac.time_of_applicability_seconds == 323584


def test_refresh_same_reference_changed_content_replaces_cache(
  tmp_path: Path,
):
  path = tmp_path / "public_yuma_almanac.json"
  save_yuma_almanac(
    path,
    convert_yuma_almanac(yuma_text()),
    downloaded_at_utc=datetime(2026, 7, 21, 10, tzinfo=UTC),
  )

  result = refresh_public_yuma_almanac(
    trusted_now=NOW,
    path=path,
    request_get=lambda *args, **kwargs: response_for(
      yuma_text(eccentricity="0.1829624176E-002")
    ),
  )

  assert result.status is YumaAlmanacRefreshStatus.UPDATED
  assert load_yuma_almanac(path).almanac.ubx_data != (
    convert_yuma_almanac(yuma_text()).ubx_data
  )


def test_refresh_older_reference_preserves_newer_cache(
  tmp_path: Path,
):
  path = tmp_path / "public_yuma_almanac.json"
  newer = convert_yuma_almanac(
    yuma_text(time_of_applicability=323584)
  )
  save_yuma_almanac(
    path,
    newer,
    downloaded_at_utc=datetime(2026, 7, 21, 10, tzinfo=UTC),
  )
  before = path.read_bytes()

  result = refresh_public_yuma_almanac(
    trusted_now=NOW,
    path=path,
    request_get=lambda *args, **kwargs: response_for(
      yuma_text()
    ),
  )

  assert result.status is YumaAlmanacRefreshStatus.PRESERVED_NEWER
  assert result.stored is not None
  assert result.stored.almanac == newer
  assert path.read_bytes() == before


@pytest.mark.parametrize("candidate_week", (377, 381))
def test_refresh_rejects_intrinsically_invalid_candidate(
  tmp_path: Path,
  candidate_week: int,
):
  path = tmp_path / "public_yuma_almanac.json"
  existing = convert_yuma_almanac(yuma_text())
  save_yuma_almanac(
    path,
    existing,
    downloaded_at_utc=datetime(2026, 7, 21, 10, tzinfo=UTC),
  )
  before = path.read_bytes()

  result = refresh_public_yuma_almanac(
    trusted_now=NOW,
    path=path,
    request_get=lambda *args, **kwargs: response_for(
      yuma_text(week=candidate_week)
    ),
  )

  assert result.status is YumaAlmanacRefreshStatus.FAILED
  assert result.stored is not None
  assert result.stored.almanac == existing
  assert path.read_bytes() == before


def test_failed_refresh_preserves_existing_cache(tmp_path: Path):
  path = tmp_path / "public_yuma_almanac.json"
  existing = convert_yuma_almanac(yuma_text())
  save_yuma_almanac(
    path,
    existing,
    downloaded_at_utc=datetime(2026, 7, 21, 10, tzinfo=UTC),
  )
  before = path.read_bytes()
  response = FakeResponse(b"error", status_code=503)

  result = refresh_public_yuma_almanac(
    trusted_now=NOW,
    path=path,
    request_get=lambda *args, **kwargs: response,
  )

  assert result.status is YumaAlmanacRefreshStatus.FAILED
  assert result.stored is not None
  assert result.stored.almanac == existing
  assert path.read_bytes() == before


def test_refresh_replaces_corrupt_existing_cache(tmp_path: Path):
  path = tmp_path / "public_yuma_almanac.json"
  path.write_bytes(b"corrupt")

  result = refresh_public_yuma_almanac(
    trusted_now=NOW,
    path=path,
    request_get=lambda *args, **kwargs: response_for(
      yuma_text()
    ),
  )

  assert result.status is YumaAlmanacRefreshStatus.UPDATED
  assert load_yuma_almanac(path).almanac.frames


def test_refresh_requires_trusted_time(tmp_path: Path):
  with pytest.raises(
    YumaAlmanacDownloadError,
    match="timezone-aware",
  ):
    refresh_public_yuma_almanac(
      trusted_now=datetime(2026, 7, 21),
      path=tmp_path / "public_yuma_almanac.json",
    )
