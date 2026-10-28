//! Development protocol endpoint. Only synthetic approved effects are implemented.
use anyhow::{Context, bail};
use serde_json::{Value, json};
use std::os::unix::fs::MetadataExt;
use std::{
    io::{self, BufRead, Write},
    path::PathBuf,
};
use synth_code_runtime::{
    Runtime,
    domain::{Command, Mutation},
};
use uuid::Uuid;

fn uuid(params: &Value, key: &str) -> anyhow::Result<Uuid> {
    Ok(params[key].as_str().context("UUID required")?.parse()?)
}
fn text<'a>(params: &'a Value, key: &str) -> anyhow::Result<&'a str> {
    params[key].as_str().context("string required")
}

async fn request(runtime: &Runtime, method: &str, p: &Value, crash: bool) -> anyhow::Result<Value> {
    match method {
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
        "synth/status" => runtime.read(None).await,
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
        "workspace/proposeEdit" => {
            let expected = match p.get("expectedSha256") {
                Some(Value::Null) => None,
                Some(Value::String(s)) => Some(s.as_str()),
                _ => bail!("expectedSha256 required: string or null"),
            };
            Ok(serde_json::to_value(
                runtime
                    .propose_edit(
                        uuid(p, "mutationId")?,
                        uuid(p, "threadId")?,
                        text(p, "path")?,
                        expected,
                        text(p, "content")?.to_string(),
                    )
                    .await?,
            )?)
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
            if allow {
                runtime.run_operation(operation, crash).await
            } else {
                runtime.operation_result(operation).await
            }
        }
        _ => bail!("unsupported_method"),
    }
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let mut args = std::env::args().skip(1);
    if args.next().as_deref() != Some("stdio") {
        bail!("usage: synth-code-runtime stdio STORE [--fixture-crash-after-effect]");
    }
    let store = PathBuf::from(args.next().context("store path required")?);
    let crash = match args.next().as_deref() {
        None => false,
        Some("--fixture-crash-after-effect") => true,
        Some(_) => bail!("unknown option"),
    };
    anyhow::ensure!(args.next().is_none(), "unexpected argument");
    let runtime = Runtime::open(&store).await?;
    let stdin = io::stdin();
    let mut input = stdin.lock();
    let mut stdout = io::stdout().lock();
    let mut initialized = false;
    loop {
        // Bound reads before allocating an arbitrarily large protocol line.
        let mut bytes = Vec::new();
        let read = std::io::Read::take(&mut input, 65_537).read_until(b'\n', &mut bytes)?;
        if read == 0 {
            break;
        }
        if bytes.len() > 65_536 {
            bail!("protocol frame too large");
        }
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
                            json!({"protocolVersion":1,"version":env!("CARGO_PKG_VERSION"),"capabilities":{"fixture":true,"workspaceRead":true,"workspaceSearch":true,"workspaceEdit":true,"provider":false,"shell":false}}),
                        )
                    }
                } else if !initialized {
                    Err(anyhow::anyhow!("not_initialized"))
                } else {
                    request(&runtime, method, params, crash).await
                };
                match result {
                    Ok(result) => json!({"jsonrpc":"2.0","id":id,"result":result}),
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
        stdout.write_all(&encoded)?;
        stdout.write_all(b"\n")?;
        stdout.flush()?;
    }
    runtime.close().await;
    Ok(())
}
