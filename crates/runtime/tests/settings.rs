mod common;
use std::os::unix::fs::MetadataExt;
use synth_code_runtime::{
    Runtime,
    domain::{Command, Mutation},
    provider::Selection,
    store::digest,
};
use uuid::Uuid;

#[tokio::test]
async fn configuration_is_durable_compare_and_swap_and_replayable() {
    let store = common::tempdir().unwrap();
    let workspace = common::tempdir().unwrap();
    let runtime = Runtime::open(store.path()).await.unwrap();
    let session = Uuid::new_v4();
    let meta = workspace.path().metadata().unwrap();
    runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::Start {
                session,
                workspace: workspace.path().to_str().unwrap().into(),
                device: meta.dev(),
                inode: meta.ino(),
            },
        })
        .await
        .unwrap();
    assert!(runtime.session(session).await.unwrap().inference.is_none());
    let selection = Selection {
        provider: "reflection".into(),
        model: "Beam-1.0-501B-A23B".into(),
        effort: "high".into(),
    };
    let request = Mutation {
        id: Uuid::new_v4(),
        command: Command::ConfigureInference {
            session,
            selection: selection.clone(),
            expected_revision: digest(b"null"),
        },
    };
    let first = runtime.mutate(request.clone()).await.unwrap();
    assert!(!first.replayed);
    runtime.close().await;
    let runtime = Runtime::open(store.path()).await.unwrap();
    assert_eq!(
        runtime.session(session).await.unwrap().inference,
        Some(selection.clone())
    );
    assert!(runtime.mutate(request).await.unwrap().replayed);
    let error = runtime
        .mutate(Mutation {
            id: Uuid::new_v4(),
            command: Command::ConfigureInference {
                session,
                selection: Selection {
                    effort: "max".into(),
                    ..selection
                },
                expected_revision: digest(b"null"),
            },
        })
        .await
        .unwrap_err();
    assert!(error.to_string().contains("session_settings_conflict"));
    runtime.close().await;
}

#[test]
fn selections_cannot_add_providers_models_credentials_or_unknown_effort() {
    for (provider, model, effort) in [
        ("openai", "Beam-1.0-501B-A23B", "high"),
        ("reflection", "other", "high"),
        ("reflection", "Beam-1.0-501B-A23B", "ultra"),
    ] {
        assert!(
            Selection {
                provider: provider.into(),
                model: model.into(),
                effort: effort.into()
            }
            .validate()
            .is_err()
        );
    }
    assert!(serde_json::from_value::<Selection>(serde_json::json!({"provider":"reflection","model":"Beam-1.0-501B-A23B","effort":"high","env_file":"/unselected"})).is_err());
}
