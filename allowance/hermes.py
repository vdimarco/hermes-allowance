"""Hermes plugin wiring: tools for the agent, /allowance for the human.

Limits and auto-pay can only be changed with the /allowance slash command, which
the user types. The model has no tool for it.
"""

from __future__ import annotations

import json
import os
import shlex

from .catalog import Channel3Catalog, DemoCatalog
from .ledger import Ledger
from .paypal import LIVE, SANDBOX, PayPalClient
from .tools import TOOLS, make_handlers
from .wallet import Wallet

TOOLSET = "allowance"


def _secret(name: str) -> str | None:
    try:
        from agent.secret_scope import get_secret  # Hermes secret scope

        val = get_secret(name)
        if val:
            return val
    except Exception:
        pass
    return os.environ.get(name) or None


def build_wallet(get_config) -> Wallet:
    home = os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")
    db_path = get_config("db_path", None) or os.path.join(home, "allowance", "ledger.db")
    c3 = _secret("CHANNEL3_API_KEY")
    use_demo = bool(get_config("demo_catalog", False)) or not c3
    catalog = DemoCatalog() if use_demo else Channel3Catalog(c3)
    env = (get_config("paypal_env", "sandbox") or "sandbox").lower()
    pp = PayPalClient(_secret("PAYPAL_CLIENT_ID"), _secret("PAYPAL_CLIENT_SECRET"),
                      base_url=LIVE if env == "live" else SANDBOX)
    public = get_config("return_base_url", None) or os.environ.get("ALLOWANCE_PUBLIC_URL") or "https://example.com"
    return Wallet(Ledger(db_path), catalog, pp, public_url=public)


HELP = """/allowance status
/allowance purchases
/allowance set budget 50 | set auto 20 | set max 100 | set quantity 5 | set rate 5
/allowance allow cables.example,keys.example   (empty = any merchant)
/allowance reset          start a new budget window
/allowance connect        save your PayPal account for auto-pay (opens PayPal)
/allowance connected <approval_token_id>   finish auto-pay setup
/allowance test-card      sandbox only: auto-pay with PayPal's test card
/allowance disconnect
/allowance cancel <purchase_id> | refund <purchase_id>"""

SET_KEYS = {"budget": "budget", "auto": "auto_approve_limit", "max": "max_per_purchase",
            "quantity": "max_quantity", "rate": "max_purchases_per_hour"}


def make_command(get_wallet):
    def allowance_cmd(raw_args: str = "", **_kw) -> str:
        try:
            parts = shlex.split(raw_args or "")
        except ValueError:
            parts = (raw_args or "").split()
        w = get_wallet()
        try:
            if not parts or parts[0] in ("status", "help"):
                if parts[:1] == ["help"]:
                    return HELP
                return json.dumps(w.status(), indent=2)
            cmd, rest = parts[0], parts[1:]
            if cmd == "purchases":
                return json.dumps(w.purchases(20), indent=2, default=str)
            if cmd == "set" and len(rest) == 2 and rest[0] in SET_KEYS:
                return json.dumps(w.set_policy({SET_KEYS[rest[0]]: rest[1]}), indent=2)
            if cmd == "allow":
                domains = [d for d in (rest[0].split(",") if rest else []) if d]
                return json.dumps(w.set_policy({"allowed_domains": domains}), indent=2)
            if cmd == "reset":
                return json.dumps(w.reset_window(), indent=2)
            if cmd == "connect":
                res = w.vault_start()
                return (f"Open this link and approve in PayPal:\n{res['approve_url']}\n"
                        f"Then run: /allowance connected {res['setup_token_id']}")
            if cmd == "connected" and rest:
                return json.dumps(w.vault_finish(rest[0]), indent=2)
            if cmd == "test-card":
                return json.dumps(w.vault_test_card(), indent=2)
            if cmd == "disconnect":
                return json.dumps(w.vault_disconnect(), indent=2)
            if cmd == "cancel" and rest:
                return json.dumps(w.cancel(rest[0], by="human"), indent=2, default=str)
            if cmd == "refund" and rest:
                return json.dumps(w.refund(rest[0], by="human"), indent=2, default=str)
            return HELP
        except Exception as exc:
            return f"allowance error: {exc}"

    return allowance_cmd


def register_all(ctx) -> None:
    state: dict = {}

    def get_config(key, default=None):
        try:
            return ctx.get_config(key, default)
        except Exception:
            return default

    def get_wallet() -> Wallet:  # lazy: register() stays free of I/O
        if "wallet" not in state:
            state["wallet"] = build_wallet(get_config)
        return state["wallet"]

    handlers: dict = {}

    def handler_for(name):
        def run(args=None, **kwargs):
            if not handlers:
                handlers.update(make_handlers(get_wallet()))
            return json.dumps(handlers[name](args or {}), default=str)
        return run

    for t in TOOLS:
        ctx.register_tool(name=t["name"], toolset=TOOLSET,
                          schema={"name": t["name"], "description": t["description"], "parameters": t["schema"]},
                          handler=handler_for(t["name"]), description=t["description"], emoji="💳")
    ctx.register_command("allowance", make_command(get_wallet),
                         description="Wallet limits, auto-pay and purchases (human only)",
                         args_hint="[status|set|connect|purchases|refund]")
