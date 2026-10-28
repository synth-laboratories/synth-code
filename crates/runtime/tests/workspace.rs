mod common;
use std::{
    os::unix::fs::{MetadataExt, PermissionsExt, symlink},
    path::Path,
};
use synth_code_runtime::{
    Runtime,
    domain::{Command, Mutation, Session},
    workspace::{MAX_EDIT, MAX_READ, Workspace},
};
use uuid::Uuid;

fn session(path: &Path) -> Session {
    let m = path.metadata().unwrap();
    Session {
        id: Uuid::new_v4(),
        workspace: path.to_str().unwrap().into(),
        device: m.dev(),
        inode: m.ino(),
    }
}
async fn start(runtime: &Runtime, path: &Path) -> Uuid {
    let s = session(path);
    runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::Start {
                session: s.id,
                workspace: s.workspace,
                device: s.device,
                inode: s.inode,
            },
        })
        .await
        .unwrap();
    s.id
}
async fn decide(runtime: &Runtime, id: Uuid, hash: &str, allow: bool) {
    runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::Approve {
                operation: id,
                request_digest: hash.into(),
                allow,
            },
        })
        .await
        .unwrap();
}

#[test]
fn reads_and_search_are_scoped_bounded_and_repeatable() {
    let dir = common::tempdir().unwrap();
    std::fs::create_dir(dir.path().join("src")).unwrap();
    std::fs::write(dir.path().join("src/a.py"), "first\nneedle café\n").unwrap();
    std::fs::write(dir.path().join(".env.local"), "needle SECRET").unwrap();
    std::fs::create_dir(dir.path().join("target")).unwrap();
    std::fs::write(dir.path().join("target/out"), "needle generated").unwrap();
    let outside = common::tempdir().unwrap();
    std::fs::write(outside.path().join("secret"), "needle outside").unwrap();
    symlink(outside.path(), dir.path().join("escape")).unwrap();
    symlink(outside.path().join("secret"), dir.path().join("link")).unwrap();
    std::fs::hard_link(outside.path().join("secret"), dir.path().join("hardlink")).unwrap();
    let ws = Workspace::open(&session(dir.path())).unwrap();
    let read = ws.read("src/a.py").unwrap();
    assert_eq!(read.text, "first\nneedle café\n");
    for path in [
        "../secret",
        "/etc/passwd",
        "src/../a.py",
        "src//a.py",
        ".env.local",
        "escape/secret",
        "link",
        "hardlink",
        "target/out",
    ] {
        assert!(ws.read(path).is_err(), "{path}");
    }
    for _ in 0..2 {
        let result = ws.search("needle").unwrap();
        assert_eq!(result.matches.len(), 1);
        assert_eq!(result.matches[0].path, "src/a.py");
        assert_eq!(result.matches[0].line, 2);
        assert!(result.skipped >= 4);
    }
    std::fs::write(dir.path().join("big"), vec![b'x'; MAX_READ + 1]).unwrap();
    assert!(ws.read("big").is_err());
    assert!(ws.search("").is_err());
}

#[test]
fn atomic_replacement_preserves_mode_and_refuses_changed_preimage() {
    let dir = common::tempdir().unwrap();
    let path = dir.path().join("run.sh");
    std::fs::write(&path, "before").unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755)).unwrap();
    let ws = Workspace::open(&session(dir.path())).unwrap();
    let before = ws.read("run.sh").unwrap();
    let edit = ws
        .prepare_edit("run.sh", Some(&before.version.sha256), "after".into())
        .unwrap();
    std::fs::write(&path, "external writer").unwrap();
    let failure = ws.apply_edit(Uuid::new_v4(), &edit, false).unwrap_err();
    assert!(!failure.uncertain);
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "external writer");
    let before = ws.read("run.sh").unwrap();
    let edit = ws
        .prepare_edit("run.sh", Some(&before.version.sha256), "approved".into())
        .unwrap();
    ws.apply_edit(Uuid::new_v4(), &edit, false).unwrap();
    assert_eq!(std::fs::read_to_string(&path).unwrap(), "approved");
    assert_eq!(path.metadata().unwrap().permissions().mode() & 0o777, 0o755);
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 1);
}

#[test]
fn create_never_overwrites_existing_entry_and_parent_swap_refuses() {
    let dir = common::tempdir().unwrap();
    std::fs::create_dir(dir.path().join("src")).unwrap();
    let ws = Workspace::open(&session(dir.path())).unwrap();
    let edit = ws.prepare_edit("src/new.txt", None, "new".into()).unwrap();
    std::fs::write(dir.path().join("src/new.txt"), "foreign").unwrap();
    assert!(ws.apply_edit(Uuid::new_v4(), &edit, false).is_err());
    assert_eq!(
        std::fs::read_to_string(dir.path().join("src/new.txt")).unwrap(),
        "foreign"
    );
    let edit = ws
        .prepare_edit("src/other.txt", None, "new".into())
        .unwrap();
    std::fs::rename(dir.path().join("src"), dir.path().join("old")).unwrap();
    std::fs::create_dir(dir.path().join("src")).unwrap();
    assert!(ws.apply_edit(Uuid::new_v4(), &edit, false).is_err());
    assert!(!dir.path().join("src/other.txt").exists());
    assert!(
        ws.prepare_edit("huge", None, "x".repeat(MAX_EDIT + 1))
            .is_err()
    );
}

#[test]
fn matching_content_on_replaced_inode_and_symlink_are_not_approved() {
    let dir = common::tempdir().unwrap();
    let path = dir.path().join("a");
    std::fs::write(&path, "old").unwrap();
    let ws = Workspace::open(&session(dir.path())).unwrap();
    let before = ws.read("a").unwrap();
    let edit = ws
        .prepare_edit("a", Some(&before.version.sha256), "new".into())
        .unwrap();
    std::fs::rename(&path, dir.path().join("old")).unwrap();
    std::fs::write(&path, "old").unwrap();
    assert!(ws.apply_edit(Uuid::new_v4(), &edit, false).is_err());
    std::fs::remove_file(&path).unwrap();
    symlink(dir.path().join("old"), &path).unwrap();
    assert!(ws.apply_edit(Uuid::new_v4(), &edit, false).is_err());
    assert_eq!(
        std::fs::read_to_string(dir.path().join("old")).unwrap(),
        "old"
    );
}

#[tokio::test]
async fn exact_edit_approval_denial_replay_and_recovery() {
    let dir = common::tempdir().unwrap();
    let store = common::tempdir().unwrap();
    std::fs::write(dir.path().join("a"), "old").unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let session = start(&runtime, dir.path()).await;
    let version = runtime.read_file(session, "a").await.unwrap().version;
    let mutation = Uuid::new_v4();
    let proposed = runtime
        .propose_edit(mutation, session, "a", Some(&version.sha256), "new".into())
        .await
        .unwrap();
    let id = proposed.result["id"].as_str().unwrap().parse().unwrap();
    assert!(
        runtime
            .mutate(Mutation {
                id: Uuid::new_v4(),
                command: Command::Approve {
                    operation: id,
                    request_digest: "wrong".into(),
                    allow: true
                }
            })
            .await
            .is_err()
    );
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    assert_eq!(
        runtime.describe(id).await.unwrap().operation.status,
        synth_code_runtime::domain::Status::AwaitingApproval
    );
    decide(
        &runtime,
        id,
        proposed.result["request_digest"].as_str().unwrap(),
        true,
    )
    .await;
    runtime.run_operation(id, false).await.unwrap();
    assert_eq!(
        std::fs::read_to_string(dir.path().join("a")).unwrap(),
        "new"
    );
    let inode = dir.path().join("a").metadata().unwrap().ino();
    runtime.run_operation(id, false).await.unwrap();
    assert_eq!(dir.path().join("a").metadata().unwrap().ino(), inode);
    assert!(
        runtime
            .propose_edit(mutation, session, "a", Some(&version.sha256), "new".into())
            .await
            .unwrap()
            .replayed
    );
    assert!(
        runtime
            .propose_edit(
                mutation,
                session,
                "a",
                Some(&version.sha256),
                "different".into()
            )
            .await
            .is_err()
    );
    let denied = runtime
        .propose_edit(Uuid::new_v4(), session, "denied", None, "no".into())
        .await
        .unwrap();
    let id = denied.result["id"].as_str().unwrap().parse().unwrap();
    decide(
        &runtime,
        id,
        denied.result["request_digest"].as_str().unwrap(),
        false,
    )
    .await;
    runtime.run_operation(id, false).await.unwrap();
    assert!(!dir.path().join("denied").exists());
    runtime.close().await;
}

#[tokio::test]
async fn changed_file_after_durable_approval_settles_failure() {
    let dir = common::tempdir().unwrap();
    let store = common::tempdir().unwrap();
    std::fs::write(dir.path().join("a"), "old").unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let session = start(&runtime, dir.path()).await;
    let version = runtime.read_file(session, "a").await.unwrap().version;
    let proposed = runtime
        .propose_edit(
            Uuid::new_v4(),
            session,
            "a",
            Some(&version.sha256),
            "new".into(),
        )
        .await
        .unwrap();
    let id = proposed.result["id"].as_str().unwrap().parse().unwrap();
    decide(
        &runtime,
        id,
        proposed.result["request_digest"].as_str().unwrap(),
        true,
    )
    .await;
    std::fs::write(dir.path().join("a"), "external").unwrap();
    runtime.run_operation(id, false).await.unwrap();
    assert_eq!(
        runtime.describe(id).await.unwrap().operation.status,
        synth_code_runtime::domain::Status::Failed
    );
    assert_eq!(
        std::fs::read_to_string(dir.path().join("a")).unwrap(),
        "external"
    );
    runtime.close().await;
}

#[test]
fn search_caps_matches_and_does_not_hang_on_special_files() {
    let dir = common::tempdir().unwrap();
    std::fs::write(dir.path().join("lines"), "needle\n".repeat(100)).unwrap();
    let fifo = std::ffi::CString::new(dir.path().join("fifo").to_str().unwrap()).unwrap();
    assert_eq!(unsafe { libc::mkfifo(fifo.as_ptr(), 0o600) }, 0);
    let ws = Workspace::open(&session(dir.path())).unwrap();
    assert!(ws.read("fifo").is_err());
    let result = ws.search("needle").unwrap();
    assert_eq!(result.matches.len(), 32);
    assert!(result.truncated);
}

#[tokio::test]
async fn chronological_history_is_paged_and_changed_revision_refuses_cursor() {
    let dir = common::tempdir().unwrap();
    let store = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let session = start(&runtime, dir.path()).await;
    let mut expected = Vec::new();
    for _ in 0..40 {
        let op = Uuid::new_v4();
        let admitted = runtime
            .mutate(Mutation {
                id: Uuid::new_v4(),
                command: Command::Admit {
                    session,
                    operation: op,
                    text: "history".into(),
                },
            })
            .await
            .unwrap();
        expected.push(op.to_string());
        decide(
            &runtime,
            op,
            admitted.result["request_digest"].as_str().unwrap(),
            false,
        )
        .await;
    }
    let first = runtime.history_page(session, 0, None).await.unwrap();
    assert_eq!(first["operations"].as_array().unwrap().len(), 32);
    let cursor = first["nextCursor"].as_u64().unwrap();
    let revision = first["sequence"].as_u64().unwrap();
    let second = runtime
        .history_page(session, cursor, Some(revision))
        .await
        .unwrap();
    assert_eq!(second["operations"].as_array().unwrap().len(), 8);
    assert!(second["nextCursor"].is_null());
    let ids = first["operations"]
        .as_array()
        .unwrap()
        .iter()
        .chain(second["operations"].as_array().unwrap())
        .map(|op| op["id"].as_str().unwrap().to_string())
        .collect::<Vec<_>>();
    assert_eq!(ids, expected);
    assert!(runtime.history_page(session, cursor, None).await.is_err());
    runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::Admit {
                session,
                operation: Uuid::new_v4(),
                text: "new".into(),
            },
        })
        .await
        .unwrap();
    assert!(
        runtime
            .history_page(session, cursor, Some(revision))
            .await
            .is_err()
    );
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    let page = runtime.history_page(session, 0, None).await.unwrap();
    let ids = page["operations"]
        .as_array()
        .unwrap()
        .iter()
        .map(|op| op["id"].as_str().unwrap().to_string())
        .collect::<Vec<_>>();
    assert_eq!(ids, expected[..32]);
    runtime.close().await;
}

#[tokio::test]
async fn catalogs_are_bounded_and_approved_edit_cannot_use_fixture_executor() {
    let dir = common::tempdir().unwrap();
    let store = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let mut ids = Vec::new();
    for _ in 0..18 {
        ids.push(start(&runtime, dir.path()).await.to_string());
    }
    ids.sort();
    let first = runtime.sessions(None).await.unwrap();
    assert_eq!(first["sessions"].as_array().unwrap().len(), 16);
    let second = runtime
        .sessions(Some(first["nextCursor"].as_str().unwrap().parse().unwrap()))
        .await
        .unwrap();
    assert_eq!(second["sessions"].as_array().unwrap().len(), 2);
    assert!(second["nextCursor"].is_null());
    let id = ids[0].parse().unwrap();
    let proposed = runtime
        .propose_edit(Uuid::new_v4(), id, "a", None, "new".into())
        .await
        .unwrap();
    let op = proposed.result["id"].as_str().unwrap().parse().unwrap();
    decide(
        &runtime,
        op,
        proposed.result["request_digest"].as_str().unwrap(),
        true,
    )
    .await;
    assert!(runtime.run_fixture(op, false).await.is_err());
    assert_eq!(std::fs::read_dir(dir.path()).unwrap().count(), 0);
    runtime.run_operation(op, false).await.unwrap();
    assert_eq!(
        std::fs::read_to_string(dir.path().join("a")).unwrap(),
        "new"
    );
    runtime.close().await;
}
