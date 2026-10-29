# Contributing

Open an issue with the version, platform and a small reproduction, or a pull
request against `dev` describing the behavior and relevant verification.
Report security issues privately as described in [SECURITY.md](SECURITY.md).
Do not include credentials, private source, journals or model continuation.

Keep Rust responsible for model calls, execution, approvals and durable state;
Python owns presentation and transport. Preserve uncertainty and stable mutation
identities: reconnecting must not repeat an effect. Use public dependencies with
recorded provenance and compatible licenses. Do not copy private implementations
or modify pinned upstream sources to accommodate application tests.

Follow [AGENTS.md](AGENTS.md) and the development and verification commands in
[README.md](README.md). Rust compilation uses mbx through `scripts/build.py` and
its admission/storage guard. Add focused failure/recovery witnesses for changed
authorization or durability behavior. Terminal changes need the affected native
Bombadil journey and independent file/journal oracle. Provider fixtures are local
and free; real-provider verification requires separate authorized credentials and
bounds. A contribution does not authorize a public release.

Contributions are provided under Apache-2.0. Retain third-party notices and do
not introduce a competing account issuer, hidden telemetry, provider fallback or
unreviewed execution authority. See [provenance](docs/PROVENANCE.md) and the
[release process](docs/RELEASE.md).

Deeply nested checkouts can exceed the OS Unix-socket path limit during tests.
Set `SYNTH_CODE_TEST_SOCKET_ROOT` to an owned short directory before running the
Python suite or terminal journeys. Fixtures create private temporary directories
beneath that root and clean up their own sockets.
