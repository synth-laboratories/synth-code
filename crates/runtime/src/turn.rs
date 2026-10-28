//! Pure durable turn state. Provider history references remain private to Rust.
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::collections::BTreeMap;
use uuid::Uuid;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Turn {
    pub id: Uuid,
    pub ordinal: u64,
    pub session: Uuid,
    pub prompt: String,
    pub phase: String,
    pub epoch: u64,
    pub attempts: u64,
    pub pending_model: Option<Uuid>,
    pub messages: Vec<VisibleMessage>,
    pub operations: Vec<Uuid>,
    pub usage: Vec<Option<Value>>,
    pub models: Vec<Option<String>>,
    pub error: Option<String>,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct VisibleMessage {
    pub role: String,
    pub content_ref: String,
}
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case", deny_unknown_fields)]
pub enum TurnCommand {
    Start {
        id: Uuid,
        session: Uuid,
        prompt: String,
        message_ref: String,
        config_ref: String,
    },
    ModelBegin {
        turn: Uuid,
        attempt: Uuid,
        request_ref: String,
        epoch: u64,
    },
    ModelFinish {
        turn: Uuid,
        attempt: Uuid,
        message_ref: String,
        display_ref: String,
        usage: Option<Value>,
        #[serde(default)]
        provider_model: Option<String>,
        epoch: u64,
    },
    ToolResult {
        turn: Uuid,
        message_ref: String,
        epoch: u64,
    },
    Finish {
        turn: Uuid,
        phase: String,
        error: Option<String>,
        epoch: u64,
    },
}
#[derive(Default)]
pub struct Turns {
    pub items: BTreeMap<Uuid, Turn>,
    pub histories: BTreeMap<Uuid, Vec<String>>,
    pub configs: BTreeMap<Uuid, String>,
    pub required_artifacts: Vec<String>,
}
impl Turns {
    pub fn active(&self, session: Uuid) -> bool {
        self.items
            .values()
            .any(|t| t.session == session && matches!(t.phase.as_str(), "running" | "waiting"))
    }
    pub fn decide(&self, command: &TurnCommand, epoch: u64) -> Result<Value, String> {
        match command {
            TurnCommand::Start {
                id,
                session,
                prompt,
                ..
            } => {
                if self.items.len() >= 4096
                    || self
                        .items
                        .values()
                        .filter(|t| matches!(t.phase.as_str(), "running" | "waiting"))
                        .count()
                        >= 8
                    || self.items.contains_key(id)
                    || prompt.is_empty()
                    || prompt.len() > 8192
                    || self.active(*session)
                    || self.histories.get(session).is_some_and(|h| h.len() >= 2048)
                {
                    return Err("turn_admission_refused".into());
                }
                Ok(json!({"id":id,"session":session,"phase":"running"}))
            }
            TurnCommand::ModelBegin {
                turn,
                attempt,
                epoch: owner,
                ..
            } => {
                let t = self.items.get(turn).ok_or("turn_not_found")?;
                if *owner != epoch
                    || t.epoch != epoch
                    || t.phase != "running"
                    || t.pending_model.is_some()
                {
                    return Err("model_attempt_refused".into());
                }
                Ok(json!({"attempt":attempt,"admitted":true}))
            }
            TurnCommand::ModelFinish {
                turn,
                attempt,
                epoch: owner,
                ..
            } => {
                let t = self.items.get(turn).ok_or("turn_not_found")?;
                if *owner != epoch
                    || t.epoch != epoch
                    || t.phase != "running"
                    || t.pending_model != Some(*attempt)
                {
                    return Err("stale_model_settlement".into());
                }
                Ok(json!({"attempt":attempt,"settled":true}))
            }
            TurnCommand::ToolResult {
                turn, epoch: owner, ..
            } => {
                let t = self.items.get(turn).ok_or("turn_not_found")?;
                if *owner != epoch
                    || t.epoch != epoch
                    || !matches!(t.phase.as_str(), "running" | "waiting")
                {
                    return Err("stale_tool_result".into());
                }
                Ok(json!({"turn":turn,"recorded":true}))
            }
            TurnCommand::Finish {
                turn,
                phase,
                error,
                epoch: owner,
            } => {
                let t = self.items.get(turn).ok_or("turn_not_found")?;
                if *owner != epoch
                    || t.epoch != epoch
                    || !matches!(t.phase.as_str(), "running" | "waiting")
                    || !["completed", "failed", "interrupted"].contains(&phase.as_str())
                    || error.as_ref().is_some_and(|e| e.len() > 1024)
                {
                    return Err("turn_finish_refused".into());
                }
                if phase == "completed" && t.pending_model.is_some() {
                    return Err("pending_model_attempt".into());
                }
                Ok(json!({"id":turn,"phase":phase}))
            }
        }
    }
    pub fn apply(&mut self, command: &TurnCommand, epoch: u64) -> Result<(), String> {
        self.decide(command, epoch)?;
        match command {
            TurnCommand::Start {
                id,
                session,
                prompt,
                message_ref,
                config_ref,
            } => {
                self.histories
                    .entry(*session)
                    .or_default()
                    .push(message_ref.clone());
                self.required_artifacts
                    .extend([message_ref.clone(), config_ref.clone()]);
                self.configs.insert(*id, config_ref.clone());
                self.items.insert(
                    *id,
                    Turn {
                        id: *id,
                        ordinal: self.items.len() as u64 + 1,
                        session: *session,
                        prompt: prompt.clone(),
                        phase: "running".into(),
                        epoch,
                        attempts: 0,
                        pending_model: None,
                        messages: Vec::new(),
                        operations: Vec::new(),
                        usage: Vec::new(),
                        models: Vec::new(),
                        error: None,
                    },
                );
            }
            TurnCommand::ModelBegin {
                turn,
                attempt,
                request_ref,
                ..
            } => {
                let t = self.items.get_mut(turn).unwrap();
                t.pending_model = Some(*attempt);
                t.attempts = t.attempts.saturating_add(1);
                t.usage.push(None);
                t.models.push(None);
                self.required_artifacts.push(request_ref.clone());
            }
            TurnCommand::ModelFinish {
                turn,
                message_ref,
                display_ref,
                usage,
                provider_model,
                ..
            } => {
                let t = self.items.get_mut(turn).unwrap();
                t.pending_model = None;
                t.messages.push(VisibleMessage {
                    role: "assistant".into(),
                    content_ref: display_ref.clone(),
                });
                *t.usage.last_mut().expect("model attempt usage slot") = usage.clone();
                *t.models.last_mut().expect("model attempt identity slot") = provider_model.clone();
                self.histories
                    .entry(t.session)
                    .or_default()
                    .push(message_ref.clone());
                self.required_artifacts
                    .extend([message_ref.clone(), display_ref.clone()]);
            }
            TurnCommand::ToolResult {
                turn, message_ref, ..
            } => {
                let t = self.items.get_mut(turn).unwrap();
                t.phase = "running".into();
                self.histories
                    .entry(t.session)
                    .or_default()
                    .push(message_ref.clone());
                self.required_artifacts.push(message_ref.clone());
            }
            TurnCommand::Finish {
                turn, phase, error, ..
            } => {
                let t = self.items.get_mut(turn).unwrap();
                t.phase = phase.clone();
                t.error = error.clone();
            }
        }
        Ok(())
    }
    pub fn recover(&mut self) {
        for t in self.items.values_mut() {
            if matches!(t.phase.as_str(), "running" | "waiting") {
                t.phase = "interrupted".into();
                t.error = Some(
                    if t.pending_model.is_some() {
                        "interrupted_with_unknown_model_outcome"
                    } else {
                        "runtime_restarted"
                    }
                    .into(),
                );
            }
        }
    }
}
