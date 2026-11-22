"""Keys FoodDash issues to other apps for agent-to-agent (A2A) calls.

A caller sends 'Authorization: Bearer ak_...'. The key maps to the calling
app and, for this demo, a fixed FoodDash user (user_1). Only SHA-256 hashes
are stored, in the JSON file (or JSON text) FOODDASH_A2A_KEYS points to:
{"<sha256>": {"app": "chathub", "user_id": "user_1", "name": "chathub"}}.

Production would use delegated user consent (OAuth-style: the user grants
the calling app access to their own FoodDash account), not a fixed mapping.

    python -m app.a2a issue chathub --user user_1 --file a2a_keys.json
    python -m app.a2a list --file a2a_keys.json
    python -m app.a2a revoke chathub --file a2a_keys.json
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from bastion.a2a import CallerKeys, hash_key, new_key

ENV = "FOODDASH_A2A_KEYS"

# What a session another app started may do. Enforced in code from the
# authenticated caller (app/requested.py), never by the prompt.
EXTERNAL_TOOLS = {"list_restaurants", "get_menu", "place_order", "get_order", "cancel_order"}
EXTERNAL_PAYMENT_MESSAGE = "Payments for orders started outside FoodDash must be completed in FoodDash."


def load() -> CallerKeys:
    return CallerKeys.from_env(ENV)


def _read(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m app.a2a")
    sub = p.add_subparsers(dest="cmd", required=True)
    issue = sub.add_parser("issue")
    issue.add_argument("app")
    issue.add_argument("--user", default="user_1")
    for cmd in ("list", "revoke"):
        sp = sub.add_parser(cmd)
        if cmd == "revoke":
            sp.add_argument("app")
    for sp in sub.choices.values():
        sp.add_argument("--file", default=os.environ.get(ENV, "a2a_keys.json"))
    args = p.parse_args(argv)
    path = Path(args.file)
    entries = _read(path)
    if args.cmd == "issue":
        key = new_key()
        entries[hash_key(key)] = {"app": args.app, "user_id": args.user, "name": args.app}
        path.write_text(json.dumps(entries, indent=2) + "\n")
        print(f"A2A key for {args.app!r} (user {args.user}), shown once; only its hash is stored:\n{key}")
    elif args.cmd == "list":
        for h, e in entries.items():
            print(f"{e['app']:<16} user={e.get('user_id')}  sha256={h[:12]}...")
    else:
        kept = {h: e for h, e in entries.items() if e.get("app") != args.app}
        path.write_text(json.dumps(kept, indent=2) + "\n")
        print(f"Revoked {len(entries) - len(kept)} key(s) for {args.app!r}.")


if __name__ == "__main__":
    main()
