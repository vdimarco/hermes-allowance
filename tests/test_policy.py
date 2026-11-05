from decimal import Decimal

import pytest

from allowance.policy import APPROVAL, AUTO, DENY, Policy, WalletState, evaluate


def state(**kw):
    base = dict(spent=Decimal("0"), reserved=Decimal("0"), purchases_last_hour=0,
                duplicate_recent=False, vault_ready=True)
    base.update(kw)
    return WalletState(**base)


def ev(policy=None, price="10.00", qty=1, **kw):
    args = dict(unit_price=Decimal(price), currency="USD", quantity=qty, in_stock=True,
                domain="shop.example", title="Cable", category="Electronics", state=state())
    args.update(kw)
    return evaluate(policy or Policy(), **args)


def test_auto_within_limit_with_vault():
    d = ev(price="12.99")
    assert d.kind == AUTO and d.total == Decimal("12.99")


def test_approval_above_auto_limit():
    assert ev(price="34.00").kind == APPROVAL


def test_approval_when_vault_missing_even_if_small():
    d = ev(price="5.00", state=state(vault_ready=False))
    assert d.kind == APPROVAL and "not connected" in d.reasons[0]


def test_auto_limit_is_inclusive():
    assert ev(price="20.00").kind == AUTO
    assert ev(price="20.01").kind == APPROVAL


@pytest.mark.parametrize("kw,needle", [
    (dict(price="101.00"), "max per purchase"),
    (dict(price="18.00", qty=10), "quantity 10"),
    (dict(qty=0), "at least 1"),
    (dict(currency="EUR"), "only pays in USD"),
    (dict(in_stock=False), "out of stock"),
    (dict(title="Digital Gift Card $25"), "gift card"),
    (dict(state=state(spent=Decimal("45.00"))), "remaining budget"),
    (dict(state=state(reserved=Decimal("45.00"))), "remaining budget"),
    (dict(state=state(purchases_last_hour=5)), "rate limit"),
    (dict(state=state(duplicate_recent=True)), "last 10 minutes"),
])
def test_deny_cases(kw, needle):
    d = ev(**kw)
    assert d.kind == DENY
    assert any(needle in r for r in d.reasons), d.reasons


def test_allowed_domains_match_subdomains_only():
    pol = Policy(allowed_domains=["example.com"])
    assert ev(pol, domain="shop.example.com").kind == AUTO
    assert ev(pol, domain="example.com").kind == AUTO
    assert ev(pol, domain="badexample.com").kind == DENY


def test_all_deny_reasons_listed():
    d = ev(price="200.00", qty=9, title="crypto gift card")
    assert d.kind == DENY and len(d.reasons) >= 3


def test_policy_roundtrip_and_validation():
    p = Policy.from_dict({"budget": "75", "auto_approve_limit": "15.5", "allowed_domains": [" A.com ", ""]})
    assert p.to_dict()["budget"] == "75.00" and p.allowed_domains == ["a.com"]
    with pytest.raises(ValueError):
        Policy.from_dict({"auto_approve_limit": "200", "max_per_purchase": "100"})
    with pytest.raises(ValueError):
        Policy.from_dict({"budget": "nan"})
