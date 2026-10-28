use synth_code_runtime::turn::{TurnCommand, Turns};
use uuid::Uuid;

#[test]
fn model_admission_has_no_policy_cap_and_retains_epoch_and_pending_guards() {
    let mut turns = Turns::default();
    let id = Uuid::new_v4();
    turns
        .apply(
            &TurnCommand::Start {
                id,
                session: Uuid::new_v4(),
                prompt: "Long task".into(),
                message_ref: "message".into(),
                config_ref: "config".into(),
            },
            1,
        )
        .unwrap();
    // Historical counts deserialize unchanged, and accounting can pass u32.
    turns.items.get_mut(&id).unwrap().attempts = u64::from(u32::MAX);
    let begin = |epoch| TurnCommand::ModelBegin {
        turn: id,
        attempt: Uuid::new_v4(),
        request_ref: "request".into(),
        epoch,
    };
    assert_eq!(
        turns.decide(&begin(2), 1).unwrap_err(),
        "model_attempt_refused"
    );
    turns.apply(&begin(1), 1).unwrap();
    assert_eq!(turns.items[&id].attempts, u64::from(u32::MAX) + 1);
    assert_eq!(
        turns.decide(&begin(1), 1).unwrap_err(),
        "model_attempt_refused"
    );
    turns.items.get_mut(&id).unwrap().pending_model = None;
    turns.items.get_mut(&id).unwrap().attempts = u64::MAX;
    turns.apply(&begin(1), 1).unwrap();
    assert_eq!(turns.items[&id].attempts, u64::MAX);
}
