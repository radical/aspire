from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any

from ci_shepherd.github import GitHubApiError, GitHubTextResponse, PagedInventory


@dataclass(frozen=True)
class SequenceResponse:
    values: tuple[object, ...]


@dataclass(frozen=True)
class PagedResponse:
    items: tuple[object, ...]
    complete: bool = True
    pages: int = 1
    next_endpoint: str | None = None


class EndpointClient:
    """Endpoint-keyed GitHub fake that remains deterministic under concurrency."""

    def __init__(self, responses: dict[str, object]) -> None:
        self._responses = dict(responses)
        self._sequence_indexes: dict[str, int] = {}
        self.calls: list[tuple[str, str, int | None]] = []
        self.request_count = 0
        self._lock = threading.Lock()

    def get(self, endpoint: str) -> Any:
        self._record("get", endpoint, None)
        return self._resolve(endpoint)

    def get_paged_inventory(
        self,
        endpoint: str,
        key: str | None = None,
    ) -> PagedInventory:
        self._record("get_paged_inventory", endpoint, None)
        value = self._resolve(endpoint)
        if isinstance(value, PagedResponse):
            return PagedInventory(
                items=value.items,
                pages=value.pages,
                complete=value.complete,
                next_endpoint=value.next_endpoint,
            )
        if isinstance(value, list):
            return PagedInventory(
                items=tuple(value),
                pages=1,
                complete=True,
                next_endpoint=None,
            )
        if isinstance(value, dict) and key is not None and isinstance(value.get(key), list):
            return PagedInventory(
                items=tuple(value[key]),
                pages=1,
                complete=True,
                next_endpoint=None,
            )
        raise AssertionError(f"Unexpected paged response for {endpoint}: {value!r}")

    def get_text(
        self,
        endpoint: str,
        max_bytes: int = 200_000,
    ) -> GitHubTextResponse:
        self._record("get_text", endpoint, max_bytes)
        value = self._resolve(endpoint)
        if isinstance(value, GitHubTextResponse):
            return value
        if not isinstance(value, str):
            raise AssertionError(f"Unexpected text response for {endpoint}: {value!r}")
        encoded = value.encode("utf-8")
        return GitHubTextResponse(
            text=encoded[:max_bytes].decode("utf-8", errors="replace"),
            truncated=len(encoded) > max_bytes,
            status=200,
            headers={},
        )

    def get_text_head_tail(
        self,
        endpoint: str,
        *,
        head_bytes: int,
        tail_bytes: int,
        selected_bytes: int = 0,
        line_selector: Callable[[str], bool] | None = None,
    ) -> GitHubTextResponse:
        self._record(
            "get_text_head_tail",
            endpoint,
            head_bytes + selected_bytes + tail_bytes,
        )
        value = self._resolve(endpoint)
        if isinstance(value, GitHubTextResponse):
            return value
        if not isinstance(value, str):
            raise AssertionError(
                f"Unexpected text response for {endpoint}: {value!r}"
            )
        encoded = value.encode("utf-8")
        truncated = len(encoded) > head_bytes + selected_bytes + tail_bytes
        if truncated:
            selected = b""
            if line_selector is not None:
                selected = b"".join(
                    line.encode("utf-8")
                    for line in value.splitlines(keepends=True)
                    if line_selector(line)
                )[-selected_bytes:]
            overlap = max(0, head_bytes + tail_bytes - len(encoded))
            tail = encoded[-tail_bytes + overlap:] if tail_bytes > overlap else b""
            encoded = (
                b"[... selected diagnostic lines retained ...]\n"
                + selected
                + b"\n[... response head retained ...]\n"
                + encoded[:head_bytes]
                + b"\n[... response tail retained ...]\n"
                + tail
            )
        else:
            encoded = value.encode("utf-8")
        return GitHubTextResponse(
            text=encoded.decode("utf-8", errors="replace"),
            truncated=truncated,
            status=200,
            headers={},
        )

    def set_response(self, endpoint: str, value: object) -> None:
        self._responses[endpoint] = value
        self._sequence_indexes.pop(endpoint, None)

    def _record(self, method: str, endpoint: str, max_bytes: int | None) -> None:
        with self._lock:
            self.calls.append((method, endpoint, max_bytes))
            self.request_count += 1

    def _resolve(self, endpoint: str) -> Any:
        if endpoint not in self._responses:
            raise AssertionError(f"Unexpected GitHub request: {endpoint}")
        value = self._responses[endpoint]
        if isinstance(value, SequenceResponse):
            with self._lock:
                index = self._sequence_indexes.get(endpoint, 0)
                if index >= len(value.values):
                    raise AssertionError(f"No response remaining for {endpoint}")
                self._sequence_indexes[endpoint] = index + 1
            value = value.values[index]
        if isinstance(value, Exception):
            raise value
        if isinstance(value, Callable):
            return value(endpoint)
        return value


def api_error(
    endpoint: str,
    *,
    category: str = "not-found",
    status: int = 404,
    retryable: bool = False,
    attempts: int = 1,
) -> GitHubApiError:
    return GitHubApiError(
        category=category,
        endpoint=endpoint,
        status=status,
        headers={},
        retryable=retryable,
        attempts=attempts,
        sanitized_stderr="unavailable",
    )


class StatefulWorkflowHarness:
    """Reopens the real store and reconstructs the coordinator every tick."""

    def __init__(
        self,
        *,
        state_directory: Path,
        repository: str,
        branch: str,
        reader: object,
        launcher_factory: Callable[[object], object],
        writer_factory: Callable[[object], object | None],
        clock: Callable[[], object],
        id_factory: Callable[[], str],
        workflow_ids: tuple[int, ...] | None = None,
    ) -> None:
        self.state_directory = state_directory
        self.repository = repository
        self.branch = branch
        self.reader = reader
        self.launcher_factory = launcher_factory
        self.writer_factory = writer_factory
        self.clock = clock
        self.id_factory = id_factory
        self.workflow_ids = workflow_ids

    def tick(self, mode):
        from ci_shepherd.workflow_loop.manager import WorkflowLoopManager
        from ci_shepherd.workflow_loop.state import WorkflowLoopStore

        store = WorkflowLoopStore(
            self.state_directory,
            repository=self.repository,
            branch=self.branch,
        )
        store.initialize(workflow_ids=self.workflow_ids)
        launcher = self.launcher_factory(store)
        writer = self.writer_factory(store)
        manager = WorkflowLoopManager(
            state_directory=self.state_directory,
            repository=self.repository,
            branch=self.branch,
            store=store,
            reader=self.reader,
            launcher=launcher,
            writer=writer,
            clock=self.clock,
            id_factory=self.id_factory,
            workflow_ids=self.workflow_ids,
        )
        return store, launcher, writer, manager.run_pass(mode=mode)
