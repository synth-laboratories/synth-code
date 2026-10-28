"""Package already-qualified native bytes; never silently starts a Rust build."""
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import importlib.util
from hatchling.builders.hooks.plugin.interface import BuildHookInterface

class NativeHook(BuildHookInterface):
    PLUGIN_NAME="custom"
    def initialize(self, version, build_data):
        if self.target_name != "wheel" or version == "editable":
            return
        binary=Path(os.environ.get("SYNTH_CODE_RUNTIME_BINARY",""))
        if not binary.is_file():
            raise RuntimeError("SYNTH_CODE_RUNTIME_BINARY must identify the qualified native runtime")
        host=(platform.system(),platform.machine())
        # Only a measured native platform may be advertised by this build.
        tags={("Darwin","arm64"):"macosx_11_0_arm64"}
        if host not in tags:
            raise RuntimeError(f"Unqualified wheel platform: {host}")
        root=Path(self.root)
        spec=importlib.util.spec_from_file_location("kernel_inputs",root.parent/"scripts/kernel_inputs.py")
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        receipt_path=Path(str(binary)+".build.json")
        receipt=json.loads(receipt_path.read_text())
        if receipt["binary_sha256"]!=hashlib.sha256(binary.read_bytes()).hexdigest() or receipt["kernel_inputs"]!=module.snapshot(root.parent):
            raise RuntimeError("Native binary/build receipt does not match current Rust inputs")
        if receipt["version"]!=self.metadata.version.replace(".dev","-dev."):
            raise RuntimeError("Native and Python versions disagree")
        scratch=root/".build"
        scratch.mkdir(exist_ok=True)
        manifest=scratch/"manifest.json"
        repo=root.parent
        files=subprocess.check_output(["git","ls-files","--cached","--others","--exclude-standard","-z"],cwd=repo).split(b"\0")
        sources={os.fsdecode(name):hashlib.sha256((repo/os.fsdecode(name)).read_bytes()).hexdigest() for name in files if name and (repo/os.fsdecode(name)).is_file()}
        cohort=hashlib.sha256(json.dumps(sources,sort_keys=True,separators=(",",":")).encode()).hexdigest()
        source_commit=subprocess.check_output(["git","rev-parse","HEAD"],cwd=repo,text=True).strip()
        dirty=bool(subprocess.check_output(["git","status","--porcelain"],cwd=repo,text=True))
        manifest.write_text(json.dumps({"version":self.metadata.version,"sha256":hashlib.sha256(binary.read_bytes()).hexdigest(),
            "kernelInputsSha256":receipt["kernel_inputs"]["sha256"],"sourceCommit":source_commit,"sourceDirty":dirty,"sourceFilesSha256":cohort,"sourceFiles":sources,"platform":tags[host]},sort_keys=True)+"\n")
        build_data["pure_python"]=False
        build_data["tag"]="py3-none-"+tags[host]
        build_data["force_include"]={str(binary.resolve()):"synth_code/native/synth-code-runtime",
            str(manifest):"synth_code/native/manifest.json", str(root.parent/"LICENSE"):"synth_code/notices/LICENSE",
            str(root.parent/"NOTICE"):"synth_code/notices/NOTICE",
            str(root.parent/"vendor/temper/LICENSE-APACHE"):"synth_code/notices/temper-LICENSE-APACHE",
            str(root.parent/"vendor/temper/LICENSE-MIT"):"synth_code/notices/temper-LICENSE-MIT",
            str(root.parent/"docs/PROVENANCE.md"):"synth_code/notices/PROVENANCE.md",
            str(root.parent/"docs/THIRD_PARTY_NOTICES.txt"):"synth_code/notices/THIRD_PARTY_NOTICES.txt",
            str(root.parent/"docs/dependencies.json"):"synth_code/notices/dependencies.json"}
