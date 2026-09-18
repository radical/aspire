from __future__ import annotations

from collections.abc import Callable, Collection
from datetime import datetime
from pathlib import Path
import time

from .core import CiCoordinator, EffectMode, PassResult
from .reader import WorkflowReader
from .scenarios.workflow_failure import (
    WorkflowFailureScenario,
    build_judgment_prompt as _judgment_prompt,
    build_judgment_request as _judgment_request,
)
from .state import WorkflowLoopStore
from .worker import JudgmentWorkerLauncher
from .writer import WorkflowWriter


class WorkflowLoopManager(CiCoordinator):
    """Compatibility facade wiring the default workflow-failure scenario."""

    def __init__(
        self,
        *,
        state_directory: Path,
        repository: str,
        branch: str,
        store: WorkflowLoopStore,
        reader: WorkflowReader,
        launcher: JudgmentWorkerLauncher,
        writer: WorkflowWriter | None,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        workflow_ids: Collection[int] | None = None,
        capacity_limit: int = 2,
        request_count: Callable[[], int] | None = None,
    ) -> None:
        super().__init__(
            state_directory=state_directory,
            repository=repository,
            branch=branch,
            store=store,
            scenarios=(WorkflowFailureScenario(reader),),
            launcher=launcher,
            writer=writer,
            clock=clock,
            id_factory=id_factory,
            monotonic=monotonic,
            workflow_ids=workflow_ids,
            capacity_limit=capacity_limit,
            request_count=request_count,
        )


__all__ = [
    "EffectMode",
    "PassResult",
    "WorkflowLoopManager",
]
