"""Small PayPal REST client: Orders v2, Payments v2 refunds, Vault v3, webhook verify.

Sandbox by default. Every money-moving call sends a PayPal-Request-Id so a retry
cannot charge twice.
"""

from __future__ import annotations

import time
import uuid

import httpx

SANDBOX = "https://api-m.sandbox.paypal.com"
LIVE = "https://api-m.paypal.com"


class PayPalError(Exception):
    def __init__(self, status: int, name: str, message: str, debug_id: str | None = None, details=None):
        super().__init__(f"PayPal {status} {name}: {message}")
        self.status = status
        self.name = name
        self.message = message
        self.debug_id = debug_id
        self.details = details or []


def link(resource: dict, rel: str) -> str | None:
    for item in resource.get("links") or []:
        if item.get("rel") == rel:
            return item.get("href")
    return None


def first_capture(order: dict) -> dict | None:
    for pu in order.get("purchase_units") or []:
        for cap in ((pu.get("payments") or {}).get("captures") or []):
            return cap
    return None


class PayPalClient:
    """With client_id and secret, the client gets an OAuth token. Without them,
    it sends no Authorization header on the token call (for a proxy that adds it)."""

    def __init__(self, client_id: str | None = None, secret: str | None = None,
                 base_url: str = SANDBOX, http: httpx.Client | None = None,
                 brand_name: str = "Allowance Demo Store"):
        self.client_id = client_id
        self.secret = secret
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30.0)
        self.brand_name = brand_name
        self._token: str | None = None
        self._token_exp = 0.0

    # auth -----------------------------------------------------------------
    def _access_token(self) -> str:
        if self._token and time.time() < self._token_exp - 60:
            return self._token
        auth = (self.client_id, self.secret) if self.client_id and self.secret else None
        r = self.http.post(f"{self.base_url}/v1/oauth2/token",
                           data={"grant_type": "client_credentials"},
                           headers={"Accept": "application/json"}, auth=auth)
        if r.status_code >= 400:
            raise PayPalError(r.status_code, "AUTH_FAILED", r.text[:300])
        data = r.json()
        self._token = data["access_token"]
        self._token_exp = time.time() + float(data.get("expires_in", 3000))
        return self._token

    def _call(self, method: str, path: str, body: dict | None = None,
              request_id: str | None = None, prefer_full: bool = True) -> dict:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Content-Type": "application/json",
        }
        if prefer_full:
            headers["Prefer"] = "return=representation"
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        r = self.http.request(method, f"{self.base_url}{path}", json=body, headers=headers)
        if r.status_code == 401:  # token expired early: refresh once
            self._token = None
            headers["Authorization"] = f"Bearer {self._access_token()}"
            r = self.http.request(method, f"{self.base_url}{path}", json=body, headers=headers)
        if r.status_code >= 400:
            try:
                err = r.json()
            except ValueError:
                err = {"name": "HTTP_ERROR", "message": r.text[:300]}
            raise PayPalError(r.status_code, err.get("name") or err.get("error") or "ERROR",
                              err.get("message") or err.get("error_description") or "",
                              err.get("debug_id"), err.get("details"))
        if not r.content:
            return {}
        return r.json()

    # orders -----------------------------------------------------------------
    def create_order(self, *, amount: str, currency: str, description: str, custom_id: str,
                     return_url: str | None = None, cancel_url: str | None = None,
                     vault_id: str | None = None, vault_type: str = "paypal", request_id: str | None = None,
                     item_name: str | None = None, quantity: int = 1, unit_amount: str | None = None) -> dict:
        unit = {
            "amount": {
                "currency_code": currency,
                "value": amount,
            },
            "description": description[:127],
            "custom_id": custom_id[:127],
            "soft_descriptor": "ALLOWANCE",
        }
        if item_name and unit_amount:
            unit["amount"]["breakdown"] = {"item_total": {"currency_code": currency, "value": amount}}
            unit["items"] = [{
                "name": item_name[:127],
                "quantity": str(quantity),
                "unit_amount": {"currency_code": currency, "value": unit_amount},
                "category": "PHYSICAL_GOODS",
            }]
        body: dict = {"intent": "CAPTURE", "purchase_units": [unit]}
        if vault_id:
            body["payment_source"] = {vault_type: {"vault_id": vault_id}}
        else:
            body["payment_source"] = {"paypal": {"experience_context": {
                "brand_name": self.brand_name,
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING",
                "return_url": return_url,
                "cancel_url": cancel_url,
            }}}
        return self._call("POST", "/v2/checkout/orders", body, request_id or str(uuid.uuid4()))

    def get_order(self, order_id: str) -> dict:
        return self._call("GET", f"/v2/checkout/orders/{order_id}", prefer_full=False)

    def capture_order(self, order_id: str, request_id: str | None = None) -> dict:
        return self._call("POST", f"/v2/checkout/orders/{order_id}/capture", {},
                          request_id or str(uuid.uuid4()))

    def refund_capture(self, capture_id: str, *, note: str = "", request_id: str | None = None) -> dict:
        body = {"note_to_payer": note[:255]} if note else {}
        return self._call("POST", f"/v2/payments/captures/{capture_id}/refund", body,
                          request_id or str(uuid.uuid4()))

    # vault (save the payer's PayPal account for buyer-not-present payments) -----
    def create_setup_token(self, *, return_url: str, cancel_url: str, request_id: str | None = None) -> dict:
        body = {"payment_source": {"paypal": {
            "description": "Let your Allowance agent pay small purchases without asking each time",
            "usage_pattern": "IMMEDIATE",
            "usage_type": "MERCHANT",
            "customer_type": "CONSUMER",
            "permit_multiple_payment_tokens": False,
            "experience_context": {
                "brand_name": self.brand_name,
                "shipping_preference": "NO_SHIPPING",
                "return_url": return_url,
                "cancel_url": cancel_url,
            },
        }}}
        return self._call("POST", "/v3/vault/setup-tokens", body, request_id or str(uuid.uuid4()))

    def create_card_setup_token(self, *, number: str, expiry: str, name: str, request_id: str | None = None) -> dict:
        """Sandbox only: save a PayPal test card server-side. A live app collects cards with PayPal's card fields."""
        body = {"payment_source": {"card": {
            "number": number, "expiry": expiry, "name": name,
            "billing_address": {"address_line_1": "2211 N First Street", "admin_area_2": "San Jose",
                                "admin_area_1": "CA", "postal_code": "95131", "country_code": "US"},
        }}}
        return self._call("POST", "/v3/vault/setup-tokens", body, request_id or str(uuid.uuid4()))

    def create_payment_token(self, setup_token_id: str, request_id: str | None = None) -> dict:
        body = {"payment_source": {"token": {"id": setup_token_id, "type": "SETUP_TOKEN"}}}
        return self._call("POST", "/v3/vault/payment-tokens", body, request_id or str(uuid.uuid4()))

    def delete_payment_token(self, token_id: str) -> None:
        self._call("DELETE", f"/v3/vault/payment-tokens/{token_id}", prefer_full=False)

    # webhooks -----------------------------------------------------------------
    def verify_webhook(self, *, headers: dict, event: dict, webhook_id: str) -> bool:
        h = {k.lower(): v for k, v in headers.items()}
        body = {
            "auth_algo": h.get("paypal-auth-algo"),
            "cert_url": h.get("paypal-cert-url"),
            "transmission_id": h.get("paypal-transmission-id"),
            "transmission_sig": h.get("paypal-transmission-sig"),
            "transmission_time": h.get("paypal-transmission-time"),
            "webhook_id": webhook_id,
            "webhook_event": event,
        }
        res = self._call("POST", "/v1/notifications/verify-webhook-signature", body, prefer_full=False)
        return res.get("verification_status") == "SUCCESS"
