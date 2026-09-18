from __future__ import annotations

import unittest

from ci_shepherd.workflow_loop.scenarios.workflow_policy import (
    WorkflowPriority,
    workflow_priority,
)


class WorkflowPriorityTests(unittest.TestCase):
    def test_exact_repository_policy_orders_rolling_tests_then_other(self) -> None:
        self.assertIs(
            WorkflowPriority.ROLLING_BUILD,
            workflow_priority(".github/workflows/ci.yml"),
        )
        for path in (
            ".github/workflows/tests.yml",
            ".github/workflows/tests-daily-smoke.yml",
            ".github/workflows/tests-outerloop.yml",
            ".github/workflows/tests-quarantine.yml",
            ".github/workflows/deployment-tests.yml",
            ".github/workflows/extension-e2e-tests.yml",
            ".github/workflows/cli-starter-validation.yml",
            ".github/workflows/polyglot-validation.yml",
            ".github/workflows/reproduce-flaky-tests.yml",
            ".github/workflows/typescript-api-compat.yml",
            ".github/workflows/typescript-sdk-tests.yml",
        ):
            with self.subTest(path=path):
                self.assertIs(
                    WorkflowPriority.TEST,
                    workflow_priority(path),
                )
        self.assertIs(
            WorkflowPriority.OTHER,
            workflow_priority(".github/workflows/pr-docs-check.lock.yml"),
        )
        for nonexistent in (
            ".github/workflows/test-scenario.yml",
            ".github/workflows/tests-runner.yml",
        ):
            self.assertIs(
                WorkflowPriority.OTHER,
                workflow_priority(nonexistent),
            )
        self.assertIs(
            WorkflowPriority.OTHER,
            workflow_priority(".github/workflows/future-tests-looking-name.yml"),
        )


if __name__ == "__main__":
    unittest.main()
