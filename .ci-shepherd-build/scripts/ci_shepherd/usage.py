"""Bounded usage projection; the supplied run roster remains the session authority.

Sources: Copilot CLI ``--usage-output-file`` is a final export option, but its
JSON format is not assumed here. This adapter reads the public SDK event schema:
https://github.com/github/copilot-sdk/blob/main/nodejs/src/generated/session-events.ts
``assistant.usage`` has per-call tokens and ``copilotUsage.totalNanoAiu``;
``session.shutdown`` has cumulative modelMetrics and totalNanoAiu;
``session.usage_checkpoint`` has cumulative cost, not cumulative token counts.
Native nano-AI units are preserved, not relabeled as AI credits or dollars.
"""
from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
from datetime import datetime
import json
import math
import os
from pathlib import Path
from typing import Any


TOKEN_METRICS = ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens")
METRICS = (*TOKEN_METRICS, "totalNanoAiu", "premiumRequests", "aiCredits")


def _time(value: object) -> datetime:
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Usage timestamps must include a timezone.")
    return result


def _numeric(value: object) -> int | float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
        return value
    return None


def _sum_known(values: Iterable[object]) -> int | float | None:
    numbers = [_numeric(value) for value in values]
    return sum(numbers) if numbers and all(value is not None for value in numbers) else None


def _cumulative(event: Mapping[str, Any]) -> dict[str, int | float | None]:
    data = event.get("data", {})
    metrics = {name: None for name in METRICS}
    metrics["totalNanoAiu"] = _numeric(data.get("totalNanoAiu"))
    metrics["premiumRequests"] = _numeric(data.get("totalPremiumRequests"))
    if event.get("type") == "session.shutdown":
        models = data.get("modelMetrics", {})
        for name in TOKEN_METRICS:
            metrics[name] = _sum_known(model.get("usage", {}).get(name) for model in models.values())
    return metrics


def _session_usage(
    entry: Mapping[str, Any],
    source: Iterable[Mapping[str, Any]],
    *,
    started_at: datetime,
    as_of: datetime,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "sessionId": entry.get("sessionId"),
        "runtimeSessionId": entry.get("runtimeSessionId"),
        "role": entry.get("role", "unknown"),
        "status": "unknown",
        "metrics": {name: None for name in METRICS},
        "metricAsOf": {},
        "limitations": [],
    }
    limitations = result["limitations"]
    if entry.get("role") == "remote-copilot-agent":
        limitations.append("remote-agent-cost-unavailable")
        return result
    runtime_id = entry.get("runtimeSessionId")
    if not runtime_id:
        limitations.append("runtime-session-binding-missing")
        return result

    # Persisted files can replay IDs; copied conflicting records are not evidence.
    unique: dict[str, Mapping[str, Any]] = {}
    for row in source:
        if not isinstance(row, Mapping) or not isinstance(row.get("id"), str):
            continue
        try:
            if _time(row.get("timestamp")) > as_of:
                continue
        except (TypeError, ValueError):
            limitations.append("invalid-event-timestamp")
            continue
        identity = row["id"]
        if identity in unique and unique[identity] != row:
            limitations.append("conflicting-event-id")
            return result
        unique[identity] = row
    events = sorted(unique.values(), key=lambda row: _time(row["timestamp"]))
    if any(row.get("type") == "session.start" and row.get("data", {}).get("sessionId") != runtime_id for row in events):
        limitations.append("runtime-session-binding-mismatch")
        return result
    cumulative_events = [row for row in events if row.get("type") in ("session.shutdown", "session.usage_checkpoint")]
    if cumulative_events:
        # A pre-run capture can bind this exact event as the next run's baseline.
        # Its timestamp remains visible because a checkpoint may lag wall time.
        result["latestCumulativeEventId"] = cumulative_events[-1]["id"]
        result["latestCumulativeAsOf"] = cumulative_events[-1]["timestamp"]

    scope = entry.get("usageScope")
    if scope == "dedicated":
        starts = [row for row in events if row.get("type") == "session.start"]
        if not starts or _time(starts[0]["timestamp"]) < started_at:
            limitations.append("dedicated-session-start-not-proven")
            return result
        baseline = {name: 0 for name in METRICS}
        baseline_time = started_at
    elif scope == "resumed":
        boundary = unique.get(str(entry.get("baselineEventId")))
        if (boundary is None or boundary.get("type") not in ("session.shutdown", "session.usage_checkpoint")
                or _time(boundary["timestamp"]) > started_at):
            limitations.append("run-baseline-missing")
            return result
        baseline = _cumulative(boundary)
        baseline_time = _time(boundary["timestamp"])
        result["baselineEventId"] = boundary["id"]
        result["baselineAsOf"] = boundary["timestamp"]
        if baseline_time < started_at:
            limitations.append("Baseline predates run start; totals cover the explicitly bound event interval, not an inferred wall-clock interval.")
    else:
        limitations.append("session-usage-scope-missing")
        return result

    totals = [row for row in events
              if row.get("type") in ("session.shutdown", "session.usage_checkpoint")
              and _time(row["timestamp"]) > baseline_time]
    previous_totals = dict(baseline)
    reset_metrics: set[str] = set()
    for row in totals:
        for name, value in _cumulative(row).items():
            initial = baseline.get(name)
            if value is None or initial is None or name in reset_metrics:
                continue
            if value < previous_totals[name]:
                result["metrics"][name] = None
                result["metricAsOf"].pop(name, None)
                limitations.append(f"{name}: cumulative-counter-decreased")
                reset_metrics.add(name)
                continue
            previous_totals[name] = value
            result["metrics"][name] = value - initial
            result["metricAsOf"][name] = row["timestamp"]

    # assistant.usage is ephemeral in some clients. Summing a persisted subset
    # would undercount silently, so only an explicitly complete SDK capture may
    # substitute per-call accounting for cumulative checkpoints.
    if entry.get("eventCoverage") == "complete":
        calls: dict[str, Mapping[str, Any]] = {}
        conflict = False
        for row in events:
            if row.get("type") != "assistant.usage" or not started_at <= _time(row["timestamp"]) <= as_of:
                continue
            data = row.get("data", {})
            identity = str(data.get("apiCallId") or row["id"])
            if identity in calls and calls[identity].get("data") != data:
                conflict = True
                break
            calls[identity] = row
        if conflict:
            limitations.append("conflicting-api-call-id")
        elif calls:
            for name in (*TOKEN_METRICS, "totalNanoAiu"):
                values = [
                    row.get("data", {}).get("copilotUsage", {}).get(name)
                    if name == "totalNanoAiu" else row.get("data", {}).get(name)
                    for row in calls.values()
                ]
                result["metrics"][name] = _sum_known(values)
                if result["metrics"][name] is not None:
                    result["metricAsOf"][name] = max(calls.values(), key=lambda row: _time(row["timestamp"]))["timestamp"]
            result["callCount"] = len(calls)

    if any(value is not None for value in result["metrics"].values()):
        result["status"] = "partial"
    final_shutdown = bool(events) and events[-1].get("type") == "session.shutdown"
    if final_shutdown and all(result["metrics"][name] is not None for name in (*TOKEN_METRICS, "totalNanoAiu")):
        result["status"] = "native-metrics-covered"
    limitations.append("AI credit conversion unavailable; totalNanoAiu retains the provider's documented nano-AI unit.")
    if not final_shutdown:
        limitations.append("Session still running or shutdown not observed; refresh after completion.")
    return result


def collect_run_usage(
    roster: Mapping[str, Any],
    sources: Mapping[str, Iterable[Mapping[str, Any]]],
    *,
    as_of: str,
) -> dict[str, Any]:
    """Join explicit session bindings and usage evidence, without scanning sessions.

    The existing run manifest may carry ``sessions`` directly. Entries bind
    sessionId to runtimeSessionId and usageScope (dedicated/resumed). A resumed
    session requires the exact pre-run cumulative baselineEventId. Mark reused
    results ``reused: true``. In-process subagents already included in a parent
    source must carry ``includedInRuntimeSessionId`` to prevent double counting.
    """
    if not roster.get("runId"):
        raise ValueError("A runId is required for usage accounting.")
    start, end = _time(roster.get("startedAt")), _time(as_of)
    if end < start:
        raise ValueError("Usage as-of time precedes the run.")
    entries = roster.get("sessions")
    if not isinstance(entries, list):
        raise ValueError("Usage requires an explicit sessions roster.")
    unique: dict[str, Mapping[str, Any]] = {}
    excluded = {"reused": 0, "skipped": 0, "included": 0}
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            raise ValueError("Session roster entries must be objects.")
        reason = ("reused" if entry.get("reused") is True else
                  "skipped" if entry.get("skipped") is True else
                  "included" if entry.get("includedInRuntimeSessionId") else None)
        if reason is not None:
            excluded[reason] += 1
            continue
        key = str(entry.get("runtimeSessionId") or entry.get("sessionId") or f"unbound:{index}")
        if key in unique and unique[key] != entry:
            raise ValueError(f"Conflicting session binding: {key}")
        unique[key] = entry
    sessions = [
        _session_usage(entry, sources.get(str(entry.get("runtimeSessionId")), ()), started_at=start, as_of=end)
        for entry in unique.values()
    ]
    metrics = {}
    for name in METRICS:
        known = [row["metrics"][name] for row in sessions if row["metrics"][name] is not None]
        times = [row["metricAsOf"][name] for row in sessions if name in row["metricAsOf"]]
        metrics[name] = {
            "value": sum(known) if known else None,
            "coveredSessions": len(known),
            "earliestAsOf": min(times, key=_time) if times else None,
            "latestAsOf": max(times, key=_time) if times else None,
        }
    return {
        "schemaVersion": 1,
        "runId": roster["runId"],
        "asOf": as_of,
        "sessionCount": len(sessions),
        "excludedReusedSessions": excluded["reused"],
        "excludedSkippedSessions": excluded["skipped"],
        "excludedIncludedSessions": excluded["included"],
        "coverage": "Partial: known subtotals only; missing session metrics are unknown, not zero.",
        "metrics": metrics,
        "sessions": sessions,
        "limitations": [
            "Coordinator and separately billed workers need explicit runtime bindings; no session discovery is performed.",
            "Report generation cannot include its own future tokens; refresh at an explicit final boundary.",
            "Input/output/cache tokens, native nano-AI units, legacy premium requests, and AI credits are distinct.",
            *dict.fromkeys(note for row in sessions for note in row["limitations"]),
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Project run usage from explicit session event-file bindings, without network calls.")
    parser.add_argument("--roster", required=True, type=Path)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    roster = json.loads(args.roster.read_text(encoding="utf-8"))
    sources: dict[str, list[Mapping[str, Any]]] = {}
    errors: list[str] = []
    for entry in roster.get("sessions", []):
        runtime_id, path = entry.get("runtimeSessionId"), entry.get("eventsPath")
        if not runtime_id or not path or entry.get("reused") or entry.get("skipped") or entry.get("includedInRuntimeSessionId"):
            continue
        event_path = Path(path)
        if not event_path.is_absolute():
            event_path = args.roster.parent / event_path
        try:
            records = []
            with event_path.open(encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    if record.get("type") in ("session.start", "session.shutdown", "session.usage_checkpoint", "assistant.usage"):
                        records.append(record)
            sources[runtime_id] = records
        except (OSError, ValueError):
            errors.append(f"{runtime_id}: usage-source-unreadable")
    result = collect_run_usage(roster, sources, as_of=args.as_of)
    result["limitations"].extend(errors)
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
        handle.write("\n")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
