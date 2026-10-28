#!/usr/bin/env python3
"""Refuse publication if the actual GitHub environment lost its protection."""
import json
import subprocess

from verify_publish import require


def check(environment, branches):
    require(environment["name"] == "pypi" and environment.get("can_admins_bypass") is False,
            "publisher environment must prevent admin bypass")
    rules = [rule for rule in environment.get("protection_rules", []) if rule["type"] == "required_reviewers"]
    require(len(rules) == 1 and len(rules[0]["reviewers"]) == 1 and
            rules[0]["reviewers"][0]["type"] == "User" and
            rules[0]["reviewers"][0]["reviewer"]["id"] == 32282839, "owner-only environment review required")
    require(environment.get("deployment_branch_policy") == {
        "protected_branches": False, "custom_branch_policies": True}, "explicit main-only branch policy required")
    selected = branches.get("branch_policies", [])
    require(len(selected) == 1 and selected[0]["name"] == "main" and selected[0]["type"] == "branch",
            "publisher environment must allow main branch only")


if __name__ == "__main__":
    prefix = "repos/synth-laboratories/synth-code/environments/pypi"
    def read(endpoint):
        return json.loads(subprocess.check_output(["gh", "api", endpoint], timeout=30))
    check(read(prefix), read(prefix + "/deployment-branch-policies"))
    print("Protected pypi environment verified")
