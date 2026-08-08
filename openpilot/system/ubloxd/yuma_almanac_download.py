from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Any

import requests

from openpilot.system.ubloxd.yuma_almanac import (
  YumaAlmanacError,
  convert_yuma_almanac,
  validate_yuma_reference_time,
)
from openpilot.system.ubloxd.yuma_almanac_store import (
  YUMA_ALMANAC_CACHE_PATH,
  StoredYumaAlmanac,
  YumaAlmanacStoreError,
  load_yuma_almanac,
  save_yuma_almanac,
)


NAVCEN_CURRENT_YUMA_URL = "https://www.navcen.uscg.gov/sites/default/files/gps/almanac/current_yuma.alm"
YUMA_ALMANAC_DOWNLOAD_TIMEOUT_SECONDS = 10.0
YUMA_ALMANAC_MAX_DOWNLOAD_BYTES = 64 * 1024
YUMA_ALMANAC_USER_AGENT = "openpilot-public-yuma/1"

RequestGet = Callable[..., Any]


class YumaAlmanacDownloadError(RuntimeError):
  pass


class YumaAlmanacRefreshStatus(StrEnum):
  UPDATED = "updated"
  UNCHANGED = "unchanged"
  PRESERVED_NEWER = "preserved_newer"
  FAILED = "failed"


@dataclass(frozen=True)
class YumaAlmanacRefreshResult:
  status: YumaAlmanacRefreshStatus
  reason: str
  stored: StoredYumaAlmanac | None
  candidate_reference_time_utc: datetime | None = None


def _trusted_utc(value: datetime) -> datetime:
  if value.tzinfo is None or value.utcoffset() is None:
    raise YumaAlmanacDownloadError(
      "trusted_now must be timezone-aware"
    )
  return value.astimezone(UTC)


def _content_length(response: Any) -> int | None:
  raw_value = response.headers.get("Content-Length")
  if raw_value is None:
    return None

  try:
    value = int(raw_value, 10)
  except (TypeError, ValueError) as exc:
    raise YumaAlmanacDownloadError(
      f"Invalid Content-Length header: {raw_value!r}"
    ) from exc

  if value < 0:
    raise YumaAlmanacDownloadError(
      f"Negative Content-Length header: {value}"
    )
  return value


def download_navcen_yuma_text(
  *,
  request_get: RequestGet = requests.get,
  url: str = NAVCEN_CURRENT_YUMA_URL,
  timeout_seconds: float = YUMA_ALMANAC_DOWNLOAD_TIMEOUT_SECONDS,
  maximum_bytes: int = YUMA_ALMANAC_MAX_DOWNLOAD_BYTES,
) -> str:
  if (
    isinstance(maximum_bytes, bool)
    or not isinstance(maximum_bytes, int)
    or maximum_bytes <= 0
  ):
    raise YumaAlmanacDownloadError(
      "maximum_bytes must be a positive integer"
    )
  if (
    isinstance(timeout_seconds, bool)
    or not isinstance(timeout_seconds, (int, float))
    or not isfinite(timeout_seconds)
    or timeout_seconds <= 0
  ):
    raise YumaAlmanacDownloadError(
      "timeout_seconds must be a positive finite number"
    )

  response = None
  try:
    response = request_get(
      url,
      headers={
        "Accept": "text/plain",
        "User-Agent": YUMA_ALMANAC_USER_AGENT,
      },
      stream=True,
      timeout=timeout_seconds,
    )
    response.raise_for_status()

    content_length = _content_length(response)
    if (
      content_length is not None
      and content_length > maximum_bytes
    ):
      raise YumaAlmanacDownloadError(
        f"NAVCEN YUMA response is too large: {content_length} bytes"
      )

    data = bytearray()
    for chunk in response.iter_content(chunk_size=8192):
      if not chunk:
        continue
      if len(data) + len(chunk) > maximum_bytes:
        raise YumaAlmanacDownloadError(
          f"NAVCEN YUMA response exceeded {maximum_bytes} bytes"
        )
      data.extend(chunk)

  except requests.RequestException as exc:
    raise YumaAlmanacDownloadError(
      f"NAVCEN YUMA download failed: {exc}"
    ) from exc
  finally:
    if response is not None:
      response.close()

  if not data:
    raise YumaAlmanacDownloadError(
      "NAVCEN YUMA response was empty"
    )

  try:
    return bytes(data).decode("utf-8-sig")
  except UnicodeDecodeError as exc:
    raise YumaAlmanacDownloadError(
      "NAVCEN YUMA response was not UTF-8"
    ) from exc


def refresh_public_yuma_almanac(
  *,
  trusted_now: datetime,
  path: Path = YUMA_ALMANAC_CACHE_PATH,
  request_get: RequestGet = requests.get,
) -> YumaAlmanacRefreshResult:
  """Perform one explicit, opportunistic NAVCEN refresh attempt."""
  normalized_now = _trusted_utc(trusted_now)
  existing: StoredYumaAlmanac | None = None
  existing_error: str | None = None

  try:
    existing = load_yuma_almanac(path)
  except YumaAlmanacStoreError as exc:
    existing_error = str(exc)

  try:
    text = download_navcen_yuma_text(
      request_get=request_get,
    )
    candidate = convert_yuma_almanac(text)
    candidate_reference = validate_yuma_reference_time(
      candidate,
      normalized_now,
    )
  except (
    YumaAlmanacDownloadError,
    YumaAlmanacError,
  ) as exc:
    reason = f"refresh_failed: {exc}"
    if existing_error is not None:
      reason += f"; existing_cache={existing_error}"
    return YumaAlmanacRefreshResult(
      status=YumaAlmanacRefreshStatus.FAILED,
      reason=reason,
      stored=existing,
    )

  if (
    existing is not None
    and existing.almanac.ubx_data == candidate.ubx_data
  ):
    return YumaAlmanacRefreshResult(
      status=YumaAlmanacRefreshStatus.UNCHANGED,
      reason="downloaded_content_matches_stored_almanac",
      stored=existing,
      candidate_reference_time_utc=candidate_reference,
    )

  existing_reference: datetime | None = None
  if existing is not None:
    try:
      existing_reference = validate_yuma_reference_time(
        existing.almanac,
        normalized_now,
      )
    except YumaAlmanacError:
      existing_reference = None

  if (
    existing_reference is not None
    and candidate_reference < existing_reference
  ):
    return YumaAlmanacRefreshResult(
      status=YumaAlmanacRefreshStatus.PRESERVED_NEWER,
      reason=", ".join((
        "downloaded_almanac_is_older_than_stored_almanac",
        f"candidate={candidate_reference.isoformat()}",
        f"stored={existing_reference.isoformat()}",
      )),
      stored=existing,
      candidate_reference_time_utc=candidate_reference,
    )

  try:
    save_yuma_almanac(
      path,
      candidate,
      downloaded_at_utc=normalized_now,
    )
  except (OSError, YumaAlmanacStoreError) as exc:
    reason = f"refresh_save_failed: {exc}"
    if existing_error is not None:
      reason += f"; existing_cache={existing_error}"
    return YumaAlmanacRefreshResult(
      status=YumaAlmanacRefreshStatus.FAILED,
      reason=reason,
      stored=existing,
      candidate_reference_time_utc=candidate_reference,
    )

  return YumaAlmanacRefreshResult(
    status=YumaAlmanacRefreshStatus.UPDATED,
    reason=(
      f"downloaded_converted_validated_and_saved: reference={candidate_reference.isoformat()}"
    ),
    stored=StoredYumaAlmanac(
      downloaded_at_utc=normalized_now,
      almanac=candidate,
    ),
    candidate_reference_time_utc=candidate_reference,
  )
