mod common;
use std::os::unix::fs::MetadataExt;
use synth_code_runtime::{
    Runtime,
    domain::{Command, Mutation, Permissions, Session, Status},
    store::digest,
};
use uuid::Uuid;

async fn start(runtime: &Runtime, path: &std::path::Path) -> Uuid {
    let session = Uuid::new_v4();
    let m = path.metadata().unwrap();
    runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::Start {
                session,
                workspace: path.to_str().unwrap().into(),
                device: m.dev(),
                inode: m.ino(),
            },
        })
        .await
        .unwrap();
    session
}
async fn configure(runtime: &Runtime, session: Uuid, mode: Permissions) -> anyhow::Result<()> {
    let current = runtime.session(session).await?.permissions;
    runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::ConfigurePermissions {
                session,
                mode,
                expected_revision: digest(&serde_json::to_vec(&current)?),
            },
        })
        .await?;
    Ok(())
}

#[tokio::test]
async fn permission_modes_authorize_only_the_selected_effects_and_recover() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let session = start(&runtime, workspace.path()).await;
    for (mode, edit_status, shell_status) in [
        (
            Permissions::Manual,
            Status::AwaitingApproval,
            Status::AwaitingApproval,
        ),
        (
            Permissions::AutoEdit,
            Status::Ready,
            Status::AwaitingApproval,
        ),
        (Permissions::Yolo, Status::Ready, Status::Ready),
    ] {
        configure(&runtime, session, mode).await.unwrap();
        let edit = runtime
            .propose_edit(Uuid::new_v4(), session, "note", None, "content".into())
            .await
            .unwrap();
        let id = edit.result["id"].as_str().unwrap().parse().unwrap();
        assert_eq!(
            runtime.describe(id).await.unwrap().operation.status,
            edit_status
        );
        assert!(
            configure(&runtime, session, Permissions::Manual)
                .await
                .unwrap_err()
                .to_string()
                .contains("session_busy_or_uncertain")
        );
        runtime
            .mutate(Mutation {
                id: Uuid::new_v4(),
                command: Command::Cancel { operation: id },
            })
            .await
            .unwrap();
        let shell = runtime
            .propose_shell(
                Uuid::new_v4(),
                session,
                synth_code_runtime::command::ShellRequest {
                    command: "printf ok".into(),
                    timeout_seconds: 1,
                    output_limit: 1024,
                },
            )
            .await
            .unwrap();
        let id = shell.result["id"].as_str().unwrap().parse().unwrap();
        assert_eq!(
            runtime.describe(id).await.unwrap().operation.status,
            shell_status
        );
        runtime
            .mutate(Mutation {
                id: Uuid::new_v4(),
                command: Command::Cancel { operation: id },
            })
            .await
            .unwrap();
    }
    let other = common::tempdir().unwrap();
    let other = start(&runtime, other.path()).await;
    assert_eq!(
        runtime.session(other).await.unwrap().permissions,
        Permissions::Manual
    );
    let edit = runtime
        .propose_edit(Uuid::new_v4(), session, "note", None, "content".into())
        .await
        .unwrap();
    let id = edit.result["id"].as_str().unwrap().parse().unwrap();
    let epoch = runtime.describe(id).await.unwrap().epoch;
    runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::Dispatch {
                operation: id,
                epoch,
            },
        })
        .await
        .unwrap();
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    assert_eq!(
        runtime.session(session).await.unwrap().permissions,
        Permissions::Yolo
    );
    assert_eq!(
        runtime.describe(id).await.unwrap().operation.status,
        Status::Unknown
    );
    assert!(
        configure(&runtime, session, Permissions::Manual)
            .await
            .unwrap_err()
            .to_string()
            .contains("session_busy_or_uncertain")
    );
    assert!(!workspace.path().join("note").exists());
    runtime.close().await;
}

#[tokio::test]
async fn permission_writes_are_compare_and_swap_and_idempotent() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let session = start(&runtime, workspace.path()).await;
    let request = Mutation {
        id: Uuid::new_v4(),
        command: Command::ConfigurePermissions {
            session,
            mode: Permissions::Yolo,
            expected_revision: digest(b"\"manual\""),
        },
    };
    runtime.mutate(request.clone()).await.unwrap();
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    assert!(runtime.mutate(request.clone()).await.unwrap().replayed);
    let mut stale = request.clone();
    stale.id = Uuid::new_v4();
    assert!(
        runtime
            .mutate(stale)
            .await
            .unwrap_err()
            .to_string()
            .contains("session_permissions_conflict")
    );
    let changed = Mutation {
        id: request.id,
        command: Command::ConfigurePermissions {
            session,
            mode: Permissions::Manual,
            expected_revision: digest(b"\"yolo\""),
        },
    };
    assert!(
        runtime
            .mutate(changed)
            .await
            .unwrap_err()
            .to_string()
            .contains("mutation_conflict")
    );
    runtime.close().await;
}

#[test]
fn older_sessions_default_to_manual_and_unknown_modes_are_refused() {
    let session: Session = serde_json::from_value(
        serde_json::json!({"id":Uuid::new_v4(),"workspace":"/workspace","device":1,"inode":1}),
    )
    .unwrap();
    assert_eq!(session.permissions, Permissions::Manual);
    for invalid in ["auto", "allow-all", "YOLO", ""] {
        assert!(serde_json::from_value::<Permissions>(serde_json::json!(invalid)).is_err());
    }
}
