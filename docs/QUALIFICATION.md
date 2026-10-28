# v0.01 candidate qualification

Registry/source version: 0.0.1. License: Apache-2.0. These are owner selections.
This document distinguishes implemented behavior from live/release acceptance.
No release is currently marked Done.

| Gate | Current evidence | Remaining acceptance |
| --- | --- | --- |
| Independent runtime and source | Real public Temper pin; independent application/reducer/store/provider/TUI; source-byte verifier | Independent source/license/provenance signoff |
| Durable effects | Commit refusal, CAS/receipt replay, crash boundaries, epoch fencing, bounded edits and command supervisor tests | Final candidate rerun and retained cohort |
| Agent loop | Real SSE fixture: read → approved edit → approved command → answer; private continuation; optional usage; malformed/401/429/partial cases | Final installed first-party coding smoke and aggregate usage/charge reconciliation |
| Native terminal | Workspace and agent Bombadil journeys, resize, denial, detach and independent file/journal oracles; intentional bad oracle fails | Final candidate installed journey; exploration and ready-recovery receipts retained |
| Account | Published SDK 0.23.9, scoped read-only grant, separate private file store; owner-approved returning browser sign-in, verified principal, account entitlements, restart, real refresh and wrong-origin refusal | Final installed account read and logout/revocation; new-account signup remains unverified |
| Package | Clean b7968ef native macOS ARM64 wheel; two matching wheel assemblies; uv tool install with builds disabled; installed native agent journey; embedded native hash and source-file cohort | Final candidate after remaining changes and supported-platform matrix; upgrade/refusal/rollback fixture passed |
| Security | Private journal/socket/artifacts; environment isolation; Gitleaks 8.30.1 source-snapshot scan reports no findings | Final exact source/history/artifact scan; independent reviewer |
| Channels | Private preparation repository and draft PR; dev/nightly/staging/main protected with strict qualification check, PRs, conversation resolution and admin enforcement | Hosted qualification, channel execution and immutable artifacts; registry ownership/trusted publishing |
| Public launch | Candidate installation/security/provider documentation | Owner signoff and GO; immutable public upload, install-page launch, exact registry install/rollback verification |

Local native evidence at this checkpoint includes
`artifacts/terminal/044748f3-d97a-4805-8115-d5d242059889/receipt.json`,
`artifacts/terminal-agent/0ed66bb7-de7b-464d-b029-4bd2f9f1d0e1/receipt.json` and
`artifacts/terminal-agent/8a82c144-f115-4cc4-9f23-d9a04d348904/receipt.json`
(the last deliberately fails its oracle). These are local retained receipts,
not a claim that this mutable source state is the final signed candidate.

Owner-approved Synth account evidence is retained in the private
`artifacts/auth/scoped-login` directory. The account grant has only account read,
organization membership and refresh scopes. The product reads the typed
`account.entitlements` contract for limits: the SDK's separate `account.limits`
route refused this read-only grant. No write scopes were added. Final installed
verification must revoke this test grant and prove it is no longer usable.

The first real Reflection coding smoke passed in
`artifacts/provider/0ffb5257-81eb-442d-b3c1-4cfd2bd888f7/receipt.json`.
Five requests used the documented `Beam-1.0-501B-A23B`, edited the exact fixture,
and ran its unchanged unit test successfully. The verified account has Special
Access free daily allowances; no card or payment changes were made. The aggregate
verification budget is 16 requests / 65,536 output tokens, maximum charge $0 under
that allowance. The final installed run and aggregate reconciliation remain.

Ready-operation recovery and bounded randomized terminal exploration passed in
`artifacts/terminal/3c4af693-55cd-4f0c-b026-86d598f6794e/receipt.json` and
`artifacts/terminal/9efed52d-1cce-4116-b69d-cf18a8058a04/receipt.json`.
Installed upgrade/refusal/rollback passed in
`artifacts/upgrade/760e9ba1-8495-4473-9b30-6e27f4c71b74/receipt.json` using
an unpublished future-version fixture, not a claim about an existing public release.

Use the Synth release policy's exact `ship-evidence` schema and signoff packet for
completion. `cleanup_complete` and `charge_final` must reflect actual measured
cleanup/billing, not an intent. Do not mark parent/child tickets Done from source
or local package construction alone.

Clean b7968ef wheel SHA-256: `7e1284e676afd818c4b918cc8640c0a051cfc451009778df683e4c687b975629`.
Installed journey receipt: `artifacts/terminal-agent/40fd179c-1b01-4de0-b807-516c3ca75f46/receipt.json`.
Both wheel assemblies use identical native bytes; this is not independent native-build reproducibility.
The owner authorized Safari to add only this repository to the organization
Actions allowlist. The existing six selections were preserved. Repository policy
now permits only the workflow's three exact SHA-pinned actions and requires full
SHA pinning. Hosted qualification is pending; the original undispatched $2.50
reservation closed at $0 actual.
Branch protections have no independent GitHub approval-count requirement; required
PR/check/conversation gates remain enforced for administrators, and separate
independent release signoff/owner GO remains mandatory. No bypass is used.
