"""Opt-in real GitHub contract coverage; no native inference or remote effects."""

import json
import os
import subprocess
import sys
import unittest

from helpers import WorkspaceTest
import local


@unittest.skipUnless(os.environ.get("CI_SHEPHERD_LIVE_CONTRACT") == "1",
                     "requires explicit read-only live-contract opt-in and tracker configuration")
class LiveContractTests(WorkspaceTest, unittest.TestCase):
    def test_real_review_feedback_through_local_credentials_and_sealed_transport(self):
        arguments = [sys.executable, "-B", str(local.ROOT / ".github/workflows/ci-shepherd/local.py"), "check-api"]
        for flag, key in (("--target", "TARGET"), ("--pr", "PR"), ("--tracker", "TRACKER"),
                          ("--authority", "AUTHORITY"), ("--tracker-node", "TRACKER_NODE")):
            variable = "CI_SHEPHERD_LIVE_" + key
            self.assertTrue(os.environ.get(variable), f"explicit {variable} required")
            arguments += [flag, os.environ[variable]]
        arguments += ["--workdir", str(self.work)]
        result = subprocess.run(arguments, capture_output=True, text=True, timeout=120)
        self.assertEqual(0, result.returncode, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual("api contract verified; read-only", report["outcome"])
        self.assertEqual(int(os.environ["CI_SHEPHERD_LIVE_PR"]), report["number"])
        self.assertGreater(report["reviewComments"], 0)
        self.assertRegex(report["head"], r"^[0-9a-f]{40}$")
        self.assertIsInstance(report["resolved"], list)
