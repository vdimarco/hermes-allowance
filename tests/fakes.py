"""A fake PayPal REST API on httpx.MockTransport, close enough to drive every wallet path."""

from __future__ import annotations

import itertools
import json

import httpx

from allowance.catalog import DemoCatalog
from allowance.ledger import Ledger
from allowance.paypal import PayPalClient
from allowance.wallet import Wallet

BASE = "https://api-m.sandbox.paypal.com"


class FakePayPal:
    def __init__(self):
        self.orders: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.request_ids: dict[str, dict] = {}
        self.ids = itertools.count(1)
        self.fail_next: str | None = None
        self.setup_tokens: dict[str, str] = {}

    def approve(self, order_id: str):
        self.orders[order_id]["status"] = "APPROVED"

    def _order_view(self, o):
        out = {"id": o["id"], "status": o["status"],
               "links": [{"rel": "payer-action", "href": f"https://www.sandbox.paypal.com/checkoutnow?token={o['id']}"}],
               "purchase_units": [{"amount": o["amount"], "custom_id": o["custom_id"]}]}
        if o.get("capture"):
            out["purchase_units"][0]["payments"] = {"captures": [o["capture"]]}
        return out

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "A21", "expires_in": 32400})
        assert request.headers.get("authorization") == "Bearer A21"
        rid = request.headers.get("paypal-request-id")
        if rid and rid in self.request_ids and request.method == "POST":
            return httpx.Response(200, json=self.request_ids[rid])
        if self.fail_next:
            name, self.fail_next = self.fail_next, None
            return httpx.Response(422, json={"name": name, "message": "fake failure", "debug_id": "dbg1"})
        body = json.loads(request.content or b"{}")
        resp = self._route(request.method, path, body)
        if rid and request.method == "POST" and resp.status_code < 300:
            self.request_ids[rid] = resp.json()
        return resp

    def _route(self, method, path, body):
        if method == "POST" and path == "/v2/checkout/orders":
            oid = f"ORDER{next(self.ids)}"
            pu = body["purchase_units"][0]
            src = body["payment_source"]["paypal"]
            o = {"id": oid, "amount": pu["amount"], "custom_id": pu["custom_id"], "status": "PAYER_ACTION_REQUIRED"}
            if src.get("vault_id"):
                o["status"] = "COMPLETED"
                o["capture"] = {"id": f"CAP{next(self.ids)}", "status": "COMPLETED"}
            self.orders[oid] = o
            return httpx.Response(201, json=self._order_view(o))
        if method == "GET" and path.startswith("/v2/checkout/orders/"):
            o = self.orders.get(path.rsplit("/", 1)[-1])
            return httpx.Response(200, json=self._order_view(o)) if o else httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})
        if method == "POST" and path.endswith("/capture"):
            o = self.orders[path.split("/")[-2]]
            if o["status"] == "COMPLETED":
                return httpx.Response(422, json={"name": "UNPROCESSABLE_ENTITY", "details": [{"issue": "ORDER_ALREADY_CAPTURED"}]})
            if o["status"] != "APPROVED":
                return httpx.Response(422, json={"name": "ORDER_NOT_APPROVED", "message": "payer has not approved"})
            o["status"] = "COMPLETED"
            o["capture"] = {"id": f"CAP{next(self.ids)}", "status": "COMPLETED"}
            return httpx.Response(201, json=self._order_view(o))
        if method == "POST" and path.startswith("/v2/payments/captures/") and path.endswith("/refund"):
            return httpx.Response(201, json={"id": f"REF{next(self.ids)}", "status": "COMPLETED"})
        if method == "POST" and path == "/v3/vault/setup-tokens":
            tid = f"SETUP{next(self.ids)}"
            return httpx.Response(201, json={"id": tid, "status": "PAYER_ACTION_REQUIRED",
                                             "links": [{"rel": "approve", "href": f"https://www.sandbox.paypal.com/agreements/approve?approval_session_id={tid}"}]})
        if method == "POST" and path == "/v3/vault/payment-tokens":
            return httpx.Response(201, json={"id": "VAULT1", "payment_source": {"paypal": {"email_address": "buyer@example.com"}}})
        if method == "DELETE" and path.startswith("/v3/vault/payment-tokens/"):
            return httpx.Response(204)
        if method == "POST" and path == "/v1/notifications/verify-webhook-signature":
            return httpx.Response(200, json={"verification_status": "SUCCESS"})
        return httpx.Response(404, json={"name": "NOT_FOUND", "message": path})


class Clock:
    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


def make_wallet(vault: bool = False):
    fake = FakePayPal()
    clock = Clock()
    http = httpx.Client(transport=httpx.MockTransport(fake.handler))
    pp = PayPalClient("cid", "secret", http=http)
    w = Wallet(Ledger(":memory:", now=clock), DemoCatalog(), pp, public_url="https://demo.example", now=clock)
    if vault:
        w.ledger.set_setting("vault", {"id": "VAULT1", "email": "buyer@example.com"})
    return w, fake, clock
