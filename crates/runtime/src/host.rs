//! Reconstructible worker tasks around the single durable Temper authority.
use crate::{
    Runtime,
    domain::{Command, Mutation, Permissions, Status},
    host_env,
    provider::{EFFORTS, MODEL, ProviderConfig, Selection},
    turn::TurnCommand,
};
use anyhow::{Context, bail, ensure};
use serde_json::{Value, json};
use std::{
    collections::BTreeMap,
    path::PathBuf,
    sync::{Arc, Mutex},
    time::Duration,
};
use tokio::{
    sync::{Notify, watch},
    task::JoinHandle,
};
use uuid::Uuid;

struct Job {
    cancel: watch::Sender<bool>,
    task: JoinHandle<()>,
}
pub struct Host {
    pub runtime: Arc<Runtime>,
    pub config: ProviderConfig,
    executable: PathBuf,
    pub binary_sha256: String,
    jobs: Mutex<BTreeMap<Uuid, Job>>,
    live: Mutex<BTreeMap<Uuid, String>>,
    pub(crate) settings: tokio::sync::Mutex<()>,
    pub changed: Notify,
    pub stop: Notify,
}
impl Host {
    pub fn new(
        runtime: Arc<Runtime>,
        config: ProviderConfig,
        executable: PathBuf,
    ) -> anyhow::Result<Arc<Self>> {
        config.validate()?;
        Ok(Arc::new(Self {
            binary_sha256: crate::store::digest(&std::fs::read(&executable)?),
            runtime,
            config,
            executable,
            jobs: Mutex::new(BTreeMap::new()),
            live: Mutex::new(BTreeMap::new()),
            settings: tokio::sync::Mutex::new(()),
            changed: Notify::new(),
            stop: Notify::new(),
        }))
    }
    pub async fn start(
        self: &Arc<Self>,
        mutation: Uuid,
        session: Uuid,
        prompt: String,
        expected_permissions: Option<Permissions>,
    ) -> anyhow::Result<Value> {
        let _settings = self.settings.lock().await;
        self.jobs
            .lock()
            .unwrap()
            .retain(|_, j| !j.task.is_finished());
        let id = Uuid::new_v5(&mutation, b"turn");
        if self.runtime.turn(id).await.is_err() {
            self.check_permissions(session, expected_permissions)
                .await?;
        }
        let message_ref = self
            .runtime
            .artifacts
            .put_json(&json!({"role":"user","content":prompt}))?;
        let config_ref = if let Ok(existing) = self.runtime.turn(id).await {
            ensure!(
                existing.turn.session == session && existing.turn.prompt == prompt,
                "mutation_conflict"
            );
            existing.config_ref
        } else {
            let config = self.session_config(session).await?;
            self.runtime.artifacts.put_json(&config)?
        };
        let admitted = self
            .runtime
            .agent_mutate(
                mutation,
                TurnCommand::Start {
                    id,
                    session,
                    prompt,
                    message_ref,
                    config_ref,
                },
            )
            .await?;
        if !admitted.replayed {
            let (sender, receiver) = watch::channel(false);
            let host = self.clone();
            let task = tokio::spawn(async move {
                if let Err(error) = host.drive(id, receiver).await
                    && let Ok(view) = host.runtime.turn(id).await
                    && matches!(view.turn.phase.as_str(), "running" | "waiting")
                {
                    let _ = host
                        .runtime
                        .agent_mutate(
                            Uuid::new_v4(),
                            TurnCommand::Finish {
                                turn: id,
                                phase: "failed".into(),
                                error: Some(error.to_string().chars().take(200).collect()),
                                epoch: view.turn.epoch,
                            },
                        )
                        .await;
                }
                host.live.lock().unwrap().remove(&id);
                host.changed.notify_waiters();
            });
            self.jobs.lock().unwrap().insert(
                id,
                Job {
                    cancel: sender,
                    task,
                },
            );
        }
        self.read_turn(id).await
    }
    async fn session_config(&self, session: Uuid) -> anyhow::Result<ProviderConfig> {
        let mut config = self.config.clone();
        if let Some(selection) = self.runtime.session(session).await?.inference {
            selection.validate()?;
            config.model = selection.model;
            config.effort = selection.effort;
        }
        config.validate()?;
        Ok(config)
    }
    pub async fn provider_status(&self, session: Option<Uuid>) -> anyhow::Result<Value> {
        let selection = if let Some(id) = session {
            self.runtime.session(id).await?.inference
        } else {
            None
        };
        let mut config = self.config.clone();
        if let Some(selection) = &selection {
            selection.validate()?;
            config.model = selection.model.clone();
            config.effort = selection.effort.clone();
        }
        config.validate()?;
        let mut value = config.public();
        value["selection"] = serde_json::to_value(config.selection())?;
        value["revision"] = json!(crate::store::digest(&serde_json::to_vec(&selection)?));
        value["choices"] = json!({"providers":["reflection"],"models":[MODEL],"efforts":EFFORTS});
        Ok(value)
    }
    pub async fn configure_provider(
        &self,
        mutation: Uuid,
        session: Uuid,
        selection: Selection,
        expected_revision: String,
    ) -> anyhow::Result<Value> {
        let _settings = self.settings.lock().await;
        let applied = self
            .runtime
            .mutate(Mutation {
                id: mutation,
                command: Command::ConfigureInference {
                    session,
                    selection,
                    expected_revision,
                },
            })
            .await?;
        Ok(json!({"applied":applied,"status":self.provider_status(Some(session)).await?}))
    }
    pub async fn permissions_status(&self, session: Uuid) -> anyhow::Result<Value> {
        let mode = self.runtime.session(session).await?.permissions;
        Ok(
            json!({"mode":mode,"revision":crate::store::digest(&serde_json::to_vec(&mode)?),
            "choices":["manual","auto-edit","yolo"]}),
        )
    }
    // Call while holding settings through admission. Existing RPC clients may
    // omit the expectation; interactive clients bind admission to their footer.
    pub(crate) async fn check_permissions(
        &self,
        session: Uuid,
        expected: Option<Permissions>,
    ) -> anyhow::Result<()> {
        if let Some(expected) = expected {
            ensure!(
                self.runtime.session(session).await?.permissions == expected,
                "session_permissions_changed"
            );
        }
        Ok(())
    }
    pub async fn configure_permissions(
        &self,
        mutation: Uuid,
        session: Uuid,
        mode: Permissions,
        expected_revision: String,
    ) -> anyhow::Result<Value> {
        let _settings = self.settings.lock().await;
        let applied = self
            .runtime
            .mutate(Mutation {
                id: mutation,
                command: Command::ConfigurePermissions {
                    session,
                    mode,
                    expected_revision,
                },
            })
            .await?;
        Ok(json!({"applied":applied,"status":self.permissions_status(session).await?}))
    }
    pub async fn read_turn(&self, id: Uuid) -> anyhow::Result<Value> {
        let view = self.runtime.turn(id).await?;
        let mut messages = Vec::new();
        for (index, message) in view.turn.messages.iter().enumerate().rev().take(8).rev() {
            let bytes = self.runtime.artifacts.get(&message.content_ref)?;
            let text = String::from_utf8(bytes)?;
            messages.push(json!({"index":index,"role":message.role,"text":text.chars().take(512).collect::<String>(),"truncated":text.chars().count()>512}));
        }
        let live = self
            .live
            .lock()
            .unwrap()
            .get(&id)
            .cloned()
            .unwrap_or_default();
        let config: ProviderConfig =
            serde_json::from_value(self.runtime.artifacts.json(&view.config_ref)?)?;
        let mut result = json!({"id":id,"session":view.turn.session,"prompt":view.turn.prompt.chars().take(1024).collect::<String>(),"phase":view.turn.phase,"attempts":view.turn.attempts,"messages":messages,"operations":view.turn.operations.iter().rev().take(32).rev().collect::<Vec<_>>(),"operationCount":view.turn.operations.len(),"usage":view.turn.usage,"models":view.turn.models,"error":view.turn.error,"modelOutcomeUnknown":view.turn.pending_model.is_some()&&view.turn.phase!="running","liveText":live.chars().rev().take(1024).collect::<Vec<_>>().into_iter().rev().collect::<String>(),"provisional":!live.is_empty(),"selection":config.selection()});
        result["permissions"] = json!(self.runtime.session(view.turn.session).await?.permissions);
        result["activity"] = json!(view.activity);
        result["modelPending"] = json!(view.turn.pending_model.is_some());
        Ok(result)
    }
    pub async fn interrupt(&self, id: Uuid) -> anyhow::Result<Value> {
        if let Some(job) = self.jobs.lock().unwrap().get(&id) {
            let _ = job.cancel.send(true);
        }
        let view = self.runtime.turn(id).await?;
        if matches!(view.turn.phase.as_str(), "running" | "waiting") {
            self.runtime
                .agent_mutate(
                    Uuid::new_v4(),
                    TurnCommand::Finish {
                        turn: id,
                        phase: "interrupted".into(),
                        error: None,
                        epoch: view.turn.epoch,
                    },
                )
                .await?;
        }
        self.changed.notify_waiters();
        self.read_turn(id).await
    }
    pub async fn launch_shell(self: &Arc<Self>, id: Uuid) -> anyhow::Result<Value> {
        let admission = {
            let mut jobs = self.jobs.lock().unwrap();
            jobs.retain(|_, job| !job.task.is_finished());
            if jobs.contains_key(&id) {
                None
            } else {
                let (sender, receiver) = watch::channel(false);
                let (started, admission) = tokio::sync::oneshot::channel();
                let host = self.clone();
                let task = tokio::spawn(async move {
                    if let Err(error) = host
                        .runtime
                        .run_shell_admitted(id, &host.executable, receiver, Some(started))
                        .await
                    {
                        eprintln!("command_worker_refused: {error}");
                    }
                    host.changed.notify_waiters();
                });
                jobs.insert(
                    id,
                    Job {
                        cancel: sender,
                        task,
                    },
                );
                Some(admission)
            }
        };
        if let Some(admission) = admission {
            admission
                .await
                .context("command_admission_unavailable")?
                .map_err(anyhow::Error::msg)?;
        }
        self.runtime.operation_result(id).await
    }
    pub async fn cancel_operation(&self, id: Uuid) -> anyhow::Result<Value> {
        let op = self.runtime.describe(id).await?;
        if let Some(turn) = op.operation.turn {
            return self.interrupt(turn).await;
        }
        if matches!(
            op.operation.status,
            Status::AwaitingApproval | Status::Ready
        ) {
            self.runtime
                .mutate(Mutation {
                    id: Uuid::new_v4(),
                    command: Command::Cancel { operation: id },
                })
                .await?;
        } else if let Some(job) = self.jobs.lock().unwrap().get(&id) {
            let _ = job.cancel.send(true);
        }
        self.changed.notify_waiters();
        self.runtime.operation_result(id).await
    }
    pub async fn shutdown(&self) {
        let jobs = std::mem::take(&mut *self.jobs.lock().unwrap());
        for job in jobs.values() {
            let _ = job.cancel.send(true);
        }
        for id in jobs.keys() {
            if self.runtime.turn(*id).await.is_ok() {
                let _ = self.interrupt(*id).await;
            }
        }
        self.changed.notify_waiters();
        let deadline = tokio::time::Instant::now() + Duration::from_secs(8);
        for (_, mut job) in jobs {
            if tokio::time::timeout_at(deadline, &mut job.task)
                .await
                .is_err()
            {
                job.task.abort();
                let _ = job.task.await;
            }
        }
    }
    async fn drive(
        self: &Arc<Self>,
        id: Uuid,
        mut cancel: watch::Receiver<bool>,
    ) -> anyhow::Result<()> {
        let initial = self.runtime.turn(id).await?;
        let config: ProviderConfig =
            serde_json::from_value(self.runtime.artifacts.json(&initial.config_ref)?)?;
        if *cancel.borrow() {
            return Ok(());
        }
        let initial_view = self.runtime.turn(id).await?;
        let history = initial_view
            .history
            .iter()
            .map(|reference| self.runtime.artifacts.json(reference))
            .collect::<anyhow::Result<Vec<_>>>()?;
        let workspace = self
            .runtime
            .session(initial_view.turn.session)
            .await?
            .workspace;
        let inventory = if host_env::container_subject(&history) {
            let observed = tokio::select! {
                result = host_env::containers("") => match result {
                    Ok(value) => value,
                    Err(error) => json!({"error":error.to_string(),"observedAt":chrono::Utc::now()}),
                },
                _ = cancel.changed() => return Ok(()),
            };
            Some(observed)
        } else {
            None
        };
        let host_context = host_env::context_message(host_env::info(&workspace), inventory);
        let mut requests = 0_u64;
        // Tool work since the last goal check; a final answer after work gets one check first.
        let mut worked = false;
        loop {
            if *cancel.borrow() {
                return Ok(());
            }
            let view = self.runtime.turn(id).await?;
            if view.turn.phase != "running" {
                return Ok(());
            }
            if config
                .max_requests
                .is_some_and(|limit| requests >= u64::from(limit))
            {
                bail!("turn_request_limit_reached");
            }
            requests = requests.saturating_add(1);
            let mut messages = vec![
                json!({"role":"system","content":"You are Synth Code, a local coding agent. Use read_file/search to inspect the workspace. Use edit_file for complete file creation/replacement and run_command for checks; the runtime enforces the session permission mode and requests user approval when required. Do not claim an effect happened until the tool result confirms it. Treat denials and unknown effects honestly. Built-in read/search/edit paths stay inside the selected workspace. You have host shell access: use host_info and list_containers to discover the actual host and existing container context, then run_command for user-requested host/project/container work, including outside the file-tool workspace. Never infer that no container or project exists on the host from an empty workspace. Container inventory is an observed snapshot: Created or stopped does not mean Running. Inspect ownership before changing shared containers; do not start/stop/restart foreign jobs or paid experiments without authorization. Do not read credentials or introduce authentication mechanisms. Host commands are not an OS sandbox. Finish with a concise report of actual changes and checks."}),
            ];
            let mut context = messages[0]["content"].as_str().unwrap().to_owned();
            if let Ok(instructions) = self.runtime.read_file(view.turn.session, "AGENTS.md").await {
                context.push_str(&format!("\n\nWorkspace AGENTS.md:\n{}", instructions.text));
            }
            context.push_str("\n\n");
            context.push_str(host_context["content"].as_str().unwrap());
            messages[0]["content"] = json!(context);
            for reference in &view.history {
                messages.push(self.runtime.artifacts.json(reference)?);
            }
            messages = repair_history(messages);
            let request_ref = self
                .runtime
                .artifacts
                .put_json(&json!({"config":config,"messages":messages}))?;
            let attempt = Uuid::new_v4();
            self.runtime
                .agent_mutate(
                    Uuid::new_v4(),
                    TurnCommand::ModelBegin {
                        turn: id,
                        attempt,
                        request_ref,
                        epoch: view.turn.epoch,
                    },
                )
                .await?;
            self.live.lock().unwrap().insert(id, String::new());
            let completion = tokio::select! {
                value=config.complete(&messages,|text|{let mut live=self.live.lock().unwrap();let entry=live.entry(id).or_default();for ch in text.chars(){if entry.len()+ch.len_utf8()>32768{break}entry.push(ch);}})=>value?,
                _=cancel.changed()=>return Ok(()),
            };
            let message_ref = self.runtime.artifacts.put_json(&completion.message)?;
            let visible = completion.message["content"].as_str().unwrap_or("");
            let display_ref = self.runtime.artifacts.put(visible.as_bytes())?;
            self.runtime
                .agent_mutate(
                    Uuid::new_v4(),
                    TurnCommand::ModelFinish {
                        turn: id,
                        attempt,
                        message_ref,
                        display_ref,
                        usage: completion.usage,
                        provider_model: completion.model,
                        epoch: view.turn.epoch,
                    },
                )
                .await?;
            self.live.lock().unwrap().remove(&id);
            let Some(calls) = completion.message["tool_calls"].as_array() else {
                if std::mem::take(&mut worked) {
                    let message_ref = self
                        .runtime
                        .artifacts
                        .put_json(&json!({"role":"user","content":CONTINUE_CHECK}))?;
                    let display_ref = self.runtime.artifacts.put(CONTINUE_LABEL.as_bytes())?;
                    self.runtime
                        .agent_mutate(
                            Uuid::new_v4(),
                            TurnCommand::Continue {
                                turn: id,
                                message_ref,
                                display_ref,
                                epoch: view.turn.epoch,
                            },
                        )
                        .await?;
                    continue;
                }
                self.runtime
                    .agent_mutate(
                        Uuid::new_v4(),
                        TurnCommand::Finish {
                            turn: id,
                            phase: "completed".into(),
                            error: None,
                            epoch: view.turn.epoch,
                        },
                    )
                    .await?;
                return Ok(());
            };
            worked |= !calls.is_empty();
            for (index, call) in calls.iter().enumerate() {
                if *cancel.borrow() {
                    return Ok(());
                }
                let call_id = call["id"].as_str().context("missing_call_id")?;
                let name = call["function"]["name"]
                    .as_str()
                    .context("missing_tool_name")?;
                let arguments: Value = serde_json::from_str(
                    call["function"]["arguments"]
                        .as_str()
                        .context("missing_tool_arguments")?,
                )?;
                let mutation = Uuid::new_v5(&attempt, format!("tool-{index}-{call_id}").as_bytes());
                let result = self
                    .tool(
                        id,
                        view.turn.session,
                        mutation,
                        name,
                        arguments,
                        &mut cancel,
                    )
                    .await;
                if *cancel.borrow() {
                    return Ok(());
                }
                for operation in &self.runtime.turn(id).await?.turn.operations {
                    if self.runtime.describe(*operation).await?.operation.status == Status::Unknown
                    {
                        bail!("effect_outcome_unknown; inspect workspace before continuing");
                    }
                }
                let content = match result {
                    Ok(value) => serde_json::to_string(&value)?,
                    Err(error) => serde_json::to_string(&json!({"error":error.to_string()}))?,
                };
                let message_ref = self
                    .runtime
                    .artifacts
                    .put_json(&json!({"role":"tool","tool_call_id":call_id,"content":content}))?;
                self.runtime
                    .agent_mutate(
                        Uuid::new_v4(),
                        TurnCommand::ToolResult {
                            turn: id,
                            message_ref,
                            epoch: view.turn.epoch,
                        },
                    )
                    .await?;
            }
        }
    }
    async fn tool(
        &self,
        turn: Uuid,
        session: Uuid,
        mutation: Uuid,
        name: &str,
        args: Value,
        cancel: &mut watch::Receiver<bool>,
    ) -> anyhow::Result<Value> {
        fn string<'a>(v: &'a Value, k: &str) -> anyhow::Result<&'a str> {
            v[k].as_str().context("tool_argument_requires_string")
        }
        let allowed = match name {
            "host_info" => vec![],
            "list_containers" => vec!["query"],
            "read_file" => vec!["path"],
            "search" => vec!["query"],
            "edit_file" => vec!["path", "expectedSha256", "content"],
            "run_command" => vec!["command", "timeout_seconds"],
            _ => bail!("unsupported_tool"),
        };
        ensure!(
            args.as_object().is_some_and(
                |v| v.len() == allowed.len() && v.keys().all(|k| allowed.contains(&k.as_str()))
            ),
            "invalid_tool_arguments"
        );
        if let Some(marker) = crate::provider::leaked_template_marker(&args) {
            // Never admit an effect from a corrupted call; the model receives this as the tool result.
            bail!(
                "provider_tool_arguments_contaminated: provider stage tool_call_parse leaked {marker:?} \
                 into the arguments; nothing was executed. Re-issue the {name} call with clean arguments"
            );
        }
        match name {
            "host_info" => {
                return Ok(crate::host_env::info(
                    &self.runtime.session(session).await?.workspace,
                ));
            }
            "list_containers" => {
                return tokio::select! {
                    value=crate::host_env::containers(string(&args,"query")?)=>value,
                    _=cancel.changed()=>Ok(json!({"canceled":true})),
                };
            }
            "read_file" => {
                return Ok(serde_json::to_value(
                    self.runtime
                        .read_file(session, string(&args, "path")?)
                        .await?,
                )?);
            }
            "search" => {
                return Ok(serde_json::to_value(
                    self.runtime
                        .search_files(session, string(&args, "query")?)
                        .await?,
                )?);
            }
            _ => {}
        }
        let admission = if name == "edit_file" {
            let expected = match &args["expectedSha256"] {
                Value::Null => None,
                Value::String(s) => Some(s.as_str()),
                _ => bail!("invalid_expected_hash"),
            };
            self.runtime
                .propose_turn_edit(
                    mutation,
                    session,
                    turn,
                    string(&args, "path")?,
                    expected,
                    string(&args, "content")?.into(),
                )
                .await?
        } else {
            self.runtime
                .propose_turn_shell(
                    mutation,
                    session,
                    turn,
                    crate::command::ShellRequest {
                        command: string(&args, "command")?.into(),
                        timeout_seconds: args["timeout_seconds"]
                            .as_u64()
                            .context("invalid_command_timeout")?,
                        output_limit: 65536,
                    },
                )
                .await?
        };
        let operation: Uuid = admission.result["id"].as_str().unwrap().parse()?;
        self.changed.notify_waiters();
        loop {
            let view = self.runtime.describe(operation).await?;
            match view.operation.status {
                Status::AwaitingApproval | Status::Dispatched => {}
                Status::Ready => {
                    if *cancel.borrow() {
                        return Ok(json!({"canceled":true}));
                    }
                    if view.operation.shell.is_some() {
                        ensure!(
                            crate::store::digest(&std::fs::read(&self.executable)?)
                                == self.binary_sha256,
                            "native_runtime_changed"
                        );
                        let result = self
                            .runtime
                            .run_shell(operation, &self.executable, cancel.clone())
                            .await?;
                        return self.model_effect_result(operation, result).await;
                    }
                    return self.runtime.run_operation(operation, false).await;
                }
                _ => {
                    let result = self.runtime.operation_result(operation).await?;
                    return self.model_effect_result(operation, result).await;
                }
            }
            tokio::select! {_=cancel.changed()=>return Ok(json!({"canceled":true})),_=self.changed.notified()=>{},_=tokio::time::sleep(Duration::from_millis(100))=>{}}
        }
    }
    async fn model_effect_result(
        &self,
        operation: Uuid,
        mut result: Value,
    ) -> anyhow::Result<Value> {
        let view = self.runtime.describe(operation).await?;
        if let Some(reference) = view
            .operation
            .outcome
            .as_ref()
            .and_then(|value| value["output_artifact"].as_str())
        {
            result["command_output"] = self.runtime.artifacts.json(reference)?;
        }
        Ok(result)
    }
}

// An interrupted assistant tool batch must receive explicit synthetic failure
// replies before a later user prompt. This repairs protocol structure only;
// it never repeats a tool or claims that a missing result means no effect.
/// Sent when the model ends a turn after tool work. Models often stop after one step of a
/// multi-step task; the turn ends only when the model answers this without more tool calls.
const CONTINUE_CHECK: &str = "Synth Code check before finishing: re-read the task and any stopping rule it sets. If the task or stopping rule is not met yet, keep working now with tools; do not ask whether to continue. If it is met, or you are blocked, reply with the final report and no tool calls.";
const CONTINUE_LABEL: &str = "Checking the task's stopping rule before finishing";

fn repair_history(history: Vec<Value>) -> Vec<Value> {
    let mut result = Vec::new();
    let mut pending = Vec::<String>::new();
    for message in history {
        if message["role"] == "tool" {
            if let Some(id) = message["tool_call_id"].as_str() {
                pending.retain(|v| v != id);
            }
        } else {
            for id in pending.drain(..) {
                result.push(json!({"role":"tool","tool_call_id":id,"content":"The preceding turn was interrupted. No durable tool result is available; effects may be unknown. Do not repeat automatically."}));
            }
            if let Some(calls) = message["tool_calls"].as_array() {
                for call in calls {
                    if let Some(id) = call["id"].as_str() {
                        pending.push(id.into());
                    }
                }
            }
        }
        result.push(message);
    }
    result
}
