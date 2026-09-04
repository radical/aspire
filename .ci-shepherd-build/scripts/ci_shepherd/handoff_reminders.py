from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Mapping

from ci_shepherd.repository_policy import HandoffReminderPolicy


def derive_handoff_reminders(
    records: list[dict[str, object]],
    events: list[dict[str, object]],
    policy: HandoffReminderPolicy,
) -> list[dict[str, object]]:
    """Attach transactional reminder state to active human handoffs."""
    terminals = [
        event
        for event in events
        if event.get("eventType") == "terminal"
        and event.get("outcome") == "executed"
    ]
    for record in records:
        if (
            record.get("lifecycle") != "handoff_required"
            or record.get("requiresHuman") is not True
            or record.get("issueOpen") is False
            or record.get("copilotAssigned") is False
        ):
            continue
        action_id = record.get("actionId")
        handoff_started_at = record.get("handoffStartedAt")
        if not isinstance(action_id, str) or not isinstance(handoff_started_at, str):
            continue
        episode_id = f"{action_id}:handoff"
        issue_number = record.get("issueNumber")
        if (
            not isinstance(issue_number, int)
            or isinstance(issue_number, bool)
            or issue_number < 1
        ):
            continue
        delivered = _delivered_ordinals(terminals, episode_id, issue_number)
        ordinal = max(delivered, default=0) + 1
        last_delivery = _latest_delivery(terminals, episode_id, issue_number)
        human_takeover = (
            record.get("humanAssigned") is True
            or any(
                isinstance(pull_request, Mapping)
                and pull_request.get("state") == "open"
                and pull_request.get("humanAuthored") is True
                for pull_request in record.get("pullRequests", [])
                if isinstance(record.get("pullRequests"), list)
            )
        )

        if human_takeover:
            base = last_delivery or _parse_timestamp(handoff_started_at)
            wakeup = _wakeup(
                base + policy.stale_progress_interval,
                "human-stale-progress",
            )
            state = "human-owned"
        elif len(delivered) >= policy.maximum:
            base = last_delivery or _parse_timestamp(handoff_started_at)
            wakeup = _wakeup(base, "operator-escalation")
            state = "operator-escalation"
        else:
            base = last_delivery or _parse_timestamp(handoff_started_at)
            wakeup = _wakeup(
                base + policy.interval if last_delivery is not None else base,
                "escalation-reminder",
            )
            state = "pending"

        record["handoffReminder"] = {
            "episodeId": episode_id,
            "ordinal": ordinal,
            "state": state,
            "nextWakeup": wakeup,
        }
        record["nextWakeup"] = wakeup
    return records


def reminder_action_identity(record: Mapping[str, object]) -> tuple[str, int] | None:
    reminder = record.get("handoffReminder")
    if not isinstance(reminder, Mapping) or reminder.get("state") != "pending":
        return None
    episode_id = reminder.get("episodeId")
    ordinal = reminder.get("ordinal")
    if (
        not isinstance(episode_id, str)
        or not episode_id
        or not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or ordinal < 1
    ):
        return None
    return episode_id, ordinal


def _delivered_ordinals(
    terminals: list[dict[str, object]],
    episode_id: str,
    issue_number: int,
) -> set[int]:
    prefix = f":ping-human-comment:{episode_id}:reminder-"
    delivered: set[int] = set()
    for event in terminals:
        if not _is_exact_comment_effect(event, issue_number):
            continue
        action_id = event.get("actionId")
        if not isinstance(action_id, str) or prefix not in action_id:
            continue
        suffix = action_id.rsplit(prefix, 1)[1]
        if suffix.isdigit() and int(suffix) > 0:
            delivered.add(int(suffix))
    return delivered


def _latest_delivery(
    terminals: list[dict[str, object]],
    episode_id: str,
    issue_number: int,
) -> datetime | None:
    prefix = f":ping-human-comment:{episode_id}:reminder-"
    instants = [
        _parse_timestamp(event["recordedAt"])
        for event in terminals
        if isinstance(event.get("actionId"), str)
        and prefix in str(event["actionId"])
        and isinstance(event.get("recordedAt"), str)
        and _is_exact_comment_effect(event, issue_number)
    ]
    return max(instants, default=None)


def _is_exact_comment_effect(
    event: Mapping[str, object],
    issue_number: int,
) -> bool:
    target = event.get("target")
    return (
        event.get("operation") in {"create-comment", "edit-comment"}
        and event.get("idempotencyKey") == f"issue:{issue_number}:status"
        and isinstance(target, Mapping)
        and target.get("kind") == "issue"
        and target.get("number") == issue_number
    )


def _parse_timestamp(value: str) -> datetime:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if instant.tzinfo is None:
        raise ValueError("Reminder timestamps must include a UTC offset.")
    return instant.astimezone(UTC)


def _wakeup(evaluate_at: datetime, reason: str) -> dict[str, str]:
    return {
        "reason": reason,
        "evaluateAt": evaluate_at.astimezone(UTC)
        .isoformat()
        .replace("+00:00", "Z"),
    }
