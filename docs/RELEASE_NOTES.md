# Synth Code v0.01 candidate (0.0.1)

A focused local coding agent: Rust/Temper owns the durable session, provider calls
and approved workspace effects; Python provides the terminal UI. The application
uses Apache-2.0 and a pinned public Temper dependency. Index stays cloud-only.

- Beam 1.0 through Reflection's first-party API, with explicit
  credential binding, optional token usage and no automatic provider retries.
- No default turn request-count limit. Explicit fixture/provider-profile caps,
  per-command time/output limits and journal/context bounds remain enforced.
- Workspace read/search, exact approved edits and supervised host commands.
  Approval denial, interruption and reconnect do not repeat uncertain effects.
- Private local journal, session recovery, explicit reconciliation, pending
  approval recovery, command output pages, resize and monochrome presentation.
- Bottom composer, optional operation pane, response copying, session timing and
  token totals. Idle clients observe peer/API-started work without submitting a task.
- Discoverable slash commands for exit, status, provider, model, effort and
  session permissions (`manual`, `auto-edit`, `yolo`). Manual is the default.
- Host/tool discovery and read-only Docker inventory, including stopped
  containers. Host commands can use explicitly selected external projects;
  inventory does not acquire ownership or change container lifecycle.
- Optional Synth browser sign-in and read-only account entitlements through the
  published SDK. Synth identity and Reflection access/billing remain separate.
- Bundled macOS Apple Silicon runtime installed through uv without compilation.
  Explicit stable/prerelease selection, native integrity checks and live-owner
  mismatch refusal. No in-app updater.

Candidate qualification and supported platforms are recorded in
[QUALIFICATION.md](QUALIFICATION.md). This is a preparation release, not a public
registry availability claim. Linux source qualification does not imply a Linux
wheel. Windows is unsupported.

Commands execute on the host with the user's approved authority; this is not an
OS sandbox. Context and storage are bounded. Long conversations compact
automatically before the request limit, and `/compact` compacts on demand; the
journal keeps the full history. Unknown operations require inspection and explicit resolution.
There is no plugin system, cloud sync, distributed execution or research loop in
this version. Horst/Hengist is reserved for v0.03.
