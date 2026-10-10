from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import hosted
import live
import pilot
import pilot_binding as bindings
import pilot_github
import pilot_handoff
import work_item_execution
import work_items
from test_work_items import control


def rendered_prompts():
    api = SimpleNamespace(repository="radical/aspire", tracker=99, authority_id=500,
                          tracker_node="TRACKER99", binding=bindings.FORK)
    record = work_items.new_record(control())
    assignment = work_items.claim(record, record["control"])
    assignment["id"] = "fixture-assignment"
    assignment["execution"] = {"approval": None}
    authority = {"repository": api.repository, "tracker": api.tracker, "comment_id": api.authority_id,
                 "tracker_node": api.tracker_node}
    prompts = {"cloud": work_item_execution.request(api, record, assignment)["prompt"]}
    for route in sorted(work_items.ROUTES):
        routed = deepcopy(assignment)
        routed["route"] = route
        prompts["local-" + route] = work_items.worker_packet(record, routed, authority)["prompt"]
    observed = {
        "number": 7, "node": "NODE7", "head": "a" * 40, "headRef": "fix-7", "kind": "pr",
        "ciEvidence": [], "feedbackEvidence": [], "description": "fixture", "workerEvidence": [],
        "feedback": [{"id": "comment:1", "body": "Quoted {{not_a_placeholder}} evidence"}],
    }
    chain = {"id": "fixture-chain", "origin": 7, "handoff": {"id": "fixture-handoff"}}
    operation = {"id": "fixture-operation", "identity": pilot_github.fingerprint(observed) + ":round:1"}
    packet = {"observation": observed}
    prompts["repair-fork"] = pilot.worker_request(api, chain, operation, packet)["prompt"]
    api.binding, api.repository = bindings.UPSTREAM, "microsoft/aspire"
    prompts["repair-upstream"] = pilot.worker_request(api, chain, operation, packet)["prompt"]
    prompts["handoff"] = pilot_handoff.worker_request(api, chain, packet)["prompt"]
    executor = SimpleNamespace(github=SimpleNamespace(actor={"id": 1472}), context={"feedback": []})
    prompts["fork-fixture"] = live.ExistingPRExecutor.prompt(executor, {
        "id": "fixture-operation", "identity": {"revision": "a" * 40, "arguments": {"feedbackIds": []}}}, "trial")
    prompts["transport-proof"] = hosted.output_prompt({"kind": "transport-proof", "quoted": "{{packet}}"})
    return prompts


class PromptTemplateTests(unittest.TestCase):
    def test_launch_builders_render_reviewable_files_without_interpreting_quoted_data(self):
        import prompt_templates
        originals = rendered_prompts()
        self.assertIn("{{not_a_placeholder}}", originals["repair-fork"])
        self.assertIn("{{packet}}", originals["transport-proof"])
        original = Path.read_text
        seen = set()
        def edited(path, *args, **kwargs):
            text = original(path, *args, **kwargs)
            if path.parent == prompt_templates.DIRECTORY:
                seen.add(path.name)
                return "Reviewed instructions. " + text
            return text
        with patch.object(Path, "read_text", edited):
            changed = rendered_prompts()
        for key in originals:
            with self.subTest(key=key):
                self.assertNotEqual(originals[key], changed[key])
                self.assertIn("Reviewed instructions.", changed[key])
        self.assertEqual({
            "cloud-work-item.md", "local-work-item.md", "repair-worker.md", "initial-handoff.md",
            "fork-fixture-worker.md", "transport-proof.md", "target-fork.md", "target-upstream.md"}, seen)

    def test_missing_invalid_and_extra_template_bindings_fail_closed(self):
        import prompt_templates
        with patch.object(Path, "read_text", return_value="Wrapped \\\nprose.\n{{value}}\n"):
            self.assertEqual("Wrapped prose.\nquoted", prompt_templates.load("cloud-work-item", value="quoted"))
        with patch.object(Path, "read_text", return_value="{{value}}\n"):
            self.assertEqual("quoted {{ignored}}", prompt_templates.load("cloud-work-item", value="quoted {{ignored}}"))
            for values in ({}, {"wrong": "text"}, {"value": 42}, {"value": "text", "extra": "text"}):
                with self.subTest(values=values), self.assertRaises(ValueError):
                    prompt_templates.load("cloud-work-item", **values)
        with patch.object(Path, "read_text", side_effect=FileNotFoundError):
            with self.assertRaises(FileNotFoundError):
                prompt_templates.load("cloud-work-item")
        for name in ("../secret", "/tmp/secret", "cloud-work-item.md"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                prompt_templates.load(name)
