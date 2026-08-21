from __future__ import annotations

import re
import unicodedata


def normalize_component(value: object) -> str:
    """Canonicalize one identifier component to lowercase/hyphen form.

    This is the single normalization contract shared by policy pattern IDs,
    fingerprint IDs, and coverage subject IDs. Both sides of a comparison must
    run through this function or a policy entry like ``HTTP_502`` would never
    match an observation pattern like ``http-502``.

    Examples:
        ``"HTTP_502"``                    -> ``"http-502"``
        ``"Tests / Hosting (ubuntu-22.04)"`` -> ``"tests-hosting-ubuntu-22-04"``
        ``None`` / ``""`` / ``"***"``     -> ``"none"``
    """
    text = "none" if value is None else str(value)
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    normalized = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return normalized or "none"
