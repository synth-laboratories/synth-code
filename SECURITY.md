# Security and local authority

Do not include credentials, private journals or provider continuation in public
issues. Report vulnerabilities privately to **josh@usesynth.ai**, with the version,
platform, reproduction and affected boundary. The initial release is maintained
on its latest stable patch; there is no older-version support promise.

The selected workspace is the boundary for built-in file read/search/edit tools.
Paths are descriptor-relative, reject traversal and symlinks, and require regular
single-link text files. Edits bind exact preimage, parent identity and replacement
to authorization, stage and sync data, recheck, then atomically install. This is not an
external-writer compare-and-swap guarantee. A crash may leave a hidden staging
file; inspect it before deleting it. No automatic staging or history deletion runs.

By default, commands require approval of the exact command, timeout and output
bound. `/permissions auto-edit` auto-authorizes file edits, while
`/permissions yolo` auto-authorizes file edits and commands. Modes are durable
and session-scoped; changing them requires an idle session with no pending or
uncertain effect. All modes retain request validation, workspace/file checks,
command limits and no-repeat recovery. A stale interactive client must refresh
a changed mode before submitting a new effect or turn.

Commands
run `/bin/sh` as the current OS user, with a held workspace directory, a clean
PATH/LANG environment, null input, CPU/file/descriptor limits and an owned process
group. They are **not sandboxed**: approved commands can read/write outside the
workspace, use the network or explicitly read credentials. A descendant which
creates a new session may escape process-group cleanup. Do not approve commands
you do not trust. Provider/account environment credentials are not inherited.

One daemon holds a private store lock. Journal commits precede effect dispatch
acknowledgment and terminal results. Recovery fences workers with a new owner
epoch; an uncertain write/command blocks overlapping workspace work. Explicit
reconciliation records a user's observation without claiming success or rerunning
an effect. This coordinates one store; separate stores and external editors are
not mutually excluded by an OS workspace lock.

The store, socket and account files must be private and owned. Model history,
outputs and provider continuation are plaintext local data protected by filesystem
permissions, not application encryption. Content-addressed artifacts are hash
verified. Token values are not journaled; provider configuration retains only its
credential binding. There is no remote sync or automatic telemetry upload.

Reflection requests use its selected first-party HTTPS origin, no redirects, and
bounded streaming. Local HTTP endpoints are supported only on loopback for test
fixtures. Tool execution waits for complete validated model calls. Partial streams
and missing outcomes cannot authorize partial tool arguments. Required provider
reasoning is private continuation, not visible transcript/debug output.

Synth account login reuses `synth-ai==0.23.9` discovery, PKCE/state, origin/resource
checks, principal verification, refresh and revocation. Synth Code requests
`offline_access user:org:read account:read` and stores this separate scoped grant
under `$XDG_CONFIG_HOME/synth-code/account` (default `~/.config/synth-code/account`).
`SYNTH_CONFIG_DIR` can explicitly select another private directory. Only file
storage is supported. Logout revokes that stored grant and clears it; inherited
API keys are not removed or revoked by logout. No new account terms are accepted
automatically during qualification.

Stable and nightly are explicit channels. Publisher credentials must never be
available to PR jobs. Promotion uses a qualified content-addressed cohort; a build
or source merge alone is not a shipping signoff.
