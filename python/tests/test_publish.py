"""Publication rejects changed bytes, provenance and cross-channel selections."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from verify_publish import verify, request
from verify_attestation import selected_attestation, PUBLISHER
from check_publisher_environment import check as check_environment


def digest(data):
    return hashlib.sha256(data).hexdigest()


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / "artifacts")
        self.assets = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.version = "0.0.1"
        self.filename = "synth_code-0.0.1-py3-none-macosx_11_0_arm64.whl"
        source = {"a.py": digest(b"fixture")}
        source_digest = digest(json.dumps(source, sort_keys=True, separators=(",", ":")).encode())
        native = b"fixture-native"
        native_digest = digest(native)
        native_manifest = {"version": self.version, "sourceDirty": False, "sourceCommit": "1" * 40,
                           "sourceFilesSha256": source_digest, "sourceFiles": source, "sha256": native_digest}
        with zipfile.ZipFile(self.assets / self.filename, "w") as wheel:
            wheel.writestr("synth_code-0.0.1.dist-info/METADATA",
                           "Metadata-Version: 2.4\nName: synth-code\nVersion: 0.0.1\nLicense-Expression: Apache-2.0\n")
            wheel.writestr("synth_code/native/manifest.json", json.dumps(native_manifest))
            wheel.writestr("synth_code/native/synth-code-runtime", native)
        wheel_digest = digest((self.assets / self.filename).read_bytes())
        self.cohort = {"schema": "synth-code.release-cohort.v1", "channel": "stable", "version": self.version,
                       "source": {"qualified_commit": "1" * 40, "source_files_sha256": source_digest},
                       "artifacts": {"wheel": {"filename": self.filename, "sha256": wheel_digest},
                                     "native": {"sha256": native_digest}}}
        self.manifest = {"schema": "synth-code.publish-manifest.v1", "channel": "stable", "version": self.version,
                         "wheel": {"filename": self.filename, "sha256": wheel_digest}, "cohort_sha256": "",
                         "source_commit": "1" * 40, "source_files_sha256": source_digest, "native_sha256": native_digest}
        self.write()

    def write(self):
        canonical = json.dumps(self.cohort, sort_keys=True, separators=(",", ":")).encode()
        (self.assets / "cohort.json").write_bytes(canonical + b"\n")
        self.manifest["cohort_sha256"] = digest(canonical)
        raw = json.dumps(self.manifest, sort_keys=True).encode()
        (self.assets / "publish-manifest.json").write_bytes(raw)
        self.selection = digest(raw)

    def test_selected_bytes_pass_and_changed_wheel_refuses(self):
        self.assertEqual(verify(self.assets, "v0.0.1", "stable", self.selection)["filename"], self.filename)
        with (self.assets / self.filename).open("ab") as wheel:
            wheel.write(b"changed")
        with self.assertRaisesRegex(ValueError, "wheel digest mismatch"):
            verify(self.assets, "v0.0.1", "stable", self.selection)

    def test_cohort_must_bind_the_selected_native_and_wheel(self):
        self.cohort["artifacts"]["native"]["sha256"] = "0" * 64
        self.write()
        with self.assertRaisesRegex(ValueError, "cohort native mismatch"):
            verify(self.assets, "v0.0.1", "stable", self.selection)

    def test_manifest_changes_require_a_new_explicit_digest(self):
        self.manifest["extra"] = "not a supported release field"
        original = self.selection
        self.write()
        with self.assertRaisesRegex(ValueError, "manifest digest mismatch"):
            verify(self.assets, "v0.0.1", "stable", original)
        with self.assertRaisesRegex(ValueError, "unexpected publication fields"):
            verify(self.assets, "v0.0.1", "stable", self.selection)

    def test_unsafe_asset_name_and_unselected_extra_wheels_refuse(self):
        self.manifest["wheel"]["filename"] = "../outside.whl"
        self.write()
        with self.assertRaisesRegex(ValueError, "unexpected wheel filename"):
            verify(self.assets, "v0.0.1", "stable", self.selection)
        self.manifest["wheel"]["filename"] = self.filename
        self.write()
        (self.assets / "unselected.whl").write_bytes(b"unselected")
        with self.assertRaisesRegex(ValueError, "unselected wheels"):
            verify(self.assets, "v0.0.1", "stable", self.selection)

    def test_stable_cannot_select_a_prerelease_or_shell_input(self):
        for tag in ["v0.0.2.dev1", "--help", "v0.0.1;false", "0.0.1", "v0.0.1\n"]:
            with self.assertRaises(ValueError):
                request(tag, "stable", self.selection)
        self.assertEqual(request("v0.0.2.dev1", "nightly", self.selection), "0.0.2.dev1")
        with self.assertRaises(ValueError):
            request("v0.0.1", "other", self.selection)

    def test_provenance_requires_exact_publisher_and_selected_nonempty_signature(self):
        local = {"fixture": "selected signature"}
        def provenance(publisher=PUBLISHER, attestations=None):
            return {"version": 1, "attestation_bundles": [{"publisher": publisher,
                    "attestations": [local] if attestations is None else attestations}]}
        self.assertEqual(selected_attestation(provenance(), local), local)
        for key in PUBLISHER:
            with self.assertRaisesRegex(ValueError, "unexpected registry publisher"):
                selected_attestation(provenance({**PUBLISHER, key: "unselected"}), local)
        for value in [[], [{"fixture": "another signature"}], [local, local]]:
            with self.assertRaises(ValueError):
                selected_attestation(provenance(attestations=value), local)
        for bundles in [[], [provenance()["attestation_bundles"][0]] * 2]:
            with self.assertRaises(ValueError):
                selected_attestation({"version": 1, "attestation_bundles": bundles}, local)

    def test_environment_label_without_actual_review_and_main_only_rules_refuses(self):
        environment = {"name": "pypi", "can_admins_bypass": False,
                       "protection_rules": [{"type": "required_reviewers", "reviewers": [
                           {"type": "User", "reviewer": {"id": 32282839}}]}],
                       "deployment_branch_policy": {"protected_branches": False, "custom_branch_policies": True}}
        branches = {"branch_policies": [{"name": "main", "type": "branch"}]}
        check_environment(environment, branches)
        for changes in [{"can_admins_bypass": True}, {"protection_rules": []},
                        {"protection_rules": [{"type": "required_reviewers", "reviewers": [
                            {"type": "User", "reviewer": {"id": 32282839}},
                            {"type": "User", "reviewer": {"id": 123}}]}]},
                        {"deployment_branch_policy": None}]:
            with self.assertRaises(ValueError):
                check_environment({**environment, **changes}, branches)
        for rules in [[], [{"name": "*", "type": "branch"}],
                      [{"name": "main", "type": "tag"}], branches["branch_policies"] * 2]:
            with self.assertRaises(ValueError):
                check_environment(environment, {"branch_policies": rules})
