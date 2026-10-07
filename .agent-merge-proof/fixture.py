"""Disposable behavior fixture; not an Aspire implementation."""

ALIASES = {"web": "frontend"}


def canonical_service(value):
    key = value.lower()
    return ALIASES.get(key, key)


def normalize_label(value):
    return value.strip().lower()


def parse_retry_count(value):
    if not value or not value.isdigit():
        raise ValueError("Retry count must contain only ASCII digits")
    return int(value)
