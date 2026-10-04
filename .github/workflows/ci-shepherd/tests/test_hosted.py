import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import patch

from helpers import WorkspaceTest, compiled_environment, compiled_step, decision_for, host_events, jsonl, wire_report
import reasoning
import round as shepherd
from test_round import RUN


class HostedTests(WorkspaceTest, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.environment = {
            "GITHUB_REPOSITORY": RUN["repository"], "GITHUB_RUN_ID": RUN["runId"],
            "GITHUB_RUN_ATTEMPT": RUN["runAttempt"], "GITHUB_WORKFLOW_SHA": RUN["workflowSha"],
        }
        self.run = self.work / "prepared"
        self.packet, _ = shepherd.prepare(self.run, RUN, native_session=True)
        self.session_id = "12345678-1234-4234-8234-123456789abc"
        self.sessions = self.work / "sessions"
        self.session = self.sessions / self.session_id
        self.session.mkdir(parents=True)
        self.logs = self.work / "logs"
        self.logs.mkdir()
        (self.logs / "process.log").write_text(wire_report(("safeoutputs-submit_decision",)))
        self.decision = decision_for(self.packet)
        self.calls = [{"toolCallId": "call_1", "toolName": "safeoutputs-submit_decision",
                       "arguments": {"decision": json.dumps(self.decision)}}]
        self.events = host_events(self.session_id, self.decision, calls=self.calls)
        (self.session / "events.jsonl").write_text(jsonl(self.events[:-1]))
        self.evidence = self.work / "evidence.json"
        self.output = self.work / "agent-output.json"
        shepherd.write_json(self.output, {"items": [{"type": "submit_decision", "decision": json.dumps(self.decision)}], "errors": []})
        self.receipt = self.work / "receipt.json"

    def collect(self):
        return shepherd.main([
            "collect", "--trusted", str(self.run / "trusted"), "--session-root", str(self.sessions),
            "--logs", str(self.logs), "--out", str(self.evidence), "--outcome", "success",
        ])

    def apply(self):
        return shepherd.main([
            "apply", "--trusted", str(self.run / "trusted"), "--evidence", str(self.evidence),
            "--decision", str(self.output), "--receipt", str(self.receipt),
        ])

    def test_hosted_collect_and_guarded_apply_use_independent_prepare(self):
        with patch.dict(os.environ, self.environment):
            self.assertEqual(self.collect(), 0)
            self.assertEqual(self.apply(), 0)
        receipt = shepherd.read_json(self.receipt)
        self.assertEqual(receipt["effects"], [])
        self.assertEqual(receipt["sessionId"], self.session_id)
        evidence = shepherd.read_json(self.evidence)
        self.assertEqual(reasoning.effective_tools(evidence["debug"]), [frozenset({"safeoutputs-submit_decision"})])

    def test_agent_packet_substitution_never_applies(self):
        with patch.dict(os.environ, self.environment):
            self.assertEqual(self.collect(), 0)
            substituted = {**self.decision, "nonce": "0" * 32}
            self.output.unlink()
            shepherd.write_json(self.output, {"items": [{"type": "submit_decision", "decision": json.dumps(substituted)}], "errors": []})
            with patch.object(shepherd, "apply") as apply:
                self.assertEqual(self.apply(), 1)
                apply.assert_not_called()
        self.assertFalse(self.receipt.exists())
        self.assertTrue((self.work / "failure.json").exists())

    def test_missing_or_duplicated_safe_output_calls_fail_collection(self):
        for index, events in enumerate([
            [event for event in self.events if event["type"] not in {"tool.execution_start", "tool.execution_complete"}],
            self.events + [next(event for event in self.events if event["type"] == "tool.execution_start")],
        ]):
            with self.subTest(index=index):
                (self.session / "events.jsonl").write_text(jsonl([event for event in events if event["type"] != "result"]))
                if self.evidence.exists():
                    self.evidence.unlink()
                with patch.dict(os.environ, self.environment):
                    self.assertEqual(self.collect(), 1)
                self.assertFalse(self.receipt.exists())

    def test_prepare_artifact_substitution_never_calls_apply(self):
        with patch.dict(os.environ, self.environment):
            self.assertEqual(self.collect(), 0)
            packet_file = self.run / "trusted" / "packet.json"
            packet_file.unlink()
            shepherd.write_json(packet_file, {**self.packet, "nonce": "0" * 32})
            with patch.object(shepherd, "apply") as apply:
                self.assertEqual(self.apply(), 1)
                apply.assert_not_called()
        self.assertFalse(self.receipt.exists())

    def test_multiple_native_sessions_and_failed_host_outcome_are_rejected(self):
        second = self.sessions / "another"
        second.mkdir()
        (second / "events.jsonl").write_text(jsonl(self.events))
        with self.assertRaisesRegex(ValueError, "exactly one"):
            reasoning.collect(self.sessions, self.logs, "success", self.evidence)
        with self.assertRaisesRegex(ValueError, "did not succeed"):
            reasoning.collect(self.sessions, self.logs, "failure", self.evidence)

    def test_host_grants_are_not_replaced_by_agent_claims(self):
        (self.logs / "process.log").write_text(wire_report(("safeoutputs-submit_decision", "bash")))
        with patch.dict(os.environ, self.environment), patch.object(shepherd, "apply") as apply:
            self.assertEqual(self.collect(), 1)
            apply.assert_not_called()
        self.assertTrue((self.work / "failure.json").exists())

    def test_failed_allowed_transport_completion_never_applies(self):
        for event in self.events:
            if event["type"] == "tool.execution_complete":
                event["data"]["success"] = False
        (self.session / "events.jsonl").write_text(jsonl(self.events[:-1]))
        with patch.dict(os.environ, self.environment), patch.object(shepherd, "apply") as apply:
            self.assertEqual(self.collect(), 1)
            apply.assert_not_called()
        self.assertFalse(self.receipt.exists())
        self.assertTrue((self.work / "failure.json").exists())

    def test_compiled_collector_reads_the_configured_awf_session_directory(self):
        engine = compiled_step("Execute GitHub Copilot CLI")
        engine_env = compiled_environment(engine, self.work)
        session_root = Path(engine_env.get("AWF_SESSION_STATE_DIR", str(self.work / "agent-session-state")))
        shutil.copytree(self.session, session_root / self.session_id)
        artifacts = self.work / "artifacts" / "ci-shepherd"
        artifacts.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.run, artifacts / "prepared")
        shutil.copytree(self.logs, artifacts / "host-logs")
        home = self.work / "home"
        home.mkdir()
        source = Path(__file__).resolve().parents[1]
        scripts = self.work / ".github" / "workflows"
        scripts.mkdir(parents=True)
        (scripts / "ci-shepherd").symlink_to(source, target_is_directory=True)
        collector = compiled_step("Collect host process and session evidence")
        environment = {
            "PATH": os.defpath, "HOME": str(home.resolve()), **self.environment,
            **compiled_environment(collector, self.work),
        }
        result = subprocess.run(["bash", "-c", collector["run"]], cwd=self.work, env=environment,
                                capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        evidence = shepherd.read_json(artifacts / "evidence.json")
        self.assertEqual(evidence["sessionId"], self.session_id)
        self.assertEqual(reasoning.validate_evidence(evidence, self.session_id, hosted=True)[0], self.decision)

    def test_compiled_awf_launch_clears_worker_credentials(self):
        step = compiled_step("Execute GitHub Copilot CLI")
        command = step["run"][step["run"].index("awf --config "):]
        names = ["GH_TOKEN", "GITHUB_TOKEN", "GH_AW_GITHUB_TOKEN", "GH_AW_GITHUB_MCP_SERVER_TOKEN",
                 "CI_SHEPHERD_USER_TOKEN"]
        capture = "import json,os; print(json.dumps({key: os.environ.get(key) for key in " + repr(
            names + ["COPILOT_GITHUB_TOKEN"]
        ) + "}))"
        environment = {
            "PATH": os.defpath, "PYTHON": sys.executable,
            **{name: "fixture-worker-credential" for name in names},
            **compiled_environment(step, self.work),
        }
        # Capture the real generated AWF launch environment without executing a
        # container, its inner shell command, or any model inference.
        script = 'awf() { "$PYTHON" -c "$CAPTURE"; }\n' + command
        result = subprocess.run(["bash", "-c", script], cwd=self.work,
                                env={**environment, "CAPTURE": capture},
                                capture_output=True, text=True, check=False, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        captured = json.loads(result.stdout)
        self.assertEqual({name: captured[name] for name in names}, dict.fromkeys(names, ""))
        self.assertEqual(captured["COPILOT_GITHUB_TOKEN"], "fixture-inference-token")


if __name__ == "__main__":
    unittest.main()
