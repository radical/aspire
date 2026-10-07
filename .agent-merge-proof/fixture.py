"""Disposable behavior fixture; not an Aspire implementation."""

ALIASES = {"web": "frontend", "api": "backend"}


def canonical_service(value):
    key = value.strip().lower()
    return ALIASES.get(key, key)


def normalize_label(value):
    return value.strip().lower()


def parse_retry_count(value):
    if not value or any(character not in "0123456789" for character in value):
        raise ValueError("Retry count must contain only ASCII digits")
    return int(value)
