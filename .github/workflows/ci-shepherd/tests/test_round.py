import copy
import json
import unittest
from unittest.mock import patch

from helpers import FakeProcess, WorkspaceTest, decision_for, fixture_executable
import round as shepherd


RUN = {
    "repository": "example/fixture",
    "runId": "123",
    "runAttempt": "1",
    "workflowSha": "a" * 40,
}


class RoundTests(WorkspaceTest, unittest.TestCase):
    def prepare(self, name="run"):
        directory = self.work / name
        packet, envelope = shepherd.prepare(directory, RUN)
        return directory, packet, envelope

    def test_prepare_is_unique_and_bound_to_host_envelope(self):
        _, packet, envelope = self.prepare()
        _, other, second = self.prepare("other")
        self.assertEqual(envelope["packet"], packet)
        self.assertEqual(packet["run"], RUN)
        self.assertNotEqual(packet["packetId"], other["packetId"])
        self.assertNotEqual(packet["nonce"], other["nonce"])
        self.assertNotEqual(envelope["sessionId"], second["sessionId"])

    def test_reused_run_directory_is_rejected(self):
        self.prepare()
        with self.assertRaises(FileExistsError):
            self.prepare()

    def test_valid_decision_writes_actual_no_effect_receipt(self):
        directory, packet, envelope = self.prepare()
        receipt = shepherd.apply(envelope, decision_for(packet), RUN, directory / "receipt.json")
        self.assertEqual(receipt["effects"], [])
        self.assertEqual(receipt["outcome"], "wait")
        self.assertEqual(receipt["packetId"], packet["packetId"])
        self.assertEqual(json.loads((directory / "receipt.json").read_text()), receipt)
        with self.assertRaises(FileExistsError):
            shepherd.apply(envelope, decision_for(packet), RUN, directory / "receipt.json")

    def test_decision_is_closed_and_identity_bound(self):
        directory, packet, envelope = self.prepare()
        for key, value in [
            ("nonce", "0" * 32), ("packetId", "other"), ("schemaVersion", True),
            ("outcome", "repair"), ("extra", "unexpected"), ("kind", "issue"),
            ("run", {**RUN, "runId": "456"}),
        ]:
            with self.subTest(key=key):
                decision = {**decision_for(packet), key: value}
                with self.assertRaises(ValueError):
                    shepherd.apply(envelope, decision, RUN, directory / "receipt.json")
                self.assertFalse((directory / "receipt.json").exists())

    def test_host_run_identity_is_not_agent_authority(self):
        directory, packet, envelope = self.prepare()
        with self.assertRaises(ValueError):
            shepherd.apply(envelope, decision_for(packet), {**RUN, "runId": "999"}, directory / "receipt.json")

    def test_substituted_packet_cannot_replace_trusted_envelope(self):
        directory, _, envelope = self.prepare()
        _, other, _ = self.prepare("other")
        with self.assertRaises(ValueError):
            shepherd.apply(envelope, decision_for(other), RUN, directory / "receipt.json")

    def test_duplicate_json_keys_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "duplicate"):
            shepherd.loads('{"outcome":"wait","outcome":"wait"}')

    def test_default_json_limit_accepts_boundary_and_rejects_one_more_byte(self):
        bound = 256 * 1024
        value = {"value": "x" * (bound - len(json.dumps({"value": ""}).encode()))}
        raw = json.dumps(value)
        self.assertEqual(bound, len(raw.encode()))
        self.assertEqual(value, shepherd.loads(raw))
        value["value"] += "x"
        with self.assertRaisesRegex(ValueError, "JSON exceeds size limit"):
            shepherd.loads(json.dumps(value))

    def test_safe_output_requires_exactly_one_closed_item(self):
        _, packet, _ = self.prepare()
        item = {"type": "submit_decision", "decision": json.dumps(decision_for(packet))}
        self.assertEqual(shepherd.safe_output({"items": [item], "errors": []}), decision_for(packet))
        for value in [
            {"items": [], "errors": []}, {"items": [item, item], "errors": []},
            {"items": [item], "errors": [], "extra": 1},
            {"items": [{**item, "extra": 1}], "errors": []},
            {"items": [{**item, "type": "noop"}], "errors": []},
            {"items": [item], "errors": ["rejected extra item"]}, {"items": [item]},
        ]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                shepherd.safe_output(value)

    def test_runner_performs_prepare_reason_validate_and_apply(self):
        process = FakeProcess()
        directory = self.work / "smoke"
        receipt = shepherd.smoke(directory, RUN, "copilot", process=process, provider_env={})
        self.assertEqual(receipt["effects"], [])
        self.assertTrue((directory / "receipt.json").exists())
        self.assertTrue((directory / "agent" / "copilot.jsonl").exists())
        self.assertEqual(len(process.launches), 1)

    def test_real_subprocess_boundary_validates_before_apply(self):
        executable = fixture_executable(self.work)
        directory = self.work / "subprocess"
        receipt = shepherd.smoke(directory, RUN, executable, provider_env={})
        self.assertEqual(receipt["effects"], [])
        self.assertTrue((directory / "reasoning-report.json").exists())
        failure = self.work / "subprocess-failure"
        with patch.object(shepherd, "apply") as apply:
            with self.assertRaisesRegex(ValueError, "status 7"):
                shepherd.smoke(failure, RUN, executable, provider_env={"COPILOT_MODEL": "fixture-failure"})
            apply.assert_not_called()
        self.assertTrue((failure / "failure.json").exists())

    def test_failed_reasoning_never_calls_apply(self):
        def missing_final(events):
            return [event for event in events if event["type"] != "assistant.message"]

        def duplicate_final(events):
            return events + [copy.deepcopy(events[-2])]

        def malformed_final(events):
            events[-2]["data"]["content"] = "not json"
            return events

        def unauthorized_call(events):
            return events + [{"type": "tool.execution_start", "data": {"toolName": "bash", "arguments": {"command": "gh api"}}}]

        def missing_session(events):
            return [event for event in events if event["type"] != "session.start"]

        def mismatched_session(events):
            events[-1]["sessionId"] = "other"
            return events

        def mismatched_packet(events):
            decision = json.loads(events[-2]["data"]["content"])
            events[-2]["data"]["content"] = json.dumps({**decision, "nonce": "0" * 32})
            return events

        def unauthorized_outcome(events):
            decision = json.loads(events[-2]["data"]["content"])
            events[-2]["data"]["content"] = json.dumps({**decision, "outcome": "repair"})
            return events

        for index, process in enumerate([
            FakeProcess(returncode=2), FakeProcess(missing_final), FakeProcess(duplicate_final),
            FakeProcess(malformed_final), FakeProcess(tools=("view", "shell")), FakeProcess(unauthorized_call),
            FakeProcess(report=False), FakeProcess(missing_session), FakeProcess(mismatched_session),
            FakeProcess(output=""), FakeProcess(output="not json"), FakeProcess(mismatched_packet),
            FakeProcess(unauthorized_outcome),
        ]):
            with self.subTest(index=index), patch.object(shepherd, "apply") as apply:
                directory = self.work / f"failure-{index}"
                with self.assertRaises(ValueError):
                    shepherd.smoke(directory, RUN, "copilot", process=process, provider_env={})
                apply.assert_not_called()
                self.assertTrue((directory / "failure.json").exists())
                self.assertFalse((directory / "receipt.json").exists())

    def test_local_tool_attempts_and_completions_never_apply(self):
        attempts = [
            {"type": "assistant.message", "data": {"content": "", "toolRequests": [
                {"name": "view", "arguments": {"path": path}},
            ]}}
            for path in ["../trusted/envelope.json", "packet.json", "home/.copilot/settings.json", "/etc/passwd"]
        ]
        attempts.extend([
            {"type": "tool.execution_start", "data": {
                "toolName": "view", "toolCallId": "read_packet", "arguments": {"path": "packet.json"},
            }},
            *[{"type": "tool.execution_complete", "data": {
                "toolCallId": "read_packet", "success": success,
            }} for success in [False, True]],
        ])
        for index, attempt in enumerate(attempts):
            process = FakeProcess(lambda events: events[:-2] + [attempt] + events[-2:])
            directory = self.work / f"attempt-{index}"
            with self.subTest(attempt=attempt), patch.object(shepherd, "apply") as apply:
                with self.assertRaises(ValueError):
                    shepherd.smoke(directory, RUN, "copilot", process=process, provider_env={})
                apply.assert_not_called()
                self.assertTrue((directory / "failure.json").exists())
                self.assertFalse((directory / "receipt.json").exists())

    def test_failed_local_read_completion_never_applies(self):
        def failed_read(events):
            return events[:-2] + [
                {"type": "tool.execution_start", "data": {
                    "toolName": "view", "toolCallId": "read_packet", "arguments": {"path": "packet.json"},
                }},
                {"type": "tool.execution_complete", "data": {"toolCallId": "read_packet", "success": False}},
            ] + events[-2:]

        directory = self.work / "failed-read"
        with patch.object(shepherd, "apply") as apply:
            with self.assertRaises(ValueError):
                shepherd.smoke(directory, RUN, "copilot", process=FakeProcess(failed_read), provider_env={})
            apply.assert_not_called()
        self.assertTrue((directory / "failure.json").exists())
        self.assertFalse((directory / "receipt.json").exists())


if __name__ == "__main__":
    unittest.main()
