from __future__ import annotations

from enum import IntEnum
from typing import Literal


# Names and dependency-only steps from .github/workflows/ci.yml, tests.yml,
# and specialized-test-runner.yml. Other failures in these jobs are real leaves.
AGGREGATE_JOB_SUFFIXES = frozenset({"Final Results", "Final Test Results"})
DEPENDENCY_SUMMARY_STEPS = frozenset({
    "Fail if any dependency failed",
    "Fail if any of the dependent jobs failed",
})
EXCLUDED_WORKFLOW_PATHS = frozenset({".github/workflows/repo-pulse.lock.yml"})


def classify_job_role(
    name: str,
    failed_steps: tuple[str, ...] | None,
) -> Literal["aggregate", "ambiguous_leaf", "leaf"]:
    """Suppress only dependency fallout, never name-only or mixed-step failures."""
    if not failed_steps:
        return "ambiguous_leaf"
    # Reusable-workflow job names have the shape "tests / Final Test Results".
    suffix = " ".join(name.rsplit("/", 1)[-1].split())
    if suffix not in AGGREGATE_JOB_SUFFIXES:
        return "leaf"
    if failed_steps and all(step in DEPENDENCY_SUMMARY_STEPS for step in failed_steps):
        return "aggregate"
    return "ambiguous_leaf"


class WorkflowPriority(IntEnum):
    ROLLING_BUILD = 0
    TEST = 1
    OTHER = 2


ROLLING_BUILD_WORKFLOWS = frozenset({
    ".github/workflows/ci.yml",
})

TEST_WORKFLOWS = frozenset({
    ".github/workflows/deployment-test-command.yml",
    ".github/workflows/deployment-tests.yml",
    ".github/workflows/extension-e2e-tests.yml",
    ".github/workflows/cli-starter-validation.yml",
    ".github/workflows/polyglot-validation.yml",
    ".github/workflows/reproduce-flaky-tests.yml",
    ".github/workflows/run-tests.yml",
    ".github/workflows/scratch-cli-platform-smoke.yml",
    ".github/workflows/specialized-test-runner.yml",
    ".github/workflows/tests-daily-smoke.yml",
    ".github/workflows/tests-outerloop.yml",
    ".github/workflows/tests-quarantine.yml",
    ".github/workflows/tests.yml",
    ".github/workflows/typescript-api-compat.yml",
    ".github/workflows/typescript-sdk-tests.yml",
})


def workflow_priority(workflow_path: str) -> WorkflowPriority:
    if workflow_path in ROLLING_BUILD_WORKFLOWS:
        return WorkflowPriority.ROLLING_BUILD
    if workflow_path in TEST_WORKFLOWS:
        return WorkflowPriority.TEST
    return WorkflowPriority.OTHER
