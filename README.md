# Synth Code

A local coding agent with an independently implemented Rust/Temper runtime and a
Python terminal UI. The v0.01 release uses registry version **0.0.1** and Apache-2.0.
This repository is preparing a release candidate; public publication and final installed
qualification remain gated. See [qualification](docs/QUALIFICATION.md).

The runtime owns model calls, approvals, file effects, command supervision and the
SQLite journal. The terminal client attaches over a private Unix socket. Closing
the UI leaves accepted work running; restarting the runtime interrupts turns and
marks dispatched effects without a recorded result as unknown. It never repeats
an unknown command or edit automatically.

## Run a development checkout

Use Python 3.12+, the [mbx Rust build cache](https://mr-boxington.jdx.dev/installation),
and Rust 1.97.1 (the measured toolchain). Builds enter through the maintained
admission/storage guard; they require 20 GiB free and an owned target below 8 GiB.
The owner workspace automatically uses its shared launcher. Other checkouts use
the portable guard included here.

```sh
python3 scripts/build.py build
UV_CACHE_DIR="$PWD/.cache/uv" uv sync --project python --group dev --locked
python/.venv/bin/synth-code --runtime "$PWD/target/debug/synth-code-runtime" \
  --store "$PWD/.state/local" --runtime-dir "$PWD/.state/run" \
  --workspace "$PWD" --env-file /absolute/path/to/provider.env
```

The selected file must contain exactly one `REFLECTION_API_KEY` binding. An
inherited `REFLECTION_API_KEY` also works. The provider is Reflection's first-party
Chat Completions endpoint, with the documented `Beam-1.0-501B-A23B` identifier and
medium effort. There is no automatic provider, model or account fallback. Actual
account access must qualify separately; an available credential is not proof of
model entitlement. [Provider configuration](docs/PROVIDER.md) records current limits.

Ask a coding task. Read/search tools inspect bounded workspace text; edits and
host commands wait for exact approval. The workspace's `AGENTS.md`, when present,
is included as bounded task context. Local coding does not require Synth login.
Optional Synth account commands are `synth-code login`, `auth status --json`,
`account limits --json` and `logout`; they use the published Synth SDK's account
authority with a separate product credential store and read-only account scopes.
Synth identity does not grant Reflection credits or model access.

Tab/Shift+Tab move focus. Enter submits; Alt+Enter inserts a newline. Ctrl+C
interrupts the active turn, or exits an idle client. Ctrl+Q detaches. `/read path`,
`/search text`, `/edit path` and `/create path` expose direct file actions. The
approval card shows the proposed replacement diff or host command and timeout.
`/output UUID` pages complete bounded command output. Approved manual operations
that survived a pre-dispatch restart offer Resume or Cancel. `--no-color` and
`NO_COLOR` select monochrome presentation.
After inspecting an unknown effect, `/resolve UUID observation` records your
observation and removes its block without executing the effect again.

Use `--list-sessions`, then `--resume UUID` to reattach. `--status` reads a running
daemon's bounded diagnostics and `--stop` requests shutdown. An installed client
refuses to attach to a daemon using different native bytes; finish/interrupt work,
stop the old daemon, then launch the upgraded client. Do not delete a live store.

## Distribution and boundaries

[Installation and getting started](docs/INSTALL.md) describes candidate wheel
installation. Wheels bundle the runtime and need no Rust compiler. macOS ARM64
has local evidence on macOS 26.4; other target/platform claims remain explicitly
unqualified until their gates pass. Linux and Windows wheels are not published.

Approval permits **host execution**, not an OS sandbox. Commands start at a held,
identity-checked workspace directory, with a small clean environment, bounded
time/output and process-group cleanup. An approved shell can access the host.
File tools refuse symlinks, hardlinks, path traversal, special/binary files and
selected credential/generated paths. Atomic replacement does not provide CAS
against an uncooperative external writer. See [security](SECURITY.md).

History and provider continuation are local private data. The UI never exposes
provider reasoning continuation. Model context is bounded at 512 KiB per request;
there is no automatic compaction in this release. Artifact storage is capped at
512 MiB/16,384 files. Start another session when context reaches its limit; retain
the old journal for inspection. Index remains cloud-only and is not a local tool.
Horst/Hengist is reserved for v0.03; the research-loop proposal is outside v0.01.

## Verify

```sh
python3 scripts/build.py test
python3 scripts/build.py clippy
python/.venv/bin/python -m unittest discover -s python/tests -v
python3 scripts/verify_sources.py
python/.venv/bin/python scripts/run_bombadil.py --bombadil /path/to/pinned/bombadil
python/.venv/bin/python scripts/run_agent_bombadil.py --bombadil /path/to/pinned/bombadil
```

Native Bombadil v0.7.10 journeys run the actual Python TUI and Rust backend.
The agent journey uses a zero-spend local Reflection SSE fixture and independent
file/journal oracles, verifies resize/detach and closes its owned daemon. Both
runners support a deliberately wrong oracle (`--inject-bad-expectation`) and
best-effort trace replay (`--reproduce TRACE_DIRECTORY`). Trace divergence fails.
The workspace runner also supports `--resume-ready` and a bounded 30-choice
state-aware `--explore` campaign, with the same independent effect oracle.
This is bounded exploration, not exhaustive coverage or automatic shrinking.

Application sources are independent of the private parallel implementation.
Public Temper is pinned to `118bb6f62354301cd76e2cd7c54ce0c97058a738` and verified
byte-for-byte. [Provenance](docs/PROVENANCE.md), [Rust notices](docs/THIRD_PARTY_NOTICES.txt)
and dependency lockfiles are retained. Python dependencies carry their own notices
in their distributions. This is not a claimed formal clean-room process.
