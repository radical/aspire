from __future__ import annotations

import unittest

from ci_shepherd.workflow_loop.scenarios import workflow_policy
from ci_shepherd.workflow_loop.scenarios.workflow_policy import (
    WorkflowPriority,
    workflow_priority,
)


class WorkflowPriorityTests(unittest.TestCase):
    def test_only_exact_aggregate_names_with_dependency_failure_steps_are_suppressed(self) -> None:
        dependency_steps = (
            "Fail if any dependency failed",
            "Fail if any of the dependent jobs failed",
        )
        for name in ("Final Results", "tests / Final Test Results", "Outerloop / Final Results"):
            for steps in (dependency_steps, dependency_steps[:1], dependency_steps[1:]):
                with self.subTest(name=name, steps=steps):
                    self.assertEqual("aggregate", workflow_policy.classify_job_role(name, steps))
        for steps in (
            None, (), ("Run tests",),
            ("Fail if any dependency failed", "Download artifacts"),
            ("Fail if any dependency failed unexpectedly",),
        ):
            with self.subTest(steps=steps):
                self.assertEqual(
                    "ambiguous_leaf",
                    workflow_policy.classify_job_role("tests / Final Test Results", steps),
                )
        for name in (
            "NotFinal Results", "Final Results tests", "Final results",
            "Generate Final Results", "Build",
        ):
            with self.subTest(name=name):
                self.assertEqual("leaf", workflow_policy.classify_job_role(name, dependency_steps))
        self.assertEqual(
            "aggregate",
            workflow_policy.classify_job_role(
                " tests  /  Final   Test Results ", dependency_steps,
            ),
        )
        for steps in (None, ()):
            with self.subTest(steps=steps):
                self.assertEqual(
                    "ambiguous_leaf",
                    workflow_policy.classify_job_role("Hosting-5 / Windows", steps),
                )

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
