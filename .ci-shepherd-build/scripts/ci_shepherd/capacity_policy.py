from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path


DEFAULT_PRODUCTION_DELEGATION_POLICY_PATH = (
    Path(__file__).resolve().parents[2]
    / "policies"
    / "production-delegation-v1.json"
)


@dataclass(frozen=True, slots=True)
class ProductionDelegationPolicy:
    repository: str
    max_actions_per_grant: int
    max_running_copilot_tasks: int
    max_copilot_starts_per_rolling_24h: int
    max_open_delegated_prs: int
    max_repository_running_copilot_tasks: int
    digest: str


def load_production_delegation_policy(
    path: Path = DEFAULT_PRODUCTION_DELEGATION_POLICY_PATH,
) -> ProductionDelegationPolicy:
    try:
        payload = path.read_bytes()
        document = json.loads(payload)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Unable to read production delegation policy: {path}") from exc
    if not isinstance(document, dict) or set(document) != {
        "schemaVersion",
        "repository",
        "maxActionsPerGrant",
        "capacity",
    }:
        raise ValueError("Production delegation policy has unsupported fields.")
    if document.get("schemaVersion") != 1:
        raise ValueError("Production delegation policy schemaVersion must equal 1.")
    repository = document.get("repository")
    if repository != "microsoft/aspire":
        raise ValueError(
            "Production delegation policy repository must be microsoft/aspire."
        )
    capacity = document.get("capacity")
    if not isinstance(capacity, dict) or set(capacity) != {
        "maxRunningCopilotTasks",
        "maxCopilotStartsPerRolling24h",
        "maxOpenDelegatedPullRequests",
        "maxRepositoryRunningCopilotTasks",
    }:
        raise ValueError("Production delegation policy capacity is invalid.")

    def positive_int(container: dict[str, object], key: str) -> int:
        value = container.get(key)
        if (
            not isinstance(value, int)
            or isinstance(value, bool)
            or value <= 0
        ):
            raise ValueError(f"Production delegation policy {key} must be positive.")
        return value

    return ProductionDelegationPolicy(
        repository=repository,
        max_actions_per_grant=positive_int(document, "maxActionsPerGrant"),
        max_running_copilot_tasks=positive_int(
            capacity,
            "maxRunningCopilotTasks",
        ),
        max_copilot_starts_per_rolling_24h=positive_int(
            capacity,
            "maxCopilotStartsPerRolling24h",
        ),
        max_open_delegated_prs=positive_int(
            capacity,
            "maxOpenDelegatedPullRequests",
        ),
        max_repository_running_copilot_tasks=positive_int(
            capacity,
            "maxRepositoryRunningCopilotTasks",
        ),
        digest=f"sha256:{hashlib.sha256(payload).hexdigest()}",
    )
