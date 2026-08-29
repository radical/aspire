from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable

from .timeutils import parse_aware_iso8601


class StageDeadlineExceeded(TimeoutError):
    """Raised when a progress stage exceeds its configured wall-clock budget."""


class ProgressTracker:
    def __init__(
        self,
        output_dir: Path,
        *,
        now: Callable[[], datetime] | None = None,
        heartbeat_interval_seconds: float = 30,
        stage_deadline_seconds: float = 900,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("heartbeat_interval_seconds must be positive.")
        if stage_deadline_seconds <= 0:
            raise ValueError("stage_deadline_seconds must be positive.")
        self._output_dir = output_dir
        self._path = output_dir / "progress.json"
        self._now = now or (lambda: datetime.now(UTC))
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._stage_deadline_seconds = stage_deadline_seconds

    def update(
        self,
        stage: str,
        event_status: str,
        *,
        message: str | None = None,
        completed_items: int | None = None,
        total_items: int | None = None,
        error: str | None = None,
    ) -> None:
        self._output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._output_dir.chmod(0o700)
        timestamp = self._timestamp()
        document = self._load(timestamp)
        if event_status == "completed":
            self._enforce_deadline(document, stage, timestamp)
        event: dict[str, Any] = {
            "stage": stage,
            "status": event_status,
            "at": timestamp,
        }
        if message is not None:
            event["message"] = message
        if completed_items is not None:
            event["completedItems"] = completed_items
        if total_items is not None:
            event["totalItems"] = total_items
        if error is not None:
            event["error"] = error
        document["events"].append(event)
        document["updatedAt"] = timestamp
        pipeline_completed = (
            stage in {"collection", "pipeline"} and event_status == "completed"
        )
        document["currentStage"] = "complete" if pipeline_completed else stage
        document["status"] = (
            "failed"
            if event_status == "failed"
            else "complete"
            if pipeline_completed
            else "running"
        )
        for key, value in (
            ("message", message),
            ("completedItems", completed_items),
            ("totalItems", total_items),
            ("error", error),
        ):
            if value is not None:
                document[key] = value
        self._write(document)

    def heartbeat(
        self,
        stage: str,
        *,
        message: str | None = None,
    ) -> bool:
        """Record bounded progress without writing more often than configured."""

        timestamp = self._timestamp()
        instant = parse_aware_iso8601(timestamp, "progress heartbeat")
        document = self._load(timestamp)
        self._enforce_deadline(document, stage, timestamp)
        stage_events = [
            event
            for event in document["events"]
            if isinstance(event, dict) and event.get("stage") == stage
        ]
        if not any(event.get("status") == "started" for event in stage_events):
            raise ValueError(f"Progress stage has not started: {stage}")

        if stage_events:
            latest_at = parse_aware_iso8601(
                stage_events[-1].get("at"),
                f"{stage} latest progress",
            )
            if (
                instant - latest_at
            ).total_seconds() < self._heartbeat_interval_seconds:
                return False

        event: dict[str, Any] = {
            "stage": stage,
            "status": "progress",
            "at": timestamp,
        }
        if message is not None:
            event["message"] = message
        document["events"].append(event)
        document.update(
            {
                "updatedAt": timestamp,
                "currentStage": stage,
                "status": "running",
            }
        )
        if message is not None:
            document["message"] = message
        self._write(document)
        return True

    def _enforce_deadline(
        self,
        document: dict[str, Any],
        stage: str,
        timestamp: str,
    ) -> None:
        if stage in {"collection", "pipeline"}:
            return
        started = next(
            (
                event
                for event in reversed(document["events"])
                if isinstance(event, dict)
                and event.get("stage") == stage
                and event.get("status") == "started"
            ),
            None,
        )
        if started is None:
            return
        instant = parse_aware_iso8601(timestamp, "progress timestamp")
        started_at = parse_aware_iso8601(
            started.get("at"),
            f"{stage} startedAt",
        )
        if (instant - started_at).total_seconds() <= self._stage_deadline_seconds:
            return

        deadline = f"{self._stage_deadline_seconds:g}"
        error = "stage-deadline-exceeded"
        message = f"{stage} exceeded its {deadline}-second deadline."
        document["events"].append(
            {
                "stage": stage,
                "status": "failed",
                "at": timestamp,
                "message": message,
                "error": error,
            }
        )
        document.update(
            {
                "updatedAt": timestamp,
                "currentStage": stage,
                "status": "failed",
                "message": message,
                "error": error,
            }
        )
        self._write(document)
        raise StageDeadlineExceeded(message)

    def _load(self, timestamp: str) -> dict[str, Any]:
        if not self._path.exists():
            return {
                "schemaVersion": 1,
                "status": "running",
                "currentStage": "starting",
                "startedAt": timestamp,
                "updatedAt": timestamp,
                "events": [],
            }
        document = json.loads(self._path.read_text(encoding="utf-8"))
        if not isinstance(document, dict) or not isinstance(document.get("events"), list):
            raise ValueError(f"Invalid progress document: {self._path}")
        return document

    def _write(self, document: dict[str, Any]) -> None:
        content = json.dumps(document, indent=2, sort_keys=True) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".progress-",
            suffix=".tmp",
            dir=self._output_dir,
        )
        temporary_path = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, self._path)
            self._path.chmod(0o600)
        finally:
            if temporary_path.exists():
                temporary_path.unlink()

    def _timestamp(self) -> str:
        return self._now().astimezone(UTC).isoformat().replace("+00:00", "Z")
