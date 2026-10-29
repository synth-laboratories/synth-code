pub mod activity;
pub mod artifacts;
pub mod command;
pub mod domain;
pub mod host;
pub mod host_env;
pub mod protocol;
pub mod provider;
pub mod reach;
pub mod store;
pub mod turn;
pub mod workspace;

use chrono::Utc;
use domain::{Command, Event, Mutation, Operation, Session, State, Status};
use serde::Serialize;
use serde_json::{Value, json};
use std::{
    path::Path,
    sync::{Arc, Mutex},
    time::Duration,
};
use store::{SqliteStore, digest};
use temper_runtime::{
    ActorSystem,
    actor::{Actor, ActorContext, ActorError, ActorRef, Message},
    persistence::{EventMetadata, EventStore, PersistenceEnvelope},
};
use tokio::sync::Notify;
use uuid::Uuid;

const JOURNAL: &str = "local:Runtime:main";

#[derive(Debug, Clone, Serialize)]
pub struct Applied {
    pub result: Value,
    pub replayed: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct OperationView {
    pub epoch: u64,
    pub session: Session,
    pub operation: Operation,
    pub activity: Option<Value>,
}

#[derive(Clone)]
pub struct TurnView {
    pub turn: turn::Turn,
    pub config_ref: String,
    pub history: Vec<String>,
    pub activity: Option<Value>,
}
struct Engine {
    artifacts: artifacts::Artifacts,
    store: SqliteStore,
    state: State,
    activities: activity::Activities,
}

impl Engine {
    async fn recover(store: SqliteStore, artifacts: artifacts::Artifacts) -> anyhow::Result<Self> {
        let mut engine = Self {
            artifacts,
            store,
            state: State::default(),
            activities: activity::Activities::default(),
        };
        for envelope in engine.store.read_events(JOURNAL, 0).await? {
            anyhow::ensure!(
                envelope.event_type == "synth_code.event.v1",
                "unsupported event type"
            );
            let event: Event = serde_json::from_value(envelope.payload)?;
            engine.state.apply(&event).map_err(anyhow::Error::msg)?;
            engine
                .activities
                .observe(&event, envelope.metadata.timestamp, &engine.state);
        }
        for hash in &engine.state.turns.required_artifacts {
            engine.artifacts.get(hash)?;
        }
        for op in engine.state.operations.values() {
            if let Some(hash) = op
                .outcome
                .as_ref()
                .and_then(|v| v["output_artifact"].as_str())
            {
                engine.artifacts.get(hash)?;
            }
        }
        let epoch = engine.state.epoch + 1;
        engine
            .persist(Event::Recovered { epoch }, Uuid::new_v4())
            .await?;
        Ok(engine)
    }

    async fn persist(&mut self, event: Event, cause: Uuid) -> anyhow::Result<()> {
        let timestamp = Utc::now();
        let envelope = PersistenceEnvelope {
            sequence_nr: self.state.sequence + 1,
            event_type: "synth_code.event.v1".into(),
            payload: serde_json::to_value(&event)?,
            metadata: EventMetadata {
                event_id: Uuid::new_v4(),
                causation_id: cause,
                correlation_id: cause,
                timestamp,
                actor_id: "coordinator".into(),
            },
        };
        self.store
            .append(JOURNAL, self.state.sequence, &[envelope])
            .await?;
        // Event validity was checked before committing. Replay checks it again.
        self.state.apply(&event).map_err(anyhow::Error::msg)?;
        self.activities.observe(&event, timestamp, &self.state);
        Ok(())
    }

    async fn apply(&mut self, mutation: Mutation) -> Result<Applied, String> {
        let (result, replayed) = self.state.decide(&mutation)?;
        if !replayed {
            let event = Event::Accepted {
                fingerprint: digest(
                    &serde_json::to_vec(&mutation.command).map_err(|e| e.to_string())?,
                ),
                mutation: Box::new(mutation.clone()),
                response: result.clone(),
            };
            self.persist(event, mutation.id)
                .await
                .map_err(|e| e.to_string())?;
        }
        Ok(Applied { result, replayed })
    }

    fn view(&self, session: Option<Uuid>) -> Value {
        let mut operations = self
            .state
            .operations
            .values()
            .filter(|op| session.is_none_or(|id| id == op.session))
            .collect::<Vec<_>>();
        operations.sort_by_key(|op| op.admitted_sequence);
        json!({"epoch":self.state.epoch,"sequence":self.state.sequence,
            "sessions":self.state.sessions.values().filter(|s|session.is_none_or(|id|id==s.id)).collect::<Vec<_>>(),
            "operations":operations})
    }

    fn page(&self, session: Uuid, after: u64, at_sequence: Option<u64>) -> Result<Value, String> {
        let session_info = self
            .state
            .sessions
            .get(&session)
            .ok_or("session_not_found")?;
        if at_sequence.is_some_and(|s| s != self.state.sequence)
            || (after > 0 && at_sequence.is_none())
        {
            return Err("stale_history_cursor".into());
        }
        let mut operations = self
            .state
            .operations
            .values()
            .filter(|op| op.session == session && op.admitted_sequence > after)
            .collect::<Vec<_>>();
        operations.sort_by_key(|op| op.admitted_sequence);
        let mut result = json!({"epoch":self.state.epoch,"sequence":self.state.sequence,"sessions":[session_info],"operations":[],"nextCursor":null});
        let mut size = serde_json::to_vec(&result)
            .map_err(|e| e.to_string())?
            .len();
        let mut next = None;
        for (index, operation) in operations.iter().enumerate() {
            let bytes = serde_json::to_vec(operation)
                .map_err(|e| e.to_string())?
                .len()
                + 1;
            if size + bytes > 60_000 || index >= 32 {
                if index == 0 {
                    return Err("history_item_too_large".into());
                }
                next = Some(operations[index - 1].admitted_sequence);
                break;
            }
            size += bytes;
            result["operations"]
                .as_array_mut()
                .unwrap()
                .push(serde_json::to_value(operation).map_err(|e| e.to_string())?);
        }
        result["nextCursor"] = json!(next);
        Ok(result)
    }

    fn catalog(&self, after: Option<Uuid>) -> Value {
        let remaining = self
            .state
            .sessions
            .values()
            .filter(|s| after.is_none_or(|id| s.id > id))
            .collect::<Vec<_>>();
        let mut sessions = Vec::new();
        let mut size = 128;
        let mut next = None;
        for (index, session) in remaining.iter().enumerate() {
            let bytes = serde_json::to_vec(session)
                .expect("serializable session")
                .len();
            if size + bytes > 60_000 || index >= 16 {
                next = sessions.last().map(|s: &&Session| s.id);
                break;
            }
            sessions.push(session);
            size += bytes;
        }
        json!({"sessions":sessions,"nextCursor":next})
    }
}

#[derive(Debug)]
enum Request {
    Apply(Mutation),
    Read(Option<Uuid>),
    Session(Uuid),
    Operation(Uuid),
    Page {
        session: Uuid,
        after: u64,
        at_sequence: Option<u64>,
    },
    Catalog(Option<Uuid>),
    Turn(Uuid),
    TurnList(Uuid),
}
impl Message for Request {}
struct Coordinator {
    initial: Mutex<Option<Engine>>,
    closed: Arc<Notify>,
}

impl Actor for Coordinator {
    type Msg = Request;
    type State = Engine;
    async fn pre_start(&self, _: &mut ActorContext<Self>) -> Result<Engine, ActorError> {
        self.initial
            .lock()
            .map_err(|_| ActorError::custom("initial state poisoned"))?
            .take()
            .ok_or_else(|| ActorError::custom("implicit actor restart refused"))
    }
    async fn handle(
        &self,
        msg: Request,
        state: &mut Engine,
        ctx: &mut ActorContext<Self>,
    ) -> Result<(), ActorError> {
        match msg {
            Request::Apply(mutation) => ctx.reply(state.apply(mutation).await),
            Request::Read(session) => ctx.reply(state.view(session)),
            Request::Page {
                session,
                after,
                at_sequence,
            } => ctx.reply(state.page(session, after, at_sequence)),
            Request::Catalog(after) => ctx.reply(state.catalog(after)),
            Request::Turn(id) => ctx.reply(
                state
                    .state
                    .turns
                    .items
                    .get(&id)
                    .map(|turn| TurnView {
                        turn: turn.clone(),
                        activity: state.activities.turns.get(&id).map(|a| {
                            a.public(matches!(turn.phase.as_str(), "running" | "waiting"))
                        }),
                        config_ref: state.state.turns.configs[&id].clone(),
                        history: state
                            .state
                            .turns
                            .histories
                            .get(&turn.session)
                            .cloned()
                            .unwrap_or_default(),
                    })
                    .ok_or_else(|| "turn_not_found".to_string()),
            ),
            Request::TurnList(session) => ctx.reply(
                state
                    .state
                    .turns
                    .items
                    .values()
                    .filter(|t| t.session == session)
                    .cloned()
                    .collect::<Vec<_>>(),
            ),
            Request::Session(id) => ctx.reply(
                state
                    .state
                    .sessions
                    .get(&id)
                    .cloned()
                    .ok_or_else(|| "session_not_found".to_string()),
            ),
            Request::Operation(id) => ctx.reply(
                state
                    .state
                    .operations
                    .get(&id)
                    .map(|operation| OperationView {
                        epoch: state.state.epoch,
                        session: state.state.sessions[&operation.session].clone(),
                        operation: operation.clone(),
                        activity: state
                            .activities
                            .operations
                            .get(&id)
                            .map(|a| a.public(operation.status == Status::Dispatched)),
                    })
                    .ok_or_else(|| "operation_not_found".to_string()),
            ),
        }
        Ok(())
    }
    async fn post_stop(&self, state: Engine, _: &mut ActorContext<Self>) {
        drop(state);
        self.closed.notify_one();
    }
}

pub struct Runtime {
    actor: ActorRef<Request>,
    _system: ActorSystem,
    closed: Arc<Notify>,
    pub artifacts: artifacts::Artifacts,
}

impl Runtime {
    pub async fn open(directory: &Path) -> anyhow::Result<Self> {
        let store = SqliteStore::open(directory)?;
        let artifacts = artifacts::Artifacts::open(directory)?;
        let engine = Engine::recover(store, artifacts.clone()).await?;
        let system = ActorSystem::new("synth-code");
        let closed = Arc::new(Notify::new());
        let actor = system.spawn(
            Coordinator {
                initial: Mutex::new(Some(engine)),
                closed: closed.clone(),
            },
            "coordinator",
        );
        Ok(Self {
            actor,
            _system: system,
            closed,
            artifacts,
        })
    }
    pub async fn mutate(&self, mutation: Mutation) -> anyhow::Result<Applied> {
        self.actor
            .ask::<Result<Applied, String>>(Request::Apply(mutation), Duration::from_secs(5))
            .await?
            .map_err(anyhow::Error::msg)
    }
    pub async fn read(&self, session: Option<Uuid>) -> anyhow::Result<Value> {
        Ok(self
            .actor
            .ask(Request::Read(session), Duration::from_secs(5))
            .await?)
    }
    pub async fn close(self) {
        let Self {
            actor,
            _system,
            closed,
            artifacts: _,
        } = self;
        drop(actor);
        closed.notified().await;
    }

    pub async fn session(&self, id: Uuid) -> anyhow::Result<Session> {
        self.actor
            .ask::<Result<Session, String>>(Request::Session(id), Duration::from_secs(5))
            .await?
            .map_err(anyhow::Error::msg)
    }
    pub async fn history_page(
        &self,
        session: Uuid,
        after: u64,
        at_sequence: Option<u64>,
    ) -> anyhow::Result<Value> {
        self.actor
            .ask::<Result<Value, String>>(
                Request::Page {
                    session,
                    after,
                    at_sequence,
                },
                Duration::from_secs(5),
            )
            .await?
            .map_err(anyhow::Error::msg)
    }
    pub async fn sessions(&self, after: Option<Uuid>) -> anyhow::Result<Value> {
        Ok(self
            .actor
            .ask(Request::Catalog(after), Duration::from_secs(5))
            .await?)
    }
    pub async fn describe(&self, id: Uuid) -> anyhow::Result<OperationView> {
        self.actor
            .ask::<Result<OperationView, String>>(Request::Operation(id), Duration::from_secs(5))
            .await?
            .map_err(anyhow::Error::msg)
    }
    pub async fn read_file(
        &self,
        session: Uuid,
        path: &str,
    ) -> anyhow::Result<workspace::ReadResult> {
        workspace::Workspace::open(&self.session(session).await?)?.read(path)
    }
    pub async fn search_files(
        &self,
        session: Uuid,
        query: &str,
    ) -> anyhow::Result<workspace::SearchResult> {
        workspace::Workspace::open(&self.session(session).await?)?.search(query)
    }
    pub async fn propose_edit(
        &self,
        id: Uuid,
        session: Uuid,
        path: &str,
        expected_sha256: Option<&str>,
        content: String,
    ) -> anyhow::Result<Applied> {
        let operation = Uuid::new_v5(&id, b"operation");
        // Replay the stored admission before consulting the changed filesystem.
        let request = match self.describe(operation).await {
            Ok(existing) => {
                let request = existing
                    .operation
                    .edit
                    .ok_or_else(|| anyhow::anyhow!("mutation_conflict"))?;
                anyhow::ensure!(
                    existing.session.id == session
                        && request.path == path
                        && request.content == content
                        && request.expected.as_ref().map(|v| v.sha256.as_str()) == expected_sha256,
                    "mutation_conflict"
                );
                request
            }
            Err(error) if error.to_string() == "operation_not_found" => workspace::Workspace::open(
                &self.session(session).await?,
            )?
            .prepare_edit(path, expected_sha256, content)?,
            Err(error) => return Err(error),
        };
        self.mutate(Mutation {
            id,
            command: Command::AdmitEdit {
                session,
                operation,
                request,
                turn: None,
            },
        })
        .await
    }
    pub async fn operation_result(&self, id: Uuid) -> anyhow::Result<Value> {
        let view = self.describe(id).await?;
        Ok(json!({"epoch":view.epoch,"sessions":[view.session],"operations":[view.operation]}))
    }
    pub async fn turn(&self, id: Uuid) -> anyhow::Result<TurnView> {
        self.actor
            .ask::<Result<TurnView, String>>(Request::Turn(id), Duration::from_secs(5))
            .await?
            .map_err(anyhow::Error::msg)
    }
    pub async fn turn_list(&self, session: Uuid) -> anyhow::Result<Vec<turn::Turn>> {
        Ok(self
            .actor
            .ask(Request::TurnList(session), Duration::from_secs(5))
            .await?)
    }
    pub async fn agent_mutate(
        &self,
        id: Uuid,
        request: turn::TurnCommand,
    ) -> anyhow::Result<Applied> {
        self.mutate(Mutation {
            id,
            command: Command::Agent { request },
        })
        .await
    }
    pub async fn propose_turn_edit(
        &self,
        id: Uuid,
        session: Uuid,
        turn: Uuid,
        path: &str,
        expected: Option<&str>,
        content: String,
    ) -> anyhow::Result<Applied> {
        let request = workspace::Workspace::open(&self.session(session).await?)?
            .prepare_edit(path, expected, content)?;
        self.mutate(Mutation {
            id,
            command: Command::AdmitEdit {
                session,
                operation: Uuid::new_v5(&id, b"operation"),
                request,
                turn: Some(turn),
            },
        })
        .await
    }
    pub async fn propose_turn_shell(
        &self,
        id: Uuid,
        session: Uuid,
        turn: Uuid,
        request: command::ShellRequest,
    ) -> anyhow::Result<Applied> {
        self.mutate(Mutation {
            id,
            command: Command::AdmitShell {
                session,
                operation: Uuid::new_v5(&id, b"operation"),
                request,
                turn: Some(turn),
            },
        })
        .await
    }
    pub async fn propose_shell(
        &self,
        id: Uuid,
        session: Uuid,
        request: command::ShellRequest,
    ) -> anyhow::Result<Applied> {
        request.validate()?;
        self.mutate(Mutation {
            id,
            command: Command::AdmitShell {
                session,
                operation: Uuid::new_v5(&id, b"operation"),
                request,
                turn: None,
            },
        })
        .await
    }
    pub async fn run_shell(
        &self,
        id: Uuid,
        executable: &Path,
        cancel: tokio::sync::watch::Receiver<bool>,
    ) -> anyhow::Result<Value> {
        self.run_shell_admitted(id, executable, cancel, None).await
    }
    pub(crate) async fn run_shell_admitted(
        &self,
        id: Uuid,
        executable: &Path,
        cancel: tokio::sync::watch::Receiver<bool>,
        admission: Option<tokio::sync::oneshot::Sender<Result<(), String>>>,
    ) -> anyhow::Result<Value> {
        let admitted: anyhow::Result<Option<(OperationView, command::ShellRequest)>> = async {
            let view = self.describe(id).await?;
            let request = view
                .operation
                .shell
                .clone()
                .ok_or_else(|| anyhow::anyhow!("not_command_operation"))?;
            if view.operation.status != Status::Ready {
                return Ok(None);
            }
            let dispatched = self
                .mutate(Mutation {
                    id: Uuid::new_v5(&id, b"dispatch"),
                    command: Command::Dispatch {
                        operation: id,
                        epoch: view.epoch,
                    },
                })
                .await?;
            Ok((!dispatched.replayed).then_some((view, request)))
        }
        .await;
        if let Some(admission) = admission {
            let _ = admission.send(admitted.as_ref().map(|_| ()).map_err(ToString::to_string));
        }
        let Some((view, request)) = admitted? else {
            return self.operation_result(id).await;
        };
        let result = command::run(
            executable,
            command::SupervisorInput {
                session: view.session,
                request,
            },
            cancel,
        )
        .await;
        let command = match result {
            Ok(output) => match self.artifacts.put_json(&output) {
                Ok(reference) => {
                    let success =
                        output.exit_code == Some(0) && !output.timed_out && !output.gateway_lost;
                    Command::Settle {
                        operation: id,
                        epoch: view.epoch,
                        success,
                        outcome: json!({"exit_code":output.exit_code,"timed_out":output.timed_out,"gateway_lost":output.gateway_lost,"output_artifact":reference,"bytes":output.bytes,"truncated":output.truncated,"stdout":output.stdout.chars().take(384).collect::<String>(),"stderr":output.stderr.chars().take(384).collect::<String>()}),
                    }
                }
                Err(error) => Command::MarkUnknown {
                    operation: id,
                    epoch: view.epoch,
                    reason: format!(
                        "command_output_not_retained_after_exit {:?}: {error}",
                        output.exit_code
                    )
                    .chars()
                    .take(200)
                    .collect(),
                },
            },
            Err(error) => Command::MarkUnknown {
                operation: id,
                epoch: view.epoch,
                reason: error.to_string().chars().take(200).collect(),
            },
        };
        self.mutate(Mutation {
            id: Uuid::new_v5(&id, b"settle"),
            command,
        })
        .await?;
        self.operation_result(id).await
    }
    pub async fn run_operation(&self, id: Uuid, crash_after_effect: bool) -> anyhow::Result<Value> {
        if self.describe(id).await?.operation.shell.is_some() {
            let (_cancel, receiver) = tokio::sync::watch::channel(false);
            return self
                .run_shell(id, &std::env::current_exe()?, receiver)
                .await;
        }
        let view = self.describe(id).await?;
        let Some(edit) = view.operation.edit else {
            return self.run_fixture(id, crash_after_effect).await;
        };
        if view.operation.status != Status::Ready {
            return self.operation_result(id).await;
        }
        let dispatched = self
            .mutate(Mutation {
                id: Uuid::new_v5(&id, b"dispatch"),
                command: Command::Dispatch {
                    operation: id,
                    epoch: view.epoch,
                },
            })
            .await?;
        if dispatched.replayed {
            return self.operation_result(id).await;
        }
        let result = match workspace::Workspace::open(&view.session) {
            Ok(workspace) => workspace.apply_edit(id, &edit, crash_after_effect),
            Err(error) => Err(workspace::EditFailure {
                uncertain: false,
                error: error.to_string(),
            }),
        };
        let command = match result {
            Ok(outcome) => Command::Settle {
                operation: id,
                epoch: view.epoch,
                success: true,
                outcome: serde_json::to_value(outcome)?,
            },
            Err(error) if error.uncertain => Command::MarkUnknown {
                operation: id,
                epoch: view.epoch,
                reason: error.error.chars().take(200).collect(),
            },
            Err(error) => Command::Settle {
                operation: id,
                epoch: view.epoch,
                success: false,
                outcome: json!({"error":error.error}),
            },
        };
        self.mutate(Mutation {
            id: Uuid::new_v5(&id, b"settle"),
            command,
        })
        .await?;
        self.operation_result(id).await
    }

    /// A synthetic local effect only. General file/shell tools are not present.
    /// Dispatch identity replay is checked before touching the independent witness.
    pub async fn run_fixture(
        &self,
        operation: Uuid,
        crash_after_effect: bool,
    ) -> anyhow::Result<Value> {
        use std::io::Write;
        use std::os::{
            fd::{AsRawFd, FromRawFd},
            unix::fs::{MetadataExt, OpenOptionsExt},
        };
        anyhow::ensure!(
            self.describe(operation).await?.operation.edit.is_none()
                && self.describe(operation).await?.operation.shell.is_none(),
            "fixture_executor_refuses_edit"
        );
        let view = self.read(None).await?;
        let op: view_types::Operation = serde_json::from_value(
            view["operations"]
                .as_array()
                .unwrap()
                .iter()
                .find(|op| op["id"] == operation.to_string())
                .ok_or_else(|| anyhow::anyhow!("operation_not_found"))?
                .clone(),
        )?;
        if op.status != Status::Ready {
            return Ok(serde_json::to_value(op)?);
        }
        let epoch = view["epoch"].as_u64().unwrap();
        let dispatch = self
            .mutate(Mutation {
                id: Uuid::new_v5(&operation, b"dispatch"),
                command: Command::Dispatch { operation, epoch },
            })
            .await?;
        if dispatch.replayed {
            return self.read(None).await;
        }
        let session = &view["sessions"]
            .as_array()
            .unwrap()
            .iter()
            .find(|s| s["id"] == op.session.to_string())
            .unwrap();
        let workspace = Path::new(session["workspace"].as_str().unwrap());
        // Intentionally fixture-only: a new uniquely named witness is never overwritten.
        // A second dispatch attempt is observable as an error instead of clobbering evidence.
        let target = workspace.join(format!("fixture-{operation}.txt"));
        let result = (|| -> anyhow::Result<Value> {
            let directory = std::fs::OpenOptions::new()
                .read(true)
                .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
                .open(workspace)?;
            let metadata = directory.metadata()?;
            anyhow::ensure!(
                Some(metadata.dev()) == session["device"].as_u64()
                    && Some(metadata.ino()) == session["inode"].as_u64(),
                "workspace identity changed"
            );
            let name = std::ffi::CString::new(format!("fixture-{operation}.txt"))?;
            // The directory descriptor pins the approved root against rename/symlink races.
            let fd = unsafe {
                libc::openat(
                    directory.as_raw_fd(),
                    name.as_ptr(),
                    libc::O_WRONLY
                        | libc::O_CREAT
                        | libc::O_EXCL
                        | libc::O_NOFOLLOW
                        | libc::O_CLOEXEC,
                    0o600,
                )
            };
            if fd < 0 {
                return Err(std::io::Error::last_os_error().into());
            }
            // SAFETY: openat returned a new owned descriptor, transferred exactly once.
            let mut witness = unsafe { std::fs::File::from_raw_fd(fd) };
            witness.write_all(op.text.as_bytes())?;
            witness.sync_all()?;
            directory.sync_all()?;
            Ok(json!({"fixture":true,"path":target,"bytes":op.text.len()}))
        })();
        if crash_after_effect {
            std::process::exit(86);
        }
        let success = result.is_ok();
        let outcome = match result {
            Ok(value) => value,
            Err(error) => json!({"error":error.to_string()}),
        };
        self.mutate(Mutation {
            id: Uuid::new_v5(&operation, b"settle"),
            command: Command::Settle {
                operation,
                epoch,
                success,
                outcome,
            },
        })
        .await?;
        self.read(Some(op.session)).await
    }
}

mod view_types {
    pub use crate::domain::Operation;
}
