"""Published Synth OAuth lifecycle with a product-scoped, read-only account grant."""
import os
import argparse
import json
from pathlib import Path
import sys
from dataclasses import replace

ACCOUNT_SCOPES=("offline_access", "user:org:read", "account:read")

def account_limits(arguments):
    from synth_ai import SynthClient
    from synth_ai.options import ReadOptions
    from synth_ai.errors import SynthError
    parser=argparse.ArgumentParser(prog="synth-code account limits")
    parser.add_argument("--json",action="store_true")
    args=parser.parse_args(arguments)
    try:
        with SynthClient() as client:
            value=client.account.entitlements(request=ReadOptions(read_retry_count=0))
    except SynthError as error:
        print(json.dumps({"code":error.code or "account_read_refused","status":error.status,"request_id":error.request_id}),file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"source":"synth.account.entitlements","entitlements":value.model_dump(mode="json")}))
    else:
        print("Product\tAccess\tLimit\tValue\tUsed\tRemaining")
        for product in value.subproducts:
            for limit in product.limits:
                fields=[product.subproduct,product.access,limit.limit_id,limit.value,limit.used,limit.remaining]
                print("\t".join("Unknown" if field is None else json.dumps(str(field),ensure_ascii=True)[1:-1] for field in fields))
    return 0

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
        if arguments[:1]==["account"]:
            if len(arguments)<2 or arguments[1] not in {"limits","entitlements"}:
                raise ValueError("Supported account commands: limits, entitlements")
            return account_limits(arguments[2:])
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
