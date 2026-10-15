import base64
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path
import tempfile

import work_item_followup as followup
from test_work_items import result
import work_item_execution as execution
from test_work_item_execution import CloudAuthority
from test_work_items import control


class ResultEncodingTests(unittest.TestCase):
    def test_operational_feedback_round_trips_through_encoder_and_strict_reader(self):
        from copy import deepcopy
        import work_items
        assignment = {"id": "assignment", "revision": 1, "request_version": 2}
        claimed = result(assignment)
        claimed["operational_feedback"] = {
            "capabilities_used": ["MCP job logs and artifacts retrieved."],
            "blockers": ["run_workflow suspended for approval; no receipt, no HTTP denial."],
            "improvements": ["Retain named argument-level results; aggregate passes are insufficient."],
        }
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "result.json", Path(directory) / "result.b64"
            source.write_text(json.dumps(claimed), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "work_item_result.py"),
                 str(source), str(output)],
                env={"COPILOT_AGENT_SESSION_ID": "session-1"}, capture_output=True, text=True)
            self.assertEqual(0, completed.returncode, completed.stderr)
            parsed = followup.parse_result(f"CSWORKITEMBEGIN{output.read_text()}CSWORKITEMEND",
                                           {"id": "example-item"}, assignment, "session-1")
            self.assertEqual(claimed, parsed)
            self.assertNotIn("proposed_actions", parsed)
            self.assertNotIn("recommended_route", parsed)
        legacy = deepcopy(claimed)
        legacy.pop("operational_feedback")
        work_items.validate_result_version(legacy, {"id": "assignment", "revision": 1})
        with self.assertRaisesRegex(ValueError, "version 2"):
            work_items.validate_result_version(claimed, {"id": "assignment", "revision": 1})

    def test_operational_feedback_rejects_unbounded_or_action_shaped_values(self):
        import work_items
        valid = {"capabilities_used": [], "blockers": [], "improvements": []}
        invalid = (
            None, [], valid | {"dispatch": True}, valid | {"blockers": "missing"},
            valid | {"blockers": ["x"] * 4}, valid | {"improvements": ["x" * 401]},
            valid | {"capabilities_used": [{"tool": "run_workflow"}]},
            {"blockers": [], "improvements": []},
        )
        for feedback in invalid:
            with self.subTest(feedback=feedback):
                claimed = result({"id": "assignment", "revision": 1})
                claimed["operational_feedback"] = feedback
                with self.assertRaises(ValueError):
                    work_items.validate_result(claimed)

    def test_operational_feedback_unicode_byte_boundary_in_strict_reader(self):
        assignment = {"id": "assignment", "revision": 1, "request_version": 2}
        for entry, accepted in (("x" * 400, True), ("x" * 401, False),
                                ("\u6f22" * 133 + "x", True), ("\u6f22" * 134, False),
                                ("\u2192" * 133 + "x", True), ("\u2192" * 134, False)):
            with self.subTest(bytes=len(entry.encode("utf-8")), accepted=accepted):
                claimed = result(assignment)
                claimed["operational_feedback"] = {
                    "capabilities_used": [], "blockers": [entry], "improvements": []}
                encoded = base64.b64encode(
                    json.dumps(claimed, ensure_ascii=False).encode("utf-8")).decode("ascii")
                envelope = f"CSWORKITEMBEGIN{encoded}CSWORKITEMEND"
                if accepted:
                    self.assertEqual(claimed, followup.parse_result(
                        envelope, {"id": "example-item"}, assignment, "session-1"))
                else:
                    with self.assertRaisesRegex(ValueError, "invalid blockers"):
                        followup.parse_result(envelope, {"id": "example-item"}, assignment, "session-1")

    def test_encoder_uses_host_identity_and_round_trips_unicode_wrapped_base64(self):
        assignment = {"id": "assignment", "revision": 1}
        claimed = result(assignment)
        claimed.update(worker_id="incorrect-worker", summary="Diagnose → fix; no repair was justified.")
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "result.json", Path(directory) / "result.b64"
            source.write_text(json.dumps(claimed), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "work_item_result.py"),
                 str(source), str(output)],
                env={"COPILOT_AGENT_SESSION_ID": "session-1"}, capture_output=True, text=True)
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual("", completed.stdout)
            encoded = output.read_text().strip()
            claimed["worker_id"] = "session-1"
            self.assertEqual(claimed, json.loads(base64.b64decode(encoded, validate=True)))
            wrapped = "\n".join(encoded[i:i + 79] for i in range(0, len(encoded), 79))
            self.assertEqual(claimed, followup.parse_result(
                f"CSWORKITEMBEGIN\n{wrapped}\nCSWORKITEMEND",
                {"id": "example-item"}, assignment, "session-1"))

    def test_missing_host_session_uses_only_the_verified_result_placeholder(self):
        assignment = {"id": "assignment", "revision": 1}
        claimed = result(assignment)
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "result.json", Path(directory) / "result.b64"
            source.write_text(json.dumps(claimed), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[1] / "work_item_result.py"),
                 str(source), str(output)], env={}, capture_output=True, text=True)
            self.assertEqual(0, completed.returncode, completed.stderr)
            encoded = output.read_text().strip()
            self.assertEqual("HOST_VERIFIED_SESSION_ID",
                             json.loads(base64.b64decode(encoded))["worker_id"])
            self.assertEqual(claimed, followup.parse_result(
                f"CSWORKITEMBEGIN{encoded}CSWORKITEMEND",
                {"id": "example-item"}, assignment, "session-1"))

    def test_invalid_or_oversized_result_produces_no_encoded_output(self):
        for text in ('{"summary":', '{"outcome":"fixed","outcome":"no_fix"}', '{"summary":NaN}',
                     json.dumps(dict(result({"id": "assignment", "revision": 1}),
                                                    summary="x" * 16000))):
            with self.subTest(input_length=len(text)), tempfile.TemporaryDirectory() as directory:
                source, output = Path(directory) / "result.json", Path(directory) / "result.b64"
                source.write_text(text, encoding="utf-8")
                completed = subprocess.run(
                    [sys.executable, str(Path(__file__).resolve().parents[1] / "work_item_result.py"),
                     str(source), str(output)], env={}, capture_output=True, text=True)
                self.assertNotEqual(0, completed.returncode)
                self.assertFalse(output.exists())
                self.assertEqual("", completed.stdout)

    def test_word_substitution_inside_base64_is_not_renderer_whitespace(self):
        assignment = {"id": "assignment", "revision": 1}
        claimed = result(assignment)
        claimed["summary"] = "was was was"
        encoded = base64.b64encode(json.dumps(claimed).encode()).decode()
        self.assertIn("d2Fz", encoded)
        damaged = encoded.replace("d2Fz", "was", 1)
        with self.assertRaisesRegex(ValueError, "invalid worker result encoding"):
            followup.parse_result(f"CSWORKITEMBEGIN\n{damaged}\nCSWORKITEMEND",
                                  {"id": "example-item"}, assignment, "session-1")

    def test_encoder_embedded_in_actual_prompt_is_executable(self):
        api = CloudAuthority()
        execution.prepare(api, control)
        record = api.ledger["workItems"][0]
        assignment = record["assignments"][0]
        prompt = execution.request(api, record, assignment)["prompt"]
        script = re.search(r"```python\n(.*?)\n```", prompt, re.S).group(1)
        with tempfile.TemporaryDirectory() as directory:
            encoder = Path(directory) / "encoder.py"
            source, output = Path(directory) / "result.json", Path(directory) / "result.b64"
            encoder.write_text(script, encoding="utf-8")
            claimed = result(assignment)
            claimed["item_id"] = record["id"]
            source.write_text(json.dumps(claimed), encoding="utf-8")
            completed = subprocess.run([sys.executable, str(encoder), str(source), str(output)],
                                       env={"COPILOT_AGENT_SESSION_ID": "session-1"},
                                       capture_output=True, text=True)
            self.assertEqual(0, completed.returncode, completed.stderr)
            self.assertEqual(claimed, followup.parse_result(
                f"CSWORKITEMBEGIN{output.read_text().strip()}CSWORKITEMEND",
                record, assignment, "session-1"))
