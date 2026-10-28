#!/usr/bin/env python3
"""Retain registry dependency license metadata/text without private source paths."""
import json
import os
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[1]
launcher=Path(os.environ.get("SYNTH_BUILD_LAUNCHER",str(ROOT.parent/"synth-dev/local_dev/scripts/core_dev_build.py")))
if not launcher.is_file() and "SYNTH_BUILD_LAUNCHER" not in os.environ:
    launcher=ROOT/"scripts/public_build_guard.py"
command=["python3",str(launcher),"--light","--label","synth-code-notices","--",shutil.which("mbx"),"metadata","--locked","--format-version","1","--manifest-path",str(ROOT/"Cargo.toml")]
result=subprocess.run(command,cwd=ROOT,capture_output=True,text=True,check=True)
metadata=next(value for line in result.stdout.splitlines() if line.startswith("{") and "packages" in (value:=json.loads(line)))
packages=[];notices=["Synth Code third-party Rust dependency notices\nGenerated from Cargo.lock and registry manifests. Includes development and platform-specific lockfile dependencies.\n\n"]
for package in sorted(metadata["packages"],key=lambda p:(p["name"],p["version"])):
 if package["name"]=="synth-code-runtime":continue
 manifest=Path(package["manifest_path"])
 root=manifest.parent
 license=package["license"]
 files=[]
 if package["name"] in {"temper-runtime","temper-spec"}:root=ROOT/"vendor/temper"
 for candidate in sorted(root.iterdir()):
  if candidate.is_file() and candidate.name.lower().startswith(("license","copying","notice")) and candidate.stat().st_size<200_000:
   files.append((candidate.name,candidate.read_text(errors="replace")))
 if not files and (root/"LICENSES").is_dir():
  for candidate in sorted((root/"LICENSES").iterdir()):
   if candidate.is_file() and candidate.stat().st_size<200_000:files.append(("LICENSES/"+candidate.name,candidate.read_text(errors="replace")))
 packages.append({"name":package["name"],"version":package["version"],"license":license,"repository":package["repository"],"source":package["source"] or "https://github.com/nerdsane/temper@118bb6f62354301cd76e2cd7c54ce0c97058a738","licenseFiles":[name for name,_ in files]})
 notices.append(f"\n{'='*72}\n{package['name']} {package['version']}\nLicense expression: {license}\nRepository: {package['repository']}\n")
 for name,text in files:notices.append(f"\n--- {name} ---\n{text}\n")
 if not files:notices.append("License text is supplied by the original project; see repository and license expression above.\n")
(ROOT/"docs/THIRD_PARTY_NOTICES.txt").write_text(''.join(notices))
(ROOT/"docs/dependencies.json").write_text(json.dumps({"schema":"synth-code.rust-dependencies.v1","packages":packages},indent=2,sort_keys=True)+'\n')
print(json.dumps({"packages":len(packages),"licenseExpressions":sorted({p["license"] or "UNKNOWN" for p in packages}),"missingText":[p['name'] for p in packages if not p['licenseFiles']]}))
