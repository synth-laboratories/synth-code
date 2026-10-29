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
    /// A `/compact` turn: it summarizes the history and ends.
    #[serde(default)]
    pub compact: bool,
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
        /// Compact the session history instead of answering; the prompt is not added to it.
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        compact: bool,
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
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        compaction: bool,
        epoch: u64,
    },
    ToolResult {
        turn: Uuid,
        message_ref: String,
        epoch: u64,
    },
    /// A runtime-authored user message that asks the model to check its goal before ending.
    Continue {
        turn: Uuid,
        message_ref: String,
        display_ref: String,
        epoch: u64,
    },
    /// Replaces the session history with retained user messages and a summary.
    Compact {
        turn: Uuid,
        message_refs: Vec<String>,
        display_ref: String,
        epoch: u64,
        /// The session's provider selection, adopted by the running turn when it
        /// changed since the turn started (settings applied at compaction).
        #[serde(default, skip_serializing_if = "Option::is_none")]
        config_ref: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        adopted_selection: Option<crate::provider::Selection>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        adopted_display_ref: Option<String>,
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
                compact,
                ..
            } => {
                if *compact && self.histories.get(session).is_none_or(Vec::is_empty) {
                    return Err("nothing_to_compact".into());
                }
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
                    || (!*compact && self.histories.get(session).is_some_and(|h| h.len() >= 2048))
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
            TurnCommand::Continue {
                turn, epoch: owner, ..
            } => {
                let t = self.items.get(turn).ok_or("turn_not_found")?;
                if *owner != epoch
                    || t.epoch != epoch
                    || t.phase != "running"
                    || t.pending_model.is_some()
                {
                    return Err("turn_continue_refused".into());
                }
                Ok(json!({"turn":turn,"continued":true}))
            }
            TurnCommand::Compact {
                turn,
                message_refs,
                epoch: owner,
                ..
            } => {
                let t = self.items.get(turn).ok_or("turn_not_found")?;
                if *owner != epoch
                    || t.epoch != epoch
                    || t.phase != "running"
                    || t.pending_model.is_some()
                    || message_refs.is_empty()
                    || message_refs.len() > 256
                {
                    return Err("turn_compact_refused".into());
                }
                Ok(json!({"turn":turn,"compacted":true}))
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
                compact,
            } => {
                let history = self.histories.entry(*session).or_default();
                if !*compact {
                    history.push(message_ref.clone());
                }
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
                        compact: *compact,
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
                compaction,
                ..
            } => {
                let t = self.items.get_mut(turn).unwrap();
                t.pending_model = None;
                if !compaction {
                    t.messages.push(VisibleMessage {
                        role: "assistant".into(),
                        content_ref: display_ref.clone(),
                    });
                }
                *t.usage.last_mut().expect("model attempt usage slot") = usage.clone();
                *t.models.last_mut().expect("model attempt identity slot") = provider_model.clone();
                if !compaction {
                    self.histories
                        .entry(t.session)
                        .or_default()
                        .push(message_ref.clone());
                }
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
            TurnCommand::Continue {
                turn,
                message_ref,
                display_ref,
                ..
            } => {
                let t = self.items.get_mut(turn).unwrap();
                t.messages.push(VisibleMessage {
                    role: "continue".into(),
                    content_ref: display_ref.clone(),
                });
                self.histories
                    .entry(t.session)
                    .or_default()
                    .push(message_ref.clone());
                self.required_artifacts
                    .extend([message_ref.clone(), display_ref.clone()]);
            }
            TurnCommand::Compact {
                turn,
                message_refs,
                display_ref,
                config_ref,
                adopted_display_ref,
                ..
            } => {
                if let Some(config_ref) = config_ref {
                    self.configs.insert(*turn, config_ref.clone());
                    self.required_artifacts.push(config_ref.clone());
                }
                let t = self.items.get_mut(turn).unwrap();
                t.messages.push(VisibleMessage {
                    role: "compaction".into(),
                    content_ref: display_ref.clone(),
                });
                if let Some(reference) = adopted_display_ref {
                    t.messages.push(VisibleMessage {
                        role: "setting".into(),
                        content_ref: reference.clone(),
                    });
                    self.required_artifacts.push(reference.clone());
                }
                self.histories.insert(t.session, message_refs.clone());
                self.required_artifacts.extend(message_refs.iter().cloned());
                self.required_artifacts.push(display_ref.clone());
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
