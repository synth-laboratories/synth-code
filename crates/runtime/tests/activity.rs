mod common;
use std::os::unix::fs::MetadataExt;
use synth_code_runtime::{
    Runtime,
    domain::{Command, Mutation},
    turn::TurnCommand,
};
use uuid::Uuid;

#[tokio::test]
async fn journal_timing_is_stable_after_reads_replay_and_recovery() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let session = Uuid::new_v4();
    let m = workspace.path().metadata().unwrap();
    runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::Start {
                session,
                workspace: workspace.path().to_str().unwrap().into(),
                device: m.dev(),
                inode: m.ino(),
            },
        })
        .await
        .unwrap();
    let turn = Uuid::new_v4();
    let reference = runtime.artifacts.put(b"timing fixture").unwrap();
    let start = Uuid::new_v4();
    let request = TurnCommand::Start {
        id: turn,
        session,
        prompt: "fixture".into(),
        message_ref: reference.clone(),
        config_ref: reference,
        compact: false,
    };
    runtime.agent_mutate(start, request.clone()).await.unwrap();
    let view = runtime.turn(turn).await.unwrap();
    let activity = view.activity.unwrap();
    assert!(activity["durationMs"].as_i64().unwrap() >= 0);
    assert!(activity["completedAt"].is_null());
    tokio::time::sleep(std::time::Duration::from_millis(20)).await;
    runtime
        .agent_mutate(
            Uuid::new_v4(),
            TurnCommand::Finish {
                turn,
                phase: "completed".into(),
                error: None,
                epoch: view.turn.epoch,
            },
        )
        .await
        .unwrap();
    let completed = runtime.turn(turn).await.unwrap().activity.unwrap();
    assert!(completed["durationMs"].as_i64().unwrap() >= 20);
    assert!(completed["completedAt"].is_string());
    assert!(runtime.agent_mutate(start, request).await.unwrap().replayed);
    let before = runtime.read(None).await.unwrap()["sequence"].clone();
    for _ in 0..3 {
        assert_eq!(
            runtime.turn(turn).await.unwrap().activity.as_ref(),
            Some(&completed)
        );
    }
    assert_eq!(runtime.read(None).await.unwrap()["sequence"], before);
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    assert_eq!(
        runtime.turn(turn).await.unwrap().activity.as_ref(),
        Some(&completed)
    );
    runtime.close().await;
}

#[tokio::test]
async fn recovery_without_a_finish_does_not_invent_elapsed_completion() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let m = workspace.path().metadata().unwrap();
    let session = Uuid::new_v4();
    runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::Start {
                session,
                workspace: workspace.path().to_str().unwrap().into(),
                device: m.dev(),
                inode: m.ino(),
            },
        })
        .await
        .unwrap();
    let turn = Uuid::new_v4();
    let r = runtime.artifacts.put(b"fixture").unwrap();
    runtime
        .agent_mutate(
            Uuid::new_v4(),
            TurnCommand::Start {
                id: turn,
                session,
                prompt: "fixture".into(),
                message_ref: r.clone(),
                config_ref: r,
                compact: false,
            },
        )
        .await
        .unwrap();
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    let view = runtime.turn(turn).await.unwrap();
    assert_eq!(view.turn.phase, "interrupted");
    assert!(view.activity.as_ref().unwrap()["durationMs"].is_null());
    assert!(view.activity.as_ref().unwrap()["completedAt"].is_null());
    runtime.close().await;
}
