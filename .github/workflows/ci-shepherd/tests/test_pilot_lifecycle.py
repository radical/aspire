"""Restarted public sweeps with a closed task-service fixture; no remote effects."""

from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
import io
from hashlib import sha256
import json
import unittest
from unittest.mock import patch

from helpers import FakeClock, FakePilotProcess, WorkspaceTest, reconciliation_evidence
from github import IncompleteInventory, LostResponse, Response
from test_pilot_github import Transport, pr
import hosted
import live
import local
import pilot
import pilot_github as github
import pilot_state as state
import reasoning
import round as contracts


RUN = {"repository": "radical/aspire", "runId": "41", "runAttempt": "1", "workflowSha": "b" * 40}
PREFIX = "repos/radical/aspire"
TASKS = "agents/repos/radical/aspire/tasks"


def decision(packet):
    return {"schemaVersion": 1, "packetId": packet["packetId"], "operation": packet["operation"],
            "action": "cloud", "replacement": None,
            "dispositions": {item["id"]: "addressed" for item in packet["observation"]["feedback"]}}


class LifecycleTransport(Transport):
    def __init__(self):
        super().__init__()
        self.send_ledgers = []
        self.lose_send_response = False

    def __call__(self, method, endpoint, body):
        if endpoint == TASKS:
            if method != "POST":
                raise AssertionError("task catalogs must not be read")
            self.writes.append((method, endpoint, deepcopy(body)))
            self.send_ledgers.append(state.parse(self.comments[0]["body"]))
            task_id = "TASK" + str(len(self.send_ledgers))
            self.values[TASKS + "/" + task_id] = {
                "id": task_id, "state": "queued", "creator": {"id": 1472},
                "repository": {"id": 746880239}, "session_count": 1, "artifacts": [],
                "updated_at": "2026-10-04T00:00:00Z",
                "sessions": [{
                    "id": "SESSION" + str(len(self.send_ledgers)), "task_id": task_id, "state": "queued",
                    "repository": {"id": 746880239}, "user": {"id": 1472},
                    "base_ref": body["base_ref"], "head_ref": body.get("head_ref", "fix-9"),
                    "prompt": body["prompt"]}]}
            if self.lose_send_response:
                raise LostResponse("task created but response lost")
            return Response({"id": task_id}, {}, 201)
        if method == "GET" and endpoint.startswith(TASKS + "/") and endpoint not in self.values:
            self.reads.append((method, endpoint, body))
            raise IncompleteInventory("saved task receipt unavailable")
        if method == "POST" and endpoint.startswith(PREFIX + "/issues/") and endpoint.endswith("/comments"):
            self.writes.append((method, endpoint, deepcopy(body)))
            comments = self.values.setdefault(endpoint, [])
            comment = {"id": 600 + len(comments), "user": deepcopy(self.values["user"]), "body": body["body"],
                       "updated_at": "2026-10-04T00:00:00Z"}
            comments.append(comment)
            self.values[PREFIX + "/issues/comments/" + str(comment["id"])] = comment
            return Response(deepcopy(comment), {}, 201)
        if method == "PATCH" and endpoint.startswith(PREFIX + "/issues/comments/") and endpoint in self.values:
            self.writes.append((method, endpoint, deepcopy(body)))
            self.values[endpoint]["body"] = body["body"]
            return Response(deepcopy(self.values[endpoint]), {}, 200)
        return super().__call__(method, endpoint, body)


class LifecycleTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.clock = FakeClock()
        self.transport = LifecycleTransport()
        self.transport.values[PREFIX + "/issues"] = [dict(pr(), pull_request={})]
        self.transport.values[PREFIX + "/pulls/7"] = pr()
        self.transport.values[PREFIX + "/issues/7/comments"] = [{
            "id": 20, "body": "Please fix normalization", "updated_at": "2026-10-04T00:00:00Z",
            "user": {"id": 1472, "login": "radical"}}]

    def fresh(self):
        api = github.PilotGitHub(self.transport, 99, 500, "TRACKER99", write=True)
        api.clock = self.clock
        return api

    def ledger(self):
        return state.parse(self.transport.comments[0]["body"])

    def complete_review(self, head):
        self.transport.values[PREFIX + "/pulls/7"]["requested_reviewers"] = []
        self.transport.values.setdefault(PREFIX + "/pulls/7/reviews", []).append({
            "id": 61, "user": {"id": 175728472, "login": "Copilot", "type": "Bot"},
            "state": "COMMENTED", "body": "", "commit_id": head,
            "submitted_at": self.clock().isoformat().replace("+00:00", "Z")})

    def prepare(self):
        api = self.fresh()
        with redirect_stdout(io.StringIO()) as logs, redirect_stderr(io.StringIO()) as errors:
            packet = pilot.prepare(api, RUN, self.clock(), present=False)
        self.logs, self.errors = logs.getvalue(), errors.getvalue()
        self.assertEqual(api.ledger, self.ledger())
        return packet

    def settle(self, packet, usage=2):
        api = self.fresh()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = pilot.settle(api, packet, reconciliation_evidence(decision(packet)), usage, self.clock())
        self.assertEqual(api.ledger, self.ledger())
        return result

    def task_writes(self):
        return [write for write in self.transport.writes if write[1] == TASKS]

    def check(self, head, conclusion, check_id=45):
        self.transport.values[PREFIX + "/commits/" + head + "/check-runs"] = {
            "total_count": 1, "check_runs": [{
                "id": check_id, "head_sha": head, "status": "completed", "conclusion": conclusion,
                "name": "Tests", "html_url": "https://github.com/radical/aspire/actions/runs/10",
                "output": {"annotations_count": 0, "title": "", "summary": "", "text": ""}}]}

    def start(self):
        self.transport.values[PREFIX + "/issues/7/comments"] = []
        self.check("a" * 40, "failure")
        packet = self.prepare()
        self.assertEqual({"outcome": "waiting", "taskId": "TASK1"}, self.settle(packet))
        return packet

    def finish(self, outcome="completed", usage=1.5, error=None):
        task = self.transport.values[TASKS + "/TASK1"]
        task["state"] = task["sessions"][0]["state"] = outcome
        task["updated_at"] = "2026-10-04T00:02:00Z"
        if usage is not None:
            task["sessions"][0]["usage"] = {"type": "ai_credits", "amount": int(usage * 1e9)}
        if error is not None:
            task["sessions"][0]["error"] = {"message": error}
        return task

    def result_facts(self, packet, *, outcome="completed", head="a" * 40, error=None):
        return {
            "operation": packet["operation"], "taskId": "TASK1", "state": outcome,
            "sessionCount": 1, "sessionIds": ["SESSION1"], "updatedAt": "2026-10-04T00:02:00Z",
            "sessionStates": [outcome], "sourceHead": "a" * 40, "artifactState": "not-reported",
            "errors": [] if error is None else [{"sessionId": "SESSION1", "message": error}],
            "errorsTruncated": False, "narrativeAvailable": False, "currentHead": head,
            "headChanged": head != "a" * 40}

    def observed(self):
        api = self.fresh()
        api.read_authority()
        api.reconcile_workers()
        return api.observe(api.ledger["chains"][0])

    def test_adoption_reserves_one_round_and_saves_one_exact_task(self):
        packet = self.prepare()
        chain = self.ledger()["chains"][0]
        self.assertEqual((7, "NODE7", None, 1, 0, True),
                         (chain["origin"], chain["node"], chain["child"], chain["rounds"],
                          chain["localAttempts"], chain["escalated"]))
        operation = chain["operations"][0]
        self.assertEqual(packet["operation"], operation["id"])
        self.assertEqual((None, 30, None, 0, None),
                         (operation["nativeActual"], operation["nativeReserved"], operation["workerActual"],
                          operation["workerReserved"], operation["taskId"]))
        self.assertEqual([], self.task_writes())
        result = self.settle(packet)
        self.assertEqual({"outcome": "waiting", "taskId": "TASK1"}, result)
        chain = self.ledger()["chains"][0]
        operation = chain["operations"][0]
        self.assertEqual((1, 1, 500), (chain["rounds"], len(chain["operations"]), state.chain_spend(chain)))
        self.assertEqual(("TASK1", "queued", 2, 0, None, 498),
                         (operation["taskId"], operation["workerState"], operation["nativeActual"],
                          operation["nativeReserved"], operation["workerActual"], operation["workerReserved"]))
        self.assertEqual(1, len(self.task_writes()))
        method, endpoint, body = self.task_writes()[0]
        self.assertEqual(("POST", TASKS, False, "fix-7", "main"),
                         (method, endpoint, body["create_pull_request"], body["head_ref"], body["base_ref"]))
        self.assertEqual({"chain": chain["id"], "operation": operation["id"], "origin": 7},
                         json.loads(body["prompt"].splitlines()[0].removeprefix(github.CORRELATION)))
        boundary = self.transport.send_ledgers[0]["chains"][0]["operations"][0]
        self.assertEqual(("sent", None, 2, 498),
                         (boundary["state"], boundary["taskId"], boundary["nativeActual"], boundary["workerReserved"]))

    def test_hosted_cli_and_native_evidence_restart_without_duplicate_decision_or_dispatch(self):
        directory = self.work / "first"
        output = self.work / "output"
        config = {"CI_SHEPHERD_ENABLE": "true", "CI_SHEPHERD_TRACKER": "99",
                  "CI_SHEPHERD_AUTHORITY_COMMENT": "500", "CI_SHEPHERD_TRACKER_NODE": "TRACKER99",
                  "SHEPHERD_MODE": "pilot", "GITHUB_OUTPUT": str(output)}
        process = FakePilotProcess()
        with patch.dict(pilot.os.environ, config, clear=True), \
                patch.object(contracts, "host_run", return_value=RUN), \
                patch.object(hosted, "require_host") as authenticated, \
                patch.object(github, "PilotTransport", return_value=self.transport), \
                patch.object(live, "clock", self.clock), \
                redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(0, hosted.main(["prepare", "--workdir", str(directory)]))
            packet = contracts.read_json(directory / "trusted/packet.json")
            self.assertIsNotNone(packet, "confirmed status publication must permit native admission")
            self.assertEqual({
                "schemaVersion", "kind", "packetId", "run", "chain", "operation", "preparedAt",
                "observation", "lane", "context", "target", "trialBrief"}, set(packet))
            self.assertEqual({"number", "kind", "node", "head", "description", "managed", "originManaged",
                              "handsOff", "state", "feedback", "ready", "attention", "pendingCI", "ciWait",
                              "reviewOnly", "diagnostics", "approval", "workflowAttention", "actionable",
                              "title", "body", "url", "headRef", "workerResults", "copilotReview"}, set(packet["observation"]))
            self.assertEqual([{"id": "comment:20:2026-10-04T00:00:00Z",
                               "body": "Please fix normalization", "url": ""}], packet["observation"]["feedback"])
            self.assertEqual([], packet["observation"]["workerResults"])
            self.assertEqual([], self.task_writes())
            native = directory / "native"
            evidence = local.execute(native, packet, "fixture-token", process=process)
            verified, _ = reasoning.validate_evidence(evidence, evidence["sessionId"], hosted=True)
            self.assertEqual([{
                "toolName": "safeoutputs-submit_decision", "toolCallId": "call-1",
                "arguments": {"decision": json.dumps(decision(packet))}}],
                [event["data"] for event in evidence["events"] if event["type"] == "tool.execution_start"])
            self.assertEqual(2, pilot.native_usage(native / "usage.json"))
            safe_output = directory / "safe-output.json"
            contracts.write_json(safe_output, {"items": [{
                "type": "submit_decision", "decision": json.dumps(verified)}], "errors": []})
            receipt = directory / "receipt.json"
            before_apply = deepcopy(self.transport.writes)
            self.assertEqual(0, hosted.main([
                "apply", "--trusted", str(directory / "trusted"), "--evidence", str(native / "evidence.json"),
                "--decision", str(safe_output), "--receipt", str(receipt)]))
            self.assertEqual({"outcome": "pilot-decision-verified", "effects": []}, contracts.read_json(receipt))
            self.assertEqual(before_apply, self.transport.writes)
            result_path = directory / "settled.json"
            arguments = ["settle", "--trusted", str(directory / "trusted"), "--evidence",
                         str(native / "evidence.json"), "--usage", str(native / "usage.json"),
                         "--result", str(result_path)]
            self.assertEqual(0, pilot.main(arguments), errors.getvalue())
            self.assertEqual({"outcome": "waiting", "taskId": "TASK1"}, contracts.read_json(result_path))
            operation = self.ledger()["chains"][0]["operations"][0]
            self.assertEqual((packet["operation"], evidence["sessionId"], "TASK1", 2, 0, 498),
                             tuple(operation[key] for key in (
                                 "id", "sessionId", "taskId", "nativeActual", "nativeReserved", "workerReserved")))
            result_path = directory / "replayed.json"
            arguments[-1] = str(result_path)
            self.assertEqual(0, pilot.main(arguments), errors.getvalue())
            self.assertEqual({"outcome": "replay"}, contracts.read_json(result_path))
            for index, task_state in enumerate(("queued", "in_progress", "in_progress")):
                self.clock.advance(minutes=2)
                task = self.transport.values[TASKS + "/TASK1"]
                task["state"] = task["sessions"][0]["state"] = task_state
                self.transport.reads.clear()
                output.write_text("")
                restarted = self.work / ("restart-" + str(index))
                self.assertEqual(0, hosted.main(["prepare", "--workdir", str(restarted)]))
                self.assertIsNone(contracts.read_json(restarted / "trusted/packet.json"))
                self.assertTrue(output.read_text().startswith("active=false\npilot=false\n"))
                self.assertEqual([TASKS + "/TASK1"],
                                 [endpoint for _, endpoint, _ in self.transport.reads if "/tasks" in endpoint])
                chain = self.ledger()["chains"][0]
                self.assertEqual((1, 1, 0, 500),
                                 (chain["rounds"], len(chain["operations"]), chain["localAttempts"],
                                  state.chain_spend(chain)))
                self.assertEqual(packet["operation"], chain["operations"][0]["id"])
                self.assertEqual(evidence["sessionId"], chain["operations"][0]["sessionId"])
            self.assertEqual(6, authenticated.call_count)
        self.assertEqual(1, len(process.launches))
        self.assertEqual(1, len(self.task_writes()))

    def test_completed_task_with_new_green_head_waits_for_copilot_then_current_head_human_approval(self):
        first = self.start()
        self.finish()
        self.transport.values[PREFIX + "/pulls/7"]["head"]["sha"] = "b" * 40
        self.check("b" * 40, "success", 46)
        self.assertIsNone(self.prepare())
        chain = self.ledger()["chains"][0]
        self.assertEqual((1, 1, 0, 33.5),
                         (chain["rounds"], len(chain["operations"]), chain["localAttempts"], state.chain_spend(chain)))
        self.assertEqual((2, 0, 1.5, 0, "completed"),
                         tuple(chain["operations"][0][key] for key in (
                             "nativeActual", "nativeReserved", "workerActual", "workerReserved", "state")))
        observed = self.observed()
        self.assertEqual([self.result_facts(first, head="b" * 40)], observed["workerResults"])
        self.assertFalse(observed["ready"])
        self.complete_review("b" * 40)
        self.assertIsNone(self.prepare())
        chain = self.ledger()["chains"][0]
        self.assertEqual("completed", chain["reviews"][0]["state"])
        self.transport.values[PREFIX + "/pulls/7/reviews"] = [{
            "id": 30, "user": {"id": 1472, "login": "radical"}, "state": "APPROVED", "body": "",
            "commit_id": "b" * 40, "submitted_at": "2026-10-04T00:03:00Z"}]
        self.assertIsNone(self.prepare())
        self.assertTrue(self.observed()["ready"])
        self.assertEqual(chain, self.ledger()["chains"][0])
        self.assertEqual(1, len(self.task_writes()))

    def test_completed_task_without_push_and_still_red_ci_reserves_one_new_decision(self):
        first = self.start()
        self.finish()
        next_packet = self.prepare()
        self.assertEqual([self.result_facts(first)], next_packet["observation"]["workerResults"])
        self.assertEqual(["check:45:" + "a" * 40 + ":failure"],
                         [item["id"] for item in next_packet["observation"]["feedback"]])
        self.assertFalse(next_packet["observation"]["ready"])
        chain = self.ledger()["chains"][0]
        self.assertEqual((2, 33.5, 1), (chain["rounds"], state.chain_spend(chain), len(self.task_writes())))
        prior = deepcopy(chain["operations"][0])
        self.assertEqual({"outcome": "waiting", "taskId": "TASK2"}, self.settle(next_packet))
        self.assertIsNone(self.prepare())
        chain = self.ledger()["chains"][0]
        self.assertEqual(prior, chain["operations"][0])
        self.assertEqual((2, 2, 500), (chain["rounds"], len(chain["operations"]), state.chain_spend(chain)))
        self.assertEqual(["TASK1", "TASK2"], [operation["taskId"] for operation in chain["operations"]])
        self.assertEqual(2, len(self.task_writes()))

    def test_changed_head_with_still_red_ci_is_not_repair_success(self):
        first = self.start()
        self.finish()
        self.transport.values[PREFIX + "/pulls/7"]["head"]["sha"] = "b" * 40
        self.check("b" * 40, "failure", 46)
        next_packet = self.prepare()
        self.assertEqual([self.result_facts(first, head="b" * 40)], next_packet["observation"]["workerResults"])
        self.assertEqual(["check:46:" + "b" * 40 + ":failure"],
                         [item["id"] for item in next_packet["observation"]["feedback"]])
        self.assertEqual(first["chain"], next_packet["chain"])
        self.assertFalse(next_packet["observation"]["ready"])
        self.assertEqual(2, self.ledger()["chains"][0]["rounds"])
        self.assertEqual(1, len(self.task_writes()))

    def terminal_error(self, outcome):
        first = self.start()
        self.finish(outcome, error="Repository tests failed")
        packet = self.prepare()
        self.assertEqual([self.result_facts(first, outcome=outcome, error="Repository tests failed")],
                         packet["observation"]["workerResults"])
        self.assertEqual(first["observation"]["feedback"], packet["observation"]["feedback"])
        self.assertFalse(packet["observation"]["ready"])
        chain = self.ledger()["chains"][0]
        self.assertEqual((2, 2, 33.5, 0),
                         (chain["rounds"], len(chain["operations"]), state.chain_spend(chain),
                          state.worker_slots(self.ledger())))
        self.assertEqual("completed" if outcome == "completed" else "failed", chain["operations"][0]["state"])
        self.assertEqual({}, chain["dispositions"])
        self.assertEqual(1, len(self.task_writes()))

    def test_failed_worker_errors_reenter_current_ci_without_final_narrative(self):
        self.terminal_error("failed")

    def test_cancelled_worker_errors_reenter_current_ci_without_final_narrative(self):
        self.terminal_error("cancelled")

    def test_timed_out_worker_errors_reenter_current_ci_without_final_narrative(self):
        self.terminal_error("timed_out")

    def test_completed_worker_with_verified_error_is_not_proof_of_success(self):
        self.terminal_error("completed")

    def late_review(self, reviewer):
        first = self.start()
        submitted = "2026-10-04T00:03:00Z"
        if reviewer == "human":
            self.transport.values[PREFIX + "/pulls/7/reviews"] = [{
                "id": 30, "user": {"id": 1472, "login": "radical"}, "state": "CHANGES_REQUESTED",
                "body": "Handle the empty input", "commit_id": "b" * 40, "submitted_at": submitted}]
            expected = [{"id": "review:30:" + submitted + ":" + sha256(b"Handle the empty input").hexdigest(),
                         "body": "Handle the empty input", "url": ""}]
        else:
            self.transport.values[PREFIX + "/pulls/7/reviews"] = [{
                "id": 31, "user": {"id": 175728472, "login": "Copilot", "type": "Bot"}, "state": "COMMENTED",
                "body": "See inline comments", "commit_id": "b" * 40, "submitted_at": submitted}]
            self.transport.values[PREFIX + "/pulls/7/comments"] = [{
                "id": 40, "node_id": "RC40", "updated_at": submitted, "body": "Handle the empty input",
                "user": {"id": 175728472, "login": "Copilot", "type": "Bot"},
                "path": "src/parser.py", "line": 12, "side": "RIGHT", "commit_id": "b" * 40}]
            expected = [{"id": "review-comment:40:" + submitted, "body": "Handle the empty input", "url": "",
                         "path": "src/parser.py", "line": 12, "side": "RIGHT", "commit_id": "b" * 40},
                        {"id": "review:31:" + submitted + ":" + sha256(b"See inline comments").hexdigest(),
                         "body": "See inline comments", "url": ""}]
        self.assertIsNone(self.prepare())
        self.assertEqual(1, self.ledger()["chains"][0]["rounds"])
        self.finish()
        self.transport.values[PREFIX + "/pulls/7"]["head"]["sha"] = "b" * 40
        self.check("b" * 40, "success", 46)
        packet = self.prepare()
        self.assertEqual(expected, packet["observation"]["feedback"])
        self.assertEqual([self.result_facts(first, head="b" * 40)], packet["observation"]["workerResults"])
        self.assertFalse(packet["observation"]["ready"])
        chain = self.ledger()["chains"][0]
        self.assertEqual((first["chain"], 2, 33.5), (chain["id"], chain["rounds"], state.chain_spend(chain)))
        history = deepcopy(chain["operations"][0])
        self.assertEqual({"outcome": "waiting", "taskId": "TASK2"}, self.settle(packet))
        self.assertIsNone(self.prepare())
        self.assertEqual(history, self.ledger()["chains"][0]["operations"][0])
        self.assertEqual(2, len(self.task_writes()))

    def test_late_human_review_waits_for_active_worker_then_enters_same_pr_loop(self):
        self.late_review("human")

    def test_late_copilot_comment_review_uses_new_inline_id_without_claiming_approval(self):
        self.late_review("copilot")

    def test_resolved_old_inline_id_waits_then_new_rereview_id_enters_same_pr_loop(self):
        submitted = "2026-10-04T00:00:00Z"
        comment = {
            "id": 40, "node_id": "RC40", "updated_at": submitted, "body": "Handle the empty input",
            "user": {"id": 175728472, "login": "Copilot", "type": "Bot"},
            "path": "src/parser.py", "line": 12, "side": "RIGHT", "commit_id": "a" * 40}
        self.transport.values[PREFIX + "/issues/7/comments"] = []
        self.transport.values[PREFIX + "/pulls/7/comments"] = [comment]
        first = self.prepare()
        self.assertEqual({"outcome": "waiting", "taskId": "TASK1"}, self.settle(first))
        self.finish()
        self.transport.values[PREFIX + "/pulls/7"]["head"]["sha"] = "b" * 40
        self.check("b" * 40, "success")
        self.transport.resolved_reviews = {40}
        self.assertIsNone(self.prepare(), "a resolved old comment is not another paid repair")
        history = deepcopy(self.ledger()["chains"][0]["operations"][0])
        for _ in range(2):
            self.clock.advance(minutes=2)
            self.assertIsNone(self.prepare())
            self.assertEqual((1, 33.5), (self.ledger()["chains"][0]["rounds"],
                                        state.chain_spend(self.ledger()["chains"][0])))
        self.complete_review("b" * 40)
        later = {**comment, "id": 41, "node_id": "RC41", "updated_at": "2026-10-04T00:04:00Z",
                 "commit_id": "b" * 40}
        self.transport.values[PREFIX + "/pulls/7/comments"].append(later)
        packet = self.prepare()
        self.assertEqual([{
            "id": "review-comment:41:2026-10-04T00:04:00Z", "body": "Handle the empty input", "url": "",
            "path": "src/parser.py", "line": 12, "side": "RIGHT", "commit_id": "b" * 40}],
            packet["observation"]["feedback"])
        self.assertEqual((first["chain"], 2, 63.5),
                         (packet["chain"], self.ledger()["chains"][0]["rounds"],
                          state.chain_spend(self.ledger()["chains"][0])))
        self.assertEqual({"outcome": "waiting", "taskId": "TASK2"}, self.settle(packet))
        self.assertIsNone(self.prepare())
        self.assertEqual(history, self.ledger()["chains"][0]["operations"][0])
        self.assertEqual(2, len(self.task_writes()))

    def takeover(self, control):
        first = self.start()
        value = self.transport.values[PREFIX + "/pulls/7"]
        if control == "closed":
            value["state"] = "closed"
        else:
            value["labels"] = [] if control == "removed" else [{"name": "shepherd-hands-off"}]
        chain_state = "closed" if control == "closed" else "hands-off"
        for _ in range(2):
            self.clock.advance(minutes=2)
            self.assertIsNone(self.prepare())
            chain = self.ledger()["chains"][0]
            self.assertEqual((first["chain"], chain_state, 1, 500),
                             (chain["id"], chain["state"], chain["rounds"], state.chain_spend(chain)))
            self.assertEqual(first["operation"], chain["operations"][0]["id"])
            self.assertEqual(1, state.worker_slots(self.ledger()))
        self.finish()
        self.assertIsNone(self.prepare())
        chain = self.ledger()["chains"][0]
        self.assertEqual((chain_state, 1, 3.5, 0),
                         (chain["state"], chain["rounds"], state.chain_spend(chain), state.worker_slots(self.ledger())))
        history = deepcopy(chain["operations"][0])
        value["state"] = "open"
        value["labels"] = [{"name": "shepherd-adopted"}]
        packet = self.prepare()
        chain = self.ledger()["chains"][0]
        self.assertEqual((first["chain"], "open", 2, 0, 33.5),
                         (chain["id"], chain["state"], chain["rounds"], chain["localAttempts"],
                          state.chain_spend(chain)))
        self.assertEqual(first["chain"], packet["chain"])
        self.assertEqual(history, chain["operations"][0])
        self.assertEqual(1, len(self.task_writes()))
        self.assertTrue(all(method == "PATCH" and endpoint == PREFIX + "/issues/comments/500"
                            or method == "POST" and endpoint == TASKS
                            for method, endpoint, _ in self.transport.writes))

    def test_human_hands_off_stops_new_work_but_reconciles_active_worker_billing(self):
        self.takeover("hands-off")

    def test_removed_adoption_stops_new_work_without_resetting_readoption_budget(self):
        self.takeover("removed")

    def test_closed_pr_stops_new_work_without_claiming_active_worker_cancelled(self):
        self.takeover("closed")

    def test_lost_send_response_retains_unknown_slot_without_discovering_or_retrying_orphan(self):
        self.transport.lose_send_response = True
        packet = self.prepare()
        self.assertEqual({"outcome": "uncertain", "taskId": None}, self.settle(packet))
        original = self.ledger()["chains"][0]
        self.assertEqual(("uncertain", "unknown", None, 2, 498),
                         tuple(original["operations"][0][key] for key in (
                             "state", "workerState", "taskId", "nativeActual", "workerReserved")))
        for expected_spend in (500, 498, 498):
            self.clock.advance(hours=12)
            self.transport.reads.clear()
            self.assertIsNone(self.prepare())
            self.assertEqual(original, self.ledger()["chains"][0])
            self.assertEqual(1, state.worker_slots(self.ledger()))
            self.assertEqual(expected_spend, state.repository_spend(self.ledger(), self.clock()))
            self.assertEqual([], [endpoint for _, endpoint, _ in self.transport.reads if "/tasks" in endpoint])
        self.assertEqual(1, len(self.task_writes()))

    def test_lost_completed_receipt_holds_known_history_until_same_saved_id_is_verified(self):
        first = self.start()
        self.finish()
        self.check("a" * 40, "success")
        self.assertIsNone(self.prepare())
        settled = deepcopy(self.ledger()["chains"][0]["operations"][0])
        task = self.transport.values.pop(TASKS + "/TASK1")
        for _ in range(2):
            self.transport.reads.clear()
            self.assertIsNone(self.prepare())
            operation = self.ledger()["chains"][0]["operations"][0]
            self.assertEqual(("waiting", "unknown", 1.5, 466.5, 2, first["operation"]),
                             tuple(operation[key] for key in (
                                 "state", "workerState", "workerActual", "workerReserved", "nativeActual", "id")))
            self.assertEqual((1, 500, 1), (self.ledger()["chains"][0]["rounds"],
                                          state.chain_spend(self.ledger()["chains"][0]),
                                          state.worker_slots(self.ledger())))
            self.assertIn("saved task receipt unavailable", self.errors)
            self.assertEqual([TASKS + "/TASK1"],
                             [endpoint for _, endpoint, _ in self.transport.reads if "/tasks" in endpoint])
        self.transport.values[TASKS + "/TASK1"] = task
        self.assertIsNone(self.prepare())
        self.assertEqual(settled, self.ledger()["chains"][0]["operations"][0])
        self.assertEqual(1, len(self.task_writes()))

    def test_completed_worker_with_unknown_usage_keeps_reservation_across_restarts_and_days(self):
        first = self.start()
        self.finish(usage=None)
        self.check("a" * 40, "success")
        for _ in range(3):
            self.clock.advance(hours=12)
            self.assertIsNone(self.prepare())
            chain = self.ledger()["chains"][0]
            self.assertEqual((1, 500, 0), (chain["rounds"], state.chain_spend(chain), state.worker_slots(self.ledger())))
            self.assertEqual(("completed", None, 498),
                             tuple(chain["operations"][0][key] for key in ("state", "workerActual", "workerReserved")))
            self.assertEqual([self.result_facts(first)], self.observed()["workerResults"])
            self.assertIn("Tracked worker finished; billing unavailable, reservation retained. No new paid repair.",
                          self.logs)
        self.finish(usage=1.5)
        self.assertIsNone(self.prepare())
        chain = self.ledger()["chains"][0]
        self.assertEqual((1, 33.5, 1.5, 0), (chain["rounds"], state.chain_spend(chain),
                                          chain["operations"][0]["workerActual"],
                                          chain["operations"][0]["workerReserved"]))
        self.assertEqual(1, len(self.task_writes()))

    def test_unknown_native_billing_survives_terminal_worker_and_can_settle_without_dispatch(self):
        packet = self.prepare()
        self.assertEqual({"outcome": "waiting", "taskId": "TASK1"}, self.settle(packet, usage=None))
        self.transport.values[PREFIX + "/issues/7/comments"] = []
        self.finish()
        for expected_spend in (31.5, 30):
            self.clock.advance(days=1)
            self.assertIsNone(self.prepare())
            chain = self.ledger()["chains"][0]
            self.assertEqual((1, 31.5, None, 30, 1.5, 0),
                             (chain["rounds"], state.chain_spend(chain), chain["operations"][0]["nativeActual"],
                              chain["operations"][0]["nativeReserved"], chain["operations"][0]["workerActual"],
                              chain["operations"][0]["workerReserved"]))
            self.assertEqual(expected_spend, state.repository_spend(self.ledger(), self.clock()))
        self.assertEqual({"outcome": "replay"}, self.settle(packet, usage=2))
        chain = self.ledger()["chains"][0]
        self.assertEqual((1, 3.5, 2, 0), (chain["rounds"], state.chain_spend(chain),
                                        chain["operations"][0]["nativeActual"], chain["operations"][0]["nativeReserved"]))
        self.assertEqual(1, len(self.task_writes()))

    def test_issue_child_artifact_enters_same_pr_loop_with_parent_history_and_budget(self):
        issue = {"id": 1008, "number": 8, "node_id": "NODE8", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}], "title": "Parser bug", "body": "Empty input fails",
                 "html_url": "https://github.com/radical/aspire/issues/8"}
        self.transport.values[PREFIX + "/issues"] = [issue]
        self.transport.values[PREFIX + "/issues/8"] = issue
        first = self.prepare()
        self.assertEqual(("issue", 8, [], []),
                         (first["observation"]["kind"], first["observation"]["number"],
                          first["observation"]["feedback"], first["observation"]["workerResults"]))
        self.assertEqual({"outcome": "waiting", "taskId": "TASK1"}, self.settle(first))
        issue_request = self.task_writes()[0][2]
        self.assertEqual({"prompt", "base_ref", "create_pull_request"}, set(issue_request))
        self.assertTrue(issue_request["create_pull_request"])
        self.assertIsNone(self.prepare())
        child = pr(9)
        self.transport.values[PREFIX + "/pulls"] = [child]
        self.transport.values[PREFIX + "/pulls/9"] = child
        self.transport.values[PREFIX + "/git/ref/heads/fix-9"] = {
            "ref": "refs/heads/fix-9", "object": {"sha": "a" * 40}}
        self.transport.values[PREFIX + "/issues"].append(dict(child, pull_request={}))
        task = self.finish()
        task["artifacts"] = [
            {"provider": "github", "type": "pull", "data": {"id": child["id"], "global_id": child["node_id"]}},
            {"provider": "github", "type": "branch", "data": {"head_ref": "fix-9", "base_ref": "main"}}]
        self.check("a" * 40, "failure")
        self.transport.reads.clear()
        packet = self.prepare()
        chain = self.ledger()["chains"][0]
        self.assertEqual((1, first["chain"], 8, "issue", 9, "NODE9", "confirmed", 2, 0, True, 33.5),
                         (len(self.ledger()["chains"]), chain["id"], chain["origin"], chain["kind"],
                          chain["child"], chain["childNode"], chain["childAdoption"], chain["rounds"],
                          chain["localAttempts"], chain["escalated"], state.chain_spend(chain)))
        self.assertEqual((first["chain"], "pr", 9, "fix-9"),
                         (packet["chain"], packet["observation"]["kind"], packet["observation"]["number"],
                          packet["observation"]["headRef"]))
        self.assertEqual([{
            "operation": first["operation"], "taskId": "TASK1", "state": "completed", "sessionCount": 1,
            "sessionIds": ["SESSION1"], "updatedAt": "2026-10-04T00:02:00Z", "sessionStates": ["completed"],
            "sourceHead": first["observation"]["head"], "artifactState": "reported", "errors": [],
            "errorsTruncated": False, "narrativeAvailable": False, "currentHead": "a" * 40,
            "headChanged": None}], packet["observation"]["workerResults"])
        reads = [endpoint.split("?")[0] for _, endpoint, _ in self.transport.reads]
        self.assertIn(PREFIX + "/pulls", reads)
        self.assertIn(PREFIX + "/pulls/9", reads)
        self.assertIn(PREFIX + "/git/ref/heads/fix-9", reads)
        history = deepcopy(chain["operations"][0])
        self.assertEqual({"outcome": "waiting", "taskId": "TASK2"}, self.settle(packet))
        self.assertEqual((False, "fix-9", "main"),
                         tuple(self.task_writes()[1][2][key] for key in (
                             "create_pull_request", "head_ref", "base_ref")))
        self.transport.reads.clear()
        self.assertIsNone(self.prepare())
        chain = self.ledger()["chains"][0]
        self.assertEqual((1, 2, 500), (len(self.ledger()["chains"]), chain["rounds"], state.chain_spend(chain)))
        self.assertEqual(history, chain["operations"][0])
        self.assertEqual([TASKS + "/TASK1", TASKS + "/TASK2"],
                         [endpoint for _, endpoint, _ in self.transport.reads if "/tasks" in endpoint])
        self.assertEqual(2, len(self.task_writes()))


if __name__ == "__main__":
    unittest.main()
