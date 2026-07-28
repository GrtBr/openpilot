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
  from openpilot.common.basedir import BASEDIR
  from openpilot.system.manager.process import NativeProcess

  mapd_path = os.path.join(BASEDIR, MAPD_BINARY_RELPATH)

  def mapd_ready(started: bool, params, CP) -> bool:
    # Run whenever the tile/working directory is present, on or offroad. mapd needs to be up
    # before the car moves so it has a map fix ready.
    return os.path.exists(MAPD_ROOT) and os.path.exists(mapd_path)

  return [
    NativeProcess("mapd", MAPD_ROOT, ["bash", "-c", f"{mapd_path} > /dev/null 2>&1"], mapd_ready),
  ]
