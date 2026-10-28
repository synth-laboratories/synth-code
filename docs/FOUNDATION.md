# Runtime and protocol contract

One embedded Temper coordinator is the durable authority. The pure reducer admits
sessions, model turns and effect operations, exact approval decisions, dispatch,
settlement, interruption and user reconciliation. Events and identity-bound
mutation receipts co-commit in one SQLite append before replies. Identical mutation
IDs return their original receipt; changed command fingerprints refuse.

SQLite uses a private owned directory/file, one process lock, WAL and synchronous
FULL. Expected-sequence appends and multi-stream batches are atomic. Replay checks
contiguous positions, envelope hashes, heads and referenced immutable payload
hashes. Future schema and unsupported store operations refuse explicitly. Current
bounds: 64 KiB/event, 100,000 events/stream, 256 sessions, 4,096 operations, 4,096
turns and 32,768 mutation receipts. The artifact pool is bounded at 512 MiB/16,384
files; individual payloads are at most 8 MiB. This is not a tamper-proof store or
an automatic retention/deletion policy.

Startup validates replay and increments its durable owner epoch. Active model
turns become interrupted, model attempts without results retain unknown usage,
and dispatched effects without terminal receipts become unknown. Manual pending
approvals and approved-but-undispatched operations survive; explicit Resume
checks the original digest and dispatches a ready operation once. There is no
automatic resume on reconnect. operations attached to interrupted turns are canceled before
dispatch. Settled receipts remain settled. Epoch/phase fences refuse late workers.
At most eight active turns and eight dispatched effects are admitted.
Unknown operations block overlapping canonical workspace trees in this store.
Explicit reconciliation retains an observation and prior outcome without repeating
an effect or reporting invented success. Separate stores/external processes are
not covered by a cross-store workspace fence.

File read/search/edit pins root device/inode and traverses relative components
through held directory descriptors and O_NOFOLLOW. Symlinks, hardlinks,
nonregular/binary/large files and selected credential/generated paths refuse.
Read limit: 12 KiB. Edit content limit: 8 KiB; encoded edit: 24 KiB; path depth: 16.
Search bounds entries, bytes examined, matches and output lines. It is literal
search, without gitignore/regular expressions/comprehensive secret detection.

Edits bind path, parent device/inode, exact preimage/version and replacement bytes
to approval. Execution stages and syncs, rechecks, then installs through atomic
rename or exclusive new-file link and syncs the parent. Replacement preserves mode;
new files are 0600. Changed preconditions fail without installing. Post-install
failure/crash becomes unknown. A killed create may retain an operation-specific
hidden stage; it requires owned inspection, not generic scratch deletion. Rename
is not CAS against an uncooperative writer. Held descriptors pin objects even if
another process relocates them.

Commands bind exact shell text, timeout and output cap to approval. A separate
supervisor holds the identity-checked workspace, starts an owned process group,
clears the environment except PATH/LANG, sets resource limits and drains bounded
stdout/stderr. Gateway EOF and timeout trigger group cleanup. The parent reads a
bounded JSON result line and closes its gateway so supervisor shutdown cannot
wait on an inherited stdin read. Dropping a worker closes the gateway; it does
not prematurely kill the supervisor responsible for cleanup. Host execution is
not an OS sandbox; a descendant creating a new session can escape group cleanup.
If command output cannot be retained, the operation becomes unknown and supports
explicit reconciliation without restarting or repeating the command. Manual
dispatch waits for durable admission before replying; capacity refusal preserves
the approved Ready operation for a later explicit Resume.
Full bounded output is immutable; paginated read exposes it independently of the
short outcome preview.

Model requests are durably admitted before network submission. Complete bounded
SSE assembly precedes tool execution. Canonical assistant/tool history is immutable
and private; required provider reasoning never appears in presentation views.
Visible assistant messages have indexed paginated reads. Usage is optional and
only known numeric token fields are projected; observed model IDs are retained
when supplied. Provisional streamed text is a bounded current snapshot, replaced
by a durable final item. Failure never upgrades provisional text to completion.
Read/search results and effect receipts feed one bounded serial tool loop.
There is no automatic model retry, effect replay or context compaction.

JSON-RPC is bounded to 64 KiB line-delimited frames, on stdio or private Unix
sockets. Each connection initializes once. Public methods include
thread/start/list/read, turn/start/read/list/message/read/interrupt,
workspace/read/search/proposeEdit/proposeCommand, synth/approval/respond,
synth/operation/read/output/cancel/reconcile/run, provider/status/checkBinding,
synth/status and synth/shutdown. Internal dispatch/settlement are Rust calls.
The synthetic fixture endpoint is development test support, not model execution.
This is not a claim of Codex App Server compatibility.

History pages sort by admission sequence and require the original sequence on
continuation, refusing intervening mutation. Catalogs and output/message pages
are bounded. Oversized replies return a typed error. The full synth/status dump
is bounded diagnostics, not an unbounded catalog. Python projects current durable
state by polling; there is no promised event-subscription API in this version.

Python owns presentation, account adaptation and transport. It never runs model
requested tools. Transport uncertainty poisons only that connection and cannot
retry a mutation silently. Socket disconnect detaches; stdio compatibility owns
its child. The client checks native-byte identity before attaching to an existing
daemon and never replaces it implicitly. Approval decisions retain identity after
an uncertain reply. Display text escapes controls/bidi. The transcript is bounded
at 100,000 characters; complete persisted messages remain in Rust.

Tests exercise the real runtime and PTYs, including failed commits, ownership,
CAS, corruption, identity replay, denied/canceled operations, precondition races,
crashes, unknown outcomes, stale settlement, EOF/timeout/worker-drop cleanup and
incomplete provider streams. Native Bombadil adds independent journal/file oracles,
required task/approval/detach/resize actions and intentionally failing oracle
checks. Offline fixtures do not qualify live accounts, provider billing or release
channels; see QUALIFICATION.md.
