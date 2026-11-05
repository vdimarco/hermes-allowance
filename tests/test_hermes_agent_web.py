import importlib.util
import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

from allowance import hermes
from allowance.agent import run_turn
from allowance.tools import TOOL_NAMES, make_handlers

from .fakes import make_wallet

ROOT = Path(__file__).resolve().parents[1]


def manifest_tools() -> list[str]:
    text = (ROOT / "plugin.yaml").read_text()
    block = text.split("provides_tools:", 1)[1]
    names = []
    for line in block.splitlines()[1:]:
        m = re.match(r"\s+-\s+(\S+)", line)
        if not m:
            break
        names.append(m.group(1))
    return names


class FakeCtx:
    def __init__(self, config=None):
        self.tools, self.commands, self.config = {}, {}, config or {}

    def register_tool(self, name, toolset, schema, handler, **kw):
        assert schema["name"] == name and "parameters" in schema
        self.tools[name] = handler

    def register_command(self, name, handler, **kw):
        self.commands[name] = handler

    def get_config(self, key, default=None):
        return self.config.get(key, default)


def load_plugin_package():
    """Import the repo root the way Hermes does: as a package with a register()."""
    spec = importlib.util.spec_from_file_location("allowance_plugin", ROOT / "__init__.py",
                                                  submodule_search_locations=[str(ROOT)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules["allowance_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_manifest_matches_registered_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = FakeCtx({"demo_catalog": True})
    load_plugin_package().register(ctx)
    assert sorted(ctx.tools) == sorted(manifest_tools()) == sorted(TOOL_NAMES)
    assert "allowance" in ctx.commands
    out = json.loads(ctx.tools["allowance_search_products"]({"query": "coffee"}))
    assert out["ok"] and out["results"][0]["product_id"] == "demo-coffee-beans"


def test_slash_command_is_the_only_way_to_change_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    ctx = FakeCtx({"demo_catalog": True})
    hermes.register_all(ctx)
    cmd = ctx.commands["allowance"]
    out = json.loads(cmd("set budget 75"))
    assert out["policy"]["budget"] == "75.00"
    assert json.loads(cmd("status"))["policy"]["budget"] == "75.00"
    assert cmd("set auto 500").startswith("allowance error")
    assert "/allowance status" in cmd("help")


# ---- agent loop with a scripted fake Claude client ----------------------------------

def block(kind, **kw):
    return SimpleNamespace(type=kind, **kw)


class FakeClaude:
    def __init__(self, script):
        self.script = list(script)
        self.requests = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self.create))

    def create(self, **kw):
        self.requests.append(kw)
        return self.script.pop(0)


def test_agent_loop_runs_tools_and_returns_reply():
    w, _, _ = make_wallet(vault=True)
    h = make_handlers(w)
    script = [
        SimpleNamespace(stop_reason="tool_use", content=[
            block("text", text="Searching."),
            block("tool_use", id="t1", name="allowance_search_products", input={"query": "usb-c cable"})]),
        SimpleNamespace(stop_reason="tool_use", content=[
            block("tool_use", id="t2", name="allowance_propose_purchase",
                  input={"product_id": "demo-usbc-cable", "quantity": 1, "reason": "asked"})]),
        SimpleNamespace(stop_reason="end_turn", content=[block("text", text="Done.")]),
    ]
    client = FakeClaude(script)
    reply, history, events = run_turn(client, [], "buy a cable", h)
    assert reply == "Done."
    assert [e["tool"] for e in events] == ["allowance_search_products", "allowance_propose_purchase"]
    req = client.requests[0]
    assert req["model"] == "claude-opus-5-5" and req["fallbacks"] == "default"
    assert {t["name"] for t in req["tools"]} == set(TOOL_NAMES)
    assert history[-1]["role"] == "assistant"


def test_agent_handles_refusal():
    w, _, _ = make_wallet()
    client = FakeClaude([SimpleNamespace(stop_reason="refusal", content=[])])
    reply, history, events = run_turn(client, [], "x", make_handlers(w))
    assert "can't help" in reply and history[-1]["role"] == "assistant"


# ---- web app --------------------------------------------------------------------------

def test_web_endpoints(tmp_path, monkeypatch):
    monkeypatch.setenv("ALLOWANCE_DB", str(tmp_path / "w.db"))
    monkeypatch.setenv("ALLOWANCE_CATALOG", "demo")
    monkeypatch.delenv("PAYPAL_WEBHOOK_ID", raising=False)
    from fastapi.testclient import TestClient

    sys.modules.pop("web.app", None)
    import web.app as webapp

    w, fake, _ = make_wallet(vault=False)
    monkeypatch.setattr(webapp, "wallet", w)
    c = TestClient(webapp.app)
    assert c.get("/").status_code == 200
    assert c.get("/api/status").json()["auto_pay_connected"] is False
    assert c.post("/api/policy", json={"budget": "80"}).json()["policy"]["budget"] == "80.00"
    assert c.post("/api/policy", json={"auto_approve_limit": "500"}).status_code == 400

    # human connects auto-pay through the PayPal redirect
    start = c.post("/api/vault/connect").json()
    r = c.get(f"/paypal/vault-return?approval_token_id={start['setup_token_id']}", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/?vault=connected"

    # approval flow returns through /paypal/return
    prop = w.propose("demo-desk-lamp", 1, "x")["purchase"]
    bought = w.buy(prop["purchase_id"])["purchase"]
    fake.approve(bought["paypal_order_id"])
    r = c.get(f"/paypal/return?token={bought['paypal_order_id']}", follow_redirects=False)
    assert r.status_code == 303
    assert w.ledger.get_purchase(prop["purchase_id"])["status"] == "PAID"
    assert c.get("/paypal/return?token=UNKNOWN").status_code == 200  # Hermes-ledger order: info page

    # webhooks are ignored without verification config
    assert c.post("/paypal/webhook", json={"event_type": "X"}).status_code == 202
    assert c.get("/api/purchases").json()["purchases"][0]["status"] == "PAID"
