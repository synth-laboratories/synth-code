//! Read-only activity projection from the journal envelope timestamps.
//! No new events, timer writes, effect retries or private model reasoning.
use crate::{
    domain::{Command, Event, State},
    turn::TurnCommand,
};
use chrono::{DateTime, Utc};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use uuid::Uuid;

#[derive(Clone)]
pub struct Activity {
    started: DateTime<Utc>,
    updated: DateTime<Utc>,
    dispatched: Option<DateTime<Utc>>,
    finished: Option<DateTime<Utc>>,
}
impl Activity {
    fn new(at: DateTime<Utc>) -> Self {
        Self {
            started: at,
            updated: at,
            dispatched: None,
            finished: None,
        }
    }
    pub fn public(&self, active: bool) -> Value {
        let start = self.dispatched.unwrap_or(self.started);
        let end = self.finished.or_else(|| active.then(Utc::now));
        json!({"startedAt":self.started,"updatedAt":self.updated,"dispatchedAt":self.dispatched,
            "completedAt":self.finished,"durationMs":end.and_then(|end|u64::try_from((end-start).num_milliseconds()).ok())})
    }
}
#[derive(Default)]
pub struct Activities {
    pub turns: BTreeMap<Uuid, Activity>,
    pub operations: BTreeMap<Uuid, Activity>,
}
impl Activities {
    pub fn observe(&mut self, event: &Event, at: DateTime<Utc>, state: &State) {
        let Event::Accepted { mutation, .. } = event else {
            return;
        };
        let mut turn = None;
        match &mutation.command {
            Command::Agent { request } => match request {
                TurnCommand::Start { id, .. } => {
                    self.turns.insert(*id, Activity::new(at));
                }
                TurnCommand::Finish { turn: id, .. } => {
                    if let Some(t) = self.turns.get_mut(id) {
                        t.updated = at;
                        t.finished = Some(at);
                    }
                }
                TurnCommand::ModelBegin { turn: id, .. }
                | TurnCommand::ModelFinish { turn: id, .. }
                | TurnCommand::ToolResult { turn: id, .. }
                | TurnCommand::Continue { turn: id, .. } => {
                    turn = Some(*id);
                }
            },
            Command::Admit { operation, .. }
            | Command::AdmitEdit { operation, .. }
            | Command::AdmitShell { operation, .. } => {
                self.operations.insert(*operation, Activity::new(at));
                turn = state.operations[operation].turn;
            }
            Command::Approve { operation, .. }
            | Command::Dispatch { operation, .. }
            | Command::Cancel { operation }
            | Command::Settle { operation, .. }
            | Command::MarkUnknown { operation, .. }
            | Command::Reconcile { operation, .. } => {
                if let Some(op) = self.operations.get_mut(operation) {
                    op.updated = at;
                    if matches!(&mutation.command, Command::Dispatch { .. }) {
                        op.dispatched = Some(at);
                    }
                    if matches!(
                        &mutation.command,
                        Command::Settle { .. }
                            | Command::MarkUnknown { .. }
                            | Command::Cancel { .. }
                            | Command::Approve { allow: false, .. }
                    ) {
                        op.finished = Some(at);
                    }
                }
                turn = state.operations[operation].turn;
            }
            _ => {}
        }
        if let Some(id) = turn
            && let Some(t) = self.turns.get_mut(&id)
        {
            t.updated = at;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn reversed_wall_clock_is_unknown_instead_of_zero_duration() {
        let start = DateTime::from_timestamp(100, 0).unwrap();
        let end = DateTime::from_timestamp(99, 0).unwrap();
        let activity = Activity {
            started: start,
            updated: end,
            dispatched: None,
            finished: Some(end),
        };
        assert!(activity.public(false)["durationMs"].is_null());
    }
}
