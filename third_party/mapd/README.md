# mapd (vendored binary) — NOT upstream

This is a **fork** build of mapd. Do not replace it with an upstream release.

| | |
|---|---|
| Source repo | `github.com/GrtBr/mapd` (fork of `pfeiferj/openpilot-mapd`) |
| Commit | `07ea8db` — "mapd: detect hazard on current way exit node (T-Junction fix)" |
| Built | 2026-06-01 |
| md5 | `0c3b552c229addc273e2c39c28924fbc` |
| Size | 21211912 bytes |
| Arch | ELF 64-bit aarch64, statically linked |
| Copied from | `~/Comma/sunnypilot/third_party/mapd_pfeiferj/mapd` (byte-identical to `mapd_source/build/mapd`) |

## Why this matters

This build contains fork-only work that upstream mapd does **not** have:

- precomputed per-node **curvature** in the tile schema,
- node-level **stop-sign / hazard** detection,
- the **T-junction fix**.

Consequences:

1. **Never run `mapd_installer.py` or any auto-update/download path.** Upstream pins
   `v1.12.0` from `pfeiferj/openpilot-mapd/releases`, which would silently replace this
   binary with one missing every feature above.
2. **Do not use `mapd_source/mapd_arm64`** (md5 `2dda8f6edc6bb135050641222fd5f60f`, May 21) —
   it predates the T-junction fix.
3. The offline tiles under `<MAPD_ROOT>/offline/` were generated with the **fork's** tile
   schema (`curvature @3` on `Coordinates`, `highway @14`), so they only work with this
   binary, and this binary expects tiles built that way.

Verify `md5sum third_party/mapd/mapd` after any copy or deploy.

## Wire contract

The binary has its capnp schema compiled in and talks msgq directly. It must agree with
`openpilot/cereal/custom.capnp` + `log.capnp` (mapd union slots @143/@144/@145, discriminants
141/142/143) and with the `QueueSize.MEDIUM` (2 MB) entries in `cereal/services.py`.
Changing any struct id, field ordinal or queue size silently breaks the wire layout.
