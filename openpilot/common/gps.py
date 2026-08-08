import os

from openpilot.common.params import Params

# Cereal service carrying the single authoritative GPS source selection (PR80).
GPS_SOURCE_STATE_SERVICE = "gpsSourceState"

# gpsSourceState is 1 Hz; three missed updates => consumers lose authority.
GPS_SOURCE_STATE_FRESH_SECONDS = 3.0


def ublox_hardware_available() -> bool:
  """Comma 4 u-blox presence: ttyHS0 and no force-quectel persist flag."""
  return os.path.exists('/dev/ttyHS0') and not os.path.exists('/persist/comma/use-quectel-gps')


def get_gps_location_service(params: Params) -> str:
  """Hardware-preferred GPS socket (static). Prefer gpsSourceState for authority."""
  if params.get_bool("UbloxAvailable"):
    return "gpsLocationExternal"
  else:
    return "gpsLocation"


def selected_source_to_service(selected: str) -> str | None:
  """Map arbiter/cereal selected source name to the authoritative GPS socket."""
  if selected in ("ubloxPrimary", "UBLOX_PRIMARY"):
    return "gpsLocationExternal"
  if selected in ("qcomFallback", "QCOM_FALLBACK"):
    return "gpsLocation"
  return None


def gps_source_state_is_fresh(*, now_mono: float, last_state_recv_mono: float | None, fresh_seconds: float = GPS_SOURCE_STATE_FRESH_SECONDS) -> bool:
  """Fail closed unless a fresh authoritative gpsSourceState was observed."""
  if last_state_recv_mono is None:
    return False
  if not isinstance(now_mono, (int, float)) or isinstance(now_mono, bool):
    return False
  if not isinstance(last_state_recv_mono, (int, float)) or isinstance(last_state_recv_mono, bool):
    return False
  age = float(now_mono) - float(last_state_recv_mono)
  return 0.0 <= age <= fresh_seconds


def accept_gps_source_epoch(
  *,
  transition_mono_ns: int,
  generation: int,
  selected: str,
  recv_mono_ns: int,
  last_transition_mono_ns: int | None,
  last_generation: int | None,
  last_selected: str | None,
) -> bool:
  """Validate gpsSourceState authority epoch for consumers (timed/locationd).

  - Reject future epochs relative to receive mono time.
  - Reject regressing epochs (transitionMonoNs moving backward).
  - Equal epoch must keep selected+generation consistent.
  - Newer epoch is accepted (including arbiter restart with gen=0).
  """
  if not isinstance(transition_mono_ns, int) or not isinstance(recv_mono_ns, int):
    return False
  if transition_mono_ns < 0 or recv_mono_ns < 0:
    return False
  # Impossible future authority epoch vs message receive time.
  if transition_mono_ns > recv_mono_ns:
    return False
  if last_transition_mono_ns is None:
    return True
  if transition_mono_ns < last_transition_mono_ns:
    return False
  if transition_mono_ns == last_transition_mono_ns:
    return generation == last_generation and selected == last_selected
  return True
