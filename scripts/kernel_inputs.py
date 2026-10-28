"""Identity of the files consumed by the public Rust runtime build."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
def snapshot(root=ROOT):
    paths={root/'Cargo.toml',root/'Cargo.lock',root/'crates/runtime/Cargo.toml'}
    paths.update((root/'crates/runtime/src').rglob('*'))
    paths.update((root/'vendor/temper').rglob('*'))
    paths.update((root/'.cargo').rglob('*'))
    paths.update(p for p in [root/'rust-toolchain',root/'rust-toolchain.toml',root/'crates/runtime/build.rs'] if p.exists())
    files={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(paths) if p.is_file()}
    digest=hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    return {'sha256':digest,'files':files}
if __name__=='__main__':print(json.dumps(snapshot(),sort_keys=True))
