"""Published Synth OAuth lifecycle with a product-scoped, read-only account grant."""
import os
from pathlib import Path
import sys
from dataclasses import replace

ACCOUNT_SCOPES=("offline_access", "user:org:read", "account:read")

def main(arguments):
    import synth_ai.cli_auth as account
    previous_argv=sys.argv
    previous_config=account.oauth_config
    previous_directory=os.environ.get("SYNTH_CONFIG_DIR")
    previous_storage=os.environ.get("SYNTH_AUTH_STORAGE")
    if previous_storage not in {None,"file"}:
        raise ValueError("Synth Code supports file account storage")
    try:
        if previous_directory is None:
            root=Path(os.environ.get("XDG_CONFIG_HOME",str(Path.home()/".config")))
            os.environ["SYNTH_CONFIG_DIR"]=str(root/"synth-code"/"account")
        os.environ["SYNTH_AUTH_STORAGE"]="file"
        # Reuse SDK discovery, PKCE/state, exchange, principal verification,
        # refresh and revocation. Local coding needs no hosted-run authority.
        account.oauth_config=lambda origin: replace(previous_config(origin),scopes=ACCOUNT_SCOPES)
        sys.argv=["synth-code",*arguments]
        return account.main()
    finally:
        account.oauth_config=previous_config
        sys.argv=previous_argv
        for key,value in [("SYNTH_CONFIG_DIR",previous_directory),("SYNTH_AUTH_STORAGE",previous_storage)]:
            if value is None:
                os.environ.pop(key,None)
            else:
                os.environ[key]=value
