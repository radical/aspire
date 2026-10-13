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
    def test_cloud_prefix_is_identical_across_assignments_and_routes(self):
        import prompt_templates
        boundary = "\n## Route instructions\n"
        for version in (None, 2):
            prefixes = []
            for index, route in enumerate(sorted(work_items.ROUTES)):
                api = SimpleNamespace(repository="radical/aspire", tracker=99 + index,
                                      authority_id=500 + index, tracker_node=f"TRACKER{index}")
                value = control()
                value.update(id=f"item-{index}", requested_route=route)
                value["issue"].update(number=7 + index, node_id=f"ISSUE{index}")
                value["scope"] = f"scope {index}"
                record = work_items.new_record(value)
                assignment = work_items.claim(record, value)
                assignment["id"] = f"assignment-{index}"
                assignment["execution"] = {"approval": None}
                if version is not None:
                    assignment["execution"]["request_version"] = version
                assignment["execution"]["approval"] = work_item_execution.approval_template(record, assignment)
                body = work_item_execution.request(api, record, assignment)
                prefix, suffix = body["prompt"].split(boundary, 1)
                prefixes.append(prefix)
                self.assertTrue(prefix.startswith(prompt_templates.load("diagnose-repair")))
                self.assertEqual(1, body["prompt"].count(work_item_execution.CORRELATION))
                correlation = next(line for line in suffix.splitlines()
                                   if line.startswith(work_item_execution.CORRELATION))
                import json
                self.assertEqual(assignment["id"], json.loads(
                    correlation.removeprefix(work_item_execution.CORRELATION))["assignment_id"])
                self.assertLessEqual(len(json.dumps(body).encode()), 20000)
            self.assertEqual([prefixes[0]] * len(prefixes), prefixes)

    def test_repair_and_handoff_prefixes_are_identical_across_dynamic_contexts(self):
        import json
        boundary = "\n## Assignment context\n"
        prefixes = {"repair": [], "handoff": []}
        for index in (1, 2):
            api = SimpleNamespace(repository="radical/aspire", tracker=99 + index,
                                  authority_id=500 + index, tracker_node=f"TRACKER{index}", binding=bindings.FORK)
            observed = {
                "number": index, "node": f"NODE{index}", "head": str(index) * 40,
                "headRef": f"branch-{index}", "kind": "pr",
                "ciEvidence": [], "feedbackEvidence": [], "workerEvidence": [],
                "description": f"changed evidence {index}", "feedback": [
                    {"id": f"comment:{index}", "body": f"feedback {index}"}],
            }
            chain = {"id": f"chain-{index}", "origin": index, "handoff": {"id": f"handoff-{index}"}}
            operation = {"id": f"operation-{index}", "identity": pilot_github.fingerprint(observed) + ":round:1"}
            packet = {"observation": observed}
            for name, body in (
                    ("repair", pilot.worker_request(api, chain, operation, packet)),
                    ("handoff", pilot_handoff.worker_request(api, chain, packet))):
                prefix, suffix = body["prompt"].split(boundary, 1)
                prefixes[name].append(prefix)
                self.assertEqual(1, body["prompt"].count(pilot_github.CORRELATION))
                self.assertIn(pilot_github.CORRELATION, suffix)
                self.assertIn(observed["description"], suffix)
                self.assertLessEqual(len(json.dumps(body).encode()), 20000)
        for name, values in prefixes.items():
            with self.subTest(name=name):
                self.assertEqual(values[0], values[1])

    def test_native_app_parent_prompt_is_checked_in_and_requires_real_native_reconciliation(self):
        import prompt_templates
        text = prompt_templates.load("app-handoff")
        for contract in ("pending-handoffs", "app-checkpoint", "confirm-app-handoff",
                         "without kickoff", "get_session", "native session inventory",
                         "applied_enabled", "**current**", "intentHead", "confirmedHead",
                         "merge_pr:false", "authorized parent attestation",
                         "Do not assume", "never repeat enable", "without compensating writes"):
            with self.subTest(contract=contract):
                self.assertIn(contract, text)
        self.assertLess(text.index("`app-checkpoint --stage intent`"), text.index("Call `open_pr_session`"))
        self.assertLess(text.index("Persist `activation-requested`"), text.index("call `manage_agent_merge"))

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
            "fork-fixture-worker.md", "transport-proof.md", "target-fork.md", "target-upstream.md",
            "failure-investigation.md", "diagnose-repair.md"}, seen)

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
