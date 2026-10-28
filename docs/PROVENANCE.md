# Source provenance

The application reducer, store adapter, actor host, development protocol, Python
client/UI and tests were written independently in this checkout. No private
application sources, tests, prompts, credentials, settings or history were copied.
This is not represented as a formal clean-room process.

Temper is acquired directly from its public GitHub revision
`118bb6f62354301cd76e2cd7c54ce0c97058a738`:
https://github.com/nerdsane/temper/tree/118bb6f62354301cd76e2cd7c54ce0c97058a738

Only temper-runtime, temper-spec, root manifests and license files are retained.
The root workspace membership/excludes are reduced; its original is retained as
UPSTREAM-Cargo.toml. Rust crate source bytes are unchanged. SOURCE.json records
per-file hashes, archive hash and adapted root manifest hash. Both upstream license
texts are included. Run `python3 scripts/verify_sources.py` to check these inputs.

Cargo.lock and python/uv.lock pin registry dependencies. Application source uses Apache-2.0. Generated Rust dependency license metadata
and texts are in dependencies.json and THIRD_PARTY_NOTICES.txt, including
platform-specific/development lockfile entries. Python runtime dependencies are
installed from pinned public packages, including synth-ai==0.23.9; their own
distributions retain their license metadata/texts. Final independent legal,
secret, dependency and public-source signoff remains required before publication.
The GitHub repository is private during candidate preparation. No PyPI package
has been uploaded.

Design references are public Mirror, Codex, OpenCode and Bombadil documentation
and source contracts. No source from these projects is embedded in this application.
