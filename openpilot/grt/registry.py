"""Registration data for fork-owned services, processes and paths.

Kept deliberately IMPORT-FREE at module level. Two reasons:

1. `cereal/services.py` imports this, and services.py is itself executed as a standalone
   script at build time (`python3 services.py > services.h`), where the repo root is not on
   sys.path. A module-level `from openpilot...` here would break the build.
2. Importing `openpilot.cereal.services` from here would be circular (services.py imports us).

So queue sizes are plain ints and the process list is built lazily inside a function.
"""

# QueueSize.MEDIUM. MUST match the mapd binary's compiled-in ServiceQueueSize table
# (mapd_source/settings/const.go): mapdOut/mapdIn/mapdExtendedOut/mapdCli = 2 MB.
# A mismatch corrupts the shared-memory mapping without an obvious error.
_MEDIUM = 2 * 1024 * 1024

# mapd's working directory on device; also where the offline OSM tiles live
# (<MAPD_ROOT>/offline/). Kept here rather than added to common/hardware/hw.py so the
# upstream Paths class needs no fork edit at all.
MAPD_ROOT = "/data/media/0/osm"

# Fork-owned config dir. Deliberately OUTSIDE /data/params so Params::clear_all() cannot
# delete anything in it. This is what makes the fork work on a PREBUILT branch, where
# grt_params_keys.inc is never compiled in and every fork param raises UnknownKeyName.
GRT_CONFIG_DIR = "/data/media/0/grt"

# Where mapd reads its settings. Fixed path, compiled into the Go binary.
MAPD_SETTINGS_PATH = "/data/params/d/MapdSettings"

# Path of the vendored mapd binary, relative to the repo root (BASEDIR).
MAPD_BINARY_RELPATH = "third_party/mapd/mapd"

# NOTE: the mapd service definitions themselves live INLINE in cereal/services.py, not here.
# services.py is executed standalone at build time (no repo root on sys.path), so it cannot
# import this module. Keeping them in one place there avoids two sources of truth; _MEDIUM
# above is retained only to document the required queue size.

# Services spliced into plannerd's SubMaster. Never add modelV2-sized (BIG) services here.
GRT_SUB: list[str] = ["mapdOut"]

# Processes that should not raise the processNotRunning safety event when absent.
# mapd is not a normal openpilot process: it is a standalone Go binary that only runs when
# the tile directory exists.
GRT_IGNORED_PROCESSES: set[str] = {"mapd"}


def grt_procs() -> list:
  """Build the fork's manager process list. Imports are local: see module docstring."""
  import os
  import sys
  from openpilot.common.basedir import BASEDIR
  from openpilot.system.manager.process import NativeProcess

  mapd_path = os.path.join(BASEDIR, MAPD_BINARY_RELPATH)

  def mapd_ready(started: bool, params, CP) -> bool:
    # Run whenever the tile/working directory is present, on or offroad. mapd needs to be up
    # before the car moves so it has a map fix ready.
    return os.path.exists(MAPD_ROOT) and os.path.exists(mapd_path)

  # Rewrite MapdSettings immediately before exec'ing mapd. On a prebuilt branch MapdSettings
  # is not in the compiled params_keys.h table, so Params::clear_all() deletes it on manager
  # start and on every ignition/offroad transition. Writing it here means mapd always reads a
  # correct file at startup regardless; mapd keeps the values in memory afterwards.
  ensure = (f"{sys.executable} -c "
            f"'from openpilot.grt.settings import write_settings_file; write_settings_file()'")
  cmd = f"{ensure} >/dev/null 2>&1; exec {mapd_path} >/dev/null 2>&1"

  return [
    NativeProcess("mapd", MAPD_ROOT, ["bash", "-c", cmd], mapd_ready),
  ]
