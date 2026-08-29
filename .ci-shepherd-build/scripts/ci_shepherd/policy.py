from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ci_shepherd.naming import normalize_component


class PolicyError(ValueError):
    pass


__all__ = ["ManualPolicy", "PolicyError", "load_policy", "load_policy_document"]


@dataclass(frozen=True, slots=True)
class ManualPolicy:
    policy_version: str
    systemic_transient_window_days: int
    retry_safe_pattern_ids: frozenset[str]

    def __post_init__(self) -> None:
        # Canonicalized on the type rather than only in the loader so every
        # construction path (loader, tests, future callers) compares against
        # observation fingerprint components using the same contract.
        # The replacement is unconditional: an already-canonical mutable set
        # compares equal to its frozenset form, so a conditional assignment
        # would leave the caller's set aliased on a frozen dataclass, making
        # the policy unhashable and mutable from a distance.
        object.__setattr__(
            self,
            "retry_safe_pattern_ids",
            frozenset(normalize_component(value) for value in self.retry_safe_pattern_ids),
        )

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": _SCHEMA_VERSION,
            "policyVersion": self.policy_version,
            "systemicTransientWindowDays": self.systemic_transient_window_days,
            "retrySafePatternIds": sorted(self.retry_safe_pattern_ids),
        }


_SCHEMA_VERSION = 1
_POLICY_FIELDS = frozenset(
    {
        "schemaVersion",
        "policyVersion",
        "systemicTransientWindowDays",
        "retrySafePatternIds",
    }
)


def load_policy(path: Path) -> ManualPolicy:
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise PolicyError(f"Unable to read policy file {path}.") from exc

    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PolicyError(f"Policy file {path} contains invalid UTF-8.") from exc

    try:
        document = json.loads(text, object_pairs_hook=lambda pairs: _strict_object_pairs_hook(path, pairs))
    except json.JSONDecodeError as exc:
        raise PolicyError(f"Policy file {path} is not valid JSON.") from exc

    return load_policy_document(document)


def load_policy_document(document: object) -> ManualPolicy:
    mapping = _require_mapping(document)
    _require_exact_keys(mapping)

    schema_version = _require_exact_int(mapping, "schemaVersion", _SCHEMA_VERSION)
    if schema_version != _SCHEMA_VERSION:
        raise PolicyError(f"schemaVersion must be {_SCHEMA_VERSION}.")

    policy_version = _require_nonempty_string(mapping, "policyVersion")
    systemic_transient_window_days = _require_positive_int(
        mapping, "systemicTransientWindowDays"
    )
    retry_safe_pattern_ids = _require_retry_safe_pattern_ids(mapping)

    return ManualPolicy(
        policy_version=policy_version,
        systemic_transient_window_days=systemic_transient_window_days,
        retry_safe_pattern_ids=retry_safe_pattern_ids,
    )


def _require_exact_keys(mapping: Mapping[str, Any]) -> None:
    for key in mapping:
        if not isinstance(key, str):
            raise PolicyError("Policy document keys must be strings.")

    unknown_fields = sorted(set(mapping) - _POLICY_FIELDS)
    if unknown_fields:
        raise PolicyError(f"Policy document has unknown fields: {', '.join(unknown_fields)}.")

    missing_fields = sorted(_POLICY_FIELDS - set(mapping))
    if missing_fields:
        raise PolicyError(f"Policy document is missing fields: {', '.join(missing_fields)}.")


def _require_mapping(document: object) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise PolicyError("Policy document must be an object.")
    return dict(document)


def _strict_object_pairs_hook(path: Path, pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise PolicyError(f"Policy file {path} contains duplicate JSON key: {key}.")
        mapping[key] = value
    return mapping


def _require_nonempty_string(mapping: Mapping[str, Any], field_name: str) -> str:
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value:
        raise PolicyError(f"{field_name} must be a nonempty string.")
    return value


def _require_exact_int(mapping: Mapping[str, Any], field_name: str, expected: int) -> int:
    value = mapping.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise PolicyError(f"{field_name} must be an integer.")
    if value != expected:
        raise PolicyError(f"{field_name} must be {expected}.")
    return value


def _require_positive_int(mapping: Mapping[str, Any], field_name: str) -> int:
    value = mapping.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PolicyError(f"{field_name} must be a positive integer.")
    return value


def _require_retry_safe_pattern_ids(mapping: Mapping[str, Any]) -> frozenset[str]:
    raw_values = mapping.get("retrySafePatternIds")
    if not isinstance(raw_values, list):
        raise PolicyError("retrySafePatternIds must be a list.")

    # Pattern IDs are canonicalized at parse time so a policy entry written as
    # "HTTP_502" matches the observation-side fingerprint component "http-502".
    # Both sides share ci_shepherd.naming.normalize_component.
    pattern_ids: set[str] = set()
    for raw_value in raw_values:
        if not isinstance(raw_value, str) or not raw_value:
            raise PolicyError("retrySafePatternIds must contain nonempty strings.")
        pattern_id = normalize_component(raw_value)
        if pattern_id == "none" and raw_value.strip().lower() != "none":
            raise PolicyError(
                f"retrySafePatternIds entry {raw_value!r} has no canonical component form."
            )
        if pattern_id in pattern_ids:
            raise PolicyError(
                f"retrySafePatternIds must not contain duplicate entries; {raw_value!r} "
                f"normalizes to {pattern_id!r} which is already present."
            )
        pattern_ids.add(pattern_id)
    return frozenset(pattern_ids)
