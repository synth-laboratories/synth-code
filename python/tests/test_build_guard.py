"""Portable guard rejects before spawn and stops only its oversized job."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[2]
(ROOT / "artifacts").mkdir(exist_ok=True)
GUARD=ROOT/"scripts/public_build_guard.py"
class BuildGuardTests(unittest.TestCase):
    def test_target_outside_checkout_refuses_before_worker(self):
        result=subprocess.run([sys.executable,str(GUARD),"--label","invalid-target-test","--","mbx","build","--target-dir","/outside-this-checkout"],capture_output=True,text=True,timeout=5)
        self.assertNotEqual(result.returncode,0)
        self.assertIn("target must stay",result.stderr)

    def test_periodic_budget_kills_only_owned_worker(self):
        with tempfile.TemporaryDirectory(dir=ROOT/"artifacts") as temporary:
            root=Path(temporary);target=root/"target";target.mkdir()
            worker=root/"mbx"
            worker.write_text(f'#!{sys.executable}\nfrom pathlib import Path\nimport os,time\nPath(os.environ["CARGO_TARGET_DIR"]).joinpath("oversize").write_bytes(b"x"*4096)\ntime.sleep(20)\n')
            worker.chmod(0o755)
            foreign=subprocess.Popen([sys.executable,"-c","import time;time.sleep(20)"])
            try:
                result=subprocess.run([sys.executable,str(GUARD),"--label","budget-fixture","--",str(worker),"build","--target-dir",str(target)],env=os.environ|{"SYNTH_PUBLIC_ADMISSION_ROOT":str(root/"admission"),"CORE_BUILD_TARGET_MAX_BYTES":"1024"},capture_output=True,text=True,timeout=8)
                self.assertNotEqual(result.returncode,0)
                self.assertIn("storage budget",result.stderr)
                self.assertIsNone(foreign.poll())
                self.assertEqual((target/"oversize").stat().st_size,4096)
            finally:
                foreign.terminate();foreign.wait(timeout=5)
