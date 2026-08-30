from __future__ import annotations

from collections.abc import Mapping


EXECUTABLE_CI_LABELS = frozenset(
    {"automation-broken", "ci-failure-cause", "test-failure"}
)


def label_names(raw_labels: object) -> frozenset[str]:
    if not isinstance(raw_labels, list):
        return frozenset()

    names: set[str] = set()
    for raw_label in raw_labels:
        name = (
            raw_label
            if isinstance(raw_label, str)
            else raw_label.get("name")
            if isinstance(raw_label, Mapping)
            else None
        )
        if isinstance(name, str) and (normalized := name.strip().casefold()):
            names.add(normalized)
    return frozenset(names)


def executable_ci_labels(raw_labels: object) -> frozenset[str]:
    return label_names(raw_labels).intersection(EXECUTABLE_CI_LABELS)
