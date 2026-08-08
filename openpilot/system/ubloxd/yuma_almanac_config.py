from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog


PUBLIC_YUMA_ALMANAC_ENABLED_PARAM = "PublicYumaAlmanacEnabled"
PUBLIC_YUMA_ALMANAC_PARAM_POLL_SECONDS = 1.0


def public_yuma_almanac_enabled(params: Params) -> bool:
  get_bool = getattr(params, "get_bool", None)
  if not callable(get_bool):
    return False

  try:
    return bool(get_bool(PUBLIC_YUMA_ALMANAC_ENABLED_PARAM))
  except Exception:
    cloudlog.exception("Failed to read public YUMA feature gate")
    return False
