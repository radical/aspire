from __future__ import annotations

from enum import IntEnum


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
