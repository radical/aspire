import unittest
import base64
from subprocess import CompletedProcess
import subprocess

from helpers import WorkspaceTest
import pilot_patch as patch
import pilot_github
import pilot_state as state
from github import Response
from helpers import FakeClock
from test_pilot_github import Transport, pr


def proposal():
    return {"profile": "python-labels-v1", "head": "a" * 40, "operation": "operation-1",
            "files": {patch.SOURCE: "def normalize_label(value):\n    return value\n",
                      patch.TEST: "import unittest\n"},
            "replacement": "def normalize_label(value):\n    return value.strip().lower()\n"}


class PilotPatchTests(WorkspaceTest, unittest.TestCase):
    def test_primary_file_inventory_uses_unique_filename_not_optional_or_invented_id(self):
        from live import API
        from github import IncompleteInventory
        for files in (
                [{"sha": "a" * 40, "status": "modified"}],
                [{"filename": patch.SOURCE}, {"filename": patch.SOURCE}]):
            with self.subTest(files=files), self.assertRaises(ValueError):
                API(lambda *_: Response(files, {})).pages("repos/radical/aspire/pulls/7/files",
                                                         identity_key="filename")
        with self.assertRaises(IncompleteInventory):
            API(lambda *_: Response([{"filename": patch.SOURCE}], {})).pages("repos/radical/aspire/pulls/7/files")

    def test_only_trusted_profile_paths_and_pure_function_are_eligible(self):
        value = proposal()
        patch.validate(value)
        for change in (
            {"profile": "arbitrary-shell"},
            {"replacement": "import os\nos.system('bad')"},
            {"replacement": "def normalize_label(value):\n    return value.__class__()\n"},
            {"files": {".github/workflows/ci.yml": "change"}},
        ):
            with self.subTest(change=change), self.assertRaises(ValueError):
                patch.validate({**value, **change})

    def test_typed_operator_fixture_signature_is_supported_and_preserved(self):
        value = proposal()
        value["files"][patch.SOURCE] = "def normalize_label(text: str) -> str:\n    return text.strip().upper()\n"
        value["replacement"] = "def normalize_label(text: str) -> str:\n    return text.strip().lower()\n"
        patch.validate(value)
        for replacement in (
            "def normalize_label(value: str) -> str:\n    return value.strip().lower()\n",
            "def normalize_label(text: dangerous()) -> str:\n    return text.lower()\n",
            "def normalize_label(text: str) -> dangerous():\n    return text.lower()\n",
        ):
            with self.subTest(replacement=replacement), self.assertRaises(ValueError):
                patch.validate({**value, "replacement": replacement})

    def test_validation_is_credential_free_and_exact_bytes_bound(self):
        calls = []

        def process(argv, **kwargs):
            calls.append((argv, kwargs))
            self.assertEqual(["PATH"], list(kwargs["env"]))
            self.assertIn("--network=none", argv)
            self.assertIn("--read-only", argv)
            self.assertIn("--cap-drop=ALL", argv)
            return CompletedProcess(argv, 0, "Ran 5 tests\nOK\n", "")

        evidence = patch.run_validation(proposal(), self.work, process=process)
        patch.verify_evidence(proposal(), evidence)
        changed = proposal()
        changed["head"] = "b" * 40
        with self.assertRaisesRegex(ValueError, "exact"):
            patch.verify_evidence(changed, evidence)
        changed = proposal()
        changed["replacement"] += "\n"
        with self.assertRaises(ValueError):
            patch.verify_evidence(changed, evidence)
        self.assertEqual(1, len(calls))

    def test_failed_tests_cannot_authorize_publication(self):
        evidence = patch.run_validation(proposal(), self.work, process=lambda argv, **kwargs: CompletedProcess(
            argv, 1, "FAILED (failures=1)", ""))
        with self.assertRaisesRegex(ValueError, "failed"):
            patch.verify_evidence(proposal(), evidence)

    def test_untrusted_argv_and_extra_evidence_reject(self):
        evidence = {"proposal": proposal(), "exitCode": 0, "argv": ["sh", "-c", "bad"],
                    "output": "OK", "schemaVersion": 1}
        with self.assertRaises(ValueError):
            patch.verify_evidence(proposal(), evidence)

    def publisher(self):
        transport = Transport()
        value = proposal()
        transport.values["repos/radical/aspire/pulls/7"] = pr()
        transport.values["repos/radical/aspire/pulls/7/files"] = [{"sha": "d" * 40, "filename": patch.SOURCE, "status": "modified"}]
        for path, content in value["files"].items():
            transport.values["repos/radical/aspire/contents/" + path] = {
                "type": "file", "path": path, "size": len(content.encode()), "encoding": "base64",
                "content": base64.b64encode(content.encode()).decode()}
        transport.values["repos/radical/aspire/git/commits/" + "a" * 40] = {"tree": {"sha": "c" * 40}}
        transport.values["repos/radical/aspire/git/ref/heads/fix-7"] = {"object": {"sha": "a" * 40}}
        api = pilot_github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.read_authority()
        chain = state.adopt(api.ledger, 7, "pr", "NODE7")
        operation = state.reserve(api.ledger, chain, "local", FakeClock()(), local=True)
        state.settle_native(operation, 2)
        value["operation"] = operation["id"]
        observation = api.observe(chain)
        api.persist()
        return transport, api, chain, observation, value

    def test_successful_inline_publish_changes_only_source_and_sends_zero_cloud_tasks(self):
        transport, api, chain, observation, value = self.publisher()
        send = transport.__call__

        def effect(method, endpoint, body):
            if method == "POST" and "/git/" in endpoint:
                transport.writes.append((method, endpoint, body))
                return Response({"sha": "d" * 40}, {}, 201)
            if method == "PATCH" and "/git/refs/" in endpoint:
                transport.writes.append((method, endpoint, body))
                return Response({"object": {"sha": "d" * 40}}, {}, 200)
            return send(method, endpoint, body)

        api.transport = effect
        evidence = patch.run_validation(value, self.work, process=lambda argv, **kwargs: CompletedProcess(
            argv, 0, "Ran 5 tests\nOK", ""))
        self.assertEqual("d" * 40, patch.publish(api, chain, observation, value, evidence))
        effects = [(endpoint, body) for method, endpoint, body in transport.writes if "/git/" in endpoint]
        self.assertEqual(4, len(effects))
        self.assertEqual(patch.SOURCE, effects[1][1]["tree"][0]["path"])
        self.assertEqual({"sha": "d" * 40, "force": False}, effects[-1][1])
        self.assertEqual([], [write for write in transport.writes if write[1].endswith("/tasks")])
        self.assertEqual("completed", chain["operations"][0]["state"])

    def test_stale_takeover_or_altered_evidence_produces_no_git_writes(self):
        for change in ("stale", "takeover", "evidence"):
            with self.subTest(change=change):
                transport, api, chain, observation, value = self.publisher()
                evidence = patch.run_validation(value, self.work, process=lambda argv, **kwargs: CompletedProcess(
                    argv, 0, "Ran 5 tests\nOK", ""))
                if change == "stale":
                    transport.values["repos/radical/aspire/pulls/7"]["head"]["sha"] = "b" * 40
                elif change == "takeover":
                    transport.values["repos/radical/aspire/pulls/7"]["labels"] = []
                else:
                    evidence["proposal"]["replacement"] += "\n"
                with self.assertRaises(ValueError):
                    patch.publish(api, chain, observation, value, evidence)
                self.assertEqual([], [write for write in transport.writes if "/git/" in write[1]])

    def test_timeout_cleans_up_only_its_own_daemon_container(self):
        calls = []

        def process(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                raise subprocess.TimeoutExpired(argv, 90)
            return CompletedProcess(argv, 0, "", "")

        evidence = patch.run_validation(proposal(), self.work, process=process)
        name = calls[0][calls[0].index("--name") + 1]
        self.assertEqual(["docker", "rm", "--force", name], calls[1])
        self.assertEqual(124, evidence["exitCode"])


if __name__ == "__main__":
    unittest.main()
