"""Allowance: a PayPal wallet with spending limits for AI agents.

The agent can search, propose and buy. Only the human can change the limits,
connect auto-pay, or approve a purchase above the auto-pay limit (in PayPal).
"""

from .ledger import Ledger
from .policy import Decision, Policy, evaluate
from .wallet import Wallet

__all__ = ["Decision", "Ledger", "Policy", "Wallet", "evaluate"]
