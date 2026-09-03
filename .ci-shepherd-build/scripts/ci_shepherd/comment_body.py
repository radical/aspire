from __future__ import annotations

import re


_EVIDENCE_REVIEWED_BLOCK = re.compile(
    r"(?m)^\*\*Evidence reviewed:\*\*\n(?:- .*\n)+(?:\n|$)"
)


def comment_bodies_materially_equal(left: str, right: str) -> bool:
    """Compare status comments without treating citation-list churn as an update."""

    return _material_comment_body(left) == _material_comment_body(right)


def _material_comment_body(body: str) -> str:
    return _EVIDENCE_REVIEWED_BLOCK.sub("", body.strip())
