# Versioned release channels

Stable source promotes `dev → staging → main`; `nightly` is an explicit opt-in
prerelease branch. Required checks and review/signoff must qualify a source before
publication. Protected branch promotion does not grant provider access or prove
installed behavior. This project has no production deployment hooks.

`python3 scripts/set_version.py --channel stable` verifies stable Python/Rust
version agreement. For a new candidate, `--version X.Y.Z` changes only the project
versions in both manifests and lockfiles. Nightly requires `--channel nightly
--version X.Y.Z.devN`, mapped to Rust `X.Y.Z-dev.N`. Choose an unused increasing
version, commit and review the generated version change, then qualify that exact
source. Do not silently rewrite an already published version or tag.

The nightly branch's workflow builds versioned candidate artifacts. This does not
publish them to PyPI or switch stable users. Stable users select a specific final
version; nightly users must explicitly select a prerelease. Both channels must
retain source commit/file cohort, native/wheel hashes, dependency/tool inputs,
accepted journeys, cleanup, known/unknown usage and charge finality.

Build a native runtime through `scripts/build.py build --release`. Then assemble
its platform wheel with `SYNTH_CODE_RUNTIME_BINARY` selecting the exact qualified
binary. The wheel carries a native SHA-256 and source-file manifest. Repeated wheel
assembly of identical native bytes is tested separately from native-build
reproducibility; one does not prove the other.

A final release packet must include the applicable Synth `ship-evidence` block,
all acceptance items, independent signoff and the owner's explicit publication GO
with credential custody and rollback owner. Promote already-qualified bytes.
Configure registry trusted publishing with the owner; no publishing credential is
available to pull-request jobs. Public visibility, registry upload, public install
page and release tags remain withheld until those actual gates pass.

The preparation repository remains private. The owner authorized adding only
`synth-laboratories/synth-code` (ID 1436450053) to the organization Actions
allowlist, preserving the existing six repositories and other settings. Readback
confirmed enablement. Repository policy permits only the three exact workflow
action SHAs and requires full SHA pinning. Jobs have read-only contents and no
publishing secrets. Hosted execution must still qualify; policy enablement is not
a successful check.

The wheel builder requires an adjacent native build receipt matching the current
Rust input hashes, binary hash and project version. A stale native binary cannot
be relabeled as a new source cohort. Package provenance also retains the complete
source-file cohort and whether the checkout was dirty.

Rollback before any public prior version exists means reinstalling the same
qualified bytes and restoring an inactive compatible store. Older development
stores/builds are not supported downgrade targets. Future stable rollback needs a
separately qualified prior artifact and migration compatibility; never delete or
mutate a running owner's journal to force an upgrade.

The prepared `publish.yml` runs only by manual dispatch from protected `main`,
behind the `pypi` environment. Register a PyPI pending publisher for project
`synth-code`, GitHub owner `synth-laboratories`, repository `synth-code`, workflow
`publish.yml`, environment `pypi`. This binding is not configured by writing the
workflow; it needs the owner's registry authorization. There are no PyPI secrets
in CI and no fallback to unrelated project-scoped tokens.

Publication selects a tag, stable/nightly channel and exact publication-manifest
SHA. The verifier binds canonical cohort, wheel/native hashes, version, license,
clean source file hashes, tag and source ancestry. It never builds replacement
bytes. Only the publishing job has `id-token: write`; the Python publisher lock
pins attestation tooling with patched certificate dependencies. It signs the
selected file, publishes through PyPI OIDC, then checks registry hashes and the
nonempty attestation's exact main-workflow identity and publisher environment,
including equality with the generated signature. The job also reads back actual
owner-review, no-admin-bypass and main-only environment rules before signing;
an environment label alone cannot pass. Public transparency-log records are part of
this release action. Publication remains held for owner GO.

The PSF2026-10-20 emSign advisory requires uv>=0.13.4, certifi>=2026.10.19 and
sigstore>=4.5.1. The source/runtime locks and public bootstrap now enforce these
floors; CI uses Python3.12.16 (checksum-pinned standalone build on macOS,
where setup-python does not publish that security-only ARM64 version). The pinned Rust webpki-roots1.0.11 removes the
compromised roots. Run `python3 scripts/check_security.py` before release.
