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
- No fixture mode and no direct `/read`, `/search`, `/edit` or `/create`
  commands: the agent does file work through approved tools. Development stores
  that contain fixture events from earlier candidates are unsupported; start a
  new store.
- Keys: ctrl-c interrupts the turn or clears the input and never exits; ctrl-d
  (empty input) or ctrl-q exits; esc closes completions or jumps to the latest
  output; pgup/pgdn, shift-up/down and the mouse wheel scroll, and new output
  follows only while you are at the bottom. The terminal title shows activity,
  the session and the workspace (`--no-title` turns it off). The footer shows
  context used against the compaction point.
- Tests, journeys and the provider smoke use a pinned real repository
  (tomli 2.2.1) instead of a toy file; the provider smoke is judged by the
  project's own test suite.
- The transcript is a list of typed items. Edits and commands are inline cards
  (heading, short diff or output excerpt, failures and unknown outcomes opened
  and highlighted); click a card or press ctrl-o to expand. Agent replies render
  Markdown with lists, highlighted code and width-aware tables (Rich; adapted
  from Reflection Mirror). Each item renders once per width.
- Bottom composer, optional operation pane, response copying, session timing and
  token totals. Idle clients observe peer/API-started work without submitting a task.
- Discoverable slash commands for exit, status, provider, model, effort and
  session permissions (`manual`, `auto-edit`, `yolo`). Manual is the default.
  Settings open an inline picker above the input: type to filter, ↑/↓ to move,
  enter to select, esc to cancel; the draft is kept. `/resume` picks a saved
  session (labelled by its first task, workspace and turn count) and opens it
  without starting a turn; `/resume SESSION` opens one directly.
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
