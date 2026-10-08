"""Disposable fork-only behavior fixture, not production Aspire code."""

ALIASES = {"web": "frontend", "api": "backend", "gateway": "backend"}


def canonical_service(value):
    key = value.lower()
    return ALIASES.get(key, key)
