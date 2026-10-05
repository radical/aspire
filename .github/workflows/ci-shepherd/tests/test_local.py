import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import FakePilotProcess, WorkspaceTest
import test_pilot
import decision_server
import local
import pilot
import pilot_state as state
import reasoning
import round as contracts


class LocalTests(WorkspaceTest, unittest.TestCase):
    def packet(self):
        return {"schemaVersion": 1, "kind": "pilot", "packetId": "packet-1", "operation": "operation-1",
                "target": "upstream-20722", "lane": "cloud", "context": None,
                "observation": {"feedback": [{"id": "feedback-1", "body": "${{ untrusted text }}"}]}}

    def test_each_decision_has_fresh_session_home_and_only_the_actions_prompt_and_tool(self):
        process = FakePilotProcess()
        for index in range(2):
            local.execute(self.work / str(index), self.packet(), "fixture-token", process=process)
        launches = process.launches
        self.assertNotEqual(*(argv[argv.index("--session-id") + 1] for argv, _ in launches))
        self.assertNotEqual(*(kwargs["env"]["HOME"] for _, kwargs in launches))
        for argv, kwargs in launches:
            self.assertEqual(local.prompt(self.packet()), argv[argv.index("--prompt") + 1])
            self.assertEqual(["safeoutputs-submit_decision"],
                             argv[argv.index("--available-tools") + 1:argv.index("--allow-tool")])
            self.assertEqual("safeoutputs(submit_decision)", argv[argv.index("--allow-tool") + 1])
            self.assertEqual("30", argv[argv.index("--max-ai-credits") + 1])
            self.assertNotIn("--resume", argv)
            self.assertNotIn("--continue", argv)
            self.assertNotIn("GH_TOKEN", kwargs["env"])
            self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
            self.assertNotIn("COPILOT_CUSTOM_INSTRUCTIONS_DIRS", kwargs["env"])
            self.assertIn("--no-custom-instructions", argv)
            self.assertIn("--no-remote-export", argv)
            self.assertEqual("0", kwargs["env"]["GH_AW_HARNESS_MAX_RETRIES"])
            self.assertEqual(600, kwargs["timeout"])
        self.assertEqual(2, pilot.native_usage(self.work / "0" / "usage.json"))

    def test_resumed_or_wrong_version_native_evidence_is_rejected_without_losing_billing(self):
        for key, value in (("alreadyInUse", True), ("copilotVersion", "other")):
            with self.subTest(key=key):
                def mutate(events):
                    events[0]["data"][key] = value
                    return events
                directory = self.work / key
                with self.assertRaises(ValueError):
                    local.execute(directory, self.packet(), "fixture-token", process=FakePilotProcess(mutate))
                self.assertEqual(2, pilot.native_usage(directory / "usage.json"))

    def test_native_failure_keeps_actual_usage_and_does_not_retry(self):
        process = FakePilotProcess(returncode=1)
        with self.assertRaisesRegex(ValueError, "exited 1"):
            local.execute(self.work / "failed", self.packet(), "fixture-token", process=process)
        self.assertEqual(1, len(process.launches))
        self.assertEqual(2, pilot.native_usage(self.work / "failed" / "usage.json"))

    def test_usage_checkpoint_is_nano_units_monotonic_and_never_missing_as_zero(self):
        self.assertIsNone(local.checkpoint_usage([]))
        events = [{"type": "session.usage_checkpoint", "data": {"totalNanoAiu": value}}
                  for value in (1000000000, 4481190000)]
        self.assertEqual(4.48119, local.checkpoint_usage(events))
        self.assertEqual(4.48119, local.checkpoint_usage([
            {"type": "session.usage_checkpoint", "data": {"totalNanoAiu": "4481190000"}}]))
        with self.assertRaisesRegex(ValueError, "backwards"):
            local.checkpoint_usage(list(reversed(events)))
        for invalid in (-1, True, "", "NaN"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                local.checkpoint_usage([{"type": "session.usage_checkpoint", "data": {"totalNanoAiu": invalid}}])

    def test_hosted_workflow_must_be_disabled_and_all_runs_terminal(self):
        def reader(path, token, **kwargs):
            return [{"total_count": 1, "workflow_runs": [{"id": 1, "status": "completed"}]}] if kwargs else {"state": "disabled_manually"}
        local.require_idle_actions("fixture-token", reader=reader)
        for active in ("queued", "in_progress", "waiting", None):
            with self.subTest(active=active), self.assertRaises(ValueError):
                local.require_idle_actions("fixture-token", reader=lambda path, token, **kwargs:
                                           [{"total_count": 1, "workflow_runs": [{"id": 1, "status": active}]}] if kwargs
                                           else {"state": "disabled_manually"})
        with self.assertRaisesRegex(ValueError, "disable"):
            local.require_idle_actions("fixture-token", reader=lambda *_: {"state": "active"})
        with self.assertRaisesRegex(ValueError, "unavailable"):
            local.require_idle_actions("fixture-token", reader=lambda path, token, **kwargs:
                                       [] if kwargs else {"state": "disabled_manually"})
        for pages in (
            [{"total_count": 2, "workflow_runs": [{"id": 1, "status": "completed"}]}],
            [{"total_count": 2, "workflow_runs": [{"id": 1, "status": "completed"}]},
             {"total_count": 2, "workflow_runs": [{"id": 1, "status": "completed"}]}],
            [{"total_count": 1, "workflow_runs": []}, {"total_count": 0, "workflow_runs": []}],
        ):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                local.require_idle_actions("fixture-token", reader=lambda path, token, **kwargs:
                                           pages if kwargs else {"state": "disabled_manually"})

    @unittest.skipUnless(os.name == "posix", "POSIX local pilot")
    def test_local_lock_rejects_second_controller_and_releases_on_failure(self):
        with self.assertRaisesRegex(ValueError, "second"):
            with local.authority_lock(self.work / "locks", 127):
                with self.assertRaisesRegex(ValueError, "another local"):
                    with local.authority_lock(self.work / "locks", 127):
                        self.fail("second controller acquired the same authority")
                raise ValueError("second check")
        with local.authority_lock(self.work / "locks", 127):
            pass

    def test_stdio_decision_tool_matches_actions_and_rejects_a_second_submission(self):
        call = {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "submit_decision", "arguments": {"decision": '{"action":"cloud"}'},
                           "_meta": {"progressToken": 0}}}
        output = self.work / "decision.json"
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, call, {**call, "id": 4},
        ]
        result = subprocess.run(
            [sys.executable, str(Path(decision_server.__file__)), "--output", str(output)],
            input="\n".join(json.dumps(request) for request in requests) + "\n",
            capture_output=True, text=True, check=True)
        responses = reasoning.jsonl(result.stdout)
        self.assertEqual([1, 2, 3, 4], [response["id"] for response in responses])
        self.assertEqual(["submit_decision"], [tool["name"] for tool in responses[1]["result"]["tools"]])
        self.assertFalse(responses[2]["result"]["isError"])
        self.assertIn("error", responses[3])
        self.assertEqual({"action": "cloud"}, contracts.read_json(output))

    def test_unknown_tools_and_duplicate_decision_json_are_rejected(self):
        for params in (
            {"name": "shell", "arguments": {"command": "anything"}},
            {"name": "submit_decision", "arguments": {"decision": '{"action":1,"action":2}'}},
            {"name": "submit_decision", "arguments": {"decision": '{"action":"human"}'}, "_meta": []},
            {"name": "submit_decision", "arguments": {"decision": '{"action":"human"}'}, "authorization": True},
        ):
            with self.subTest(params=params), self.assertRaises(ValueError):
                decision_server.handle({"method": "tools/call", "params": params}, self.work / "decision.json")
        self.assertFalse((self.work / "decision.json").exists())

    def test_shared_core_settles_local_failure_before_any_worker_send(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.api.token = "fixture-token"
        fixture.api.enabled = lambda: True
        def fail(directory, packet, token):
            directory.mkdir()
            contracts.write_json(directory / "usage.json", {"ai_credits": 3})
            raise ValueError("native failed")
        with patch.object(local.live, "clock", fixture.clock), patch.object(fixture.api, "publish_status"):
            result = local.sweep(fixture.api, self.work / "sweep", "b" * 40, executor=fail)
        self.assertEqual("failed", result["outcome"])
        operation = fixture.api.ledger["chains"][0]["operations"][0]
        self.assertEqual((3, 0), (operation["nativeActual"], operation["nativeReserved"]))
        self.assertEqual([], [write for write in fixture.transport.writes if write[1].endswith("/tasks")])

    def test_unchanged_wait_never_constructs_a_decision_agent(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.api.token = "fixture-token"
        fixture.api.enabled = lambda: True
        fixture.transport.values["repos/radical/aspire/issues/7/comments"] = []
        with patch.object(local.live, "clock", fixture.clock), patch.object(fixture.api, "publish_status"):
            result = local.sweep(fixture.api, self.work / "wait", "b" * 40,
                                 executor=lambda *_: self.fail("waiting sweep must not infer"))
        self.assertEqual("waiting; no inference", result["outcome"])
        self.assertEqual(0, fixture.api.ledger["chains"][0]["rounds"])

    def test_missing_native_usage_is_retained_not_refunded_after_failure(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.api.token = "fixture-token"
        fixture.api.enabled = lambda: True
        def fail(*_):
            raise ValueError("no native receipt")
        with patch.object(local.live, "clock", fixture.clock), patch.object(fixture.api, "publish_status"):
            result = local.sweep(fixture.api, self.work / "unknown", "b" * 40, executor=fail)
        self.assertEqual("failed", result["outcome"])
        operation = fixture.api.ledger["chains"][0]["operations"][0]
        self.assertIsNone(operation["nativeActual"])
        self.assertEqual(30, operation["nativeReserved"])
        self.assertEqual([], [write for write in fixture.transport.writes if write[1].endswith("/tasks")])

    def test_source_drift_rejects_an_effect_without_blocking_billing_authority_guard(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.transport.values["repos/microsoft/aspire"] = {
            "id": 696529789, "full_name": "microsoft/aspire", "default_branch": "main"}
        fixture.transport.comments[0]["body"] = state.render(state.new_ledger("microsoft/aspire"))
        with patch.object(local.github, "PilotTransport", return_value=fixture.transport):
            api = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True, revision="b" * 40)
        api.read_authority()
        with patch.object(local, "command", side_effect=["b" * 40, " M local.py"]), \
                patch.object(local, "require_idle_actions"), patch.object(api, "enabled", return_value=True):
            with self.assertRaisesRegex(ValueError, "source changed"):
                api.guard({}, {})
            api.ledger["cursor"] = 1
            api.persist()
        self.assertEqual(1, state.parse(fixture.transport.comments[0]["body"])["cursor"])


if __name__ == "__main__":
    unittest.main()
