mod common;
use chrono::Utc;
use serde_json::json;
use std::{os::unix::fs::MetadataExt, sync::Arc};
use synth_code_runtime::{
    Runtime,
    domain::{Command, Mutation},
    store::SqliteStore,
};
use temper_runtime::persistence::*;
use uuid::Uuid;

fn event(sequence: u64) -> PersistenceEnvelope {
    let id = Uuid::new_v4();
    PersistenceEnvelope {
        sequence_nr: sequence,
        event_type: "test".into(),
        payload: json!({"value":sequence}),
        metadata: EventMetadata {
            event_id: id,
            causation_id: id,
            correlation_id: id,
            timestamp: Utc::now(),
            actor_id: "test".into(),
        },
    }
}
fn start(workspace: &std::path::Path) -> Mutation {
    let m = workspace.metadata().unwrap();
    Mutation {
        id: Uuid::new_v4(),
        command: Command::Start {
            session: Uuid::new_v4(),
            workspace: workspace.to_str().unwrap().into(),
            device: m.dev(),
            inode: m.ino(),
        },
    }
}
async fn session(runtime: &Runtime, workspace: &std::path::Path) -> Uuid {
    let m = start(workspace);
    let Command::Start { session, .. } = m.command else {
        unreachable!()
    };
    runtime.mutate(m).await.unwrap();
    session
}
const WITNESS: &str = "witness.txt";
/// An approved edit that creates `witness.txt`: an independent, observable effect.
async fn admit(runtime: &Runtime, session: Uuid) -> Uuid {
    let applied = runtime
        .propose_edit(
            Uuid::new_v4(),
            session,
            WITNESS,
            None,
            "independent witness".into(),
        )
        .await
        .unwrap();
    applied.result["id"].as_str().unwrap().parse().unwrap()
}
async fn digest(runtime: &Runtime, operation: Uuid) -> String {
    runtime
        .describe(operation)
        .await
        .unwrap()
        .operation
        .request_digest
}
async fn approve(runtime: &Runtime, operation: Uuid, allow: bool) -> Mutation {
    let m = Mutation {
        id: Uuid::new_v4(),
        command: Command::Approve {
            operation,
            request_digest: digest(runtime, operation).await,
            allow,
        },
    };
    runtime.mutate(m.clone()).await.unwrap();
    m
}

#[tokio::test]
async fn batch_rolls_back_every_stream_and_sequence_on_conflict() {
    let dir = common::tempdir().unwrap();
    let store = SqliteStore::open(dir.path()).unwrap();
    store.append("b", 0, &[event(1)]).await.unwrap();
    assert!(
        store
            .append_batch(&[
                PersistenceAppend {
                    persistence_id: "a".into(),
                    expected_sequence: 0,
                    events: vec![event(1)]
                },
                PersistenceAppend {
                    persistence_id: "b".into(),
                    expected_sequence: 0,
                    events: vec![event(1)]
                },
            ])
            .await
            .is_err()
    );
    assert!(store.read_events("a", 0).await.unwrap().is_empty());
    assert_eq!(store.read_events("b", 0).await.unwrap().len(), 1);
    assert!(store.append("a", 0, &[event(2)]).await.is_err());
    assert!(store.read_events("a", 0).await.unwrap().is_empty());
    assert!(store.load_snapshot("a").await.is_err());
    assert!(
        store
            .lookup_by_key("local", "Session", "name", "x")
            .await
            .is_err()
    );
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn competing_appends_have_exactly_one_winner() {
    let dir = common::tempdir().unwrap();
    let store = Arc::new(SqliteStore::open(dir.path()).unwrap());
    let mut jobs = Vec::new();
    for _ in 0..16 {
        let store = store.clone();
        jobs.push(tokio::spawn(async move {
            store.append("race", 0, &[event(1)]).await.is_ok()
        }));
    }
    let mut winners = 0;
    for j in jobs {
        winners += usize::from(j.await.unwrap());
    }
    assert_eq!(winners, 1);
}

#[tokio::test]
async fn corruption_and_missing_tail_refuse_replay() {
    for tamper in ["UPDATE events SET body='{}'", "DELETE FROM events"] {
        let dir = common::tempdir().unwrap();
        let store = SqliteStore::open(dir.path()).unwrap();
        store.append("stream", 0, &[event(1)]).await.unwrap();
        drop(store);
        let db = rusqlite::Connection::open(dir.path().join("journal.sqlite")).unwrap();
        db.execute(tamper, []).unwrap();
        drop(db);
        let store = SqliteStore::open(dir.path()).unwrap();
        assert!(store.read_events("stream", 0).await.is_err());
    }
}

#[tokio::test]
async fn ownership_and_future_schema_fail_closed() {
    let dir = common::tempdir().unwrap();
    let store = SqliteStore::open(dir.path()).unwrap();
    assert!(SqliteStore::open(dir.path()).is_err());
    drop(store);
    let db = rusqlite::Connection::open(dir.path().join("journal.sqlite")).unwrap();
    db.pragma_update(None, "user_version", 99).unwrap();
    drop(db);
    assert!(SqliteStore::open(dir.path()).is_err());
}

#[tokio::test]
async fn mutation_replay_survives_restart_and_changed_payload_refuses() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let m = start(workspace.path());
    let first = runtime.mutate(m.clone()).await.unwrap();
    assert!(!first.replayed);
    assert_eq!(
        runtime.mutate(m.clone()).await.unwrap().result,
        first.result
    );
    let mut changed = m.clone();
    if let Command::Start { workspace, .. } = &mut changed.command {
        *workspace = "/changed".into();
    }
    assert!(
        runtime
            .mutate(changed)
            .await
            .unwrap_err()
            .to_string()
            .contains("mutation_conflict")
    );
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    let replay = runtime.mutate(m).await.unwrap();
    assert!(replay.replayed);
    assert_eq!(replay.result, first.result);
    runtime.close().await;
}

#[tokio::test]
async fn pending_approval_survives_and_denial_cannot_dispatch() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let s = session(&runtime, workspace.path()).await;
    let op = admit(&runtime, s).await;
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    assert_eq!(
        runtime.read(Some(s)).await.unwrap()["operations"][0]["status"],
        "awaiting_approval"
    );
    let denied = approve(&runtime, op, false).await;
    assert_eq!(
        runtime.mutate(denied).await.unwrap().result["approved"],
        false
    );
    runtime.run_operation(op, false).await.unwrap();
    assert_eq!(std::fs::read_dir(workspace.path()).unwrap().count(), 0);
    assert!(
        runtime
            .mutate(Mutation {
                id: Uuid::new_v4(),
                command: Command::Dispatch {
                    operation: op,
                    epoch: 2
                }
            })
            .await
            .is_err()
    );
    runtime.close().await;
}

#[tokio::test]
async fn crash_boundary_becomes_unknown_and_stale_settlement_is_rejected() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let s = session(&runtime, workspace.path()).await;
    let op = admit(&runtime, s).await;
    approve(&runtime, op, true).await;
    runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::Dispatch {
                operation: op,
                epoch: 1,
            },
        })
        .await
        .unwrap();
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    assert_eq!(
        runtime.read(Some(s)).await.unwrap()["operations"][0]["status"],
        "unknown"
    );
    runtime.run_operation(op, false).await.unwrap();
    assert_eq!(std::fs::read_dir(workspace.path()).unwrap().count(), 0);
    assert!(
        runtime
            .mutate(Mutation {
                id: Uuid::new_v4(),
                command: Command::Settle {
                    operation: op,
                    epoch: 1,
                    success: true,
                    outcome: json!({})
                }
            })
            .await
            .is_err()
    );
    let result = runtime
        .propose_edit(Uuid::new_v4(), s, "repeat.txt", None, "repeat".into())
        .await;
    assert!(
        result
            .unwrap_err()
            .to_string()
            .contains("workspace_busy_or_uncertain")
    );
    runtime.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn concurrent_dispatch_runs_once() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let runtime = Arc::new(Runtime::open(store.path()).await.unwrap());
    let s = session(&runtime, workspace.path()).await;
    let op = admit(&runtime, s).await;
    approve(&runtime, op, true).await;
    let mut jobs = Vec::new();
    for _ in 0..16 {
        let runtime = runtime.clone();
        jobs.push(tokio::spawn(async move {
            runtime.run_operation(op, false).await.unwrap();
        }));
    }
    for j in jobs {
        j.await.unwrap();
    }
    assert_eq!(std::fs::read_dir(workspace.path()).unwrap().count(), 1);
    assert_eq!(
        std::fs::read_to_string(workspace.path().join(WITNESS)).unwrap(),
        "independent witness"
    );
    assert_eq!(
        runtime.read(Some(s)).await.unwrap()["operations"][0]["status"],
        "succeeded"
    );
    Arc::try_unwrap(runtime).ok().unwrap().close().await;
}

#[tokio::test]
async fn replaced_workspace_is_rejected_before_writing() {
    let store = common::tempdir().unwrap();
    let parent = common::tempdir().unwrap();
    let workspace = parent.path().join("workspace");
    std::fs::create_dir(&workspace).unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let s = session(&runtime, &workspace).await;
    let op = admit(&runtime, s).await;
    approve(&runtime, op, true).await;
    std::fs::rename(&workspace, parent.path().join("old")).unwrap();
    std::fs::create_dir(&workspace).unwrap();
    runtime.run_operation(op, false).await.unwrap();
    assert_eq!(std::fs::read_dir(&workspace).unwrap().count(), 0);
    assert_eq!(
        runtime.read(Some(s)).await.unwrap()["operations"][0]["status"],
        "failed"
    );
    runtime.close().await;
}

#[tokio::test]
async fn failed_commit_never_acknowledges_approval_or_dispatches_effect() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let session = session(&runtime, workspace.path()).await;
    let op = admit(&runtime, session).await;
    let db = rusqlite::Connection::open(store.path().join("journal.sqlite")).unwrap();
    db.execute_batch("CREATE TRIGGER fail_commit BEFORE INSERT ON events BEGIN SELECT RAISE(ABORT,'injected I/O refusal'); END;").unwrap();
    let approval = Mutation {
        id: Uuid::new_v4(),
        command: Command::Approve {
            operation: op,
            request_digest: digest(&runtime, op).await,
            allow: true,
        },
    };
    assert!(runtime.mutate(approval).await.is_err());
    let result = runtime.run_operation(op, false).await.unwrap();
    assert_eq!(result["operations"][0]["status"], "awaiting_approval");
    assert_eq!(std::fs::read_dir(workspace.path()).unwrap().count(), 0);
    assert_eq!(
        runtime.describe(op).await.unwrap().operation.status,
        synth_code_runtime::domain::Status::AwaitingApproval
    );
    db.execute_batch("DROP TRIGGER fail_commit").unwrap();
    drop(db);
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    assert_eq!(
        runtime.describe(op).await.unwrap().operation.status,
        synth_code_runtime::domain::Status::AwaitingApproval
    );
    runtime.close().await;
}
#[test]
fn store_refuses_public_directory_and_symlink_lock() {
    use std::os::unix::fs::{PermissionsExt, symlink};
    let directory = common::tempdir().unwrap();
    std::fs::set_permissions(directory.path(), std::fs::Permissions::from_mode(0o755)).unwrap();
    assert!(SqliteStore::open(directory.path()).is_err());
    std::fs::set_permissions(directory.path(), std::fs::Permissions::from_mode(0o700)).unwrap();
    let outside = common::tempdir().unwrap();
    std::fs::write(outside.path().join("file"), "untouched").unwrap();
    symlink(
        outside.path().join("file"),
        directory.path().join("owner.lock"),
    )
    .unwrap();
    assert!(SqliteStore::open(directory.path()).is_err());
    assert_eq!(
        std::fs::read_to_string(outside.path().join("file")).unwrap(),
        "untouched"
    );
}
