"""
Copyright (c) 2021-, Haibin Wen, sunnypilot, and a number of other contributors.

This file is part of sunnypilot and is licensed under the MIT License.
See the LICENSE.md file in the root directory for more details.

mazda-dev: ship the Firehose Model as the default driving model.

We don't commit the model binary into the release; instead we make it the default selection and let
the existing, hash-validated download path fetch it on the first offroad cycle that has network. This
is driven from the models manager main loop (which already refreshes ``available_bundles`` and processes
``ModelManager_DownloadIndex`` every second), so an offline boot is simply a no-op that retries next loop.

Applied exactly once, guarded by ``DefaultModelApplied``: a device that already has a model explicitly
selected keeps it, and a user who later reverts to the stock/Default model is never overridden again.
"""

from openpilot.common.params import Params
from openpilot.common.swaglog import cloudlog
from openpilot.cereal import custom

# Firehose Model (short name "FM") — index in sunnypilot's driving_models_v17.json manifest.
DEFAULT_MODEL_BUNDLE_INDEX = 30


def maybe_apply_default_model(params: Params, available_bundles: list["custom.ModelManagerSP.ModelBundle"]) -> None:
  if params.get_bool("DefaultModelApplied"):
    return

  # A model is already active (device migrated in with a selection, or the user already picked one):
  # respect it and never touch the default again.
  if params.get("ModelManager_ActiveBundle") is not None:
    params.put_bool("DefaultModelApplied", True)
    return

  # Nothing active yet: queue the Firehose Model for download once it appears in the available list.
  # Offline / not-yet-fetched -> no-op, we retry on the next loop.
  bundle = next((b for b in available_bundles if b.index == DEFAULT_MODEL_BUNDLE_INDEX), None)
  if bundle is None:
    return

  params.put("ModelManager_DownloadIndex", bundle.index)
  params.put_bool("DefaultModelApplied", True)
  cloudlog.warning(f"Applying Firehose Model as the default (queued download index {bundle.index})")
