# Captain's Log — `release-mici-staging`

Running record of code changes to **this checkout only** (`~/Comma/openpilot/release-mici-staging`,
branch `release-mici-staging`, from `upstream/release-mici-staging`). Newest entry first. Each entry:
what changed, why, how it was verified, and current deploy status.

The sibling `~/Comma/openpilot/nightly-dev/` checkout keeps its **own** `captains_log.md` for the
`nightly-dev` branch. The two branches diverge — changes logged there (driver-distracted lockout,
e2e accel low-pass filter, etc.) are **not** present here unless an entry below says they were
cherry-picked.

---

## 2026-07-28

### 1. Checkout created

Fresh full clone of `GrtBr/openpilot` into `/home/pi5-ubuntu/Comma/openpilot/release-mici-staging`,
so a second branch could be worked on without disturbing the `nightly-dev` tree.

- `origin` → `https://github.com/GrtBr/openpilot.git` (the fork — pushable)
- `upstream` → `https://github.com/commaai/openpilot.git`
- Local branch `release-mici-staging` created from `upstream/release-mici-staging` at `70e157462`
  (openpilot v0.11.1), and tracks `upstream/release-mici-staging`. The fork has no
  `release-mici-staging` branch — only `master` and `nightly-dev` — so the branch had to come from
  upstream.
- Git identity is set **per-repo** (`Gert` / `gert@atonce.co.za`); there is no global identity on the
  Pi5, and the first commit attempt failed because of it.

Layout matches `nightly-dev`: opendbc is vendored at `opendbc_repo/` (plain tracked dir, no
`.gitmodules`, no nested `.git`), so car-port edits are made directly in this repo.

### 2. RHD Staria (US4 4th gen) FW fingerprint

**Files:** `opendbc_repo/opendbc/car/hyundai/fingerprints.py`
**Commit:** `0af132822`, cherry-picked from `b91340b` on `nightly-dev`

Adds the RHD firmware versions to `CAR.HYUNDAI_STARIA_4TH_GEN`, which previously carried only the LHD
KOR entries and so would not match this car:

- `(Ecu.fwdCamera, 0x7c4, None)`: `b'\xf1\x00US4 MFC  AT GEN RHD 1.00 1.01 99211-CG200 250207'`
- `(Ecu.fwdRadar, 0x7d0, None)`: `b'\xf1\x00US4_ RDR -----      1.00 1.01 99110-CG100         '`

No new car and no `PlatformConfig` change — the platform already existed.

The cherry-pick applied clean: this branch's `fingerprints.py` was byte-identical to the
pre-fingerprint version on `nightly-dev`, so that one commit was the entire delta.

**Verification** (on the Pi5; the repo has no populated venv, so deps came from
`uv run --no-project --with numpy --with pycapnp --with tqdm --with pytest --with parameterized
--with pytest-subtests --with pytest-xdist --with pycryptodome`):

- `match_fw_to_car_exact` returns exactly `{CAR.HYUNDAI_STARIA_4TH_GEN}` for the RHD camera+radar
  pair, for the pre-existing LHD pair, and for a mixed RHD-camera/LHD-radar pair.
  Note the lookup dict is keyed by `(address, subAddress)` — **not** by `(Ecu, address, subAddress)`;
  keying it with the Ecu silently returns an empty set for every car.
- `opendbc/car/tests/test_fw_fingerprint.py`: **14 passed, 138 skipped, 2473 subtests passed** — no
  cross-model collisions introduced. Must be run from inside `opendbc_repo/` with
  `-c ./pyproject.toml --confcutdir=. -n0`; otherwise the parent openpilot `conftest.py` is collected
  and fails locally (it needs `zmq` and a built `params_pyx`).

**Deploy status:** local only. Not pushed to `origin`, not deployed to the comma4 — which is running
`nightly-dev` (see the sibling checkout's log).
