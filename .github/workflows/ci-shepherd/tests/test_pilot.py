import unittest
import base64
from unittest.mock import patch

from helpers import FakeClock, WorkspaceTest, reconciliation_evidence
from test_pilot_github import Transport, pr
import pilot
import pilot_github
import pilot_state as state
import pilot_patch
import round as contracts


RUN = {"repository": "radical/aspire", "runId": "41", "runAttempt": "1", "workflowSha": "b" * 40}


class PilotTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.transport = Transport()
        self.transport.values["repos/radical/aspire/issues"] = [dict(pr(), pull_request={})]
        self.transport.values["repos/radical/aspire/pulls/7"] = pr()
        self.transport.values["repos/radical/aspire/issues/7/comments"] = [{
            "id": 20, "body": "Please fix normalization", "updated_at": "2026-10-04T00:00:00Z",
            "user": {"id": 1472, "login": "radical"}}]
        self.api = pilot_github.PilotGitHub(self.transport, 99, 500, "TRACKER99", write=True)

    def prepared(self):
        return pilot.prepare(self.api, RUN, self.clock(), present=False)

    def decision(self, packet, action="cloud"):
        return {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
                "action": action, "replacement": None,
                "dispositions": {item["id"]: "addressed" for item in packet["observation"]["feedback"]}}

    def test_disabled_or_unconfigured_does_not_construct_api_or_infer(self):
        for config in ({}, {"CI_SHEPHERD_ENABLE": "false"}, {"CI_SHEPHERD_ENABLE": "true"}):
            self.assertIsNone(pilot.configuration(config))
        self.assertEqual([], self.transport.writes)

    def test_unchanged_wait_does_not_reserve_native(self):
        self.transport.values["repos/radical/aspire/issues/7/comments"] = []
        self.assertIsNone(self.prepared())
        self.assertEqual(0, self.api.ledger["chains"][0]["rounds"])
        self.assertIsNone(self.prepared())

    def test_failed_native_usage_settles_before_malformed_decision(self):
        packet = self.prepared()
        outcome = pilot.settle(self.api, packet, None, 7.5, self.clock())
        operation = self.api.ledger["chains"][0]["operations"][0]
        self.assertEqual("failed", outcome["outcome"])
        self.assertEqual(7.5, operation["nativeActual"])
        self.assertEqual(0, operation["nativeReserved"])
        self.assertEqual([], [write for write in self.transport.writes if write[1].endswith("/tasks")])

    def test_stale_or_removed_adoption_never_dispatches_but_settles_billing(self):
        packet = self.prepared()
        self.transport.values["repos/radical/aspire/pulls/7"]["head"]["sha"] = "c" * 40
        decision = self.decision(packet)
        result = pilot.settle(self.api, packet, reconciliation_evidence(decision), 2, self.clock())
        self.assertEqual("failed", result["outcome"])
        self.assertEqual(2, self.api.ledger["chains"][0]["operations"][0]["nativeActual"])
        self.assertEqual([], [write for write in self.transport.writes if write[1].endswith("/tasks")])

    def test_fresh_settlement_ignores_external_workers_without_catalog_reads(self):
        packet = self.prepared()
        original = self.transport.__call__

        def transport(method, endpoint, body):
            if method == "GET" and "/tasks" in endpoint:
                self.fail("no saved task IDs, so no task GET is allowed")
            return original(method, endpoint, body)

        fresh = pilot_github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        result = pilot.settle(fresh, packet, reconciliation_evidence(self.decision(packet)), 2, self.clock())
        self.assertEqual("uncertain", result["outcome"])
        self.assertEqual(2, fresh.ledger["chains"][0]["operations"][0]["nativeActual"])
        self.assertEqual(1, len([write for write in self.transport.writes if write[1].endswith("/tasks")]))

    def test_disabled_real_entrypoint_authenticates_separate_job_and_settles_without_effects(self):
        packet = self.prepared()
        trusted = self.work / "trusted"
        trusted.mkdir()
        contracts.write_json(trusted / "packet.json", packet)
        usage = self.work / "usage.json"
        contracts.write_json(usage, {"ai_credits": 2})
        result_path = self.work / "result.json"
        config = {"CI_SHEPHERD_ENABLE": "false", "CI_SHEPHERD_TRACKER": "99",
                  "CI_SHEPHERD_AUTHORITY_COMMENT": "500", "CI_SHEPHERD_TRACKER_NODE": "TRACKER99",
                  "GITHUB_OUTPUT": str(self.work / "output")}
        fresh = pilot_github.PilotGitHub(self.transport, 99, 500, "TRACKER99", write=True)
        self.transport.writes.clear()
        with patch.dict(pilot.os.environ, config, clear=True), patch.object(contracts, "host_run", return_value=RUN), \
                patch("hosted.require_host") as authenticated, \
                patch.object(pilot.github, "PilotTransport", return_value=self.transport):
            self.assertEqual(0, pilot.main(["settle", "--trusted", str(trusted), "--usage", str(usage),
                                           "--result", str(result_path)]))
            authenticated.assert_called_once_with(RUN, allowed_events={"workflow_dispatch", "schedule"})
        fresh.read_authority()
        operation = fresh.ledger["chains"][0]["operations"][0]
        self.assertEqual((2, 0, "failed"), (operation["nativeActual"], operation["nativeReserved"], operation["state"]))
        self.assertEqual("billing-only", contracts.read_json(result_path)["outcome"])
        self.assertTrue(all(method == "PATCH" and endpoint.endswith("/comments/500")
                            for method, endpoint, body in self.transport.writes))

    def test_disabled_billing_retains_unknown_usage_and_potentially_sent_effects(self):
        packet = self.prepared()
        operation = self.api.ledger["chains"][0]["operations"][0]
        state.reserve_worker(self.api.ledger, self.api.ledger["chains"][0], operation, self.clock())
        state.sent(operation)
        self.api.persist()
        fresh = pilot_github.PilotGitHub(self.transport, 99, 500, "TRACKER99", write=True)
        pilot.settle(fresh, packet, None, None, self.clock(), billing_only=True)
        operation = fresh.ledger["chains"][0]["operations"][0]
        self.assertEqual(30, operation["nativeReserved"])
        self.assertEqual("sent", operation["state"])
        self.assertEqual(1, state.worker_slots(fresh.ledger))

    def test_cloud_send_unknown_holds_slot_and_never_reposts(self):
        packet = self.prepared()
        decision = self.decision(packet)
        result = pilot.settle(self.api, packet, reconciliation_evidence(decision), 2, self.clock())
        self.assertEqual("uncertain", result["outcome"])
        operation = self.api.ledger["chains"][0]["operations"][0]
        self.assertEqual(1, state.worker_slots(self.api.ledger))
        self.assertEqual(1, len([write for write in self.transport.writes if write[1].endswith("/tasks")]))
        pilot.settle(self.api, packet, reconciliation_evidence(decision), 2, self.clock())
        self.assertEqual(1, len([write for write in self.transport.writes if write[1].endswith("/tasks")]))
        self.assertIsNone(self.prepared())

    def test_readable_status_names_lane_attempts_spend_blocker(self):
        packet = self.prepared()
        chain = self.api.ledger["chains"][0]
        rendered = self.api.status(chain, packet["observation"], self.clock())
        self.assertIn("cloud", rendered)
        self.assertIn("0/2", rendered)
        self.assertIn("1/10", rendered)
        self.assertIn("reservation: 30", rendered)
        self.assertIn("no retry", rendered.replace("never retry", "no retry"))

    def test_expired_packet_settles_native_without_effect(self):
        packet = self.prepared()
        self.clock.advance(minutes=11)
        result = pilot.settle(self.api, packet, reconciliation_evidence(self.decision(packet)), 4, self.clock())
        self.assertEqual("failed", result["outcome"])
        self.assertEqual(4, state.chain_spend(self.api.ledger["chains"][0]))

    def test_two_independent_prs_progress_while_issue_waits_for_worker_capacity(self):
        second = pr(9)
        issue = {"id": 1008, "number": 8, "node_id": "NODE8", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}], "title": "Broken", "body": "Bug"}
        self.transport.values["repos/radical/aspire/issues"].extend([dict(second, pull_request={}), issue])
        self.transport.values["repos/radical/aspire/pulls/9"] = second
        self.transport.values["repos/radical/aspire/issues/8"] = issue
        self.transport.values["repos/radical/aspire/issues/9/comments"] = [{
            "id": 29, "body": "Please fix", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}]
        first = self.prepared()
        pilot.settle(self.api, first, reconciliation_evidence(self.decision(first)), 2, self.clock())
        second_packet = self.prepared()
        self.assertEqual(9, second_packet["observation"]["number"])
        pilot.settle(self.api, second_packet, reconciliation_evidence(self.decision(second_packet)), 2, self.clock())
        self.assertIsNone(self.prepared())
        self.assertEqual(2, state.worker_slots(self.api.ledger))
        self.assertEqual(0, state.find_chain(self.api.ledger, 8)["rounds"])

    def test_native_overshoot_is_recorded_but_blocks_code_effects(self):
        packet = self.prepared()
        result = pilot.settle(self.api, packet, reconciliation_evidence(self.decision(packet)), 501, self.clock())
        self.assertEqual("failed", result["outcome"])
        self.assertEqual(501, state.chain_spend(self.api.ledger["chains"][0]))
        self.assertEqual([], [write for write in self.transport.writes if write[1].endswith("/tasks")])

    def test_uncertain_presentation_does_not_starve_independent_due_pr(self):
        self.transport.values["repos/radical/aspire/issues"].append(dict(pr(9), pull_request={}))
        self.transport.values["repos/radical/aspire/pulls/9"] = pr(9)
        self.transport.values["repos/radical/aspire/issues/9/comments"] = [{
            "id": 29, "body": "Fix", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}]
        self.api.read_authority()
        first = state.adopt(self.api.ledger, 7, "pr", "NODE7")
        first["statusPending"] = True
        self.api.persist()
        packet = self.prepared()
        self.assertEqual(9, packet["observation"]["number"])
        self.assertEqual(0, first["rounds"])

    def test_native_usage_primary_field_is_credit_units_and_missing_is_unknown(self):
        self.assertIsNone(pilot.native_usage(None))
        path = self.work / "agent_usage.json"
        contracts.write_json(path, {"input_tokens": 7863, "output_tokens": 996, "ai_credits": 5.81384})
        self.assertEqual(5.81384, pilot.native_usage(path))

    def test_re_adoption_keeps_counters_and_sticky_escalation(self):
        packet = self.prepared()
        chain = self.api.ledger["chains"][0]
        pilot.settle(self.api, packet, None, 2, self.clock())
        self.transport.values["repos/radical/aspire/pulls/7"]["labels"] = []
        self.assertIsNone(self.prepared())
        self.transport.values["repos/radical/aspire/pulls/7"]["labels"] = [{"name": "shepherd-adopted"}]
        next_packet = self.prepared()
        self.assertEqual(chain["id"], next_packet["chain"])
        self.assertEqual(2, chain["rounds"])
        self.assertTrue(chain["escalated"])

    def test_broader_work_escalates_from_inline_packet_without_another_native_round(self):
        self.transport.values["repos/radical/aspire/pulls/7/files"] = [
            {"sha": "d" * 40, "filename": pilot_patch.SOURCE, "status": "modified"}]
        for path, content in {
            pilot_patch.SOURCE: "def normalize_label(value):\n    return value\n",
            pilot_patch.TEST: "import unittest\n",
        }.items():
            self.transport.values["repos/radical/aspire/contents/" + path] = {
                "type": "file", "path": path, "size": len(content.encode()), "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode()}
        packet = self.prepared()
        self.assertEqual("local", packet["lane"])
        result = pilot.settle(self.api, packet, reconciliation_evidence(self.decision(packet)), 2, self.clock())
        chain = self.api.ledger["chains"][0]
        self.assertEqual("uncertain", result["outcome"])
        self.assertEqual((1, 1, True), (chain["localAttempts"], chain["rounds"], chain["escalated"]))
        self.assertEqual("cloud", chain["operations"][0]["lane"])

    def test_primary_added_source_and_test_files_route_cloud_without_id_assumption(self):
        self.transport.values["repos/radical/aspire/pulls/7/files"] = [
            {"sha": "d" * 40, "filename": path, "status": "added"}
            for path in (pilot_patch.SOURCE, pilot_patch.TEST)]
        packet = self.prepared()
        self.assertEqual("cloud", packet["lane"])
        self.assertEqual(0, self.api.ledger["chains"][0]["localAttempts"])
        self.assertEqual([], [write for write in self.transport.writes if write[1].endswith("/tasks")])

    def test_primary_node_and_check_identity_persist_real_feedback_batch(self):
        value = self.transport.values["repos/radical/aspire/pulls/7"]
        value["node_id"] = "PR_kwDOLIR8788AAAABGk2Orw"
        self.transport.values["repos/radical/aspire/issues"][0]["node_id"] = value["node_id"]
        self.transport.values["repos/radical/aspire/issues/7/comments"] = []
        self.transport.values["repos/radical/aspire/commits/" + "a" * 40 + "/check-runs"] = {
            "total_count": 30, "check_runs": [
                {"id": 90000000000 + index, "head_sha": "a" * 40, "status": "completed",
                 "conclusion": "failure", "name": "Fixture tests", "html_url": "https://github.com/check"}
                for index in range(30)]}
        packet = self.prepared()
        chain = self.api.ledger["chains"][0]
        identity = chain["operations"][0]["identity"]
        self.assertGreater(len(identity.encode()), 256)
        self.assertEqual(30, len(packet["observation"]["feedback"]))
        recovered = state.parse(state.render(self.api.ledger))
        self.assertEqual(identity, recovered["chains"][0]["operations"][0]["identity"])
        self.assertEqual(1, recovered["chains"][0]["rounds"])

    def test_issue_native_failure_can_retry_without_new_chain_allocation(self):
        issue = {"id": 1008, "number": 8, "node_id": "NODE8", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}], "title": "Bug", "body": "Actual defect"}
        self.transport.values["repos/radical/aspire/issues"] = [issue]
        self.transport.values["repos/radical/aspire/issues/8"] = issue
        first = self.prepared()
        pilot.settle(self.api, first, None, 2, self.clock())
        second = self.prepared()
        self.assertEqual(first["chain"], second["chain"])
        self.assertEqual(2, self.api.ledger["chains"][0]["rounds"])

    def test_feedback_overflow_is_visible_wait_and_other_pr_progresses(self):
        self.transport.values["repos/radical/aspire/issues"].append(dict(pr(9), pull_request={}))
        self.transport.values["repos/radical/aspire/pulls/9"] = pr(9)
        self.transport.values["repos/radical/aspire/issues/9/comments"] = [{
            "id": 29, "body": "Fix", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}]
        self.transport.values["repos/radical/aspire/issues/7/comments"] = [
            {"id": index, "body": "Feedback", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}
            for index in range(1, 32)]
        packet = self.prepared()
        self.assertEqual(9, packet["observation"]["number"])
        chain = state.find_chain(self.api.ledger, 7)
        observation = self.api.observe(chain)
        self.assertFalse(observation["actionable"])
        self.assertEqual([], observation["feedback"])
        self.assertIn("exceeds 30", self.api.status(chain, observation, self.clock()))

    def test_prepared_empty_feedback_issue_overflow_blocks_dispatch_but_allows_presentation(self):
        issue = {"id": 1008, "number": 8, "node_id": "NODE8", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}], "title": "Broken", "body": "Bug"}
        self.transport.values["repos/radical/aspire/issues"] = [issue]
        self.transport.values["repos/radical/aspire/issues/8"] = issue
        self.transport.values["repos/radical/aspire/issues/8/comments"] = []
        packet = self.prepared()
        self.assertEqual([], packet["observation"]["feedback"])
        self.transport.values["repos/radical/aspire/issues/8/comments"] = [
            {"id": index, "body": "Feedback", "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}
            for index in range(1, 32)]
        chain = self.api.ledger["chains"][0]
        fresh = self.api.guard(chain, packet["observation"], effect=False)
        self.assertIn("exceeds 30", fresh["attention"])
        self.assertIn("exceeds 30", self.api.status(chain, fresh, self.clock()))
        result = pilot.settle(self.api, packet, reconciliation_evidence(self.decision(packet)), 2, self.clock())
        self.assertEqual("failed", result["outcome"])
        self.assertIn("exceeds 30", result["error"])
        self.assertEqual(2, chain["operations"][0]["nativeActual"])
        self.assertEqual([], [write for write in self.transport.writes if write[1].endswith("/tasks")])

    def test_native_human_handoff_requires_observed_label_transition_to_resume(self):
        packet = self.prepared()
        decision = self.decision(packet, "human")
        decision["dispositions"] = {identity: "needs-human" for identity in decision["dispositions"]}
        result = pilot.settle(self.api, packet, reconciliation_evidence(decision), 2, self.clock())
        self.assertEqual("human", result["outcome"])
        chain = self.api.ledger["chains"][0]
        self.assertIsNone(self.prepared())
        self.assertEqual("human", chain["state"])
        self.transport.values["repos/radical/aspire/pulls/7"]["labels"] = [{"name": "shepherd-hands-off"}]
        self.assertIsNone(self.prepared())
        self.assertEqual("hands-off", chain["state"])
        self.transport.values["repos/radical/aspire/pulls/7"]["labels"] = [{"name": "shepherd-adopted"}]
        self.prepared()
        self.assertEqual("open", chain["state"])
        self.assertEqual(1, chain["rounds"])


if __name__ == "__main__":
    unittest.main()
