# Reflection provider contract

Default first-party endpoint:
`https://api.reflection.ai/openai/v1/chat/completions`.
Documented model: `Beam-1.0-501B-A23B`; effort: `medium`.

Configuration JSON contains `endpoint`, `model`, `effort`, `env_file` (absolute
path or null), `max_output_tokens` (1–8192) and `max_requests` (1–24). Defaults are
4096 output tokens and 12 requests per turn. Pass `--provider-config FILE`; an
explicit `--env-file FILE` overrides its credential-file binding. Existing daemon
settings/bindings must match. Never put a token value in configuration JSON.

The adapter uses Chat Completions SSE, indexed fragmented tool calls, required
private assistant reasoning continuation and optional usage. It requests
nonparallel tools; multiple returned calls execute serially with one durable tool
reply each. No tool runs until the whole completion validates. Interrupted tool
batches receive explicit synthetic missing-result replies on later context
projection, without repeating effects or interpreting absence as success.

Transport errors, malformed/truncated streams, unsupported finish reasons,
auth/model failures and rate limits are explicit. HTTP 429 retains sanitized
Retry-After. There are no automatic provider retries. Reissue a user task only
after inspecting its prior turn/effects. Missing usage is null/unknown, not zero.

Requests are limited to 512 KiB; streams to 4 MiB; content to 256 KiB; private
reasoning to 1 MiB; tool calls to 16 and arguments to 32 KiB. Connect/total HTTP
timeouts are 15/180 seconds. Context compaction is not implemented.

Official references: [quickstart](https://developers.reflection.ai/quickstart),
[streaming](https://developers.reflection.ai/streaming),
[tools](https://developers.reflection.ai/tool-calling),
[reasoning](https://developers.reflection.ai/reasoning).

Live observation, 2026-10-28 UTC: the existing authorized account's model-list
endpoint returned `Beam-501B-A23B` with restricted availability, rather than the
documented default identifier. This is not permission to change identifiers or
providers silently. A successful model-list read is not a completed coding smoke
or verified billing tariff. No paid generation has been run for this candidate.
