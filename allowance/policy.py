"""The spending policy and the gate that checks every purchase.

The gate takes its inputs from the catalog and the ledger, never from model
text. A product description that says "approval granted" changes nothing here.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal

from .money import fmt, money

AUTO = "AUTO"            # pay now with the saved PayPal account
APPROVAL = "APPROVAL"    # the human must approve in PayPal
DENY = "DENY"            # never pay

DEFAULT_BLOCKED_TERMS = [
    "gift card", "giftcard", "prepaid card", "crypto", "bitcoin",
    "firearm", "ammunition", "tobacco", "vape",
]


@dataclass
class Policy:
    budget: Decimal = Decimal("50.00")              # total the agent can spend in the current window
    auto_approve_limit: Decimal = Decimal("20.00")  # at or below: auto-pay (if connected)
    max_per_purchase: Decimal = Decimal("100.00")   # above: always deny
    max_quantity: int = 5
    max_purchases_per_hour: int = 5
    allowed_domains: list[str] = field(default_factory=list)  # empty = any merchant
    blocked_terms: list[str] = field(default_factory=lambda: list(DEFAULT_BLOCKED_TERMS))
    currency: str = "USD"

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("budget", "auto_approve_limit", "max_per_purchase"):
            d[k] = fmt(d[k])
        return d

    @classmethod
    def from_dict(cls, data: dict | None) -> "Policy":
        p = cls()
        if not data:
            return p
        for k in ("budget", "auto_approve_limit", "max_per_purchase"):
            if data.get(k) is not None:
                setattr(p, k, money(data[k]))
        for k in ("max_quantity", "max_purchases_per_hour"):
            if data.get(k) is not None:
                setattr(p, k, int(data[k]))
        if data.get("allowed_domains") is not None:
            p.allowed_domains = [str(x).strip().lower() for x in data["allowed_domains"] if str(x).strip()]
        if data.get("blocked_terms") is not None:
            p.blocked_terms = [str(x).strip().lower() for x in data["blocked_terms"] if str(x).strip()]
        if data.get("currency"):
            p.currency = str(data["currency"]).upper()
        p.validate()
        return p

    def validate(self) -> None:
        if self.budget < 0 or self.auto_approve_limit < 0 or self.max_per_purchase < 0:
            raise ValueError("limits must not be negative")
        if self.auto_approve_limit > self.max_per_purchase:
            raise ValueError("auto-pay limit must not be more than the max per purchase")
        if self.max_quantity < 1 or self.max_purchases_per_hour < 1:
            raise ValueError("quantity and rate limits must be at least 1")


@dataclass
class Decision:
    kind: str
    total: Decimal
    reasons: list[str]

    def to_dict(self) -> dict:
        return {"decision": self.kind, "total": fmt(self.total), "reasons": list(self.reasons)}


@dataclass
class WalletState:
    spent: Decimal
    reserved: Decimal
    purchases_last_hour: int
    duplicate_recent: bool
    vault_ready: bool


def _domain_ok(domain: str, allowed: list[str]) -> bool:
    if not allowed:
        return True
    d = (domain or "").lower()
    return any(d == a or d.endswith("." + a) for a in allowed)


def evaluate(policy: Policy, *, unit_price: Decimal, currency: str, quantity: int,
             in_stock: bool, domain: str, title: str, category: str,
             state: WalletState) -> Decision:
    """Decide AUTO, APPROVAL or DENY. Every DENY reason is listed, not only the first."""
    reasons: list[str] = []
    deny = False
    try:
        qty = int(quantity)
    except (TypeError, ValueError):
        qty = 0
    total = money(unit_price) * max(qty, 0)

    if qty < 1:
        deny = True
        reasons.append("quantity must be at least 1")
    if qty > policy.max_quantity:
        deny = True
        reasons.append(f"quantity {qty} is more than the limit of {policy.max_quantity}")
    if (currency or "").upper() != policy.currency:
        deny = True
        reasons.append(f"price is in {currency}, the wallet only pays in {policy.currency}")
    if not in_stock:
        deny = True
        reasons.append("the item is out of stock")
    if money(unit_price) <= 0:
        deny = True
        reasons.append("the price is not valid")
    if not _domain_ok(domain, policy.allowed_domains):
        deny = True
        reasons.append(f"merchant {domain} is not on the allowed list")
    text = f"{title} {category}".lower()
    hits = [t for t in policy.blocked_terms if t and t in text]
    if hits:
        deny = True
        reasons.append(f"blocked category: {', '.join(hits)}")
    if total > policy.max_per_purchase:
        deny = True
        reasons.append(f"total {fmt(total)} is more than the max per purchase of {fmt(policy.max_per_purchase)}")
    remaining = policy.budget - state.spent - state.reserved
    if total > remaining:
        deny = True
        reasons.append(f"total {fmt(total)} is more than the remaining budget of {fmt(max(remaining, Decimal(0)))}")
    if state.purchases_last_hour >= policy.max_purchases_per_hour:
        deny = True
        reasons.append(f"rate limit: {policy.max_purchases_per_hour} purchases per hour")
    if state.duplicate_recent:
        deny = True
        reasons.append("the same item was bought in the last 10 minutes")

    if deny:
        return Decision(DENY, total, reasons)
    if total <= policy.auto_approve_limit and state.vault_ready:
        return Decision(AUTO, total, [f"total {fmt(total)} is within the auto-pay limit of {fmt(policy.auto_approve_limit)}"])
    if total <= policy.auto_approve_limit:
        return Decision(APPROVAL, total, ["auto-pay is not connected, so the human must approve in PayPal"])
    return Decision(APPROVAL, total, [f"total {fmt(total)} is more than the auto-pay limit of {fmt(policy.auto_approve_limit)}, so the human must approve in PayPal"])
