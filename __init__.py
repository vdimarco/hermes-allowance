"""Hermes plugin entry point for Allowance."""


def register(ctx):
    from .allowance.hermes import register_all

    register_all(ctx)
