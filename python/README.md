# Synth Code

A local coding agent with a durable Rust/Temper runtime and a Python terminal UI.
The public v0.01 release uses package version **0.0.1** and Apache-2.0.

Release quickstart for macOS Apple Silicon, qualified on macOS 15 and 26.
Python 3.12.16+ (or another security-patched Python) and uv 0.13.4+ are required.
The wheel bundles the runtime; no Rust compiler is needed.

```sh
uv tool install synth-code==0.0.1
synth-code --version
synth-code --workspace /absolute/path/to/project --env-file /absolute/path/to/provider.env
```

The selected file contains one `REFLECTION_API_KEY` binding. Beam 1.0 runs through
Reflection's first-party API. Provider access and charges belong to that separate
account. Optional Synth identity uses `synth-code login`, `auth status --json`,
`account limits --json` and `logout`; Synth sign-in does not provide model credits.

Submit a task, inspect the edit diff or command, and choose Allow or Deny.
Tab/Shift+Tab navigate; Ctrl+C interrupts; Ctrl+Q detaches. Use `--list-sessions`
and `--resume UUID` to return. `--no-color` enables monochrome presentation.

Approval authorizes host execution, not an OS sandbox. Interrupted effects can
remain unknown and require inspection and explicit reconciliation. Context and
storage are bounded; there is no automatic compaction or history deletion.
Linux source tests are qualified; Linux and Windows wheels are not distributed.
Index remains cloud-only. Horst/Hengist is reserved for v0.03.

[Installation, upgrade and safety](https://github.com/synth-laboratories/synth-code/blob/main/docs/INSTALL.md) ·
[Source](https://github.com/synth-laboratories/synth-code) ·
[Security](https://github.com/synth-laboratories/synth-code/blob/main/SECURITY.md)

These release commands become available at immutable registry publication.
Candidate wheels can be installed directly before that gate; current release
status and evidence are tracked in the repository's qualification document.
