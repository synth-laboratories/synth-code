mod common;
use std::os::unix::fs::MetadataExt;
use synth_code_runtime::{
    command::{ShellRequest, SupervisorInput},
    domain::Session,
};
use uuid::Uuid;

#[tokio::test]
async fn dropping_command_worker_closes_gateway_and_kills_owned_descendants() {
    let workspace = common::tempdir().unwrap();
    let m = workspace.path().metadata().unwrap();
    let input = SupervisorInput {
        session: Session {
            inference: None,
            id: Uuid::new_v4(),
            workspace: workspace.path().to_str().unwrap().into(),
            device: m.dev(),
            inode: m.ino(),
        },
        request: ShellRequest {
            command: "printf started > started; (sleep 1; touch escaped) & wait".into(),
            timeout_seconds: 5,
            output_limit: 1024,
        },
    };
    let (_sender, receiver) = tokio::sync::watch::channel(false);
    let job = tokio::spawn(async move {
        synth_code_runtime::command::run(
            std::path::Path::new(env!("CARGO_BIN_EXE_synth-code-runtime")),
            input,
            receiver,
        )
        .await
    });
    for _ in 0..200 {
        if workspace.path().join("started").exists() {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(10)).await;
    }
    assert!(workspace.path().join("started").exists());
    job.abort();
    let _ = job.await;
    tokio::time::sleep(std::time::Duration::from_millis(1200)).await;
    assert!(!workspace.path().join("escaped").exists());
}
