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

The new preparation repository is private. Its Actions execution is currently
blocked by organization policy; repo-level enablement returns 409 and the existing
credential cannot read/change the org Actions policy (403, admin:org missing).
Do not broaden that credential or change unrelated repositories to work around it.
An org administrator must allow repository ID 1436450053 (`synth-laboratories/synth-code`).
The proposed workflow uses read-only contents and three SHA-pinned GitHub actions;
no publisher secrets are present. No hosted jobs have executed, so its first CI
reservation is unused.

Rollback before any public prior version exists means reinstalling the same
qualified bytes and restoring an inactive compatible store. Older development
stores/builds are not supported downgrade targets. Future stable rollback needs a
separately qualified prior artifact and migration compatibility; never delete or
mutate a running owner's journal to force an upgrade.
