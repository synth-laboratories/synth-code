//! Bounded local JSON-RPC transport shared by stdio and Unix sockets.
use crate::{
    domain::{Command, Mutation},
    host::Host,
};
use anyhow::{Context, bail};
use serde_json::{Value, json};
use std::os::unix::fs::MetadataExt;
use tokio::io::{AsyncBufReadExt, AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt, BufReader};
use uuid::Uuid;
#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Fault {
    None,
    BeforeDispatch,
    AfterEffect,
}
fn uuid(params: &Value, key: &str) -> anyhow::Result<Uuid> {
    Ok(params[key].as_str().context("UUID required")?.parse()?)
}
fn text<'a>(params: &'a Value, key: &str) -> anyhow::Result<&'a str> {
    params[key].as_str().context("string required")
}

// Only a newly admitted auto-authorized proposal may start execution here.
// Replays read the durable outcome, including ready/unknown after a crash.
async fn proposed_effect(
    host: &std::sync::Arc<Host>,
    applied: crate::Applied,
    fault: Fault,
) -> anyhow::Result<Value> {
    let id: Uuid = applied.result["id"]
        .as_str()
        .context("operation required")?
        .parse()?;
    let view = host.runtime.describe(id).await?;
    if !applied.replayed && view.operation.status == crate::domain::Status::Ready {
        if fault == Fault::BeforeDispatch {
            std::process::exit(85);
        }
        if view.operation.shell.is_some() {
            host.launch_shell(id).await?;
        } else {
            host.runtime
                .run_operation(id, fault == Fault::AfterEffect)
                .await?;
        }
    }
    let view = host.runtime.describe(id).await?;
    Ok(
        json!({"result":view.operation,"permissions":view.session.permissions,"replayed":applied.replayed}),
    )
}

async fn request(
    host: &std::sync::Arc<Host>,
    method: &str,
    p: &Value,
    fault: Fault,
) -> anyhow::Result<Value> {
    let runtime = &host.runtime;
    match method {
        "synth/host/status" => Ok(crate::host_env::info(
            &runtime.session(uuid(p, "threadId")?).await?.workspace,
        )),
        "synth/host/containers" => {
            runtime.session(uuid(p, "threadId")?).await?;
            crate::host_env::containers(text(p, "query")?).await
        }
        "turn/start" => {
            host.start(
                uuid(p, "mutationId")?,
                uuid(p, "threadId")?,
                text(p, "prompt")?.into(),
                p.get("expectedPermissions")
                    .map(|v| serde_json::from_value(v.clone()))
                    .transpose()?,
            )
            .await
        }
        "turn/read" => host.read_turn(uuid(p, "turnId")?).await,
        "turn/message/read" => {
            let view = runtime.turn(uuid(p, "turnId")?).await?;
            let index = p["index"].as_u64().context("message index required")? as usize;
            let message = view.turn.messages.get(index).context("message not found")?;
            let text = String::from_utf8(runtime.artifacts.get(&message.content_ref)?)?;
            let chars = text.chars().collect::<Vec<_>>();
            let after = p["after"].as_u64().unwrap_or(0) as usize;
            anyhow::ensure!(after <= chars.len(), "invalid message cursor");
            let end = (after + 2048).min(chars.len());
            Ok(
                json!({"text":chars[after..end].iter().collect::<String>(),"nextCursor":if end<chars.len(){Some(end)}else{None}}),
            )
        }
        "turn/list" => {
            let after = p["after"].as_u64().unwrap_or(0);
            let mut all = runtime.turn_list(uuid(p, "threadId")?).await?;
            all.sort_by_key(|t| t.ordinal);
            let remaining = all
                .into_iter()
                .filter(|t| t.ordinal > after)
                .collect::<Vec<_>>();
            let turns = remaining
                .iter()
                .take(32)
                .map(|t| json!({"id":t.id,"phase":t.phase,"ordinal":t.ordinal}))
                .collect::<Vec<_>>();
            Ok(
                json!({"turns":turns,"nextCursor":if remaining.len()>32 {Some(remaining[31].ordinal)}else{None}}),
            )
        }
        "turn/interrupt" => host.interrupt(uuid(p, "turnId")?).await,
        "synth/permissions/status" => host.permissions_status(uuid(p, "threadId")?).await,
        "synth/permissions/configure" => {
            host.configure_permissions(
                uuid(p, "mutationId")?,
                uuid(p, "threadId")?,
                serde_json::from_value(p["mode"].clone())?,
                text(p, "expectedRevision")?.into(),
            )
            .await
        }
        "synth/provider/status" => {
            host.provider_status(p.get("threadId").map(|_| uuid(p, "threadId")).transpose()?)
                .await
        }
        "synth/provider/configure" => {
            host.configure_provider(
                uuid(p, "mutationId")?,
                uuid(p, "threadId")?,
                serde_json::from_value(p["selection"].clone())?,
                text(p, "expectedRevision")?.into(),
            )
            .await
        }
        "synth/provider/checkBinding" => {
            let expected = if let Some(path) = p["configFile"].as_str() {
                let path = std::path::Path::new(path);
                anyhow::ensure!(path.metadata()?.len() < 16384, "config too large");
                let mut c: crate::provider::ProviderConfig =
                    serde_json::from_slice(&std::fs::read(path)?)?;
                if let Some(env) = p["envFile"].as_str() {
                    c.env_file = Some(env.into());
                }
                c.env_file
            } else {
                p["envFile"].as_str().map(std::path::PathBuf::from)
            };
            anyhow::ensure!(
                expected == host.config.env_file,
                "daemon_credential_binding_differs"
            );
            Ok(json!({"matches":true}))
        }
        "synth/operation/cancel" => host.cancel_operation(uuid(p, "operationId")?).await,
        "synth/shutdown" => Ok(json!({"stopping":true})),
        "thread/start" => {
            let mutation = uuid(p, "mutationId")?;
            let session = Uuid::new_v5(&mutation, b"session");
            if let Ok(existing) = runtime.session(session).await {
                // An identical admission retry must not need the workspace to
                // still exist. Its durable identity has already been captured.
                let supplied = std::path::Path::new(text(p, "workspace")?);
                anyhow::ensure!(
                    supplied == std::path::Path::new(&existing.workspace)
                        || std::fs::canonicalize(supplied)
                            .is_ok_and(|p| p == std::path::Path::new(&existing.workspace)),
                    "mutation_conflict"
                );
                return Ok(serde_json::to_value(
                    runtime
                        .mutate(Mutation {
                            id: mutation,
                            command: Command::Start {
                                session,
                                workspace: existing.workspace,
                                device: existing.device,
                                inode: existing.inode,
                            },
                        })
                        .await?,
                )?);
            }
            let workspace = std::fs::canonicalize(text(p, "workspace")?)?;
            anyhow::ensure!(workspace.is_dir(), "workspace must be a directory");
            let metadata = workspace.metadata()?;
            Ok(serde_json::to_value(
                runtime
                    .mutate(Mutation {
                        id: mutation,
                        command: Command::Start {
                            session,
                            workspace: workspace
                                .to_str()
                                .context("workspace must be UTF-8")?
                                .into(),
                            device: metadata.dev(),
                            inode: metadata.ino(),
                        },
                    })
                    .await?,
            )?)
        }
        "thread/read" => {
            runtime
                .history_page(
                    uuid(p, "threadId")?,
                    p.get("after")
                        .map(|v| v.as_u64().context("after must be unsigned"))
                        .transpose()?
                        .unwrap_or(0),
                    p.get("atSequence")
                        .map(|v| v.as_u64().context("atSequence must be unsigned"))
                        .transpose()?,
                )
                .await
        }
        "thread/list" => {
            runtime
                .sessions(
                    p.get("after")
                        .map(|v| -> anyhow::Result<Uuid> {
                            Ok(v.as_str().context("after must be UUID")?.parse()?)
                        })
                        .transpose()?,
                )
                .await
        }
        "synth/status" => {
            let mut value = runtime.read(None).await?;
            value["ownerPid"] = json!(std::process::id());
            value["version"] = json!(env!("CARGO_PKG_VERSION"));
            value["runtimeSha256"] = json!(host.binary_sha256);
            Ok(value)
        }
        "workspace/read" => Ok(serde_json::to_value(
            runtime
                .read_file(uuid(p, "threadId")?, text(p, "path")?)
                .await?,
        )?),
        "workspace/search" => Ok(serde_json::to_value(
            runtime
                .search_files(uuid(p, "threadId")?, text(p, "query")?)
                .await?,
        )?),
        "workspace/proposeCommand" | "workspace/proposeEdit" => {
            let mutation = uuid(p, "mutationId")?;
            let session = uuid(p, "threadId")?;
            let applied = {
                let _settings = host.settings.lock().await;
                if runtime
                    .describe(Uuid::new_v5(&mutation, b"operation"))
                    .await
                    .is_err()
                {
                    host.check_permissions(
                        session,
                        p.get("expectedPermissions")
                            .map(|v| serde_json::from_value(v.clone()))
                            .transpose()?,
                    )
                    .await?;
                }
                if method == "workspace/proposeCommand" {
                    runtime
                        .propose_shell(
                            mutation,
                            session,
                            serde_json::from_value(p["request"].clone())?,
                        )
                        .await?
                } else {
                    let expected = match p.get("expectedSha256") {
                        Some(Value::Null) => None,
                        Some(Value::String(s)) => Some(s.as_str()),
                        _ => bail!("expectedSha256 required: string or null"),
                    };
                    runtime
                        .propose_edit(
                            mutation,
                            session,
                            text(p, "path")?,
                            expected,
                            text(p, "content")?.into(),
                        )
                        .await?
                }
            };
            proposed_effect(host, applied, fault).await
        }
        "synth/operation/reconcile" => {
            let id = uuid(p, "operationId")?;
            runtime
                .mutate(Mutation {
                    id: uuid(p, "mutationId")?,
                    command: Command::Reconcile {
                        operation: id,
                        request_digest: text(p, "requestDigest")?.into(),
                        observation: text(p, "observation")?.into(),
                    },
                })
                .await?;
            runtime.operation_result(id).await
        }
        "synth/operation/output" => {
            let op = runtime.describe(uuid(p, "operationId")?).await?;
            let reference = op
                .operation
                .outcome
                .as_ref()
                .and_then(|v| v["output_artifact"].as_str())
                .context("command_output_unavailable")?;
            let output = runtime.artifacts.json(reference)?;
            let stream = text(p, "stream")?;
            anyhow::ensure!(
                ["stdout", "stderr"].contains(&stream),
                "invalid output stream"
            );
            let text = output[stream].as_str().context("invalid output")?;
            let after = p["after"].as_u64().unwrap_or(0) as usize;
            let chars = text.chars().collect::<Vec<_>>();
            anyhow::ensure!(after <= chars.len(), "invalid output cursor");
            let end = (after + 2048).min(chars.len());
            Ok(
                json!({"text":chars[after..end].iter().collect::<String>(),"nextCursor":if end<chars.len(){Some(end)}else{None},"truncated":output["truncated"]}),
            )
        }
        "synth/operation/run" => {
            let id = uuid(p, "operationId")?;
            let view = runtime.describe(id).await?;
            anyhow::ensure!(view.operation.turn.is_none(), "turn_worker_owns_operation");
            anyhow::ensure!(
                view.operation.request_digest == text(p, "requestDigest")?,
                "stale_approval"
            );
            if view.operation.status != crate::domain::Status::Ready {
                return runtime.operation_result(id).await;
            }
            if view.operation.shell.is_some() {
                host.launch_shell(id).await
            } else {
                runtime.run_operation(id, false).await
            }
        }
        "synth/operation/read" => Ok(serde_json::to_value(
            runtime.describe(uuid(p, "operationId")?).await?,
        )?),
        "fixture/admit" => {
            let mutation = uuid(p, "mutationId")?;
            let operation = Uuid::new_v5(&mutation, b"operation");
            Ok(serde_json::to_value(
                runtime
                    .mutate(Mutation {
                        id: mutation,
                        command: Command::Admit {
                            session: uuid(p, "threadId")?,
                            operation,
                            text: text(p, "text")?.into(),
                        },
                    })
                    .await?,
            )?)
        }
        "synth/approval/respond" => {
            let operation = uuid(p, "operationId")?;
            let allow = p["allow"].as_bool().context("allow must be boolean")?;
            runtime
                .mutate(Mutation {
                    id: uuid(p, "mutationId")?,
                    command: Command::Approve {
                        operation,
                        request_digest: text(p, "requestDigest")?.into(),
                        allow,
                    },
                })
                .await?;
            host.changed.notify_waiters();
            let op = runtime.describe(operation).await?;
            if allow && fault == Fault::BeforeDispatch {
                std::process::exit(85);
            }
            if allow && op.operation.turn.is_none() {
                if op.operation.shell.is_some() {
                    host.launch_shell(operation).await
                } else {
                    runtime
                        .run_operation(operation, fault == Fault::AfterEffect)
                        .await
                }
            } else {
                runtime.operation_result(operation).await
            }
        }
        _ => bail!("unsupported_method"),
    }
}

pub async fn connection<R: AsyncRead + Unpin, W: AsyncWrite + Unpin>(
    reader: R,
    mut stdout: W,
    host: std::sync::Arc<Host>,
    fault: Fault,
) -> anyhow::Result<()> {
    let mut input = BufReader::new(reader);
    let mut initialized = false;
    loop {
        // Bound reads before allocating an arbitrarily large protocol line.
        let mut bytes = Vec::new();
        let read = (&mut input)
            .take(65_537)
            .read_until(b'\n', &mut bytes)
            .await?;
        if read == 0 {
            break;
        }
        if bytes.len() > 65_536 {
            bail!("protocol frame too large");
        }
        let mut request_shutdown = false;
        let response = match serde_json::from_slice::<Value>(&bytes) {
            Err(_) => {
                json!({"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"invalid JSON"}})
            }
            Ok(value) => {
                let valid_id = match value.get("id") {
                    Some(Value::String(s)) => s.len() <= 128,
                    Some(Value::Number(n)) => n.is_i64() || n.is_u64(),
                    _ => false,
                };
                let id = if valid_id {
                    value["id"].clone()
                } else {
                    Value::Null
                };
                let method = value["method"].as_str().unwrap_or("");
                let params = &value["params"];
                let result = if value["jsonrpc"] != "2.0"
                    || !valid_id
                    || method.len() > 128
                    || !params.is_object()
                {
                    Err(anyhow::anyhow!("invalid_request"))
                } else if method == "initialize" {
                    if initialized {
                        Err(anyhow::anyhow!("already_initialized"))
                    } else {
                        initialized = true;
                        Ok(
                            json!({"protocolVersion":1,"version":env!("CARGO_PKG_VERSION"),"runtimeSha256":host.binary_sha256,"capabilities":{"fixture":true,"workspaceRead":true,"workspaceSearch":true,"workspaceEdit":true,"provider":true,"shell":true,"daemon":true}}),
                        )
                    }
                } else if !initialized {
                    Err(anyhow::anyhow!("not_initialized"))
                } else {
                    request(&host, method, params, fault).await
                };
                match result {
                    Ok(result) => {
                        request_shutdown = method == "synth/shutdown";
                        json!({"jsonrpc":"2.0","id":id,"result":result})
                    }
                    Err(error) => {
                        json!({"jsonrpc":"2.0","id":id,"error":{"code":-32000,"message":error.to_string()}})
                    }
                }
            }
        };
        let mut encoded = serde_json::to_vec(&response)?;
        if encoded.len() > 65_535 {
            encoded = serde_json::to_vec(
                &json!({"jsonrpc":"2.0","id":response["id"],"error":{"code":-32000,"message":"response_too_large"}}),
            )?;
        }
        stdout.write_all(&encoded).await?;
        stdout.write_all(b"\n").await?;
        stdout.flush().await?;
        if request_shutdown {
            host.stop.notify_one();
            break;
        }
    }
    Ok(())
}
