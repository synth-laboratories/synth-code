# Foundation contract

The selected embedded Temper ActorSystem owns one coordinator. Its pure command
reducer admits sessions, fixture and file-edit operations, exact approval decisions, dispatch
and settlement. Delta events and identity-bound mutation receipts co-commit in a
single SQLite append before a reply. Duplicate mutation IDs return the original
receipt only when their command fingerprint matches.

The SQLite adapter holds a single process owner lock, uses WAL/synchronous FULL,
checks expected sequences, and rolls multi-stream appends back atomically. Journal
reads validate contiguous positions, envelope hashes and stored heads. Snapshots,
entity queries and index operations refuse explicitly. The adapter has finite
limits: 64 KiB/event, 100,000 events/stream; the coordinator admits at most 256
sessions, 4,096 operations and 32,768 mutation receipts. These are prototype bounds,
not a retention policy or a tamper-proof store.

Startup replays and validates events, increments the persisted owner epoch and
marks dispatched unsettled operations unknown. Pending approval survives; settled
receipts remain settled. A stale epoch cannot dispatch or settle. One unresolved
operation excludes other work in overlapping canonical workspace trees within
this store. There is no cross-store ownership fence yet.

The fixture executor writes a uniquely named synthetic witness. File tools provide
bounded text read/literal search and approved create/replace. All pin the workspace
device/inode and traverse relative components with held directory descriptors and
O_NOFOLLOW. Symlinks, hardlinked files, nonregular/binary/large files and selected
credential/generated paths refuse. Search skips refused entries and reports bounds;
it does not implement gitignore, regular expressions or comprehensive secret detection.

An edit admission retains the exact path, parent device/inode, preimage/version and
replacement bytes. Its fingerprint binds approval to all those values. Execution
stages in the same directory, syncs, rechecks the approved version and installs via
atomic rename (replace) or exclusive link (create), then syncs the parent. File mode
is preserved for replacement; new files use 0600. Reads are limited to 12 KiB, edits
to 8 KiB and 24 KiB encoded requests; paths have at most 16 components. Search caps
entries, examined text bytes, matches and output lines with explicit truncation.

Repeated dispatch identities never enter an executor twice. Failed preconditions
settle failure without changing the target; failures after installation settle unknown.
A crash between replacement and settlement also remains unknown. Recovery never
automatically reapplies that edit. A killed create can retain an operation-specific
hidden staging link; it is excluded from search and requires later owned reconciliation,
not deletion as generic scratch. This is a bounded local prototype, not adversarial
OS isolation: POSIX rename cannot compare-and-swap against uncooperative external
writers, and held descriptors pin objects even if another process relocates them.
There is no cross-store workspace exclusion. Shell processes and a sandbox are pending.

The development protocol is bounded line-delimited JSON-RPC over stdio:
initialize, thread/start/list/read, synth/status, workspace/read/search/proposeEdit,
synth/operation/read, fixture/admit and synth/approval/respond. It is not a claim of Codex App Server compatibility. Internal
dispatch/settlement are Rust calls, not public methods. Durable mutations require
mutationId. Thread and operation IDs derive from their admission mutation UUIDs.
Only complete command results are exposed; live streaming/replay are pending.
History pages sort by journal admission sequence and cap items/encoded bytes. A
subsequent page must supply the original sequence revision; intervening mutations
refuse the stale cursor. Session catalogs are bounded and paginated. Operation-specific
replies avoid serializing growing history, and oversized protocol responses return a
typed error rather than breaking the client's frame bound. The diagnostic synth/status
full dump is still size-limited and is not a production catalog API.

Python owns presentation and transport only. It never executes the synthetic
operation. Transport failure poisons the connection and never causes automatic
mutation retries. The UI retains the identity of an uncertain approval decision.
The current child-process lifecycle does not provide daemon detachment. The UI can
list saved sessions and resume a selected history/pending approval. It shows a bounded
diff and escapes control/bidi characters in untrusted display text. Receipt identity is
retained after an uncertain approval reply. Full current-state history remains in Rust.

Tests cover owner exclusion, atomic rollback, competing CAS appends, corrupted or
truncated journals, future schemas, stable receipt replay, approval/denial,
concurrent dispatch, workspace replacement, unknown/stale settlement, abrupt process
exit in a transaction and after an effect. Python tests exercise the real runtime
and a real PTY at 80×24 with resize to 40×12, approval, denial and cleanup. These
fixtures do not qualify live provider behavior, terminal portability or a release.
The initial native Bombadil gate uses the real PTY driver, a pinned official binary,
finite terminal properties and mandatory action milestones, then independently checks
the Rust journal and target file. A deliberately incorrect file expectation verifies
the gate returns failure rather than accepting terminal pass text alone.
