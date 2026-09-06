from __future__ import annotations

import unittest

from ci_shepherd.usage import collect_run_usage


def event(kind: str, identity: str, at: str, **data: object) -> dict[str, object]:
    return {"type": kind, "id": identity, "timestamp": at, "data": data}


def shutdown(identity: str, at: str, tokens: int, nano: int) -> dict[str, object]:
    return event(
        "session.shutdown", identity, at, totalNanoAiu=nano, totalPremiumRequests=2,
        modelMetrics={"model": {"usage": {
            "inputTokens": tokens, "outputTokens": 4,
            "cacheReadTokens": 3, "cacheWriteTokens": 2,
        }}},
    )


class UsageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.roster = {
            "runId": "run:one", "startedAt": "2026-09-05T12:00:00Z",
            "sessions": [
                {"sessionId": "app:one", "runtimeSessionId": "runtime:one",
                 "role": "coordinator", "usageScope": "dedicated"},
            ],
        }
        self.as_of = "2026-09-05T12:10:00Z"
        self.start = event("session.start", "start", "2026-09-05T12:00:00Z", sessionId="runtime:one")

    def collect(self, events: list[dict[str, object]]) -> dict[str, object]:
        return collect_run_usage(self.roster, {"runtime:one": events}, as_of=self.as_of)

    def test_missing_usage_is_unknown_not_zero(self) -> None:
        usage = self.collect([self.start])
        self.assertIsNone(usage["metrics"]["inputTokens"]["value"])
        self.assertIsNone(usage["metrics"]["aiCredits"]["value"])
        self.assertEqual(usage["metrics"]["inputTokens"]["coveredSessions"], 0)
        self.assertEqual(usage["sessionCount"], 1)

    def test_cumulative_shutdowns_and_replayed_events_are_not_added(self) -> None:
        final = shutdown("final", self.as_of, 30, 3000)
        usage = self.collect([self.start, shutdown("first", "2026-09-05T12:01:00Z", 10, 1000), final, final])
        self.assertEqual(usage["metrics"]["inputTokens"]["value"], 30)
        self.assertEqual(usage["metrics"]["cacheReadTokens"]["value"], 3)
        self.assertEqual(usage["metrics"]["totalNanoAiu"]["value"], 3000)
        self.assertEqual(usage["metrics"]["premiumRequests"]["value"], 2)
        self.assertIsNone(usage["metrics"]["aiCredits"]["value"])

    def test_reused_workers_and_unbound_ids_are_not_charged(self) -> None:
        self.roster["sessions"].extend([
            {"sessionId": "old", "runtimeSessionId": "runtime:old", "role": "worker", "reused": True},
            {"sessionId": "unbound", "role": "worker"},
            {"sessionId": "task:one", "role": "remote-copilot-agent"},
        ])
        usage = collect_run_usage(self.roster, {
            "runtime:one": [self.start, shutdown("final", self.as_of, 30, 3000)],
            "runtime:old": [shutdown("old", self.as_of, 999, 99999)],
            "not-in-roster": [shutdown("other", self.as_of, 999, 99999)],
        }, as_of=self.as_of)
        self.assertEqual(usage["metrics"]["inputTokens"]["value"], 30)
        self.assertEqual(usage["sessionCount"], 3)
        self.assertEqual(usage["excludedReusedSessions"], 1)
        self.assertEqual(usage["metrics"]["inputTokens"]["coveredSessions"], 1)
        self.assertIn("runtime-session-binding-missing", str(usage))
        self.assertIn("remote-agent-cost-unavailable", str(usage))

    def test_resumed_coordinator_requires_and_subtracts_explicit_boundary(self) -> None:
        self.roster["sessions"][0].update({"usageScope": "resumed", "baselineEventId": "baseline"})
        usage = self.collect([
            shutdown("baseline", "2026-09-05T11:59:59Z", 20, 1000),
            shutdown("final", self.as_of, 30, 3000),
            shutdown("future", "2026-09-05T12:11:00Z", 100, 9000),
        ])
        self.assertEqual(usage["metrics"]["inputTokens"]["value"], 10)
        self.assertEqual(usage["metrics"]["totalNanoAiu"]["value"], 2000)
        del self.roster["sessions"][0]["baselineEventId"]
        unknown = self.collect([shutdown("final", self.as_of, 30, 3000)])
        self.assertIsNone(unknown["metrics"]["inputTokens"]["value"])
        self.assertIn("run-baseline-missing", str(unknown))

    def test_running_checkpoint_updates_cost_not_stale_shutdown_tokens(self) -> None:
        self.roster["sessions"][0].update({"usageScope": "resumed", "baselineEventId": "baseline"})
        usage = self.collect([
            shutdown("old", "2026-09-05T11:00:00Z", 20, 1000),
            event("session.usage_checkpoint", "baseline", "2026-09-05T12:00:00Z", totalNanoAiu=2000),
            event("session.usage_checkpoint", "latest", "2026-09-05T12:09:00Z", totalNanoAiu=5000),
        ])
        self.assertEqual(usage["metrics"]["totalNanoAiu"]["value"], 3000)
        self.assertIsNone(usage["metrics"]["inputTokens"]["value"])
        self.assertEqual(usage["sessions"][0]["status"], "partial")
        self.assertEqual(usage["sessions"][0]["metricAsOf"]["totalNanoAiu"], "2026-09-05T12:09:00Z")

    def test_explicit_complete_per_call_capture_deduplicates_call_ids(self) -> None:
        self.roster["sessions"][0]["eventCoverage"] = "complete"
        call = event(
            "assistant.usage", "call", "2026-09-05T12:01:00Z", apiCallId="api:one",
            inputTokens=10, outputTokens=2, cacheReadTokens=3, cacheWriteTokens=0,
            copilotUsage={"totalNanoAiu": 500}, cost=4,
        )
        usage = self.collect([self.start, call, {**call, "id": "replayed-call"}])
        self.assertEqual(usage["metrics"]["inputTokens"]["value"], 10)
        self.assertEqual(usage["metrics"]["totalNanoAiu"]["value"], 500)
        self.assertIsNone(usage["metrics"]["aiCredits"]["value"])
        self.assertIsNone(usage["metrics"]["premiumRequests"]["value"])

    def test_session_binding_and_duplicate_roster_are_validated(self) -> None:
        wrong = event("session.start", "wrong", "2026-09-05T12:00:00Z", sessionId="different")
        usage = self.collect([wrong, shutdown("final", self.as_of, 30, 3000)])
        self.assertIsNone(usage["metrics"]["inputTokens"]["value"])
        self.assertIn("runtime-session-binding-mismatch", str(usage))
        self.roster["sessions"].append(dict(self.roster["sessions"][0]))
        usage = self.collect([self.start, shutdown("final", self.as_of, 30, 3000)])
        self.assertEqual(usage["sessionCount"], 1)
        self.assertEqual(usage["metrics"]["inputTokens"]["value"], 30)

    def test_stale_shutdown_tokens_expose_their_own_as_of_time(self) -> None:
        usage = self.collect([
            self.start,
            shutdown("stopped", "2026-09-05T12:02:00Z", 30, 3000),
            event("session.usage_checkpoint", "running", "2026-09-05T12:09:00Z", totalNanoAiu=5000),
        ])
        self.assertEqual(usage["sessions"][0]["metricAsOf"]["inputTokens"], "2026-09-05T12:02:00Z")
        self.assertEqual(usage["sessions"][0]["metricAsOf"]["totalNanoAiu"], "2026-09-05T12:09:00Z")
        self.assertEqual(usage["sessions"][0]["status"], "partial")

    def test_missing_one_call_metric_does_not_make_partial_sum_look_complete(self) -> None:
        self.roster["sessions"][0]["eventCoverage"] = "complete"
        calls = [
            event("assistant.usage", "one", "2026-09-05T12:01:00Z", inputTokens=10),
            event("assistant.usage", "two", "2026-09-05T12:02:00Z", outputTokens=20),
        ]
        usage = self.collect([self.start, *calls])
        self.assertIsNone(usage["metrics"]["inputTokens"]["value"])
        self.assertIsNone(usage["metrics"]["outputTokens"]["value"])

    def test_counter_reset_does_not_invent_cross_epoch_cost(self) -> None:
        usage = self.collect([
            self.start,
            event("session.usage_checkpoint", "one", "2026-09-05T12:01:00Z", totalNanoAiu=1000),
            event("session.usage_checkpoint", "reset", "2026-09-05T12:02:00Z", totalNanoAiu=500),
            event("session.usage_checkpoint", "later", "2026-09-05T12:03:00Z", totalNanoAiu=2000),
        ])
        self.assertIsNone(usage["metrics"]["totalNanoAiu"]["value"])
        self.assertIn("cumulative-counter-decreased", str(usage))

    def test_invocation_scope_includes_followup_and_retrospective_once(self) -> None:
        self.roster["sessions"][0]["eventCoverage"] = "complete"
        calls = [
            event(
                "assistant.usage", name, at, apiCallId=name,
                inputTokens=tokens, outputTokens=1, cacheReadTokens=0, cacheWriteTokens=0,
                copilotUsage={"totalNanoAiu": tokens * 10},
            )
            for name, at, tokens in [
                ("primary", "2026-09-05T12:01:00Z", 10),
                ("followup", "2026-09-05T12:05:00Z", 20),
                ("retrospective", "2026-09-05T12:09:00Z", 30),
            ]
        ]
        usage = self.collect([self.start, *calls, calls[0]])
        self.assertEqual(usage["metrics"]["inputTokens"]["value"], 60)
        self.assertEqual(usage["metrics"]["totalNanoAiu"]["value"], 600)
        self.assertEqual(usage["sessionCount"], 1)

    def test_skipped_workers_are_excluded_from_new_run_cost(self) -> None:
        self.roster["sessions"].extend([
            {"sessionId": "skipped", "runtimeSessionId": "runtime:skipped", "role": "worker", "skipped": True},
            {"sessionId": "included", "role": "worker", "includedInRuntimeSessionId": "runtime:one"},
        ])
        usage = collect_run_usage(self.roster, {
            "runtime:one": [self.start, shutdown("final", self.as_of, 30, 3000)],
            "runtime:skipped": [shutdown("prior", self.as_of, 999, 99999)],
        }, as_of=self.as_of)
        self.assertEqual(usage["sessionCount"], 1)
        self.assertEqual(usage["excludedSkippedSessions"], 1)
        self.assertEqual(usage["excludedIncludedSessions"], 1)
        self.assertEqual(usage["excludedReusedSessions"], 0)
        self.assertEqual(usage["metrics"]["totalNanoAiu"]["value"], 3000)


if __name__ == "__main__":
    unittest.main()
