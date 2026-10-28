//! Pure commands and delta events. No provider or filesystem effects here.
use crate::command::ShellRequest;
use crate::store::digest;
use crate::turn::{TurnCommand, Turns};
use crate::workspace::EditRequest;
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
    Canceled,
    Reconciled,
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
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub edit: Option<EditRequest>,
    #[serde(default)]
    pub admitted_sequence: u64,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub shell: Option<ShellRequest>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub turn: Option<Uuid>,
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
    AdmitEdit {
        session: Uuid,
        operation: Uuid,
        request: EditRequest,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        turn: Option<Uuid>,
    },
    AdmitShell {
        session: Uuid,
        operation: Uuid,
        request: ShellRequest,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        turn: Option<Uuid>,
    },
    Agent {
        request: TurnCommand,
    },
    Approve {
        operation: Uuid,
        request_digest: String,
        allow: bool,
    },
    Cancel {
        operation: Uuid,
    },
    Reconcile {
        operation: Uuid,
        request_digest: String,
        observation: String,
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
    MarkUnknown {
        operation: Uuid,
        epoch: u64,
        reason: String,
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
        mutation: Box<Mutation>,
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
    pub turns: Turns,
}

impl State {
    fn available(
        &self,
        session: &Uuid,
        operation: &Uuid,
        turn: Option<Uuid>,
    ) -> Result<(), String> {
        if !self.sessions.contains_key(session) || self.operations.contains_key(operation) {
            return Err("invalid_operation".into());
        }
        if self.operations.len() >= 4096 {
            return Err("operation_capacity".into());
        }
        let workspace = &self.sessions[session].workspace;
        if self.turns.items.values().any(|t| {
            let a = std::path::Path::new(workspace);
            let b = std::path::Path::new(&self.sessions[&t.session].workspace);
            Some(t.id) != turn
                && matches!(t.phase.as_str(), "running" | "waiting")
                && (a.starts_with(b) || b.starts_with(a))
        }) {
            return Err("workspace_has_active_turn".into());
        }
        if self.operations.values().any(|op| {
            let other = &self.sessions[&op.session].workspace;
            let a = std::path::Path::new(workspace);
            let b = std::path::Path::new(other);
            (a.starts_with(b) || b.starts_with(a))
                && matches!(
                    op.status,
                    Status::AwaitingApproval | Status::Ready | Status::Dispatched | Status::Unknown
                )
        }) {
            return Err("workspace_busy_or_uncertain".into());
        }
        Ok(())
    }
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
                    || serde_json::to_vec(workspace)
                        .map_err(|e| e.to_string())?
                        .len()
                        > 8192
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
                self.available(session, operation, None)?;
                if text.len() > 8192 {
                    return Err("invalid_operation".into());
                }
                Ok((
                    json!({"id":operation,"status":"awaiting_approval","request_digest":digest(text.as_bytes())}),
                    false,
                ))
            }
            Command::AdmitEdit {
                session,
                operation,
                request,
                turn,
            } => {
                self.available(session, operation, *turn)?;
                if let Some(id) = turn {
                    let t = self.turns.items.get(id).ok_or("turn_not_found")?;
                    if t.session != *session
                        || t.epoch != self.epoch
                        || !matches!(t.phase.as_str(), "running" | "waiting")
                    {
                        return Err("stale_turn_operation".into());
                    }
                }
                request.validate().map_err(|e| e.to_string())?;
                Ok((
                    json!({"id":operation,"status":"awaiting_approval","request_digest":request.fingerprint(),"edit":request}),
                    false,
                ))
            }
            Command::AdmitShell {
                session,
                operation,
                request,
                turn,
            } => {
                self.available(session, operation, *turn)?;
                if let Some(id) = turn {
                    let t = self.turns.items.get(id).ok_or("turn_not_found")?;
                    if t.session != *session
                        || t.epoch != self.epoch
                        || !matches!(t.phase.as_str(), "running" | "waiting")
                    {
                        return Err("stale_turn_operation".into());
                    }
                }
                request.validate().map_err(|e| e.to_string())?;
                Ok((
                    json!({"id":operation,"status":"awaiting_approval","request_digest":request.fingerprint(),"shell":request}),
                    false,
                ))
            }
            Command::Agent { request } => {
                if let TurnCommand::Start { session, .. } = request {
                    if !self.sessions.contains_key(session) {
                        return Err("session_not_found".into());
                    }
                    self.available(session, &Uuid::nil(), None)?;
                }
                self.turns
                    .decide(request, self.epoch)
                    .map(|result| (result, false))
            }
            Command::Cancel { operation } => {
                let op = self
                    .operations
                    .get(operation)
                    .ok_or("operation_not_found")?;
                if !matches!(op.status, Status::AwaitingApproval | Status::Ready) {
                    return Err("operation_already_dispatched_or_terminal".into());
                }
                Ok((json!({"id":operation,"canceled":true}), false))
            }
            Command::Reconcile {
                operation,
                request_digest,
                observation,
            } => {
                let op = self
                    .operations
                    .get(operation)
                    .ok_or("operation_not_found")?;
                if op.status != Status::Unknown
                    || &op.request_digest != request_digest
                    || observation.trim().is_empty()
                    || observation.len() > 1024
                {
                    return Err("invalid_reconciliation".into());
                }
                if op.turn.is_some_and(|id| {
                    self.turns
                        .items
                        .get(&id)
                        .is_some_and(|t| matches!(t.phase.as_str(), "running" | "waiting"))
                }) {
                    return Err("interrupt_turn_before_reconciliation".into());
                }
                Ok((json!({"id":operation,"reconciled":true}), false))
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
                if *epoch != self.epoch
                    || op.status != Status::Ready
                    || self
                        .operations
                        .values()
                        .filter(|op| op.status == Status::Dispatched)
                        .count()
                        >= 8
                    || op.turn.is_some_and(|id| {
                        self.turns.items.get(&id).is_none_or(|t| {
                            t.epoch != self.epoch
                                || !matches!(t.phase.as_str(), "running" | "waiting")
                        })
                    })
                {
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
            Command::MarkUnknown {
                operation,
                epoch,
                reason,
            } => {
                let op = self
                    .operations
                    .get(operation)
                    .ok_or("operation_not_found")?;
                if *epoch != self.epoch
                    || op.dispatched_epoch != Some(*epoch)
                    || op.status != Status::Dispatched
                    || reason.len() > 1024
                {
                    return Err("stale_or_invalid_uncertain_outcome".into());
                }
                Ok((json!({"id":operation,"unknown":true}), false))
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
                self.turns.recover();
                for op in self.operations.values_mut() {
                    if op.status == Status::Dispatched {
                        op.status = Status::Unknown;
                    } else if op.turn.is_some()
                        && matches!(op.status, Status::AwaitingApproval | Status::Ready)
                    {
                        op.status = Status::Canceled;
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
                                edit: None,
                                admitted_sequence: self.sequence + 1,
                                shell: None,
                                turn: None,
                            },
                        );
                    }
                    Command::AdmitEdit {
                        session,
                        operation,
                        request,
                        turn,
                    } => {
                        if let Some(id) = turn {
                            let t = self.turns.items.get_mut(id).unwrap();
                            t.operations.push(*operation);
                            t.phase = "waiting".into();
                        }
                        self.operations.insert(
                            *operation,
                            Operation {
                                id: *operation,
                                session: *session,
                                text: format!("Edit {}", request.path),
                                request_digest: request.fingerprint(),
                                status: Status::AwaitingApproval,
                                dispatched_epoch: None,
                                outcome: None,
                                edit: Some(request.clone()),
                                admitted_sequence: self.sequence + 1,
                                shell: None,
                                turn: *turn,
                            },
                        );
                    }
                    Command::AdmitShell {
                        session,
                        operation,
                        request,
                        turn,
                    } => {
                        if let Some(id) = turn {
                            let t = self.turns.items.get_mut(id).unwrap();
                            t.operations.push(*operation);
                            t.phase = "waiting".into();
                        }
                        self.operations.insert(
                            *operation,
                            Operation {
                                id: *operation,
                                session: *session,
                                text: request.command.clone(),
                                request_digest: request.fingerprint(),
                                status: Status::AwaitingApproval,
                                dispatched_epoch: None,
                                outcome: None,
                                edit: None,
                                admitted_sequence: self.sequence + 1,
                                shell: Some(request.clone()),
                                turn: *turn,
                            },
                        );
                    }
                    Command::Agent { request } => {
                        self.turns.apply(request, self.epoch)?;
                        if let TurnCommand::Finish { turn, phase, .. } = request
                            && phase != "completed"
                        {
                            for op in self
                                .operations
                                .values_mut()
                                .filter(|op| op.turn == Some(*turn))
                            {
                                if matches!(op.status, Status::AwaitingApproval | Status::Ready) {
                                    op.status = Status::Canceled;
                                } else if op.status == Status::Dispatched {
                                    op.status = Status::Unknown;
                                }
                            }
                        }
                    }
                    Command::Cancel { operation } => {
                        self.operations.get_mut(operation).unwrap().status = Status::Canceled;
                    }
                    Command::Reconcile {
                        operation,
                        observation,
                        ..
                    } => {
                        let op = self.operations.get_mut(operation).unwrap();
                        op.status = Status::Reconciled;
                        op.outcome = Some(
                            json!({"prior":op.outcome,"user_observation":observation,"effect_repeated":false}),
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
                    Command::MarkUnknown {
                        operation, reason, ..
                    } => {
                        let op = self.operations.get_mut(operation).unwrap();
                        op.status = Status::Unknown;
                        op.outcome = Some(json!({"reason":reason}));
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
