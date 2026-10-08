import base64
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import FakePilotProcess, WorkspaceTest
import test_pilot
import test_pilot_github
import test_pilot_reminders
import decision_server
import local
import pilot
import pilot_binding as bindings
import pilot_patch
import pilot_reminders as reminders
import pilot_state as state
import reasoning
import round as contracts
from helpers import result_capable


class LocalTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.use_native_cli()

    def test_version_preflight_rejects_node_shim_without_running_it(self):
        shim = self.work / "bin" / "copilot"
        shim.write_text("#!/usr/bin/env node\n")
        stderr = io.StringIO()
        with patch.object(local, "command", side_effect=self.command) as commands, redirect_stderr(stderr):
            result = local.main(["run", "--target", "fork", "--tracker", "99", "--authority", "500",
                                 "--tracker-node", "TRACKER99", "--workdir", str(self.work / "run")])
        self.assertEqual(1, result)
        self.assertIn("native Copilot CLI executable", stderr.getvalue())
        self.assertEqual(["gh", "git", "git"], [call.args[0][0] for call in commands.call_args_list])

    def test_report_failure_preserves_primary_controller_error(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        stderr = io.StringIO()
        with patch.object(local, "run_sweep", side_effect=ValueError("authority publication uncertain")), \
                patch.object(Path, "write_text", side_effect=PermissionError("report denied")), \
                redirect_stderr(stderr), self.assertRaisesRegex(ValueError, "authority publication uncertain"):
            local.sweep(fixture.api, self.work / "report-failure", "b" * 40)
        self.assertIn("report denied", stderr.getvalue())

    def test_guard_failure_saves_report_and_preserves_exception_without_retry(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.api.enabled = lambda: True
        with patch.object(local.pilot, "prepare", side_effect=ValueError("source changed")) as prepare, \
                self.assertRaisesRegex(ValueError, "source changed"):
            local.sweep(fixture.api, self.work / "guard", "b" * 40,
                        executor=lambda *_: self.fail("must not infer"))
        self.assertEqual(1, prepare.call_count)
        report = (self.work / "guard" / "report.md").read_text()
        self.assertIn("Outcome: **failed**", report)
        self.assertIn("source changed", report)

    def test_api_contract_check_uses_readonly_controller_without_inference_or_tracking_changes(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.transport.values["repos/radical/aspire/pulls/7/comments"] = [{
            "id": 31, "node_id": "COMMENT31", "body": "Fix normalization",
            "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}]
        fixture.transport.resolved_reviews.add(31)
        before = fixture.transport.comments[0]["body"]
        output = io.StringIO()
        with patch.object(local, "command", side_effect=self.command), \
                patch.object(local.github, "PilotTransport", return_value=fixture.transport), \
                redirect_stdout(output):
            result = local.main(["check-api", "--target", "fork", "--pr", "7",
                                 "--tracker", "99", "--authority", "500", "--tracker-node", "TRACKER99",
                                 "--workdir", str(self.work / "contract")])
        self.assertEqual(0, result)
        self.assertEqual({"outcome": "api contract verified; read-only", "repository": "radical/aspire",
                          "number": 7, "head": "a" * 40, "reviewComments": 1, "resolved": [31]},
                         json.loads(output.getvalue()))
        self.assertEqual(before, fixture.transport.comments[0]["body"])
        self.assertEqual([], fixture.transport.writes)

    def test_api_contract_failure_cannot_pass_as_a_paused_observation(self):
        for change in ("schema unavailable", "empty inventory", "incomplete inventory", "stale head", "head moved"):
            with self.subTest(change=change):
                fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                fixture.transport.values["repos/radical/aspire/pulls/7/comments"] = [{
                    "id": 31, "node_id": "COMMENT31", "body": "Fix normalization",
                    "updated_at": "2026-10-04T00:00:00Z", "user": {"id": 1472, "login": "radical"}}]
                if change == "empty inventory":
                    fixture.transport.values["repos/radical/aspire/pulls/7/comments"] = []
                before = fixture.transport.comments[0]["body"]

                def reader(method, endpoint, body):
                    response = fixture.transport(method, endpoint, body)
                    if endpoint == "graphql":
                        if change == "schema unavailable":
                            return local.github.Response(
                                {"errors": [{"type": "undefinedField", "fieldName": "thread"}]}, {})
                        if change == "incomplete inventory":
                            response.payload["data"]["repository"]["pullRequest"]["reviewThreads"]["nodes"] = []
                        if change == "stale head":
                            response.payload["data"]["repository"]["pullRequest"]["headRefOid"] = "b" * 40
                        if change == "head moved":
                            fixture.transport.values["repos/radical/aspire/pulls/7"]["head"]["sha"] = "b" * 40
                    return response

                stdout, stderr = io.StringIO(), io.StringIO()
                with patch.object(local, "command", side_effect=self.command) as commands, \
                        patch.object(local.github, "PilotTransport", return_value=reader) as transport, \
                        patch.object(local, "execute", side_effect=AssertionError("must not infer")), \
                        redirect_stdout(stdout), redirect_stderr(stderr):
                    result = local.main(["check-api", "--target", "fork", "--pr", "7",
                                         "--tracker", "99", "--authority", "500", "--tracker-node", "TRACKER99",
                                         "--workdir", str(self.work / "contract")])
                self.assertEqual(1, result)
                self.assertEqual("", stdout.getvalue())
                self.assertTrue(stderr.getvalue().startswith("CI Shepherd local stopped: "))
                self.assertFalse(transport.call_args.kwargs["write"])
                self.assertEqual(["gh", "auth", "token", "--hostname", "github.com", "--user", "radical"],
                                 commands.call_args_list[0].args[0])
                self.assertEqual(["gh", "git"], [call.args[0][0] for call in commands.call_args_list])
                self.assertEqual(before, fixture.transport.comments[0]["body"])
                self.assertEqual([], fixture.transport.writes)

    def test_api_contract_requires_explicit_closed_subject_before_credentials(self):
        common = ["--tracker", "99", "--authority", "500", "--tracker-node", "TRACKER99",
                  "--workdir", str(self.work)]
        for arguments in (["check-api"], ["check-api", "--pr", "0"], ["check-api", "--pr", "121"],
                          ["check-api", "--pr", "7"], ["observe", "--pr", "20722"]):
            with self.subTest(arguments=arguments), \
                    patch.object(local, "command", side_effect=AssertionError("must not authenticate")):
                with self.assertRaises(SystemExit) as stopped:
                    local.main(arguments + common)
                self.assertEqual(2, stopped.exception.code)

    def packet(self):
        return {"schemaVersion": 1, "kind": "pilot", "packetId": "packet-1", "operation": "operation-1",
                "target": "upstream-20722", "lane": "cloud", "context": None,
                "observation": {"feedback": [{"id": "feedback-1", "body": "${{ untrusted text }}"}]}}

    def command(self, argv, **kwargs):
        if argv[:3] == ["gh", "auth", "token"]:
            return "fixture-token"
        if argv[0] == "git":
            if argv[-1] == "HEAD":
                return "b" * 40
            self.assertEqual("--porcelain", argv[-1])
            return ""
        if Path(argv[0]).name == "copilot":
            self.assertTrue(Path(argv[0]).is_absolute())
            self.assertEqual([argv[0], "--no-auto-update", "--version"], argv)
            return "GitHub Copilot CLI 1.0.93-3."
        self.assertEqual(["gh", "api", "--hostname", "github.com"], argv[:4])
        path = argv[4]
        if path.endswith("/CI_SHEPHERD_ENABLE"):
            return json.dumps({"name": "CI_SHEPHERD_ENABLE", "value": "true"})
        if path == local.WORKFLOW:
            return json.dumps({"state": "disabled_manually"})
        self.assertEqual(local.WORKFLOW + "/runs?per_page=100", path)
        return json.dumps([{"total_count": 0, "workflow_runs": []}])

    def test_each_decision_has_fresh_session_home_and_only_the_actions_prompt_and_tool(self):
        process = FakePilotProcess()
        for index in range(2):
            local.execute(self.work / str(index), self.packet(), "fixture-token", process=process)
        launches = process.launches
        self.assertNotEqual(*(argv[argv.index("--session-id") + 1] for argv, _ in launches))
        self.assertNotEqual(*(kwargs["env"]["HOME"] for _, kwargs in launches))
        for argv, kwargs in launches:
            self.assertEqual(str((self.work / "bin" / "copilot").resolve()), argv[0])
            self.assertEqual(os.defpath, kwargs["env"]["PATH"])
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

    def test_node_shim_is_rejected_before_inference(self):
        shim = self.work / "bin" / "copilot"
        shim.write_text("#!/usr/bin/env node\n")
        process = FakePilotProcess()
        with self.assertRaisesRegex(ValueError, "native Copilot CLI executable"):
            local.execute(self.work / "agent", self.packet(), "fixture-token", process=process)
        self.assertEqual([], process.launches)

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
        self.assertTrue((self.work / "sweep" / "report.md").exists(), "Every sweep must save a human report")
        report = (self.work / "sweep" / "report.md").read_text()
        self.assertIn("Outcome: **failed**", report)
        self.assertIn("Newly recorded credits: 3", report)
        self.assertIn("No new saved repair task", report)

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
        self.assertEqual("observed; no inference", result["outcome"])
        self.assertEqual("Waiting for human review / supported new feedback; no inference.",
                         result["reasons"][0]["reason"])
        self.assertEqual(0, fixture.api.ledger["chains"][0]["rounds"])
        self.assertTrue((self.work / "wait" / "report.md").exists(), "Waiting sweeps must save a human report")
        report = (self.work / "wait" / "report.md").read_text()
        self.assertIn("Outcome: **observed; no inference**", report)
        self.assertIn("Waiting for human review / supported new feedback; no inference.", report)
        self.assertIn("New action rounds: 0", report)

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

    def test_source_and_disable_guards_cover_notifications_without_blocking_billing(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.transport.values["repos/microsoft/aspire"] = {
            "id": 696529789, "full_name": "microsoft/aspire", "default_branch": "main"}
        fixture.transport.comments[0]["body"] = state.render(state.new_ledger("microsoft/aspire"))
        with patch.object(local.github, "PilotTransport", return_value=fixture.transport):
            api = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True, revision="b" * 40)
        api.read_authority()
        with patch.object(local, "command", side_effect=["b" * 40, " M local.py"] * 2), \
                patch.object(local, "require_idle_actions"), patch.object(api, "enabled", return_value=True):
            for effect in (True, False):
                with self.subTest(effect=effect), self.assertRaisesRegex(ValueError, "source changed"):
                    api.guard({}, {}, effect=effect)
            api.ledger["cursor"] = 1
            api.persist()
        self.assertEqual(1, state.parse(fixture.transport.comments[0]["body"])["cursor"])
        with patch.object(local, "require_source"), patch.object(api, "enabled", return_value=False):
            for effect in (True, False):
                with self.subTest(effect=effect), self.assertRaisesRegex(ValueError, "globally disabled"):
                    api.guard({}, {}, effect=effect)

    def test_fork_local_prepare_uses_cloud_even_for_an_inline_eligible_pr(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.transport.values["repos/radical/aspire/pulls/7/files"] = [
            {"filename": pilot_patch.SOURCE, "status": "modified"}]
        for path, content in {
            pilot_patch.SOURCE: "def normalize_label(value):\n    return value\n",
            pilot_patch.TEST: "import unittest\n",
        }.items():
            fixture.transport.values["repos/radical/aspire/contents/" + path] = {
                "type": "file", "path": path, "size": len(content.encode()), "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode()}
        with patch.object(local.github, "PilotTransport", return_value=fixture.transport):
            api = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True,
                                    revision="b" * 40, binding=bindings.FORK)
        api.clock = fixture.clock

        with patch.object(local, "command", side_effect=self.command):
            result_capable(api)
            packet = pilot.prepare(api, test_pilot.RUN, fixture.clock(), present=False)
        self.assertEqual("fork", packet["target"])
        self.assertEqual("cloud", packet["lane"])
        self.assertIsNone(packet["context"])
        chain = api.ledger["chains"][0]
        self.assertEqual((1, 0), (chain["rounds"], chain["localAttempts"]))
        self.assertEqual(30, chain["operations"][0]["nativeReserved"])

    def test_explicit_fork_observe_reads_adopted_issue_without_effects(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        issue = {"id": 1008, "number": 8, "node_id": "NODE8", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}], "title": "Bug", "body": "Broken",
                 "html_url": "https://github.com/radical/aspire/issues/8"}
        fixture.transport.values["repos/radical/aspire/issues"] = [issue]
        fixture.transport.values["repos/radical/aspire/issues/8"] = issue

        with patch.object(local, "command", side_effect=self.command), \
                patch.object(local.github, "PilotTransport", return_value=fixture.transport):
            result = local.main(["observe", "--target", "fork", "--tracker", "99", "--authority", "500",
                                 "--tracker-node", "TRACKER99", "--workdir", str(self.work / "observe")])
        self.assertEqual(0, result)
        self.assertTrue(any(endpoint.split("?")[0] == "repos/radical/aspire/issues/8"
                            for _, endpoint, _ in fixture.transport.reads))
        self.assertEqual([], fixture.transport.writes)

    def test_closed_local_targets_and_fork_resume_reject_before_credentials(self):
        common = ["--tracker", "99", "--authority", "500", "--tracker-node", "TRACKER99",
                  "--workdir", str(self.work)]
        for arguments in (
            ["observe", "--target", "another/repository"],
            ["resume", "--target", "fork", "--operation", "operation-1", "--expected-head", "a" * 40],
        ):
            with self.subTest(arguments=arguments), \
                    patch.object(local, "command", side_effect=AssertionError("must not authenticate")):
                with self.assertRaises(SystemExit) as stopped:
                    local.main(arguments + common)
                self.assertEqual(2, stopped.exception.code)

    def test_default_upstream_target_does_not_reuse_a_fork_authority(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.transport.values["repos/microsoft/aspire"] = {
            "id": 696529789, "full_name": "microsoft/aspire", "default_branch": "main"}
        with patch.object(local, "command", side_effect=self.command), \
                patch.object(local.github, "PilotTransport", return_value=fixture.transport):
            result = local.main(["observe", "--tracker", "99", "--authority", "500",
                                 "--tracker-node", "TRACKER99", "--workdir", str(self.work)])
        self.assertEqual(1, result)
        self.assertEqual([], fixture.transport.writes)

    def test_explicit_fork_run_uses_fork_authority_when_effects_are_disabled(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)

        def command(argv, **kwargs):
            if argv[:4] == ["gh", "api", "--hostname", "github.com"] and argv[4].endswith("/CI_SHEPHERD_ENABLE"):
                return json.dumps({"name": "CI_SHEPHERD_ENABLE", "value": "false"})
            return self.command(argv, **kwargs)

        directory = self.work / "disabled"
        with patch.object(local, "command", side_effect=command), \
                patch.object(local.github, "PilotTransport", return_value=fixture.transport), \
                patch.object(local.Path, "home", return_value=self.work):
            result = local.main(["run", "--target", "fork", "--tracker", "99", "--authority", "500",
                                 "--tracker-node", "TRACKER99", "--workdir", str(directory)])
        self.assertEqual(0, result)
        results = list(directory.glob("*/result.json"))
        self.assertEqual(1, len(results))
        self.assertEqual({"outcome": "disabled; billing observation only"}, contracts.read_json(results[0]))
        self.assertEqual([], fixture.transport.writes)

    def test_disabled_issue_worker_observation_records_billing_without_adopting_child(self):
        fixture = test_pilot_github.PilotGitHubTests()
        fixture.setUp()
        chain, operation, task = fixture.issue_worker()
        fixture.transport.values["repos/radical/aspire/pulls/9"]["labels"] = []
        fixture.api.token = "fixture-token"
        fixture.api.enabled = lambda: False

        result = local.sweep(fixture.api, self.work / "disabled-child", "b" * 40,
                             executor=lambda *_: self.fail("disabled sweep must not infer"))

        self.assertEqual({"outcome": "disabled; billing observation only"}, result)
        saved = state.parse(fixture.transport.comments[0]["body"])["chains"][0]
        self.assertEqual(chain["id"], saved["id"])
        self.assertEqual((1, 1), (saved["rounds"], len(saved["operations"])))
        self.assertIsNone(saved["child"])
        self.assertEqual((operation["id"], "TASK1", "completed", 1.5, 0),
                         tuple(saved["operations"][0][key]
                               for key in ("id", "taskId", "workerState", "workerActual", "workerReserved")))
        self.assertEqual([], [endpoint for method, endpoint, _ in fixture.transport.writes
                              if method == "POST"])

    def test_readonly_fork_observe_reports_terminal_issue_worker_without_remote_changes(self):
        fixture = test_pilot_github.PilotGitHubTests()
        fixture.setUp()
        chain, operation, task = fixture.issue_worker()
        fixture.transport.values["repos/radical/aspire/pulls/9"]["labels"] = []
        before = fixture.transport.comments[0]["body"]
        fixture.transport.writes.clear()

        with patch.object(local, "command", side_effect=self.command), \
                patch.object(local.github, "PilotTransport", return_value=fixture.transport):
            result = local.main(["observe", "--target", "fork", "--tracker", "99", "--authority", "500",
                                 "--tracker-node", "TRACKER99", "--workdir", str(self.work / "terminal-observe")])

        self.assertEqual(0, result)
        self.assertEqual(before, fixture.transport.comments[0]["body"])
        self.assertEqual([], fixture.transport.writes)
        self.assertTrue(any(endpoint == "agents/repos/radical/aspire/tasks/TASK1"
                            for _, endpoint, _ in fixture.transport.reads))

    def test_child_label_send_rechecks_local_source_and_global_stop_without_losing_billing(self):
        for stop in ("source changed", "globally disabled"):
            with self.subTest(stop=stop):
                fixture = test_pilot_github.PilotGitHubTests()
                fixture.setUp()
                chain, operation, task = fixture.issue_worker()
                fixture.transport.values["repos/radical/aspire/pulls/9"]["labels"] = []

                def command(argv, **kwargs):
                    if stop == "source changed" and argv[0] == "git" and argv[-1] == "--porcelain":
                        return " M .github/workflows/ci-shepherd/local.py"
                    if stop == "globally disabled" and argv[0] == "gh" and argv[4].endswith("/CI_SHEPHERD_ENABLE"):
                        return json.dumps({"name": "CI_SHEPHERD_ENABLE", "value": "false"})
                    return self.command(argv, **kwargs)

                with patch.object(local, "command", side_effect=command), \
                        patch.object(local.github, "PilotTransport", return_value=fixture.transport):
                    api = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True,
                                            revision="b" * 40, binding=bindings.FORK)
                    api.read_authority()
                    with self.assertRaisesRegex(ValueError, stop):
                        api.sweep()

                saved = state.parse(fixture.transport.comments[0]["body"])["chains"][0]
                self.assertEqual((chain["id"], 1, 1),
                                 (saved["id"], saved["rounds"], len(saved["operations"])))
                self.assertEqual((operation["id"], "TASK1", 1.5, 0),
                                 tuple(saved["operations"][0][key]
                                       for key in ("id", "taskId", "workerActual", "workerReserved")))
                self.assertEqual([], [endpoint for method, endpoint, _ in fixture.transport.writes
                                      if method == "POST"])

    def test_fresh_failed_issue_receipt_holds_unchanged_attempt_without_replacing_saved_task(self):
        fixture = test_pilot_github.PilotGitHubTests()
        fixture.setUp()
        chain, operation, task = fixture.issue_worker()
        task["state"] = task["sessions"][0]["state"] = "failed"
        task["artifacts"] = []

        with patch.object(local, "command", side_effect=self.command), \
                patch.object(local.github, "PilotTransport", return_value=fixture.transport):
            api = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True,
                                    revision="b" * 40, binding=bindings.FORK)
            packet = pilot.prepare(api, test_pilot.RUN, api.clock(), present=False)

        self.assertIsNone(packet)
        saved = state.parse(fixture.transport.comments[0]["body"])["chains"][0]
        self.assertEqual((chain["id"], 1, 1), (saved["id"], saved["rounds"], len(saved["operations"])))
        self.assertIsNone(saved["child"])
        self.assertEqual((operation["id"], "TASK1", "failed", 2, 1.5, 0),
                         tuple(saved["operations"][0][key]
                               for key in ("id", "taskId", "workerState", "nativeActual", "workerActual", "workerReserved")))
        self.assertEqual([], [endpoint for method, endpoint, _ in fixture.transport.writes if method == "POST"])

    def test_failed_issue_with_unknown_worker_cost_does_not_reserve_another_decision(self):
        fixture = test_pilot_github.PilotGitHubTests()
        fixture.setUp()
        chain, operation, task = fixture.issue_worker()
        task["state"] = task["sessions"][0]["state"] = "failed"
        task["sessions"][0]["usage"] = None
        task["artifacts"] = []

        with patch.object(local, "command", side_effect=self.command), \
                patch.object(local.github, "PilotTransport", return_value=fixture.transport):
            api = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True,
                                    revision="b" * 40, binding=bindings.FORK)
            packet = pilot.prepare(api, test_pilot.RUN, api.clock(), present=False)

        self.assertIsNone(packet)
        saved = state.parse(fixture.transport.comments[0]["body"])["chains"][0]
        self.assertEqual((chain["id"], 1, 1), (saved["id"], saved["rounds"], len(saved["operations"])))
        self.assertIsNone(saved["child"])
        self.assertEqual((operation["id"], "TASK1", "failed", None, 498),
                         tuple(saved["operations"][0][key]
                               for key in ("id", "taskId", "workerState", "workerActual", "workerReserved")))
        self.assertEqual([], [endpoint for method, endpoint, _ in fixture.transport.writes if method == "POST"])

    def test_failed_issue_with_unmappable_reported_child_retains_verified_billing(self):
        fixture = test_pilot_github.PilotGitHubTests()
        fixture.setUp()
        chain, operation, task = fixture.issue_worker()
        task["state"] = task["sessions"][0]["state"] = "failed"
        fixture.transport.values["repos/radical/aspire/git/ref/heads/fix-9"]["object"]["sha"] = "b" * 40
        fixture.api.token = "fixture-token"
        fixture.api.enabled = lambda: True

        with self.assertRaisesRegex(ValueError, "independent child branch mapping mismatch"):
            local.sweep(fixture.api, self.work / "unmapped-artifact", "b" * 40,
                        executor=lambda *_: self.fail("unverified child must not infer"))

        saved = state.parse(fixture.transport.comments[0]["body"])["chains"][0]
        self.assertEqual((chain["id"], 1, 1), (saved["id"], saved["rounds"], len(saved["operations"])))
        self.assertIsNone(saved["child"])
        self.assertEqual((operation["id"], "TASK1", "failed", "failed", 2, 1.5, 0),
                         tuple(saved["operations"][0][key]
                               for key in ("id", "taskId", "state", "workerState",
                                           "nativeActual", "workerActual", "workerReserved")))
        self.assertEqual([], [endpoint for method, endpoint, _ in fixture.transport.writes if method == "POST"])

    def test_local_issue_handoff_reminder_passes_real_guard_and_dedupes_after_restart(self):
        fixture = test_pilot_reminders.ReminderTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        base, transport, chain = fixture.setup_issue_api()
        observed = base.observe(chain)
        operation = state.reserve(base.ledger, chain, local.github.fingerprint(observed) + ":round:1",
                                  base.clock(), local=False)
        state.settle_native(operation, 2)
        operation["sessionId"] = "NATIVE-HANDOFF"
        state.finish(operation, "completed")
        chain["state"] = "human"
        base.persist()

        with patch.object(local, "command", side_effect=self.command), \
                patch.object(local.github, "PilotTransport", return_value=base.transport):
            api = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True,
                                    revision="b" * 40, binding=bindings.FORK)
            api.clock = base.clock
            api.read_authority()
            current = api.ledger["chains"][0]
            reminders.process(api, current, api.observe(current), api.clock())
            api.clock.advance(seconds=59)
            reminders.process(api, current, api.observe(current), api.clock())
            self.assertEqual([], fixture.posts)
            api.clock.advance(seconds=1)
            reminders.process(api, current, api.observe(current), api.clock())
            self.assertEqual(1, len(fixture.posts))

            fresh = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True,
                                      revision="b" * 40, binding=bindings.FORK)
            fresh.clock = api.clock
            fresh.read_authority()
            restarted = fresh.ledger["chains"][0]
            reminders.process(fresh, restarted, fresh.observe(restarted), fresh.clock())
        self.assertEqual(1, len(fixture.posts))
        self.assertEqual(("confirmed", 1, 1),
                         (restarted["reminder"]["sendState"], restarted["rounds"], len(restarted["operations"])))
        self.assertEqual(2, restarted["operations"][0]["nativeActual"])
        self.assertTrue(reminders.valid_body(fixture.posts[0], fresh.repository, chain["origin"]))

    def test_local_unconfirmed_child_reminder_posts_only_to_origin_without_label_retry(self):
        fixture = test_pilot_reminders.ReminderTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        base, transport, chain = fixture.setup_issue_api()
        child = test_pilot_github.pr(9)
        child["labels"] = []
        transport.values[f"{base.prefix}/pulls/9"] = child
        state.bind_child(base.ledger, chain, 9, child["node_id"])
        chain.update(childAdoption="sent", state="human")
        base.persist()
        transport.writes.clear()

        with patch.object(local, "command", side_effect=self.command), \
                patch.object(local.github, "PilotTransport", return_value=base.transport):
            api = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True,
                                   revision="b" * 40, binding=bindings.FORK)
            api.clock = base.clock
            api.read_authority()
            current = api.ledger["chains"][0]
            reminders.process(api, current, api.observe(current), api.clock())
            api.clock.advance(seconds=60)
            reminders.process(api, current, api.observe(current), api.clock())
            reminders.process(api, current, api.observe(current), api.clock())

        self.assertEqual(1, len(fixture.posts))
        self.assertEqual("confirmed", current["reminder"]["sendState"])
        self.assertEqual(("human", "sent", 0, []),
                          (current["state"], current["childAdoption"], current["rounds"], current["operations"]))
        self.assertEqual([], [endpoint for method, endpoint, _ in transport.writes
                              if method == "POST" and endpoint.endswith("/labels")])
        self.assertEqual(1, len(transport.values[f"{base.prefix}/issues/8/comments"]))
        self.assertEqual([], transport.values.get(f"{base.prefix}/issues/9/comments", []))

    def test_local_adoption_reminder_rechecks_takeover_after_adoption_read(self):
        for change in ("origin removed", "origin hands-off", "origin identity", "child closed", "child hands-off"):
            with self.subTest(change=change):
                fixture = test_pilot_reminders.ReminderTests()
                fixture.setUp()
                self.addCleanup(fixture.doCleanups)
                base, transport, chain = fixture.setup_issue_api()
                child = test_pilot_github.pr(9)
                child["labels"] = []
                transport.values[f"{base.prefix}/pulls/9"] = child
                state.bind_child(base.ledger, chain, 9, child["node_id"])
                chain.update(childAdoption="sent", state="human")
                base.persist()

                def command(argv, **kwargs):
                    if argv[0] == "gh" and argv[4].endswith("/CI_SHEPHERD_ENABLE"):
                        origin = transport.values[f"{base.prefix}/issues/8"]
                        if change == "origin removed":
                            origin["labels"] = []
                        elif change == "origin hands-off":
                            origin["labels"].append({"name": "shepherd-hands-off"})
                        elif change == "origin identity":
                            origin["node_id"] = "REPLACED"
                        elif change == "child closed":
                            child["state"] = "closed"
                        else:
                            child["labels"] = [{"name": "shepherd-hands-off"}]
                    return self.command(argv, **kwargs)

                with patch.object(local, "command", side_effect=command), \
                        patch.object(local.github, "PilotTransport", return_value=base.transport):
                    api = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True,
                                            revision="b" * 40, binding=bindings.FORK)
                    api.clock = base.clock
                    api.read_authority()
                    current = api.ledger["chains"][0]
                    reminders.process(api, current, api.observe(current), api.clock())
                    api.clock.advance(seconds=60)
                    reminders.process(api, current, api.observe(current), api.clock())

                self.assertEqual([], fixture.posts)
                self.assertEqual("observed", current["reminder"]["sendState"])
                self.assertEqual(0, current["rounds"])

    def test_fork_issue_sweep_dispatches_and_tracks_one_worker_across_restart(self):
        fixture = test_pilot.PilotTests("test_unchanged_wait_does_not_reserve_native")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        issue = {"id": 1008, "number": 8, "node_id": "NODE8", "state": "open",
                 "labels": [{"name": "shepherd-adopted"}], "title": "Bug", "body": "Broken",
                 "html_url": "https://github.com/radical/aspire/issues/8"}
        fixture.transport.values["repos/radical/aspire/issues"] = [issue]
        fixture.transport.values["repos/radical/aspire/issues/8"] = issue
        task_path = "agents/repos/radical/aspire/tasks/OWNED8"

        def transport(method, endpoint, body):
            if method == "POST" and endpoint == "agents/repos/radical/aspire/tasks":
                task = {"id": "OWNED8", "state": "in_progress", "repository": {"id": 746880239},
                        "creator": {"id": 1472}, "session_count": 1, "artifacts": [],
                        "updated_at": "2026-10-04T00:00:00Z",
                        "sessions": [{"id": "SESSION8", "task_id": "OWNED8", "state": "in_progress",
                                      "repository": {"id": 746880239}, "user": {"id": 1472},
                                      "base_ref": "main", "head_ref": "copilot/fix-issue-8",
                                      "prompt": body["prompt"], "usage": None}]}
                fixture.transport.writes.append((method, endpoint, body))
                fixture.transport.values[task_path] = task
                return local.github.Response(task, {}, 201)
            if method == "POST" and endpoint == "repos/radical/aspire/issues/8/comments":
                comment = {"id": 600, "user": {"id": 1472, "login": "radical"}, "body": body["body"],
                           "updated_at": "2026-10-04T00:00:00Z"}
                fixture.transport.writes.append((method, endpoint, body))
                fixture.transport.values[endpoint] = [comment]
                fixture.transport.values["repos/radical/aspire/issues/comments/600"] = comment
                return local.github.Response(comment, {}, 201)
            if method == "PATCH" and endpoint == "repos/radical/aspire/issues/comments/600":
                comment = fixture.transport.values[endpoint]
                comment["body"] = body["body"]
                fixture.transport.writes.append((method, endpoint, body))
                return local.github.Response(comment, {}, 200)
            return fixture.transport(method, endpoint, body)

        process = FakePilotProcess()
        with patch.object(local, "command", side_effect=self.command), \
                patch.object(local.github, "PilotTransport", return_value=transport), \
                patch.object(local.live, "clock", fixture.clock):
            api = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True,
                                    revision="b" * 40, binding=bindings.FORK)
            api.clock = fixture.clock
            result_capable(api)
            result = local.sweep(
                api, self.work / "first", "b" * 40,
                executor=lambda directory, packet, token: local.execute(directory, packet, token, process=process))
            self.assertEqual({"outcome": "waiting", "taskId": "OWNED8"}, result)
            first = state.parse(fixture.transport.comments[0]["body"])
            fresh = local.LocalGitHub("fixture-token", 99, 500, "TRACKER99", write=True,
                                      revision="b" * 40, binding=bindings.FORK)
            fresh.clock = fixture.clock
            result_capable(fresh)
            second = local.sweep(fresh, self.work / "second", "b" * 40,
                                 executor=lambda *_: self.fail("active saved worker must not infer"))
        self.assertEqual("observed; no inference", second["outcome"])
        chain = fresh.ledger["chains"][0]
        self.assertEqual(first["chains"][0]["id"], chain["id"])
        self.assertEqual((1, 1), (chain["rounds"], len(chain["operations"])))
        operation = chain["operations"][0]
        self.assertEqual(("OWNED8", "in_progress", 2, 498),
                         (operation["taskId"], operation["workerState"],
                          operation["nativeActual"], operation["workerReserved"]))
        self.assertEqual(1, len(process.launches))
        requests = [body for method, endpoint, body in fixture.transport.writes
                    if method == "POST" and endpoint == "agents/repos/radical/aspire/tasks"]
        self.assertEqual(1, len(requests))
        self.assertTrue(requests[0]["create_pull_request"])
        self.assertNotIn("head_ref", requests[0])
        self.assertEqual([task_path], sorted({endpoint for _, endpoint, _ in fixture.transport.reads
                                             if "/tasks" in endpoint}))
        self.assertEqual(fresh.ledger, state.parse(fixture.transport.comments[0]["body"]))


if __name__ == "__main__":
    unittest.main()
