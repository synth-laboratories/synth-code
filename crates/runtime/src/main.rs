//! Local durable agent daemon and bounded stdio compatibility endpoint.
use anyhow::{Context, bail, ensure};
use std::{
    os::unix::fs::{FileTypeExt, PermissionsExt},
    path::PathBuf,
    sync::Arc,
};
use synth_code_runtime::{Runtime, host::Host, protocol, provider::ProviderConfig};

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let mut args = std::env::args().skip(1);
    let mode = args.next().context("mode required: serve or stdio")?;
    if mode == "supervise-command" {
        return synth_code_runtime::command::supervisor().await;
    }
    ensure!(
        ["serve", "stdio"].contains(&mode.as_str()),
        "unsupported mode"
    );
    let store = PathBuf::from(args.next().context("store required")?);
    let mut socket = None;
    let mut config = ProviderConfig::default();
    #[cfg_attr(not(feature = "test-hooks"), allow(unused_mut))]
    let mut fault = protocol::Fault::None;
    while let Some(flag) = args.next() {
        match flag.as_str() {
            "--socket" => socket = Some(PathBuf::from(args.next().context("socket required")?)),
            "--provider-config" => {
                let path = PathBuf::from(args.next().context("config path required")?);
                ensure!(path.metadata()?.len() < 16384, "config too large");
                config = serde_json::from_slice(&std::fs::read(path)?)?;
            }
            "--env-file" => {
                config.env_file = Some(PathBuf::from(args.next().context("env path required")?))
            }
            #[cfg(feature = "test-hooks")]
            "--test-crash-after-effect" => fault = protocol::Fault::AfterEffect,
            #[cfg(feature = "test-hooks")]
            "--test-crash-before-dispatch" => fault = protocol::Fault::BeforeDispatch,
            _ => bail!("unknown option"),
        }
    }
    ensure!(
        mode == "stdio" || fault == protocol::Fault::None,
        "fault injection is stdio-only"
    );
    config.validate()?;
    let runtime = Arc::new(Runtime::open(&store).await?);
    let host = Host::new(runtime.clone(), config, std::env::current_exe()?)?;
    if mode == "stdio" {
        let result =
            protocol::connection(tokio::io::stdin(), tokio::io::stdout(), host.clone(), fault)
                .await;
        host.shutdown().await;
        drop(host);
        Arc::try_unwrap(runtime)
            .map_err(|_| anyhow::anyhow!("runtime still owned"))?
            .close()
            .await;
        return result;
    }
    let socket = socket.context("serve requires --socket")?;
    ensure!(
        socket.as_os_str().as_encoded_bytes().len() < 104,
        "socket path too long"
    );
    let parent = socket.parent().context("socket parent required")?;
    ensure!(
        !std::fs::symlink_metadata(parent).is_ok_and(|m| m.file_type().is_symlink()),
        "socket parent symlink"
    );
    if parent.exists() {
        ensure!(
            parent.metadata()?.permissions().mode() & 0o077 == 0,
            "socket parent must be private"
        );
    } else {
        std::fs::create_dir_all(parent)?;
        std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700))?;
    }
    if let Ok(metadata) = std::fs::symlink_metadata(&socket) {
        ensure!(metadata.file_type().is_socket(), "socket path occupied");
        ensure!(
            tokio::net::UnixStream::connect(&socket).await.is_err(),
            "daemon already running"
        );
        std::fs::remove_file(&socket)?;
    }
    let listener = tokio::net::UnixListener::bind(&socket)?;
    std::fs::set_permissions(&socket, std::fs::Permissions::from_mode(0o600))?;
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut connections = tokio::task::JoinSet::new();
    loop {
        tokio::select! {
         accepted=listener.accept()=>{let (stream,_)=accepted?;if connections.len()>=32 {drop(stream);continue} let host=host.clone();connections.spawn(async move{let(r,w)=stream.into_split();let _=protocol::connection(r,w,host,protocol::Fault::None).await;});},
         _=host.stop.notified()=>break,
         _=terminate.recv()=>break,
         _=tokio::signal::ctrl_c()=>break,
         _=connections.join_next(),if !connections.is_empty()=>{}
        }
    }
    drop(listener);
    connections.abort_all();
    while connections.join_next().await.is_some() {}
    host.shutdown().await;
    drop(host);
    Arc::try_unwrap(runtime)
        .map_err(|_| anyhow::anyhow!("runtime still owned"))?
        .close()
        .await;
    std::fs::remove_file(socket)?;
    Ok(())
}
