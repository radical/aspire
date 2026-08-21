from __future__ import annotations

"""Timezone-aware timestamp parsing and formatting shared across ci_shepherd modules.

Deliberately narrow: this module holds the two helpers that every module needs to
agree on when reading GitHub timestamps and writing them back out. It is not a
general utility module - anything that carries observation or lifecycle meaning
belongs with the module that owns that meaning.
"""

from datetime import datetime, timezone


__all__ = ["format_utc_z", "parse_aware_iso8601"]


def parse_aware_iso8601(value: object, name: str) -> datetime:
    """Parse a timezone-aware ISO-8601 timestamp into UTC."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a nonempty string.")
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as ex:
        raise ValueError(f"{name} must be a timezone-aware ISO-8601 timestamp.") from ex
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware ISO-8601 timestamp.")
    return parsed.astimezone(timezone.utc)


def format_utc_z(value: datetime) -> str:
    """Render a datetime as UTC ISO-8601 with a ``Z`` suffix."""
    text = value.astimezone(timezone.utc).isoformat()
    return f"{text[:-6]}Z" if text.endswith("+00:00") else text
