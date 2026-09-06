from __future__ import annotations

from collections.abc import Mapping
import copy
from datetime import datetime
from typing import Any

from .timeutils import format_utc_z, parse_aware_iso8601


def unknown_meaningful_progress() -> dict[str, Any]:
    return {
        "status": "unknown",
        "at": None,
        "basis": None,
        "evidenceIds": [],
        "precision": None,
    }


def validate_meaningful_progress(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != set(unknown_meaningful_progress()):
        raise ValueError("Meaningful progress has invalid fields.")
    if value["status"] == "unknown":
        if value != unknown_meaningful_progress():
            raise ValueError("Unknown meaningful progress cannot assert an event.")
        return
    if value["status"] != "observed":
        raise ValueError("Meaningful progress status must be observed or unknown.")
    if (
        not isinstance(value["basis"], str)
        or not isinstance(value["precision"], str)
        or (value["basis"], value["precision"]) not in {
            ("human-comment", "source-event"),
            ("human-review", "source-event"),
            ("pull-request-head-change", "observed-change"),
        }
    ):
        raise ValueError("Meaningful progress basis and precision are unsupported.")
    parse_aware_iso8601(value["at"], "meaningful progress at")
    ids = value["evidenceIds"]
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(item, str) or not item for item in ids)
        or len(set(ids)) != len(ids)
    ):
        raise ValueError("Observed meaningful progress requires distinct evidence IDs.")


def attach_meaningful_progress(
    snapshot: dict[str, Any],
    previous_snapshot: Mapping[str, Any] | None,
    *,
    shepherd_author: str | None,
) -> None:
    """Derive progress from frozen source events, never GitHub updated_at.

    The configured shepherd actor may also participate manually. Automation
    markers, not that actor's login, distinguish automated activity.
    """
    observed_at = parse_aware_iso8601(snapshot["collectedAt"], "collectedAt")
    previous_snapshot = previous_snapshot or {}
    if previous_snapshot and previous_snapshot.get("repository") != snapshot["repository"]:
        raise ValueError("Meaningful progress requires the same repository.")
    previous_pull_requests = {
        item["number"]: item for item in previous_snapshot.get("pullRequests", [])
    }
    pull_request_progress = {}
    for pull_request in snapshot.get("pullRequests", []):
        evidence_id = f"pr:{pull_request['number']}"
        source = snapshot.get("evidence", {}).get(evidence_id, {})
        prior_source = previous_snapshot.get("evidence", {}).get(evidence_id, {})
        state = source.get("payload", {}).get("currentState", {})
        prior_state = prior_source.get("payload", {}).get("currentState", {})
        progress = copy.deepcopy(
            previous_pull_requests.get(pull_request["number"], {}).get(
                "meaningfulProgress", unknown_meaningful_progress(),
            )
        )
        validate_meaningful_progress(progress)
        source_at = _event_time(source.get("collectedAt"), observed_at)
        if (
            source.get("availability") == "available"
            and prior_source.get("availability") == "available"
            and source_at is not None
            and state.get("headSha")
            and prior_state.get("headSha")
            and state["headSha"] != prior_state["headSha"]
        ):
            progress = {
                "status": "observed",
                "at": format_utc_z(source_at),
                "basis": "pull-request-head-change",
                "evidenceIds": [evidence_id],
                # This dates the first observation, not the underlying commit.
                "precision": "observed-change",
            }
        for event in state.get("progressEvents", []):
            if (
                source.get("availability") != "available"
                or event.get("basis") != "human-review"
                or not isinstance(event.get("author"), str)
            ):
                continue
            at = _event_time(event.get("at"), observed_at)
            if at is not None and (
                progress["at"] is None
                or at > parse_aware_iso8601(progress["at"], "previous progress at")
            ):
                progress = {
                    "status": "observed",
                    "at": format_utc_z(at),
                    "basis": "human-review",
                    "evidenceIds": [evidence_id],
                    "precision": "source-event",
                }
        pull_request["meaningfulProgress"] = progress
        pull_request_progress[pull_request["number"]] = progress
    previous_records = {
        record["actionId"]: record
        for record in previous_snapshot.get("delegationStatus", {}).get("records", [])
    }
    for record in snapshot.get("delegationStatus", {}).get("records", []):
        candidates = []
        previous_record = previous_records.get(record["actionId"], {})
        previous_progress = previous_record.get("meaningfulProgress")
        if previous_progress is not None:
            validate_meaningful_progress(previous_progress)
        if (
            isinstance(previous_progress, Mapping)
            and previous_progress.get("status") == "observed"
        ):
            candidates.append(copy.deepcopy(dict(previous_progress)))
        previous_pulls = {
            pull["databaseId"]: pull
            for pull in previous_record.get("pullRequests", [])
        }
        for pull in record.get("pullRequests", []):
            progress = pull_request_progress.get(pull.get("number"), {})
            if progress.get("status") == "observed":
                candidates.append(copy.deepcopy(progress))
            prior_pull = previous_pulls.get(pull["databaseId"], {})
            head = pull.get("progressSource", {}).get("headSha")
            prior_head = prior_pull.get("progressSource", {}).get("headSha")
            if (
                pull.get("number")
                and pull.get("number") == prior_pull.get("number")
                and head and prior_head and head != prior_head
            ):
                candidates.append({
                    "status": "observed",
                    "at": format_utc_z(observed_at),
                    "basis": "pull-request-head-change",
                    "evidenceIds": [f"pr:{pull['number']}"],
                    "precision": "observed-change",
                })
        for evidence_id, evidence in snapshot.get("evidence", {}).items():
            if (
                not evidence_id.startswith(f"issue:{record['issueNumber']}:comment:")
                or evidence.get("kind") != "issue-comment"
                or evidence.get("availability") != "available"
            ):
                continue
            payload = evidence.get("payload", {})
            author = payload.get("author")
            if (
                payload.get("authorType") != "User"
                or not isinstance(author, str)
                or not author
                or author.casefold().endswith("[bot]")
                or payload.get("shepherdStatus", {}).get("owned") is True
                or not isinstance(payload.get("body"), str)
                or not payload["body"].strip()
                or payload["body"].lstrip().casefold().startswith("[automated]")
            ):
                continue
            # updatedAt cannot identify who edited the comment; only its
            # creation event proves interaction by the verified human author.
            at = _event_time(payload.get("createdAt"), observed_at)
            if at is not None:
                candidates.append({
                    "status": "observed",
                    "at": format_utc_z(at),
                    "basis": "human-comment",
                    "evidenceIds": [evidence_id],
                    "precision": "source-event",
                })
        record["meaningfulProgress"] = max(
            candidates,
            key=lambda item: (
                parse_aware_iso8601(item["at"], "progress at"),
                item["evidenceIds"],
            ),
            default=unknown_meaningful_progress(),
        )


def _event_time(value: object, observed_at: datetime) -> datetime | None:
    try:
        at = parse_aware_iso8601(value, "progress event time")
    except ValueError:
        return None
    return at if at <= observed_at else None
