# Install and start Synth Code v0.01

The registry version is **0.0.1**. Check [official releases](https://github.com/synth-laboratories/synth-code/releases)
for availability, qualified artifacts and exact checksums. Do not install a
similarly named unverified package. The wheel targets macOS ARM64 on macOS 15
and 26; Intel macOS, Linux and Windows distributions are unsupported.
Python 3.12.16+ (or another security-patched Python) and uv 0.13.4+ are needed; installing a wheel requires no Rust compiler.

Stable registry commands, once the selected release is published:

```sh
uv tool install --no-build 'synth-code==0.0.1'
uvx --no-build --from 'synth-code==0.0.1' synth-code --version
```

For a downloaded, verified release wheel:

```sh
uv tool install --from /absolute/path/to/synth_code-0.0.1-py3-none-macosx_11_0_arm64.whl synth-code
synth-code --version
synth-code --workspace /absolute/path/to/project --env-file /absolute/path/to/provider.env
```

The provider file contains your separate `REFLECTION_API_KEY`. It is read by the
Rust daemon and is not written into the journal or passed to command children.
The default model is Beam 1.0 through Reflection's first-party API. Its account
must have actual access; Synth login does not provide that access or credits.

Optional Synth identity uses the existing Clerk-backed account journey:

```sh
synth-code login
synth-code auth status --json
synth-code account limits --json
synth-code logout
```

The browser can sign in to an existing Synth account or open its signup route.
Account creation/terms and consent remain explicit user actions. The requested
CLI grant is limited to account read, organization membership and a refreshable
session. Device login is not advertised. `login --no-browser` retains a private
login URL file for a browser on the same machine; it does not implement a remote
headless callback. Model credentials remain separate.

Submit a task, inspect the edit diff/command, and choose Allow or Deny. Tab and
Shift+Tab navigate. Alt+Enter inserts a newline. Ctrl+C interrupts an active turn;
Ctrl+Q detaches. The daemon continues after detach. Save the session UUID from the
transcript, or find it with `synth-code --list-sessions`, then `--resume UUID`.

Default state is `$XDG_STATE_HOME/synth-code/v1` (otherwise
`~/.local/state/synth-code/v1`); the runtime socket is under
`~/.cache/synth-code/run`. `--store` and `--runtime-dir` select explicit private
locations. `--status` reads a live daemon and `--stop` shuts it down. Back up an
inactive store including its artifact directory; do not copy only the SQLite
file while its owner is running.

Before upgrading, finish or interrupt active work and stop the daemon. Install
only the selected stable wheel/version. An existing client refuses native-byte
mismatch instead of silently replacing a live owner. Reinstalling the same bytes
preserves the v1 store. Downgrade to older development builds is not supported;
future release migrations must explicitly qualify forward/rollback behavior.
Nightly packages will be opt-in prereleases, never an automatic stable update.

If a command/edit is unknown, inspect actual workspace state, retain the receipt,
then use `/resolve UUID observation` to record the reconciliation. No command is
repeated by that action. Context/storage-cap errors require a new session or an
explicit offline archival decision; never delete an active journal.

Publication gate: exact registry install/upgrade/rollback and uvx commands must be
verified after immutable upload. A local-wheel install is not proof of a PyPI or
public installation-page launch. See [qualification](QUALIFICATION.md).
