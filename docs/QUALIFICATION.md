# v0.01 candidate qualification

Registry/source version: 0.0.1. License: Apache-2.0. These are owner selections.
This document distinguishes implemented behavior from live/release acceptance.
No release is currently marked Done.

| Gate | Current evidence | Remaining acceptance |
| --- | --- | --- |
| Independent runtime and source | Real public Temper pin; independent application/reducer/store/provider/TUI; source-byte verifier | Independent source/license/provenance signoff |
| Durable effects | Commit refusal, CAS/receipt replay, crash boundaries, epoch fencing, bounded edits and command supervisor tests | Final candidate rerun and retained cohort |
| Agent loop | Real SSE fixture: read → approved edit → approved command → answer; private continuation; optional usage; malformed/401/429/partial cases | First-party paid coding smoke and final usage/charge reconciliation |
| Native terminal | Workspace and agent Bombadil journeys, resize, denial, detach and independent file/journal oracles; intentional bad oracle fails | Installed candidate journey and broader exploration/regression receipt |
| Account | Published SDK 0.23.9, scoped read-only grant, separate private file store; real returning browser sign-in reaches consent; cancellation refuses and stores no credential | Owner-confirmed consent, principal/org/limits, restart, expiration/refusal and logout/revocation |
| Package | Native macOS ARM64 wheel; local uv tool install with builds disabled; embedded native hash and source-file cohort | Clean final-source wheel, reproducibility comparison, fresh installed journey, upgrade/reinstall and supported-platform matrix |
| Security | Private journal/socket/artifacts; environment isolation; Gitleaks 8.30.1 source-snapshot scan reports no findings | Final exact source/history/artifact scan; independent reviewer |
| Channels | Private preparation repository `synth-laboratories/synth-code`; registry name returned 404 on availability check | Protected dev/nightly/staging/main, qualified channel workflow and immutable artifacts; registry ownership/trusted publishing |
| Public launch | Candidate installation/security/provider documentation | Owner signoff and GO; immutable public upload, install-page launch, exact registry install/rollback verification |

Local native evidence at this checkpoint includes
`artifacts/terminal/044748f3-d97a-4805-8115-d5d242059889/receipt.json`,
`artifacts/terminal-agent/0ed66bb7-de7b-464d-b029-4bd2f9f1d0e1/receipt.json` and
`artifacts/terminal-agent/8a82c144-f115-4cc4-9f23-d9a04d348904/receipt.json`
(the last deliberately fails its oracle). These are local retained receipts,
not a claim that this mutable source state is the final signed candidate.

The existing Synth environment bindings returned 401 against the canonical API.
The browser account `josh@usesynth.ai` reached the real Synth consent screen.
The broad SDK grant was canceled; Synth Code now requests only account read,
organization membership and a refreshable session. A new scoped consent awaits
owner confirmation required by the Computer Use policy for new OAuth access.

Reflection's authorized model-list read succeeded but advertised a restricted
identifier differing from the documented default. Billing tariff and the real
coding smoke remain unverified. No paid generation has been run. Do not replace
this gate with an offline fixture or silently change model/provider.

Use the Synth release policy's exact `ship-evidence` schema and signoff packet for
completion. `cleanup_complete` and `charge_final` must reflect actual measured
cleanup/billing, not an intent. Do not mark parent/child tickets Done from source
or local package construction alone.
