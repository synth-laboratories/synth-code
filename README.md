# Synth Code

An independent Rust/Temper runtime and Python terminal client, under development.
This checkout currently implements a **local offline foundation preview**, not a
released coding agent. It is not published or connected to a GitHub remote.

The first slice provides a real Temper coordinator actor, SQLite WAL journal,
identity-bound mutation receipts, approvals, owner fencing and unknown-effect
recovery. The Python preview can propose a synthetic local file effect and allow
or deny it. Rust performs the effect. No model credentials, provider calls or
shell execution are used.

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

Enter submits fixture text. Allow or Deny acts on its exact operation. Tab and
Shift+Tab move focus; Ctrl+Q or Ctrl+C exits this preview. Approved text becomes a
new `fixture-<operation-id>.txt` in the selected workspace. Never use this fixture
as a general file-editing tool.

The current stdio preview owns its backend child; exit stops that child. Durable
state remains, but the UI starts a new session on the next launch. Detachable
Unix-socket hosting and user-facing resume/interruption are later work. Native
protocol reads can inspect saved sessions; unknown effects cannot be automatically
repeated or cleared through this preview.

## Source boundaries and status

Application code is independently written for this repository. Public Temper
sources are pinned and verified in `vendor/temper/SOURCE.json`; crate sources are
unchanged. See [provenance](docs/PROVENANCE.md) and [foundation contract](docs/FOUNDATION.md).
Application licensing and final public names/versions remain to be selected before
publication; third-party licenses remain with their dependencies.

Remaining v0.01 work includes general workspace tools and command supervision,
turn lifecycle/streaming, protocol pagination/reconnect, a complete TUI, Reflection
Beam, Synth account integration, native wheels, Bombadil campaigns and release
qualification. Horst/Hengist is planned for v0.03.
