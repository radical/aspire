"""Behavioral tests use private regular files, never network or provider calls."""

import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
import uuid


ROOT = Path(__file__).parent
HELPER = ROOT / "provider_diagnostic.py"
MODEL = "gpt-6.1-sol"


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.root = ROOT / "test-work" / uuid.uuid4().hex
        self.root.mkdir(parents=True)
        self.logs = self.root / "logs"
        self.logs.mkdir()
        self.snapshot = self.logs / "models.json"
        self.state = self.root / "intent.json"
        self.receipt = self.root / "receipt.json"
        self.run_id = "123456"
        self.env = dict(os.environ, GITHUB_RUN_ID=self.run_id,
                        GITHUB_RUN_ATTEMPT="1", GITHUB_JOB="provider_diagnostic",
                        GITHUB_SHA="a" * 40, PYTHONDONTWRITEBYTECODE="1",
                        GITHUB_OUTPUT=str(self.root / "step-output"))

    def tearDown(self):
        shutil.rmtree(self.root)

    def call(self, operation, *extra):
        if self.receipt.exists():
            self.receipt.unlink()
        result = subprocess.run(
            [sys.executable, str(HELPER), operation, "--logs", str(self.logs),
             "--state", str(self.state), "--receipt", str(self.receipt), *extra],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=5, check=False,
        )
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        return result

    def prepare(self):
        result = self.call("prepare")
        self.assertEqual(result.returncode, 0)
        return json.loads(self.state.read_text())

    def data(self):
        observed = now()
        return {
            "timestamp": observed,
            "providers": {"copilot": {
                "configured": True, "models": [MODEL, "private-other-model"],
                "target": "private.provider.invalid",
                "model_metadata": [{
                    "id": MODEL, "source": "provider", "observed_at": observed,
                    "pricing": {"default": {
                        "input": 1.0, "output": 2.0, "cachedInput": 0.1,
                        "cacheWrite": 0.2,
                    }},
                }],
            }},
            "model_aliases": {"private": ["secret-model"]},
        }

    def write_data(self, data=None):
        self.snapshot.write_text(json.dumps(self.data() if data is None else data))

    def attest(self, **overrides):
        state = json.loads(self.state.read_text())
        state.update({
            "proxy": {
                "id": "b" * 64, "image_id": "sha256:" + "c" * 64,
                "image_ref": "ghcr.io/github/gh-aw-firewall/api-proxy:0.28.20"
                             "@sha256:30ab6d3261dd95364281fa0b52ab78450e1c01aa074eccc3a8d4f04b12a6560b",
                "host_logs": str(self.logs), "container_logs": "/var/log/api-proxy",
                "uid": os.geteuid(), "gid": os.getegid(),
                "created_ns": time.time_ns(),
            },
            "awf_exit": 0, "completed_ns": time.time_ns(),
            "cli_version": "1.0.85", "proxy_observations": 1,
            "discovery_direct_success": True,
            "proxy_absent_after_return": True,
        })
        state.update(overrides)
        self.state.with_suffix(".observed.json").write_text(json.dumps(state))
        self.state.with_suffix(".version").write_text("1.0.85\n")
        # This is the pinned structured event shape, not an arbitrary message match.
        (self.logs / "api-proxy.jsonl").write_text(json.dumps({
            "timestamp": now(), "event": "model_validation",
            "requested_model": MODEL, "resolved_via": "direct",
        }) + "\n")

    def observe(self, cleanup="absent"):
        data = json.loads(self.state.with_suffix(".observed.json").read_text())
        self.state.with_suffix(".observed.json").unlink()
        proxy = data["proxy"]
        proxy.update({
            "user": f"{os.geteuid()}:{os.getegid()}", "created": now(),
            "mounts": [{"Source": str(self.logs), "Destination": "/var/log/api-proxy",
                        "Type": "bind", "RW": True}],
        })
        (self.root / "docker-inspect.json").write_text(json.dumps(proxy))
        self.state.with_suffix(".exit").write_text("0\n")
        docker = self.root / "docker"
        docker.write_text(
            f"#!{sys.executable}\n"
            "import pathlib,sys,time\n"
            "root=pathlib.Path(__file__).parent\n"
            f"cleanup={cleanup!r}\n"
            "def cleanup_result():\n"
            " if cleanup=='outage':\n"
            "  print('SECRET daemon/permission failure',file=sys.stderr)\n"
            "  raise SystemExit(1)\n"
            " if cleanup=='timeout': time.sleep(4)\n"
            " if cleanup=='malformed': print('not-a-container-id')\n"
            " if cleanup=='present': print('b'*64)\n"
            " raise SystemExit(0)\n"
            "if sys.argv[1]=='ps':\n"
            " if any(arg.startswith('id=') for arg in sys.argv): cleanup_result()\n"
            " print('b'*64+' awf-api-proxy')\n"
            " raise SystemExit(0)\n"
            "if sys.argv[1]=='inspect':\n"
            " count=root/'inspect-count'\n"
            " if count.exists():\n"
            "  if cleanup=='absent': raise SystemExit(1)\n"
            "  cleanup_result()\n"
            " count.write_text('1')\n"
            " print((root/'docker-inspect.json').read_text())\n"
            "elif sys.argv[1]=='logs': print((root/'logs/api-proxy.jsonl').read_text())\n"
            "else: raise SystemExit(99)\n"
        )
        docker.chmod(0o700)
        for path in (self.root / "inspect-count", self.state.with_suffix(".ready")):
            if path.exists():
                path.unlink()
        result = self.call("watch", "--docker", str(docker), "--watch-seconds", "1")
        self.assertEqual(result.returncode, 0)

    def finish(self):
        result = self.call("finish", "--wait-seconds", "0")
        return result, json.loads(self.receipt.read_text())

    def reject(self, reason):
        result, receipt = self.finish()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(receipt["reason"], reason)
        self.assertFalse(receipt["inference_authorized"])
        return receipt

    def test_selected_model_only_and_no_inference_authority(self):
        self.prepare()
        self.write_data()
        self.attest()
        result, receipt = self.finish()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(receipt["status"], "provider-metadata-observed")
        self.assertFalse(receipt["inference_authorized"])
        self.assertEqual(receipt["medium_transport"], "unobserved")
        self.assertEqual(receipt["native_policy"], "unobserved")
        self.assertEqual(receipt["budget_guard_acceptance"], "unobserved")
        self.assertEqual(receipt["selected_sol"]["pricing"]["default"]["input"], 1.0)
        self.assertNotIn("private", self.receipt.read_text())

    def test_preexisting_snapshot_is_rejected_not_deleted(self):
        self.write_data()
        before = self.snapshot.read_bytes()
        self.assertNotEqual(self.call("prepare").returncode, 0)
        self.assertEqual(self.snapshot.read_bytes(), before)

    def test_preexisting_symlink_is_rejected(self):
        self.snapshot.symlink_to(self.root / "missing")
        self.assertNotEqual(self.call("prepare").returncode, 0)

    def test_missing_snapshot(self):
        self.prepare()
        self.attest()
        self.reject("snapshot-missing")

    def test_symlink_fifo_and_oversize_are_bounded(self):
        for kind in ("symlink", "fifo", "oversize"):
            with self.subTest(kind=kind):
                if self.snapshot.exists() or self.snapshot.is_symlink():
                    self.snapshot.unlink()
                if self.state.exists():
                    self.state.unlink()
                for existing in self.logs.iterdir():
                    existing.unlink()
                self.prepare()
                if kind == "symlink":
                    target = self.root / "private"
                    target.write_text("secret")
                    self.snapshot.symlink_to(target)
                elif kind == "fifo":
                    os.mkfifo(self.snapshot)
                else:
                    self.snapshot.write_bytes(b"x" * (1024 * 1024 + 1))
                self.attest()
                self.reject("snapshot-unsafe")

    def test_parent_symlink(self):
        self.prepare()
        self.write_data()
        self.attest()
        self.logs.rename(self.root / "real")
        self.logs.symlink_to(self.root / "real", target_is_directory=True)
        self.reject("snapshot-unsafe")

    def test_bad_json_duplicate_keys_and_null(self):
        self.prepare()
        for raw in ('{', '{"timestamp":null,"timestamp":"x"}', 'null', '[]'):
            with self.subTest(raw=raw):
                self.snapshot.write_text(raw)
                self.attest()
                self.reject("snapshot-malformed")

    def test_foreign_attempt_and_missing_attestation(self):
        self.prepare()
        self.write_data()
        self.attest()
        self.env["GITHUB_RUN_ATTEMPT"] = "2"
        self.reject("intent-mismatch")
        self.env["GITHUB_RUN_ATTEMPT"] = "1"
        self.attest(proxy=None)
        self.reject("proxy-unattested")

    def test_stale_snapshot_and_record_timestamps(self):
        self.prepare()
        for field in ("timestamp", "observed_at"):
            with self.subTest(field=field):
                data = self.data()
                if field == "timestamp":
                    data[field] = "2001-01-01T00:00:00Z"
                else:
                    data["providers"]["copilot"]["model_metadata"][0][field] = "2001-01-01T00:00:00Z"
                self.write_data(data)
                self.attest()
                self.reject("snapshot-stale")

    def test_wrong_owner(self):
        self.prepare()
        self.write_data()
        self.attest()
        state = json.loads(self.state.with_suffix(".observed.json").read_text())
        state["proxy"]["uid"] = os.geteuid() + 1
        self.state.with_suffix(".observed.json").write_text(json.dumps(state))
        self.reject("proxy-unattested")

    def test_failed_or_retried_invocation(self):
        self.prepare()
        self.write_data()
        self.attest(awf_exit=1)
        self.reject("awf-not-complete")
        self.attest(proxy_observations=2)
        self.reject("proxy-unattested")
        self.attest(proxy_absent_after_return=False)
        self.reject("proxy-cleanup-unobserved")

    def test_exact_membership_not_alias_or_fallback(self):
        self.prepare()
        for model in ("auto", "gpt-6.1", "gpt-6-astra", "copilot/gpt-6.1-sol"):
            with self.subTest(model=model):
                data = self.data()
                data["providers"]["copilot"]["models"] = [model]
                self.write_data(data)
                self.attest()
                self.reject("selected-model-unknown")

    def test_null_unconfigured_duplicate_metadata(self):
        self.prepare()
        for mutation in ("null", "configured", "duplicate", "source"):
            with self.subTest(mutation=mutation):
                data = self.data()
                p = data["providers"]["copilot"]
                if mutation == "null":
                    p["model_metadata"] = None
                elif mutation == "configured":
                    p["configured"] = False
                elif mutation == "duplicate":
                    p["model_metadata"] *= 2
                else:
                    p["model_metadata"][0]["source"] = "bundled"
                self.write_data(data)
                self.attest()
                self.reject("selected-model-unknown")

    def test_prices_complete_finite_nonnegative_not_bool(self):
        self.prepare()
        for value in (None, True, False, -1, float("inf"), float("nan"), "1", 10 ** 400):
            with self.subTest(value=repr(value)):
                data = self.data()
                data["providers"]["copilot"]["model_metadata"][0]["pricing"]["default"]["cacheWrite"] = value
                self.write_data(data)
                self.attest()
                self.reject("pricing-unknown")

    def test_optional_tier_all_fields_required(self):
        self.prepare()
        data = self.data()
        p = data["providers"]["copilot"]["model_metadata"][0]["pricing"]
        p["default"]["threshold"] = 128000
        p["long_context"] = dict(p["default"])
        self.write_data(data)
        self.attest()
        result, receipt = self.finish()
        self.assertEqual(result.returncode, 0)
        self.assertIn("long_context", receipt["selected_sol"]["pricing"])
        del p["long_context"]["cachedInput"]
        self.write_data(data)
        self.attest()
        self.reject("pricing-unknown")

    def test_unknown_tier_and_invalid_threshold(self):
        self.prepare()
        for mutation in ("tier", "threshold"):
            data = self.data()
            pricing = data["providers"]["copilot"]["model_metadata"][0]["pricing"]
            if mutation == "tier":
                pricing["secret-tier"] = dict(pricing["default"])
            else:
                pricing["default"]["threshold"] = True
            self.write_data(data)
            self.attest()
            self.reject("pricing-unknown")

    def test_file_presence_and_startup_are_not_discovery_success(self):
        self.prepare()
        self.write_data()
        self.attest()
        for event in ("startup", "model_fetch_error", "model_validation_skipped"):
            (self.logs / "api-proxy.jsonl").write_text(json.dumps({
                "timestamp": now(), "event": event, "error": "SECRET provider payload",
            }) + "\n")
            self.observe()
            receipt = self.reject("discovery-unobserved")
            self.assertNotIn("SECRET", json.dumps(receipt))

    def test_model_discovery_error_overrides_success(self):
        self.prepare()
        self.write_data()
        self.attest()
        with (self.logs / "api-proxy.jsonl").open("a") as f:
            f.write(json.dumps({"timestamp": now(), "event": "model_fetch_error"}) + "\n")
        self.observe()
        self.reject("discovery-unobserved")

    def test_actual_owned_docker_transport_double(self):
        self.prepare()
        self.write_data()
        self.attest()
        self.observe()
        result, receipt = self.finish()
        self.assertEqual(result.returncode, 0)
        self.assertEqual(receipt["selected_sol"]["discovery"], "direct-success")

    def test_unknown_image_mount_or_container_identity(self):
        self.prepare()
        self.write_data()
        for field, value in (("image_ref", "mutable:latest"),
                             ("host_logs", "/foreign"),
                             ("id", "foreign-container")):
            with self.subTest(field=field):
                self.attest()
                path = self.state.with_suffix(".observed.json")
                state = json.loads(path.read_text())
                state["proxy"][field] = value
                path.write_text(json.dumps(state))
                self.reject("proxy-unattested")

    def test_native_commands_are_never_invoked_and_secrets_never_emitted(self):
        self.prepare()
        self.write_data()
        self.attest()
        commands = self.root / "commands"
        commands.mkdir()
        for command in ("copilot", "gh", "curl", "node"):
            executable = commands / command
            executable.write_text(
                f"#!{sys.executable}\n"
                "import pathlib\n"
                "pathlib.Path(__file__).parent.joinpath('invoked').touch()\n"
                "raise SystemExit(98)\n"
            )
            executable.chmod(0o700)
        self.env["PATH"] = str(commands)
        self.env["COPILOT_GITHUB_TOKEN"] = "synthetic-SECRET-never-a-real-token"
        self.observe()
        result, receipt = self.finish()
        self.assertEqual(result.returncode, 0)
        self.assertFalse((commands / "invoked").exists())
        self.assertNotIn("SECRET", json.dumps(receipt))

    def test_missing_default_price_field_is_not_filled(self):
        self.prepare()
        for field in ("input", "output", "cachedInput", "cacheWrite"):
            data = self.data()
            del data["providers"]["copilot"]["model_metadata"][0]["pricing"]["default"][field]
            self.write_data(data)
            self.attest()
            self.reject("pricing-unknown")

    def test_changed_after_return_is_not_same_invocation_evidence(self):
        self.prepare()
        self.write_data()
        self.attest()
        self.write_data()
        self.reject("snapshot-stale")

    def test_bounded_unknown_cli_version_is_not_policy_or_inference(self):
        self.prepare()
        self.write_data()
        self.attest()
        self.state.with_suffix(".version").write_text("SECRET unknown-version\n")
        receipt = self.reject("cli-version-unobserved")
        self.assertNotIn("SECRET", json.dumps(receipt))

    def test_preexisting_receipt_cannot_gain_upload_authority(self):
        self.prepare()
        self.write_data()
        self.attest()
        self.receipt.write_text("SECRET preexisting untrusted artifact")
        result = subprocess.run(
            [sys.executable, str(HELPER), "finish", "--logs", str(self.logs),
             "--state", str(self.state), "--receipt", str(self.receipt),
             "--wait-seconds", "0"],
            env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=5, check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertEqual(result.stderr, b"")
        self.assertFalse((self.root / "step-output").exists())

    def test_observed_proxy_then_cleanup_transport_failure_never_means_absent(self):
        self.prepare()
        self.write_data()
        self.attest()
        self.observe(cleanup="outage")
        observed = json.loads(self.state.with_suffix(".observed.json").read_text())
        self.assertEqual(observed["proxy"]["id"], "b" * 64)
        self.assertTrue(observed["discovery_direct_success"])
        self.assertEqual(observed["awf_exit"], 0)
        receipt = self.reject("proxy-cleanup-unobserved")
        self.assertNotIn("SECRET", json.dumps(receipt))
        self.assertFalse(observed["proxy_absent_after_return"])

    def test_cleanup_timeout_malformed_or_still_present_never_means_absent(self):
        self.prepare()
        self.write_data()
        for cleanup in ("timeout", "malformed", "present"):
            with self.subTest(cleanup=cleanup):
                self.attest()
                self.observe(cleanup=cleanup)
                self.reject("proxy-cleanup-unobserved")


if __name__ == "__main__":
    unittest.main()
