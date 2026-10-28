#!/usr/bin/env python3
"""Keep the verified emSign incident fixes in runtime and release tooling."""
import json
from pathlib import Path
import re
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def version(value):
    return tuple(int(part) for part in value.split("."))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check():
    pins = json.loads((ROOT / "tools/pins.json").read_text())
    require(version(pins["uv"]["version"]) >= (0, 13, 4), "uv security floor")
    cargo = tomllib.loads((ROOT / "Cargo.lock").read_text())
    roots = [p["version"] for p in cargo["package"] if p["name"] == "webpki-roots"]
    require(roots and all(version(value) >= (1, 0, 11) for value in roots), "Rust trust root floor")
    result = {"uv": pins["uv"]["version"], "rust_roots": roots}
    for directory in [ROOT / "python", ROOT / "tools/publish"]:
        project = tomllib.loads((directory / "pyproject.toml").read_text())
        floors = [re.fullmatch(r"certifi>=(\d+\.\d+\.\d+)", dependency)
                  for dependency in project["project"]["dependencies"]]
        require(any(match and version(match[1]) >= (2026, 10, 19) for match in floors), "certifi declared floor")
        locked = tomllib.loads((directory / "uv.lock").read_text())
        certifi = next(p["version"] for p in locked["package"] if p["name"] == "certifi")
        require(version(certifi) >= (2026, 10, 19), "certifi locked floor")
        result[directory.relative_to(ROOT).as_posix()] = {"certifi": certifi}
        if directory.name == "publish":
            sigstore = next(p["version"] for p in locked["package"] if p["name"] == "sigstore")
            require(version(sigstore) >= (4, 5, 1), "signer security floor")
            result["tools/publish"]["sigstore"] = sigstore
    return result


if __name__ == "__main__":
    print(json.dumps(check(), sort_keys=True))
