import unittest

from helpers import FakeProcess, WorkspaceTest, decision_for, host_events, wire_report
import reasoning
import round as shepherd
from test_round import RUN


class ReasoningTests(WorkspaceTest, unittest.TestCase):
    def test_each_launch_has_fresh_session_and_isolated_environment(self):
        process = FakeProcess()
        sessions = []
        for index in range(2):
            directory = self.work / str(index)
            packet, envelope = shepherd.prepare(directory, RUN)
            reasoning.execute(directory / "agent", packet, envelope["sessionId"], "copilot",
                              process=process, provider_env={})
            sessions.append(envelope["sessionId"])
        self.assertNotEqual(*sessions)
        for argv, kwargs in process.launches:
            self.assertNotIn("--resume", argv)
            self.assertNotIn("--continue", argv)
            start = argv.index("--available-tools") + 1
            end = next(index for index in range(start, len(argv)) if argv[index].startswith("--"))
            self.assertEqual(argv[start:end], [])
            self.assertNotIn("--allow-tool", argv)
            self.assertEqual(argv[argv.index("--max-ai-credits") + 1], "5")
            self.assertEqual(kwargs["timeout"], 600)
            self.assertEqual(kwargs["env"]["GH_AW_HARNESS_MAX_RETRIES"], "0")
            self.assertNotIn("GH_TOKEN", kwargs["env"])
            self.assertNotIn("GITHUB_TOKEN", kwargs["env"])
            self.assertNotIn("COPILOT_GITHUB_TOKEN", kwargs["env"])
            self.assertTrue(kwargs["env"]["COPILOT_HOME"].startswith(str(self.work.resolve())))

    def test_provider_environment_rejects_worker_credentials_and_shell_commands(self):
        for name in ["GH_TOKEN", "GITHUB_TOKEN", "COPILOT_GITHUB_TOKEN", "COPILOT_PROVIDER_API_KEY_COMMAND",
                     "COPILOT_ALLOW_ALL", "NODE_OPTIONS"]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                reasoning.child_environment(self.work, {name: "not-a-credential"})

    def test_host_reports_are_required_and_cannot_be_agent_self_report(self):
        packet, envelope = shepherd.prepare(self.work / "run", RUN)
        events = host_events(envelope["sessionId"], decision_for(packet))
        for event_type in ["session.start", "result"]:
            with self.subTest(event_type=event_type), self.assertRaises(ValueError):
                reasoning.validate([event for event in events if event["type"] != event_type], envelope["sessionId"], debug=wire_report())
        with self.assertRaisesRegex(ValueError, "effective tool report"):
            reasoning.validate(events, envelope["sessionId"])

    def test_local_reads_are_rejected_including_the_packet(self):
        packet, envelope = shepherd.prepare(self.work / "run", RUN)
        for path in ["../trusted/envelope.json", "/etc/passwd", "packet.json"]:
            events = host_events(envelope["sessionId"], decision_for(packet), calls=[
                {"toolName": "view", "arguments": {"path": path}}
            ])
            with self.subTest(path=path), self.assertRaisesRegex(ValueError, "unauthorized tool call"):
                reasoning.validate(events, envelope["sessionId"], debug=wire_report())

    def test_missing_and_duplicate_final_are_rejected(self):
        packet, envelope = shepherd.prepare(self.work / "run", RUN)
        events = host_events(envelope["sessionId"], decision_for(packet))
        with self.assertRaises(ValueError):
            reasoning.validate(events + [events[-2]], envelope["sessionId"], debug=wire_report())
        events[-2]["data"]["content"] = '{"schemaVersion": 1, "schemaVersion": 1}'
        with self.assertRaises(ValueError):
            reasoning.validate(events, envelope["sessionId"], debug=wire_report())

    def test_resumed_session_and_unverified_version_are_rejected(self):
        packet, envelope = shepherd.prepare(self.work / "run", RUN)
        for key, value in [("alreadyInUse", True), ("copilotVersion", "1.0.88")]:
            events = host_events(envelope["sessionId"], decision_for(packet))
            events[0]["data"][key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                reasoning.validate(events, envelope["sessionId"], debug=wire_report())

    def test_newer_copilot_version_is_supported_when_host_evidence_is_valid(self):
        packet, envelope = shepherd.prepare(self.work / "run", RUN)
        events = host_events(envelope["sessionId"], decision_for(packet))
        events[0]["data"]["copilotVersion"] = "1.0.93-3"
        decision, evidence = reasoning.validate(events, envelope["sessionId"], debug=wire_report())

        self.assertEqual(decision_for(packet), decision)
        self.assertEqual("1.0.93-3", evidence["copilotVersion"])

    def test_copilot_version_gate_requires_the_minimum_supported_cli_contract(self):
        for version, supported in [
            ("GitHub Copilot CLI 1.0.93-3.", True),
            ("1.0.92-3", True),
            ("1.0.92", True),
            ("1.0.92-2", False),
            ("1.0.88", False),
            ("future", False),
        ]:
            with self.subTest(version=version):
                self.assertEqual(supported, reasoning.copilot_version_supported(version))

    def test_unauthorized_requests_are_rejected_even_without_execution(self):
        packet, envelope = shepherd.prepare(self.work / "run", RUN)
        events = host_events(envelope["sessionId"], decision_for(packet))
        events.insert(-2, {"type": "assistant.message", "data": {
            "content": "", "toolRequests": [{"name": "bash", "arguments": {"command": "gh api"}}],
        }})
        with self.assertRaisesRegex(ValueError, "unauthorized tool request"):
            reasoning.validate(events, envelope["sessionId"], debug=wire_report())

    def test_malformed_host_tool_fingerprints_fail_closed(self):
        packet, envelope = shepherd.prepare(self.work / "run", RUN)
        events = host_events(envelope["sessionId"], decision_for(packet))
        for debug in [
            "[DEBUG] [rust:model_wire] Wire request: []",
            "[DEBUG] [rust:model_wire] Wire request: not json",
            "[DEBUG] [rust:model_wire] Wire request: {}",
        ]:
            with self.subTest(debug=debug), self.assertRaises(ValueError):
                reasoning.validate(events, envelope["sessionId"], debug=debug)

    def test_prompt_substitution_is_strict(self):
        self.assertEqual(reasoning.render("{{packet}}", packet="{}"), "{}")
        for template, values in [("{{bad}}", {"packet": "{}"}), ("{{bad syntax}}", {}), ("plain", {"extra": "x"})]:
            with self.subTest(template=template), self.assertRaises(ValueError):
                reasoning.render(template, **values)


if __name__ == "__main__":
    unittest.main()
