# Foundation contract

The selected embedded Temper ActorSystem owns one coordinator. Its pure command
reducer admits sessions, synthetic operations, exact approval decisions, dispatch
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

The only executor writes a uniquely named synthetic witness. It pins the workspace
device/inode, uses a held directory descriptor and exclusive descriptor-relative
creation, syncs the result, then settles. Repeated dispatch identities never enter
the executor. A crash between effect and settlement remains unknown. General file
edits, shell processes and a sandbox are not implemented or qualified.

The development protocol is bounded line-delimited JSON-RPC over stdio:
initialize, thread/start, thread/read, synth/status, fixture/admit and
synth/approval/respond. It is not a claim of Codex App Server compatibility. Internal
dispatch/settlement are Rust calls, not public methods. Durable mutations require
mutationId. Thread and operation IDs derive from their admission mutation UUIDs.
Only complete command results are exposed; live streaming/replay and pagination
are pending. Large catalog replies are not a supported production interface.

Python owns presentation and transport only. It never executes the synthetic
operation. Transport failure poisons the connection and never causes automatic
mutation retries. The UI retains the identity of an uncertain approval decision.
The current child-process lifecycle does not provide daemon detachment.

Tests cover owner exclusion, atomic rollback, competing CAS appends, corrupted or
truncated journals, future schemas, stable receipt replay, approval/denial,
concurrent dispatch, workspace replacement, unknown/stale settlement, abrupt process
exit in a transaction and after an effect. Python tests exercise the real runtime
and a real PTY at 80×24 with resize to 40×12, approval, denial and cleanup. These
fixtures do not qualify live provider behavior, terminal portability or a release.
