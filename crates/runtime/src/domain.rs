//! Pure commands and delta events. No provider or filesystem effects here.
use crate::store::digest;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use uuid::Uuid;

#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Status {
    AwaitingApproval,
    Ready,
    Denied,
    Dispatched,
    Succeeded,
    Failed,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Operation {
    pub id: Uuid,
    pub session: Uuid,
    pub text: String,
    pub request_digest: String,
    pub status: Status,
    pub dispatched_epoch: Option<u64>,
    pub outcome: Option<Value>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Session {
    pub id: Uuid,
    pub workspace: String,
    pub device: u64,
    pub inode: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum Command {
    Start {
        session: Uuid,
        workspace: String,
        device: u64,
        inode: u64,
    },
    Admit {
        session: Uuid,
        operation: Uuid,
        text: String,
    },
    Approve {
        operation: Uuid,
        request_digest: String,
        allow: bool,
    },
    Dispatch {
        operation: Uuid,
        epoch: u64,
    },
    Settle {
        operation: Uuid,
        epoch: u64,
        success: bool,
        outcome: Value,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Mutation {
    pub id: Uuid,
    pub command: Command,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
pub enum Event {
    Recovered {
        epoch: u64,
    },
    Accepted {
        mutation: Mutation,
        fingerprint: String,
        response: Value,
    },
}

#[derive(Default)]
pub struct State {
    pub sequence: u64,
    pub epoch: u64,
    pub sessions: BTreeMap<Uuid, Session>,
    pub operations: BTreeMap<Uuid, Operation>,
    receipts: BTreeMap<Uuid, (String, Value)>,
}

impl State {
    pub fn decide(&self, mutation: &Mutation) -> Result<(Value, bool), String> {
        let fingerprint =
            digest(&serde_json::to_vec(&mutation.command).map_err(|e| e.to_string())?);
        if let Some((old, response)) = self.receipts.get(&mutation.id) {
            return if old == &fingerprint {
                Ok((response.clone(), true))
            } else {
                Err("mutation_conflict".into())
            };
        }
        if self.receipts.len() >= 32_768 {
            return Err("mutation_capacity".into());
        }
        match &mutation.command {
            Command::Start {
                session, workspace, ..
            } => {
                if self.sessions.len() >= 256
                    || self.sessions.contains_key(session)
                    || workspace.is_empty()
                    || workspace.len() > 4096
                {
                    return Err("invalid_or_existing_session".into());
                }
                Ok((json!({"id":session,"workspace":workspace}), false))
            }
            Command::Admit {
                session,
                operation,
                text,
            } => {
                if !self.sessions.contains_key(session)
                    || self.operations.contains_key(operation)
                    || text.len() > 8192
                {
                    return Err("invalid_operation".into());
                }
                if self.operations.len() >= 4096 {
                    return Err("operation_capacity".into());
                }
                let workspace = &self.sessions[session].workspace;
                if self.operations.values().any(|op| {
                    let other = &self.sessions[&op.session].workspace;
                    let a = std::path::Path::new(workspace);
                    let b = std::path::Path::new(other);
                    (a.starts_with(b) || b.starts_with(a))
                        && matches!(
                            op.status,
                            Status::AwaitingApproval
                                | Status::Ready
                                | Status::Dispatched
                                | Status::Unknown
                        )
                }) {
                    return Err("workspace_busy_or_uncertain".into());
                }
                Ok((
                    json!({"id":operation,"status":"awaiting_approval","request_digest":digest(text.as_bytes())}),
                    false,
                ))
            }
            Command::Approve {
                operation,
                request_digest,
                allow,
            } => {
                let op = self
                    .operations
                    .get(operation)
                    .ok_or("operation_not_found")?;
                if op.status != Status::AwaitingApproval || &op.request_digest != request_digest {
                    return Err("stale_approval".into());
                }
                Ok((json!({"id":operation,"approved":allow}), false))
            }
            Command::Dispatch { operation, epoch } => {
                let op = self
                    .operations
                    .get(operation)
                    .ok_or("operation_not_found")?;
                if *epoch != self.epoch || op.status != Status::Ready {
                    return Err("dispatch_refused".into());
                }
                Ok((json!({"id":operation,"epoch":epoch}), false))
            }
            Command::Settle {
                operation,
                epoch,
                outcome,
                ..
            } => {
                let op = self
                    .operations
                    .get(operation)
                    .ok_or("operation_not_found")?;
                if *epoch != self.epoch
                    || op.dispatched_epoch != Some(*epoch)
                    || op.status != Status::Dispatched
                {
                    return Err("stale_settlement".into());
                }
                if serde_json::to_vec(outcome)
                    .map_err(|e| e.to_string())?
                    .len()
                    > 8192
                {
                    return Err("outcome_too_large".into());
                }
                Ok((json!({"id":operation,"settled":true}), false))
            }
        }
    }

    pub fn apply(&mut self, event: &Event) -> Result<(), String> {
        match event {
            Event::Recovered { epoch } => {
                if *epoch != self.epoch + 1 {
                    return Err("invalid_owner_epoch".into());
                }
                self.epoch = *epoch;
                for op in self.operations.values_mut() {
                    if op.status == Status::Dispatched {
                        op.status = Status::Unknown;
                    }
                }
            }
            Event::Accepted {
                mutation,
                fingerprint,
                response,
            } => {
                let (expected, replayed) = self.decide(mutation)?;
                if replayed
                    || &expected != response
                    || &digest(&serde_json::to_vec(&mutation.command).map_err(|e| e.to_string())?)
                        != fingerprint
                {
                    return Err("invalid_mutation_receipt".into());
                }
                match &mutation.command {
                    Command::Start {
                        session,
                        workspace,
                        device,
                        inode,
                    } => {
                        self.sessions.insert(
                            *session,
                            Session {
                                id: *session,
                                workspace: workspace.clone(),
                                device: *device,
                                inode: *inode,
                            },
                        );
                    }
                    Command::Admit {
                        session,
                        operation,
                        text,
                    } => {
                        self.operations.insert(
                            *operation,
                            Operation {
                                id: *operation,
                                session: *session,
                                text: text.clone(),
                                request_digest: digest(text.as_bytes()),
                                status: Status::AwaitingApproval,
                                dispatched_epoch: None,
                                outcome: None,
                            },
                        );
                    }
                    Command::Approve {
                        operation, allow, ..
                    } => {
                        self.operations.get_mut(operation).unwrap().status = if *allow {
                            Status::Ready
                        } else {
                            Status::Denied
                        };
                    }
                    Command::Dispatch { operation, epoch } => {
                        let op = self.operations.get_mut(operation).unwrap();
                        op.status = Status::Dispatched;
                        op.dispatched_epoch = Some(*epoch);
                    }
                    Command::Settle {
                        operation,
                        success,
                        outcome,
                        ..
                    } => {
                        let op = self.operations.get_mut(operation).unwrap();
                        op.status = if *success {
                            Status::Succeeded
                        } else {
                            Status::Failed
                        };
                        op.outcome = Some(outcome.clone());
                    }
                }
                self.receipts
                    .insert(mutation.id, (fingerprint.clone(), response.clone()));
            }
        }
        self.sequence += 1;
        Ok(())
    }
}
