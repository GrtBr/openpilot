#!/usr/bin/env python3
"""Write mapd's own settings blob (the `MapdSettings` param).

mapd reads this JSON at startup and whenever it receives a `mapdIn` message of type
`reloadSettings`. Run this ONCE at install time:

    python3 -m openpilot.grt.settings          # write defaults (only if absent)
    python3 -m openpilot.grt.settings --force  # overwrite an existing blob
    python3 -m openpilot.grt.settings --show   # print what is currently stored

Unlike sunnypilot -- which rewrites this file every second from mapd_manager.py because
openpilot's Params::clear_all() deletes any param not listed in params_keys.h -- we register
`MapdSettings` as PERSISTENT (openpilot/common/grt_params_keys.inc), so a single write
survives manager restarts and ignition/offroad transitions. Do NOT reintroduce the loop.
"""
import argparse
import json

# Baseline is sunnypilot's known-good working config, with deliberate differences marked.
# Do NOT "improve" the tuning values: target_speed_* are the user's tuned map-curve braking
# profile, confirmed working on the car.
DEFAULT_MAPD_SETTINGS: dict = {
  "settings_version": 1,

  # --- behaviour 1: map curve speed control ---
  "map_curve_speed_control_enabled": True,
  "map_curve_use_enable_speed": False,

  # --- behaviour 3: automatic speed-limit adoption ---
  # CHANGED from sunnypilot's False. This is what makes mapd fold the posted limit (plus
  # offset, hold-last-seen and the next-limit lookahead) into mapdOut.speedLimitSuggestedSpeed,
  # which the python controller consumes. See Phase 6 of PORT_MAPD_FROM_SUNNYPILOT.md.
  "speed_limit_control_enabled": True,
  "speed_limit_priority": "map",
  "speed_limit_use_enable_speed": False,
  "speed_limit_change_requires_accept": False,
  "hold_last_seen_speed_limit": False,
  # NOTE: 0.0 means openpilot holds EXACTLY the posted limit, which many drivers find slower
  # than expected. Sanity-check this on a real drive before trusting it; a small positive
  # offset is a legitimate tune.
  "speed_limit_offset": 0.0,
  "external_speed_limit_control_enabled": False,

  # --- vision curve: OFF ---
  # CHANGED from sunnypilot's True. Nothing in this port consumes mapdOut.visionCurveSpeed,
  # so leaving it on only burns CPU on device.
  "vision_curve_speed_control_enabled": False,
  "vision_curve_use_enable_speed": False,
  "vision_curve_target_lat_a": 2.7,
  "vision_curve_min_target_v": 0.0,

  # --- speed-limit accept/override UX (unused here; we have no confirmation UI) ---
  "press_gas_to_accept_speed_limit": False,
  "press_gas_to_override_speed_limit": False,
  "adjust_set_speed_to_accept_speed_limit": False,
  "accept_speed_limit_timeout": 5.0,

  # --- tuned braking profile: leave alone ---
  "target_speed_jerk": 0.6,
  "target_speed_accel": 0.6,
  "target_speed_time_offset": 4.0,
  "enable_speed": 0.0,

  # --- logging ---
  "log_level": "warn",
  "log_json": False,
  "log_source": False,
}

PARAM_KEY = "MapdSettings"


def write_settings(overrides: dict | None = None, force: bool = False, notify: bool = True) -> dict:
  """Write the settings blob. Returns the settings actually stored.

  Without `force`, an existing blob is left untouched so a reinstall never silently discards
  on-car tuning.
  """
  from openpilot.common.params import Params
  params = Params()

  existing = params.get(PARAM_KEY)
  if existing and not force:
    return json.loads(existing)

  settings = dict(DEFAULT_MAPD_SETTINGS)
  if overrides:
    settings.update(overrides)
  params.put(PARAM_KEY, json.dumps(settings))

  if notify:
    _notify_reload()
  return settings


def _notify_reload() -> bool:
  """Best-effort: tell a already-running mapd to re-read its settings."""
  try:
    import openpilot.cereal.messaging as messaging
    pm = messaging.PubMaster(["mapdIn"])
    msg = messaging.new_message("mapdIn")
    msg.mapdIn.type = "reloadSettings"
    pm.send("mapdIn", msg)
    return True
  except Exception:
    # mapd not running / msgq unavailable -- it will read the blob at next startup anyway.
    return False


def main() -> None:
  ap = argparse.ArgumentParser(description="write mapd's MapdSettings param")
  ap.add_argument("--force", action="store_true", help="overwrite an existing settings blob")
  ap.add_argument("--show", action="store_true", help="print stored settings and exit")
  ap.add_argument("--no-notify", action="store_true", help="don't send reloadSettings to mapd")
  args = ap.parse_args()

  from openpilot.common.params import Params
  if args.show:
    cur = Params().get(PARAM_KEY)
    print(json.dumps(json.loads(cur), indent=2) if cur else f"{PARAM_KEY}: <not set>")
    return

  stored = write_settings(force=args.force, notify=not args.no_notify)
  print(json.dumps(stored, indent=2))


if __name__ == "__main__":
  main()
