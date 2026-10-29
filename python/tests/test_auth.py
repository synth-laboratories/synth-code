import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from synth_code.auth import main, ACCOUNT_SCOPES

ROOT=Path(__file__).resolve().parents[2]
(ROOT / "artifacts").mkdir(exist_ok=True)
class AccountBoundaryTests(unittest.TestCase):
    def test_product_store_and_read_only_scopes_are_restored_after_sdk_failure(self):
        import synth_ai.cli_auth as sdk
        original=sdk.oauth_config
        def verify():
            config=sdk.oauth_config("https://api.usesynth.ai")
            self.assertEqual(config.scopes,ACCOUNT_SCOPES)
            self.assertNotIn("synth:action",config.scopes)
            self.assertEqual(os.environ["SYNTH_AUTH_STORAGE"],"file")
            self.assertTrue(os.environ["SYNTH_CONFIG_DIR"].endswith("synth-code/account"))
            raise RuntimeError("test refusal")
        with patch.dict(os.environ,{},clear=True),patch.object(sdk,"main",side_effect=verify):
            with self.assertRaisesRegex(RuntimeError,"test refusal"):
                main(["login"])
            self.assertNotIn("SYNTH_CONFIG_DIR",os.environ)
            self.assertNotIn("SYNTH_AUTH_STORAGE",os.environ)
            self.assertIs(sdk.oauth_config,original)

    def test_existing_sdk_store_is_not_selected_by_default(self):
        import synth_ai.cli_auth as sdk
        with tempfile.TemporaryDirectory(dir=ROOT/"artifacts") as temporary:
            with patch.dict(os.environ,{"XDG_CONFIG_HOME":temporary},clear=True):
                with patch.object(sdk,"main",side_effect=lambda:os.environ["SYNTH_CONFIG_DIR"]):
                    self.assertEqual(main(["auth","status"]),str(Path(temporary)/"synth-code/account"))

    def test_keychain_is_not_opened_by_account_commands(self):
        with patch.dict(os.environ,{"SYNTH_AUTH_STORAGE":"keychain"}):
            with self.assertRaisesRegex(ValueError,"file account storage"):
                main(["auth","status"])
