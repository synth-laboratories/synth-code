# Synth Code

Rust owns execution, authorization and durable state. Python owns presentation
and protocol transport. Do not move tool execution into the frontend.

Keep application code independent of private implementations. Dependencies must
have pinned public provenance and notices; vendor/temper/SOURCE.json identifies
the upstream source. Never edit vendored Rust source to make application tests pass.

Use mbx for all Rust compilation. In the owner's workspace, invoke it through
the maintained admission/storage-guarded core_dev_build.py launcher and keep the
explicit target directory inside this checkout. Do not configure sccache or bypass
host admission. The launcher path is an environment integration, not a runtime dependency.

Foundation tests use synthetic local effects and no model credentials or network.
Unknown external effects must never be automatically repeated. Keep mutation IDs
stable on retry and reject changed requests under an existing identity.

Every slash command in `python/src/synth_code/app.py` `COMMANDS` declares what it
does while the agent works (`RUN`, `QUEUE` or `REFUSE`) and why. Add a sample
invocation to `SAMPLE_ARGUMENTS` in `python/tests/test_slash_commands.py`; the
policy test fails until the behaviour matches the declaration.
