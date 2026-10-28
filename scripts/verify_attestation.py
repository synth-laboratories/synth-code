#!/usr/bin/env python3
"""Require the selected signature and the exact protected publisher identity."""
import argparse
import json
from pathlib import Path
import urllib.request

from verify_publish import bounded_file, require, verify

REPOSITORY = "synth-laboratories/synth-code"
IDENTITY = f"https://github.com/{REPOSITORY}/.github/workflows/publish.yml@refs/heads/main"
PUBLISHER = {"kind": "GitHub", "repository": REPOSITORY,
             "workflow": "publish.yml", "environment": "pypi"}


def selected_attestation(provenance, local):
    require(provenance.get("version") == 1, "unsupported provenance version")
    bundles = provenance.get("attestation_bundles")
    require(isinstance(bundles, list) and len(bundles) == 1, "expected one nonempty publisher bundle")
    bundle = bundles[0]
    require(bundle.get("publisher") == PUBLISHER, "unexpected registry publisher")
    attestations = bundle.get("attestations")
    require(isinstance(attestations, list) and len(attestations) == 1, "expected one nonempty attestation")
    require(attestations[0] == local, "registry signature differs from selected attestation")
    return attestations[0]


def identity_policy():
    from sigstore.verify import policy
    return policy.AllOf([
        policy.OIDCIssuerV2("https://token.actions.githubusercontent.com"),
        policy.Identity(identity=IDENTITY),
        policy.OIDCSourceRepositoryURI(f"https://github.com/{REPOSITORY}"),
        policy.OIDCSourceRepositoryIdentifier("1436450053"),
        policy.OIDCSourceRepositoryRef("refs/heads/main"),
        policy.OIDCBuildConfigURI(IDENTITY),
    ])


def verify_signature(attestation, wheel):
    from pypi_attestations import Attestation, Distribution
    parsed = Attestation.model_validate(attestation)
    predicate, body = parsed.verify(identity_policy(), Distribution.from_file(wheel))
    require(predicate == "https://docs.pypi.org/attestations/publish/v1" and body in (None, {}),
            "unexpected publication predicate")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--channel", choices=["stable", "nightly"], required=True)
    parser.add_argument("--manifest-sha256", required=True)
    parser.add_argument("--registry", action="store_true")
    args = parser.parse_args()
    result = verify(args.assets, args.tag, args.channel, args.manifest_sha256, check_git=True)
    wheel = args.assets / result["filename"]
    local = json.loads(bounded_file(Path(str(wheel) + ".publish.attestation"), 1024 * 1024))
    attestation = local
    if args.registry:
        url = f"https://pypi.org/integrity/synth-code/{result['version']}/{result['filename']}/provenance"
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read(1024 * 1024 + 1)
        require(len(raw) <= 1024 * 1024, "registry provenance exceeds bound")
        provenance = json.loads(raw)
        attestation = selected_attestation(provenance, local)
        from pypi_attestations import Provenance
        Provenance.model_validate(provenance)
        (args.assets / "registry-provenance.json").write_bytes(raw)
    verify_signature(attestation, wheel)
    print(json.dumps({"verified": True, "registry": args.registry, "identity": IDENTITY,
                      "publisher": PUBLISHER, "wheel_sha256": result["wheel_sha256"]}, sort_keys=True))


if __name__ == "__main__":
    main()
