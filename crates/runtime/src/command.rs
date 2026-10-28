//! Owned command supervisor. Approved host execution, not an OS sandbox.
use crate::domain::Session;
use anyhow::{Context, ensure};
use serde::{Deserialize, Serialize};
use std::{os::unix::process::CommandExt, process::Stdio, time::Duration};
use tokio::{
    io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt, BufReader},
    process::Command,
};

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ShellRequest {
    pub command: String,
    pub timeout_seconds: u64,
    pub output_limit: usize,
}
impl ShellRequest {
    pub fn validate(&self) -> anyhow::Result<()> {
        ensure!(
            !self.command.trim().is_empty()
                && self.command.len() <= 4096
                && !self.command.contains('\0'),
            "invalid_command"
        );
        ensure!(
            (1..=120).contains(&self.timeout_seconds)
                && (1024..=65536).contains(&self.output_limit),
            "invalid_command_limits"
        );
        Ok(())
    }
    pub fn fingerprint(&self) -> String {
        crate::store::digest(&serde_json::to_vec(self).expect("serializable shell request"))
    }
}
#[derive(Serialize, Deserialize)]
pub struct SupervisorInput {
    pub session: Session,
    pub request: ShellRequest,
}
#[derive(Debug, Serialize, Deserialize)]
pub struct ShellOutput {
    pub exit_code: Option<i32>,
    pub timed_out: bool,
    pub gateway_lost: bool,
    pub stdout: String,
    pub stderr: String,
    pub bytes: u64,
    pub truncated: bool,
}

/// The parent holds stdin open for the supervisor lifetime. EOF requests cleanup.
/// Neither the shell nor its children inherit model/account credentials.
pub async fn supervisor() -> anyhow::Result<()> {
    let mut input = BufReader::new(tokio::io::stdin());
    let mut line = String::new();
    (&mut input).take(16385).read_line(&mut line).await?;
    ensure!(line.len() <= 16384, "supervisor_frame_too_large");
    let configuration: SupervisorInput = serde_json::from_str(&line)?;
    configuration.request.validate()?;
    use std::os::{
        fd::AsRawFd,
        unix::fs::{MetadataExt, OpenOptionsExt},
    };
    let directory = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_DIRECTORY | libc::O_NOFOLLOW | libc::O_CLOEXEC)
        .open(&configuration.session.workspace)?;
    let metadata = directory.metadata()?;
    ensure!(
        metadata.dev() == configuration.session.device
            && metadata.ino() == configuration.session.inode,
        "workspace_identity_changed"
    );
    let fd = directory.as_raw_fd();
    let timeout = configuration.request.timeout_seconds;
    let mut shell = Command::new("/bin/sh");
    shell
        .args(["-c", &configuration.request.command])
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env_clear()
        .env("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
        .env("LANG", "C.UTF-8");
    // SAFETY: pre_exec uses only async-signal-safe syscalls and inherited numeric
    // descriptors. The captured directory stays live until spawning completes.
    unsafe {
        shell.as_std_mut().pre_exec(move || {
            if libc::setsid() < 0 || libc::fchdir(fd) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            let limits = [
                (libc::RLIMIT_CORE, 0),
                (libc::RLIMIT_FSIZE, 16 * 1024 * 1024),
                (libc::RLIMIT_NOFILE, 128),
                (libc::RLIMIT_CPU, timeout + 2),
            ];
            for (resource, value) in limits {
                let limit = libc::rlimit {
                    rlim_cur: value as libc::rlim_t,
                    rlim_max: value as libc::rlim_t,
                };
                if libc::setrlimit(resource, &limit) != 0 {
                    return Err(std::io::Error::last_os_error());
                }
            }
            Ok(())
        });
    }
    let mut child = shell.spawn().context("command_spawn_failed")?;
    let pid = child.id().context("missing_command_pid")? as i32;
    struct Group(i32);
    impl Drop for Group {
        fn drop(&mut self) {
            unsafe { libc::kill(-self.0, libc::SIGKILL) };
        }
    }
    let group = Group(pid);
    let limit = configuration.request.output_limit / 2;
    async fn capture(
        mut pipe: impl tokio::io::AsyncRead + Unpin,
        limit: usize,
    ) -> std::io::Result<(Vec<u8>, u64)> {
        let mut captured = Vec::new();
        let mut bytes = 0;
        let mut buffer = [0u8; 4096];
        loop {
            let n = pipe.read(&mut buffer).await?;
            if n == 0 {
                break;
            }
            bytes += n as u64;
            if captured.len() < limit {
                captured.extend_from_slice(&buffer[..n.min(limit - captured.len())]);
            }
        }
        Ok((captured, bytes))
    }
    let stdout = tokio::spawn(capture(child.stdout.take().unwrap(), limit));
    let stderr = tokio::spawn(capture(child.stderr.take().unwrap(), limit));
    let mut gateway_lost = false;
    let mut timed_out = false;
    let mut probe = [0u8; 1];
    let status = tokio::select! {
        status=child.wait()=>status?,
        _=tokio::time::sleep(Duration::from_secs(timeout))=>{timed_out=true;unsafe{libc::kill(-pid,libc::SIGKILL)};child.wait().await?},
        _=input.read(&mut probe)=>{gateway_lost=true;unsafe{libc::kill(-pid,libc::SIGKILL)};child.wait().await?},
    };
    drop(group); // also reap/terminate descendants after an early parent-shell exit
    let (out, out_bytes) = tokio::time::timeout(Duration::from_secs(2), stdout).await???;
    let (err, err_bytes) = tokio::time::timeout(Duration::from_secs(2), stderr).await???;
    let result = ShellOutput {
        exit_code: status.code(),
        timed_out,
        gateway_lost,
        stdout: String::from_utf8_lossy(&out).into_owned(),
        stderr: String::from_utf8_lossy(&err).into_owned(),
        bytes: out_bytes + err_bytes,
        truncated: out_bytes > out.len() as u64 || err_bytes > err.len() as u64,
    };
    tokio::io::stdout()
        .write_all(&[serde_json::to_vec(&result)?, vec![b'\n']].concat())
        .await?;
    Ok(())
}

pub async fn run(
    executable: &std::path::Path,
    input: SupervisorInput,
    cancel: tokio::sync::watch::Receiver<bool>,
) -> anyhow::Result<ShellOutput> {
    ensure!(!*cancel.borrow(), "command_canceled_before_spawn");
    let mut child = Command::new(executable)
        .arg("supervise-command")
        .env_clear()
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(false) // gateway EOF, rather than killing the supervisor, owns descendant cleanup
        .spawn()?;
    let mut gateway = child.stdin.take().unwrap();
    gateway.write_all(&serde_json::to_vec(&input)?).await?;
    gateway.write_all(b"\n").await?;
    gateway.flush().await?;
    let mut stdout = BufReader::new(child.stdout.take().unwrap()).take(512 * 1024 + 1);
    let mut bytes = Vec::new();
    let mut cancel = cancel;
    let mut gateway = Some(gateway);
    {
        let read = async { stdout.read_until(b'\n', &mut bytes).await };
        tokio::pin!(read);
        tokio::select! {
            result=&mut read=>{result?;},
            _=cancel.changed()=>{gateway.take();tokio::time::timeout(Duration::from_secs(5),&mut read).await??;},
        }
    }
    drop(gateway); // unblock supervisor stdin reader and its runtime shutdown
    let status = tokio::time::timeout(Duration::from_secs(5), child.wait()).await??;
    ensure!(status.success(), "command_supervisor_failed");
    ensure!(bytes.len() <= 512 * 1024, "supervisor_output_too_large");
    Ok(serde_json::from_slice(&bytes)?)
}
