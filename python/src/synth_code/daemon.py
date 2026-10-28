"""Attach to one durable owner; UI disconnect never terminates its daemon."""
import asyncio
import hashlib
import os
import subprocess
from pathlib import Path
from .client import Client

def address(store: Path, runtime_dir=None):
    base=runtime_dir or Path(os.environ.get("SYNTH_CODE_RUNTIME_DIR",str(Path.home()/".cache/synth-code/run")))
    socket_dir=Path(base)/hashlib.sha256(str(store).encode()).hexdigest()[:12]
    socket_dir.mkdir(parents=True,mode=0o700,exist_ok=True)
    info=socket_dir.lstat()
    if socket_dir.is_symlink() or info.st_mode&0o077 or info.st_uid!=os.getuid():
        raise RuntimeError("Daemon socket directory must be private and owned")
    return socket_dir/"agent.sock"

async def connect(executable: Path, store: Path, *, config=None, env_file=None, runtime_dir=None):
    store.mkdir(parents=True, exist_ok=True, mode=0o700)
    socket=address(store,runtime_dir)
    try:
        client = await Client.attach(socket)
    except (FileNotFoundError, ConnectionRefusedError):
        command = [str(executable), "serve", str(store), "--socket", str(socket)]
        if config:
            command += ["--provider-config", str(config.resolve())]
        if env_file:
            command += ["--env-file", str(env_file.resolve())]
        log = store / "daemon.log"
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=descriptor, start_new_session=True)
        finally:
            os.close(descriptor)
        for _ in range(100):
            try:
                client = await Client.attach(socket)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                if process.poll() is not None:
                    # A concurrent launcher may have won store ownership.
                    try:
                        client = await Client.attach(socket)
                        break
                    except OSError:
                        raise RuntimeError(f"Daemon startup failed; see {log}") from None
                await asyncio.sleep(0.05)
        else:
            raise RuntimeError(f"Daemon startup timed out; see {log}")
    if client.info.get("runtimeSha256")!=hashlib.sha256(executable.read_bytes()).hexdigest():
        await client.close()
        raise RuntimeError("Running daemon uses different native bytes; stop it after active work finishes")
    if config or env_file:
        import json
        actual = await client.call("synth/provider/status", {})
        if config:
            requested = json.loads(config.read_text())
            keys = {"endpoint":"endpoint", "model":"model", "effort":"effort", "max_output_tokens":"maxOutputTokens", "max_requests":"maxRequests"}
            if any(actual[target] != requested[source] for source, target in keys.items()):
                await client.close()
                raise RuntimeError("Running daemon has different provider settings")
        # Credential binding identity is checked by Rust without returning secrets.
        await client.call("synth/provider/checkBinding", {"envFile":str(env_file.resolve()) if env_file else None,
            "configFile":str(config.resolve()) if config else None})
    return client
