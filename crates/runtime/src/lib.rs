pub mod domain;
pub mod store;

use chrono::Utc;
use domain::{Command, Event, Mutation, State, Status};
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

struct Engine {
    store: SqliteStore,
    state: State,
}

impl Engine {
    async fn recover(store: SqliteStore) -> anyhow::Result<Self> {
        let mut engine = Self {
            store,
            state: State::default(),
        };
        for envelope in engine.store.read_events(JOURNAL, 0).await? {
            anyhow::ensure!(
                envelope.event_type == "synth_code.event.v1",
                "unsupported event type"
            );
            let event: Event = serde_json::from_value(envelope.payload)?;
            engine.state.apply(&event).map_err(anyhow::Error::msg)?;
        }
        let epoch = engine.state.epoch + 1;
        engine
            .persist(Event::Recovered { epoch }, Uuid::new_v4())
            .await?;
        Ok(engine)
    }

    async fn persist(&mut self, event: Event, cause: Uuid) -> anyhow::Result<()> {
        let envelope = PersistenceEnvelope {
            sequence_nr: self.state.sequence + 1,
            event_type: "synth_code.event.v1".into(),
            payload: serde_json::to_value(&event)?,
            metadata: EventMetadata {
                event_id: Uuid::new_v4(),
                causation_id: cause,
                correlation_id: cause,
                timestamp: Utc::now(),
                actor_id: "coordinator".into(),
            },
        };
        self.store
            .append(JOURNAL, self.state.sequence, &[envelope])
            .await?;
        // Event validity was checked before committing. Replay checks it again.
        self.state.apply(&event).map_err(anyhow::Error::msg)?;
        Ok(())
    }

    async fn apply(&mut self, mutation: Mutation) -> Result<Applied, String> {
        let (result, replayed) = self.state.decide(&mutation)?;
        if !replayed {
            let event = Event::Accepted {
                fingerprint: digest(
                    &serde_json::to_vec(&mutation.command).map_err(|e| e.to_string())?,
                ),
                mutation: mutation.clone(),
                response: result.clone(),
            };
            self.persist(event, mutation.id)
                .await
                .map_err(|e| e.to_string())?;
        }
        Ok(Applied { result, replayed })
    }

    fn view(&self, session: Option<Uuid>) -> Value {
        json!({"epoch":self.state.epoch,"sequence":self.state.sequence,
            "sessions":self.state.sessions.values().filter(|s|session.is_none_or(|id|id==s.id)).collect::<Vec<_>>(),
            "operations":self.state.operations.values().filter(|op|session.is_none_or(|id|id==op.session)).collect::<Vec<_>>()})
    }
}

#[derive(Debug)]
enum Request {
    Apply(Mutation),
    Read(Option<Uuid>),
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
}

impl Runtime {
    pub async fn open(directory: &Path) -> anyhow::Result<Self> {
        let engine = Engine::recover(SqliteStore::open(directory)?).await?;
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
        } = self;
        drop(actor);
        closed.notified().await;
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
