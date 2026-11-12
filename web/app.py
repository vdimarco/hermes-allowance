"""Allowance web demo: chat with a Claude shopping agent, and a wallet panel only the human controls.

Run:  uvicorn web.app:app --reload
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from allowance.paypal import PayPalError
from allowance.tools import make_handlers
from allowance.wallet import build_from_env

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Allowance")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

wallet = build_from_env()
handlers = make_handlers(wallet)

_sessions: dict[str, list] = {}
_sessions_lock = threading.Lock()
_hits: dict[str, deque] = {}
_day = {"date": "", "count": 0}
PER_IP = int(os.environ.get("ALLOWANCE_CHAT_PER_10MIN", "30"))
PER_DAY = int(os.environ.get("ALLOWANCE_CHAT_PER_DAY", "1000"))

_client = None


def claude():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client


def _rate_ok(ip: str) -> bool:
    now = time.time()
    today = time.strftime("%Y-%m-%d")
    with _sessions_lock:
        if _day["date"] != today:
            _day.update(date=today, count=0)
        if _day["count"] >= PER_DAY:
            return False
        q = _hits.setdefault(ip, deque())
        while q and now - q[0] > 600:
            q.popleft()
        if len(q) >= PER_IP:
            return False
        q.append(now)
        _day["count"] += 1
        return True


class ChatIn(BaseModel):
    session_id: str | None = None
    message: str


class PolicyIn(BaseModel):
    budget: str | None = None
    auto_approve_limit: str | None = None
    max_per_purchase: str | None = None
    max_quantity: int | None = None
    max_purchases_per_hour: int | None = None
    allowed_domains: list[str] | None = None
    blocked_terms: list[str] | None = None


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/api/chat")
def chat(body: ChatIn, request: Request):
    text = (body.message or "").strip()
    if not text:
        raise HTTPException(400, "message is required")
    if len(text) > 2000:
        raise HTTPException(400, "message is too long")
    ip = request.headers.get("x-forwarded-for", request.client.host if request.client else "?").split(",")[0]
    if not _rate_ok(ip):
        raise HTTPException(429, "Too many messages. Wait a few minutes.")
    sid = body.session_id or uuid.uuid4().hex
    with _sessions_lock:
        history = list(_sessions.get(sid, []))
    from allowance.agent import run_turn

    try:
        reply, history, events = run_turn(claude(), history, text, handlers)
    except Exception as exc:  # surface config errors (e.g. missing ANTHROPIC_API_KEY) to the UI
        return JSONResponse({"session_id": sid, "reply": f"Agent error: {type(exc).__name__}: {exc}",
                             "events": [], "status": wallet.status()}, status_code=200)
    with _sessions_lock:
        _sessions[sid] = history[-60:] if len(history) <= 60 else _trim(history)
        if len(_sessions) > 500:
            _sessions.pop(next(iter(_sessions)))
    return {"session_id": sid, "reply": reply, "events": events, "status": wallet.status()}


def _trim(history: list) -> list:
    """Keep the tail, starting at a plain user text turn so tool_use/tool_result pairs stay whole."""
    tail = history[-60:]
    for i, m in enumerate(tail):
        if m["role"] == "user" and isinstance(m["content"], str):
            return tail[i:]
    return []


@app.get("/api/status")
def status():
    return wallet.status()


@app.get("/api/purchases")
def purchases(limit: int = 20):
    return wallet.purchases(limit)


@app.get("/api/events")
def events(limit: int = 30):
    return {"events": wallet.ledger.events(limit)}


@app.post("/api/policy")
def set_policy(body: PolicyIn):
    try:
        return wallet.set_policy(body.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.post("/api/reset")
def reset():
    return wallet.reset_window()


@app.post("/api/purchases/{pid}/cancel")
def cancel(pid: str):
    return wallet.cancel(pid, by="human")


@app.post("/api/purchases/{pid}/refund")
def refund(pid: str):
    return wallet.refund(pid, by="human")


@app.post("/api/purchases/{pid}/confirm")
def confirm(pid: str):
    return wallet.confirm(pid)


@app.post("/api/vault/connect")
def vault_connect():
    try:
        return wallet.vault_start()
    except PayPalError as exc:
        raise HTTPException(502, f"PayPal error: {exc.name}: {exc.message}")


@app.post("/api/vault/test-card")
def vault_test_card():
    try:
        return wallet.vault_test_card()
    except PayPalError as exc:
        raise HTTPException(502, f"PayPal error: {exc.name}: {exc.message}")


@app.post("/api/vault/disconnect")
def vault_disconnect():
    return wallet.vault_disconnect()


@app.get("/paypal/vault-return")
def vault_return(approval_token_id: str = "", approval_session_id: str = ""):
    try:
        res = wallet.vault_finish(approval_token_id)
    except PayPalError as exc:
        return RedirectResponse(f"/?vault=error&msg={exc.name}", status_code=303)
    return RedirectResponse("/?vault=" + ("connected" if res.get("ok") else "error"), status_code=303)


@app.get("/paypal/return")
def paypal_return(token: str = ""):
    p = wallet.ledger.find_by_order(token) if token else None
    if p is None:  # e.g. an order from the Hermes plugin's own ledger
        return HTMLResponse(_page("Approved in PayPal",
                                  "Go back to your agent and tell it you approved. It will check PayPal and finish the payment."))
    wallet.confirm(p["id"])
    return RedirectResponse(f"/?purchase={p['id']}", status_code=303)


@app.get("/paypal/cancel")
def paypal_cancel(token: str = ""):
    p = wallet.ledger.find_by_order(token) if token else None
    if p is None:
        return HTMLResponse(_page("Payment canceled", "Nothing was charged. You can close this tab."))
    wallet.cancel(p["id"], by="human")
    return RedirectResponse(f"/?purchase={p['id']}", status_code=303)


@app.post("/paypal/webhook")
async def paypal_webhook(request: Request):
    event = await request.json()
    webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID")
    if not webhook_id:
        return JSONResponse({"ok": False, "note": "PAYPAL_WEBHOOK_ID is not set; webhook ignored"}, status_code=202)
    try:
        ok = wallet.paypal.verify_webhook(headers=dict(request.headers), event=event, webhook_id=webhook_id)
    except PayPalError:
        ok = False
    if not ok:
        raise HTTPException(400, "signature verification failed")
    return wallet.handle_webhook(event)


def _page(title: str, msg: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><link rel="stylesheet" href="/static/style.css"></head>
<body><main class="notice"><h1>{title}</h1><p>{msg}</p></main></body></html>"""
