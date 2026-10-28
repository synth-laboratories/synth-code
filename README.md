# Synth Code

An independent Rust/Temper runtime and Python terminal client, under development.
This checkout currently implements a **local offline foundation preview**, not a
released coding agent. It is not published or connected to a GitHub remote.

The preview provides a real Temper coordinator actor, SQLite WAL journal,
identity-bound mutation receipts, approvals, owner fencing and unknown-effect
recovery. The Python client supports workspace read/search, approved atomic file
creation/replacement, saved-session listing and resume. Rust performs file effects.
No model credentials, provider calls or shell execution are used.

## Develop

Python 3.12+ and Rust 1.92+ are declared baselines; current local verification uses
macOS ARM64 and Python 3.13. Exact dependencies are retained in Cargo.lock and
python/uv.lock. Package versions/names are development placeholders.

In the owner's workspace, use the maintained build launcher with mbx:

```sh
python3 scripts/build.py test
python3 scripts/build.py clippy
UV_CACHE_DIR="$PWD/.cache/uv" uv sync --project python --group dev --locked
PYTHONPATH=python/src python/.venv/bin/python -m unittest discover -s python/tests -v
python3 scripts/verify_sources.py
```

`SYNTH_BUILD_LAUNCHER` can select the maintained launcher explicitly. The wrapper
refuses a missing launcher or mbx; it does not bypass admission/storage guards.
Other host setup and clean public distribution builds remain unqualified.

## Open the fixture preview

```sh
mkdir -p .state/workspace
python/.venv/bin/synth-code-dev \
  --runtime "$PWD/target/debug/synth-code-runtime" \
  --store "$PWD/.state/preview" \
  --workspace "$PWD/.state/workspace"
```

`/read path` reads a bounded text file; `/search text` performs bounded literal
search; `/edit path` opens an existing file in the editor and `/create path` opens
a new-file editor. Propose shows the replacement diff, then Allow or Deny acts on
that exact operation. Tab and Shift+Tab move focus; Ctrl+Q or Ctrl+C exits.

Plain text still submits the isolated synthetic fixture effect, creating a new
`fixture-<operation-id>.txt` only after approval. It is not a model task. Use a
dedicated workspace for this development preview.

The current stdio preview owns its backend child; exit stops that child. Durable
state remains. List saved sessions with the same runtime/store arguments plus
`--list-sessions`; reopen one with `--resume UUID`. Pending approvals and operation
history are restored from paginated Rust state. Detachable Unix-socket hosting and
turn interruption are later work. Unknown effects cannot be automatically repeated
or cleared through this preview.

## Native terminal verification

`scripts/run_bombadil.py` runs the actual Python client and Rust executable through
Bombadil's native terminal driver, with no browser bridge or provider calls. It
requires the checked Python development environment and a pinned official Bombadil
v0.7.10 asset. Download the asset matching the host into `artifacts/tools/`, retaining
its official filename. The runner verifies its SHA-256 against the release digest.

```sh
python/.venv/bin/python scripts/run_bombadil.py \
  --bombadil artifacts/tools/bombadil-v0.7.10/bombadil-aarch64-darwin
```

The journey resumes a pending edit, allows it, reads/searches the result, denies a
fixture effect, resizes and quits. The runner requires terminal milestones, matching
Rust receipts, the actual file result and store-lock release. A watchdog and output
cap prevent unbounded runs; process supervision uses the maintained light launcher.
Traces and receipts remain under `artifacts/terminal/`. The optional
`--inject-bad-expectation` deliberately fails the independent oracle and must exit
nonzero. `--reproduce TRACE_DIRECTORY` attempts best-effort action replay on a fresh
fixture; trace/scheduling divergence is a failure, not a passing reproduction.

This is an initial deterministic Bombadil journey. Random exploration, shrink
regression integration, native wheels and the complete supported terminal/platform
matrix remain pending.

## Source boundaries and status

Application code is independently written for this repository. Public Temper
sources are pinned and verified in `vendor/temper/SOURCE.json`; crate sources are
unchanged. See [provenance](docs/PROVENANCE.md) and [foundation contract](docs/FOUNDATION.md).
Application licensing and final public names/versions remain to be selected before
publication; third-party licenses remain with their dependencies.

Remaining v0.01 work includes command supervision, complete turn lifecycle/streaming,
live protocol replay/reconnect, a complete TUI, Reflection Beam, Synth account
integration, native wheels, broader Bombadil campaigns and release qualification.
Horst/Hengist is planned for v0.03.
