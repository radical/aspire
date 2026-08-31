from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping


class RepositoryPolicyError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class RetryTestResultsPolicy:
    aggregate_job_suffixes: tuple[str, ...]
    artifact_names: frozenset[str]
    trx_path_pattern: str
    job_name_pattern: str
    trusted_events: frozenset[str]
    require_head_repository_match: bool

    def matches_aggregate_job(self, job_name: str) -> bool:
        return any(
            job_name == suffix or job_name.endswith(f" / {suffix}")
            for suffix in self.aggregate_job_suffixes
        )

    def matches_artifact(self, artifact_name: str) -> bool:
        return artifact_name in self.artifact_names

    def identify_trx(self, path: str) -> tuple[str, str] | None:
        match = re.fullmatch(self.trx_path_pattern, path)
        if match is None:
            return None
        return match.group("lane"), match.group("os")

    def matches_test_job(
        self,
        job_name: str,
        *,
        lane: str,
        os_name: str,
    ) -> bool:
        match = re.fullmatch(self.job_name_pattern, job_name)
        return (
            match is not None
            and match.group("lane") == lane
            and match.group("os") == os_name
        )

    def trusts_run(
        self,
        *,
        event: str,
        head_repository: str,
        target_repository: str,
    ) -> bool:
        return (
            event.casefold() in self.trusted_events
            and (
                not self.require_head_repository_match
                or head_repository.casefold() == target_repository.casefold()
            )
        )


@dataclass(frozen=True, slots=True)
class QuarantinePullRequestPolicy:
    base_ref: str
    allowed_head_repositories: frozenset[str]
    required_approving_reviews: int

    def allows_head_repository(self, repository: str) -> bool:
        return repository.casefold() in self.allowed_head_repositories


@dataclass(frozen=True, slots=True)
class RepositoryPolicy:
    policy_version: str
    repositories: frozenset[str]
    retry_test_results: RetryTestResultsPolicy
    quarantine_pull_request: QuarantinePullRequestPolicy

    def supports_repository(self, repository: str) -> bool:
        return repository.casefold() in self.repositories

    def as_public_dict(self) -> dict[str, Any]:
        return {
            "schemaVersion": _SCHEMA_VERSION,
            "policyVersion": self.policy_version,
            "repositories": sorted(self.repositories),
            "retryTestResults": {
                "aggregateJobSuffixes": list(
                    self.retry_test_results.aggregate_job_suffixes
                ),
                "artifactNames": sorted(
                    self.retry_test_results.artifact_names
                ),
                "trxPathPattern": self.retry_test_results.trx_path_pattern,
                "jobNamePattern": self.retry_test_results.job_name_pattern,
                "trustedEvents": sorted(self.retry_test_results.trusted_events),
                "requireHeadRepositoryMatch": (
                    self.retry_test_results.require_head_repository_match
                ),
            },
            "quarantinePullRequest": {
                "baseRef": self.quarantine_pull_request.base_ref,
                "allowedHeadRepositories": sorted(
                    self.quarantine_pull_request.allowed_head_repositories
                ),
                "requiredApprovingReviews": (
                    self.quarantine_pull_request.required_approving_reviews
                ),
            },
        }

    @property
    def digest(self) -> str:
        canonical = json.dumps(
            self.as_public_dict(),
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return f"sha256:{hashlib.sha256(canonical).hexdigest()}"


_SCHEMA_VERSION = 1
_POLICY_FIELDS = frozenset(
    {
        "schemaVersion",
        "policyVersion",
        "repositories",
        "retryTestResults",
        "quarantinePullRequest",
    }
)
_QUARANTINE_PULL_REQUEST_FIELDS = frozenset(
    {
        "baseRef",
        "allowedHeadRepositories",
        "requiredApprovingReviews",
    }
)
_RETRY_TEST_RESULTS_FIELDS = frozenset(
    {
        "aggregateJobSuffixes",
        "artifactNames",
        "trxPathPattern",
        "jobNamePattern",
        "trustedEvents",
        "requireHeadRepositoryMatch",
    }
)


def load_repository_policy(path: Path) -> RepositoryPolicy:
    if path.is_symlink():
        raise RepositoryPolicyError(
            f"Repository policy file {path} must not be a symlink."
        )
    try:
        file_stat = path.stat()
        raw_bytes = path.read_bytes()
    except OSError as exc:
        raise RepositoryPolicyError(
            f"Unable to read repository policy file {path}."
        ) from exc
    if not stat.S_ISREG(file_stat.st_mode):
        raise RepositoryPolicyError(
            f"Repository policy file {path} must be a regular file."
        )
    if hasattr(os, "geteuid") and file_stat.st_uid != os.geteuid():
        raise RepositoryPolicyError(
            f"Repository policy file {path} must be owned by the current user."
        )
    if file_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise RepositoryPolicyError(
            f"Repository policy file {path} must not be writable by other users."
        )
    try:
        document = json.loads(
            raw_bytes.decode("utf-8"),
            object_pairs_hook=lambda pairs: _strict_object_pairs_hook(path, pairs),
        )
    except UnicodeDecodeError as exc:
        raise RepositoryPolicyError(
            f"Repository policy file {path} contains invalid UTF-8."
        ) from exc
    except json.JSONDecodeError as exc:
        raise RepositoryPolicyError(
            f"Repository policy file {path} is not valid JSON."
        ) from exc
    return load_repository_policy_document(document)


def load_repository_policy_document(document: object) -> RepositoryPolicy:
    mapping = _require_mapping(document, "Repository policy")
    _require_exact_keys(mapping, _POLICY_FIELDS, "Repository policy")
    schema_version = mapping.get("schemaVersion")
    if schema_version != _SCHEMA_VERSION or isinstance(schema_version, bool):
        raise RepositoryPolicyError(
            f"Repository policy schemaVersion must be {_SCHEMA_VERSION}."
        )

    retry_mapping = _require_mapping(
        mapping.get("retryTestResults"),
        "retryTestResults",
    )
    _require_exact_keys(
        retry_mapping,
        _RETRY_TEST_RESULTS_FIELDS,
        "retryTestResults",
    )
    pull_request_mapping = _require_mapping(
        mapping.get("quarantinePullRequest"),
        "quarantinePullRequest",
    )
    _require_exact_keys(
        pull_request_mapping,
        _QUARANTINE_PULL_REQUEST_FIELDS,
        "quarantinePullRequest",
    )
    return RepositoryPolicy(
        policy_version=_require_nonempty_string(mapping, "policyVersion"),
        repositories=frozenset(
            value.casefold()
            for value in _require_unique_strings(mapping, "repositories")
        ),
        retry_test_results=RetryTestResultsPolicy(
            aggregate_job_suffixes=tuple(
                _require_unique_strings(
                    retry_mapping,
                    "aggregateJobSuffixes",
                )
            ),
            artifact_names=frozenset(
                _require_unique_strings(retry_mapping, "artifactNames")
            ),
            trx_path_pattern=_require_pattern(
                retry_mapping,
                "trxPathPattern",
                required_groups={"lane", "os"},
            ),
            job_name_pattern=_require_pattern(
                retry_mapping,
                "jobNamePattern",
                required_groups={"lane", "os"},
            ),
            trusted_events=frozenset(
                value.casefold()
                for value in _require_unique_strings(
                    retry_mapping,
                    "trustedEvents",
                )
            ),
            require_head_repository_match=_require_bool(
                retry_mapping,
                "requireHeadRepositoryMatch",
            ),
        ),
        quarantine_pull_request=QuarantinePullRequestPolicy(
            base_ref=_require_nonempty_string(
                pull_request_mapping,
                "baseRef",
            ),
            allowed_head_repositories=frozenset(
                value.casefold()
                for value in _require_unique_strings(
                    pull_request_mapping,
                    "allowedHeadRepositories",
                )
            ),
            required_approving_reviews=_require_bounded_integer(
                pull_request_mapping,
                "requiredApprovingReviews",
                minimum=1,
                maximum=10,
            ),
        ),
    )


def load_embedded_repository_policy(
    document: object,
    repository: str,
) -> RepositoryPolicy:
    mapping = _require_mapping(document, "Embedded repository policy")
    _require_exact_keys(
        mapping,
        _POLICY_FIELDS | {"digest"},
        "Embedded repository policy",
    )
    digest = mapping.pop("digest")
    policy = load_repository_policy_document(mapping)
    if digest != policy.digest:
        raise RepositoryPolicyError(
            "repositoryPolicy digest does not match its content."
        )
    if not policy.supports_repository(repository):
        raise RepositoryPolicyError(
            "Embedded repository policy does not support the target repository."
        )
    return policy


def _require_mapping(value: object, label: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise RepositoryPolicyError(f"{label} must be an object.")
    if not all(isinstance(key, str) for key in value):
        raise RepositoryPolicyError(f"{label} keys must be strings.")
    return dict(value)


def _require_exact_keys(
    mapping: Mapping[str, Any],
    expected: frozenset[str],
    label: str,
) -> None:
    unknown = sorted(set(mapping) - expected)
    if unknown:
        raise RepositoryPolicyError(
            f"{label} has unknown fields: {', '.join(unknown)}."
        )
    missing = sorted(expected - set(mapping))
    if missing:
        raise RepositoryPolicyError(
            f"{label} is missing fields: {', '.join(missing)}."
        )


def _require_nonempty_string(
    mapping: Mapping[str, Any],
    field_name: str,
) -> str:
    value = mapping.get(field_name)
    if not isinstance(value, str) or not value:
        raise RepositoryPolicyError(f"{field_name} must be a nonempty string.")
    return value


def _require_unique_strings(
    mapping: Mapping[str, Any],
    field_name: str,
) -> list[str]:
    values = mapping.get(field_name)
    if (
        not isinstance(values, list)
        or not values
        or not all(isinstance(value, str) and value for value in values)
    ):
        raise RepositoryPolicyError(
            f"{field_name} must be a nonempty list of nonempty strings."
        )
    if len(values) != len(set(values)):
        raise RepositoryPolicyError(f"{field_name} must not contain duplicates.")
    return values


def _require_bounded_integer(
    mapping: Mapping[str, Any],
    field_name: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = mapping.get(field_name)
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or value > maximum
    ):
        raise RepositoryPolicyError(
            f"{field_name} must be an integer from {minimum} through {maximum}."
        )
    return value


def _require_bool(
    mapping: Mapping[str, Any],
    field_name: str,
) -> bool:
    value = mapping.get(field_name)
    if not isinstance(value, bool):
        raise RepositoryPolicyError(f"{field_name} must be a boolean.")
    return value


def _require_pattern(
    mapping: Mapping[str, Any],
    field_name: str,
    *,
    required_groups: set[str],
) -> str:
    value = _require_nonempty_string(mapping, field_name)
    try:
        pattern = re.compile(value)
    except re.error as error:
        raise RepositoryPolicyError(f"{field_name} must be a valid regular expression.") from error
    missing_groups = required_groups - set(pattern.groupindex)
    if missing_groups:
        raise RepositoryPolicyError(
            f"{field_name} must define named groups: "
            + ", ".join(sorted(required_groups))
            + "."
        )
    return value


def _strict_object_pairs_hook(
    path: Path,
    pairs: list[tuple[str, Any]],
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key, value in pairs:
        if key in mapping:
            raise RepositoryPolicyError(
                f"Repository policy file {path} contains duplicate JSON key: {key}."
            )
        mapping[key] = value
    return mapping
