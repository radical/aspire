"""Strict parsing and classification for CI Shepherd autonomous operation policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import re
from types import MappingProxyType
from typing import Any, Mapping

from .models import stable_json
from .timeutils import format_utc_z, parse_aware_iso8601


__all__ = [
    "DEFAULT_CAPS",
    "DEFAULT_EXPIRY_DAYS",
    "HARD_MAX_PER_RUN",
    "HARD_MAX_ROLLING_24H",
    "MAX_EXPIRY_DAYS",
    "OPERATION_CLASS_BY_OPERATION",
    "OPERATION_CLASSES",
    "OperationClassPolicy",
    "OperationPolicyError",
    "OperationPolicyRevision",
    "classify_operation",
    "load_operation_policy_document",
]


class OperationPolicyError(ValueError):
    pass


OPERATION_CLASSES = (
    "create-comment",
    "edit-comment",
    "close-issue",
    "delegate-copilot",
    "rerun-or-retry",
)
DEFAULT_CAPS = {
    "create-comment": {"maxPerRun": 10, "maxRolling24h": 30},
    "edit-comment": {"maxPerRun": 10, "maxRolling24h": 30},
    "close-issue": {"maxPerRun": 5, "maxRolling24h": 10},
    "delegate-copilot": {"maxPerRun": 3, "maxRolling24h": 5},
    "rerun-or-retry": {"maxPerRun": 5, "maxRolling24h": 15},
}
HARD_MAX_PER_RUN = 100
HARD_MAX_ROLLING_24H = 300
DEFAULT_EXPIRY_DAYS = 30
MAX_EXPIRY_DAYS = 90
OPERATION_CLASS_BY_OPERATION = {
    "create-comment": "create-comment",
    "edit-comment": "edit-comment",
    "close-issue": "close-issue",
    "assign-copilot": "delegate-copilot",
}

_SCHEMA_VERSION = 1
_POLICY_FIELDS = frozenset(
    {
        "schemaVersion",
        "repository",
        "revisionId",
        "revision",
        "status",
        "createdAtUtc",
        "expiresAtUtc",
        "actor",
        "replacesRevisionId",
        "operationClasses",
        "deniedActionIds",
        "deniedTargets",
    }
)
_OPERATION_CLASS_FIELDS = frozenset({"enabled", "maxPerRun", "maxRolling24h"})
_ALLOWED_STATUSES = frozenset({"active", "paused", "revoked"})
_OWNER_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?"
_REPOSITORY_NAME_PATTERN = r"[A-Za-z0-9._-]+"
_REPOSITORY_RE = re.compile(rf"^{_OWNER_PATTERN}/{_REPOSITORY_NAME_PATTERN}$")
_ACTOR_RE = re.compile(rf"^github:{_OWNER_PATTERN}$")
_REVISION_ID_RE = re.compile(r"^policy:[1-9][0-9]*$")


@dataclass(frozen=True, slots=True)
class OperationClassPolicy:
    enabled: bool
    max_per_run: int
    max_rolling_24h: int

    def as_public_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "maxPerRun": self.max_per_run,
            "maxRolling24h": self.max_rolling_24h,
        }


@dataclass(frozen=True, slots=True)
class OperationPolicyRevision:
    schema_version: int
    repository: str
    revision_id: str
    revision: int
    status: str
    created_at_utc: datetime
    expires_at_utc: datetime
    actor: str
    replaces_revision_id: str | None
    operation_classes: Mapping[str, OperationClassPolicy]
    denied_action_ids: tuple[str, ...]
    denied_targets: tuple[str, ...]
    _digest: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "operation_classes",
            MappingProxyType(dict(self.operation_classes)),
        )
        object.__setattr__(self, "denied_action_ids", tuple(self.denied_action_ids))
        object.__setattr__(self, "denied_targets", tuple(self.denied_targets))

    def as_public_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": self.schema_version,
            "repository": self.repository,
            "revisionId": self.revision_id,
            "revision": self.revision,
            "status": self.status,
            "createdAtUtc": format_utc_z(self.created_at_utc),
            "expiresAtUtc": format_utc_z(self.expires_at_utc),
            "actor": self.actor,
            "replacesRevisionId": self.replaces_revision_id,
            "operationClasses": {
                name: self.operation_classes[name].as_public_dict()
                for name in OPERATION_CLASSES
            },
            "deniedActionIds": list(self.denied_action_ids),
            "deniedTargets": list(self.denied_targets),
        }

    @property
    def digest(self) -> str:
        return self._digest

    def active_at(self, now: datetime) -> bool:
        current = _require_aware_datetime(now, "now")
        return (
            self.status == "active"
            and self.created_at_utc <= current
            and current < self.expires_at_utc
        )


def classify_operation(operation: str) -> str | None:
    if not isinstance(operation, str):
        return None
    return OPERATION_CLASS_BY_OPERATION.get(operation)


def load_operation_policy_document(document: object) -> OperationPolicyRevision:
    mapping = _require_mapping(document, "Operation policy")
    _require_exact_keys(mapping, _POLICY_FIELDS, "Operation policy")
    digest = _digest_policy_document(mapping)

    schema_version = _require_exact_int(mapping, "schemaVersion", _SCHEMA_VERSION)
    repository = _require_repository_identity(mapping.get("repository"))
    revision = _require_positive_int(mapping.get("revision"), "revision")
    revision_id = _require_revision_identity(mapping.get("revisionId"), revision)
    status = _require_status(mapping.get("status"))
    created_at_utc = _parse_policy_timestamp(mapping.get("createdAtUtc"), "createdAtUtc")
    expires_at_utc = _parse_policy_timestamp(mapping.get("expiresAtUtc"), "expiresAtUtc")
    if created_at_utc >= expires_at_utc:
        raise OperationPolicyError("createdAtUtc must be earlier than expiresAtUtc.")
    if expires_at_utc > created_at_utc + timedelta(days=MAX_EXPIRY_DAYS):
        raise OperationPolicyError("expiresAtUtc must not exceed 90 days after createdAtUtc.")
    actor = _require_actor_identity(mapping.get("actor"))
    replaces_revision_id = _require_replaces_revision_id(
        mapping.get("replacesRevisionId"), revision
    )
    operation_classes = _require_operation_classes(mapping.get("operationClasses"))
    _require_total_caps(operation_classes)
    denied_action_ids = _require_unique_strings(
        mapping.get("deniedActionIds"),
        "deniedActionIds",
    )
    denied_targets = _require_unique_strings(
        mapping.get("deniedTargets"),
        "deniedTargets",
    )

    return OperationPolicyRevision(
        schema_version=schema_version,
        repository=repository,
        revision_id=revision_id,
        revision=revision,
        status=status,
        created_at_utc=created_at_utc,
        expires_at_utc=expires_at_utc,
        actor=actor,
        replaces_revision_id=replaces_revision_id,
        operation_classes=operation_classes,
        denied_action_ids=denied_action_ids,
        denied_targets=denied_targets,
        _digest=digest,
    )


def _require_mapping(document: object, label: str) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise OperationPolicyError(f"{label} must be an object.")
    if not all(isinstance(key, str) for key in document):
        raise OperationPolicyError(f"{label} keys must be strings.")
    return dict(document)


def _require_exact_keys(
    mapping: Mapping[str, Any],
    expected_keys: frozenset[str],
    label: str,
) -> None:
    unknown_fields = sorted(set(mapping) - expected_keys)
    if unknown_fields:
        raise OperationPolicyError(
            f"{label} has unknown fields: {', '.join(unknown_fields)}."
        )
    missing_fields = sorted(expected_keys - set(mapping))
    if missing_fields:
        raise OperationPolicyError(
            f"{label} is missing fields: {', '.join(missing_fields)}."
        )


def _require_exact_int(mapping: Mapping[str, Any], field_name: str, expected: int) -> int:
    value = mapping.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise OperationPolicyError(f"{field_name} must be an integer.")
    if value != expected:
        raise OperationPolicyError(f"{field_name} must be {expected}.")
    return value


def _require_positive_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise OperationPolicyError(f"{field_name} must be a positive integer.")
    return value


def _require_non_negative_int(value: object, field_name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise OperationPolicyError(f"{field_name} must be a non-negative integer.")
    return value


def _require_nonempty_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise OperationPolicyError(f"{field_name} must be a nonempty string.")
    return value


def _require_repository_identity(value: object) -> str:
    repository = _require_nonempty_string(value, "repository")
    if _REPOSITORY_RE.fullmatch(repository) is None:
        raise OperationPolicyError("repository must be a repository identity.")
    return repository


def _require_actor_identity(value: object) -> str:
    actor = _require_nonempty_string(value, "actor")
    if _ACTOR_RE.fullmatch(actor) is None:
        raise OperationPolicyError("actor must be a GitHub actor identity.")
    return actor


def _require_revision_identity(value: object, revision: int) -> str:
    revision_id = _require_nonempty_string(value, "revisionId")
    if _REVISION_ID_RE.fullmatch(revision_id) is None:
        raise OperationPolicyError("revisionId must be a policy revision identity.")
    if revision_id != f"policy:{revision}":
        raise OperationPolicyError("revisionId must match revision.")
    return revision_id


def _require_replaces_revision_id(value: object, revision: int) -> str | None:
    if revision == 1:
        if value is not None:
            raise OperationPolicyError(
                "replacesRevisionId must be null for revision 1."
            )
        return None
    replaces_revision_id = _require_nonempty_string(value, "replacesRevisionId")
    if _REVISION_ID_RE.fullmatch(replaces_revision_id) is None:
        raise OperationPolicyError("replacesRevisionId must be a policy revision identity.")
    replaces_revision = int(replaces_revision_id.split(":", 1)[1])
    if replaces_revision == revision:
        raise OperationPolicyError(
            "replacesRevisionId must reference an earlier policy revision, not the current revision."
        )
    if replaces_revision > revision:
        raise OperationPolicyError(
            "replacesRevisionId must reference an earlier policy revision."
        )
    return replaces_revision_id


def _require_status(value: object) -> str:
    status = _require_nonempty_string(value, "status")
    if status not in _ALLOWED_STATUSES:
        raise OperationPolicyError("status must be active, paused, or revoked.")
    return status


def _require_operation_classes(
    value: object,
) -> dict[str, OperationClassPolicy]:
    mapping = _require_mapping(value, "operationClasses")
    _require_exact_keys(mapping, frozenset(OPERATION_CLASSES), "operationClasses")

    classes: dict[str, OperationClassPolicy] = {}
    for name in OPERATION_CLASSES:
        class_mapping = _require_mapping(mapping[name], f"operationClasses[{name}]")
        _require_exact_keys(
            class_mapping,
            _OPERATION_CLASS_FIELDS,
            f"operationClasses[{name}]",
        )
        classes[name] = OperationClassPolicy(
            enabled=_require_bool(class_mapping.get("enabled"), f"operationClasses[{name}].enabled"),
            max_per_run=_require_non_negative_int(
                class_mapping.get("maxPerRun"),
                f"operationClasses[{name}].maxPerRun",
            ),
            max_rolling_24h=_require_non_negative_int(
                class_mapping.get("maxRolling24h"),
                f"operationClasses[{name}].maxRolling24h",
            ),
        )
    return classes


def _require_total_caps(operation_classes: Mapping[str, OperationClassPolicy]) -> None:
    total_per_run = sum(policy.max_per_run for policy in operation_classes.values())
    total_rolling_24h = sum(
        policy.max_rolling_24h for policy in operation_classes.values()
    )
    if total_per_run > HARD_MAX_PER_RUN:
        raise OperationPolicyError("Total maxPerRun caps must not exceed 100 per run.")
    if total_rolling_24h > HARD_MAX_ROLLING_24H:
        raise OperationPolicyError("Total maxRolling24h caps must not exceed 300 per 24h.")


def _require_unique_strings(value: object, field_name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise OperationPolicyError(f"{field_name} must be a list.")
    seen: set[str] = set()
    items: list[str] = []
    for raw_item in value:
        item = _require_nonempty_string(raw_item, field_name)
        if item in seen:
            raise OperationPolicyError(
                f"{field_name} must not contain duplicate entries; {item!r} is already present."
            )
        seen.add(item)
        items.append(item)
    return tuple(items)


def _require_bool(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise OperationPolicyError(f"{field_name} must be a boolean.")
    return value


def _require_aware_datetime(value: object, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise OperationPolicyError(f"{field_name} must be a timezone-aware datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise OperationPolicyError(f"{field_name} must be a timezone-aware datetime.")
    return value.astimezone(UTC)


def _parse_policy_timestamp(value: object, field_name: str) -> datetime:
    try:
        return parse_aware_iso8601(value, field_name)
    except ValueError as exc:
        raise OperationPolicyError(str(exc)) from exc


def _digest_policy_document(document: Mapping[str, Any]) -> str:
    try:
        canonical = stable_json(document).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise OperationPolicyError(
            "Operation policy document must be JSON serializable for digest calculation."
        ) from exc
    return f"sha256:{hashlib.sha256(canonical).hexdigest()}"
