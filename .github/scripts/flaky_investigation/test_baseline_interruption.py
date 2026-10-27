"""Behavioral tests; all writable fixtures stay beside this packet."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import unittest
import uuid
from unittest.mock import patch

import baseline

HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("interruption", HERE / "baseline_interruption.py")
if SPEC and SPEC.loader and (HERE / "baseline_interruption.py").exists():
    proof = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(proof)
else:
    proof = None

REAL_POPEN = subprocess.Popen
CID = "a" * 64
IMAGE = "sha256:" + "b" * 64


class FixtureDocker:
    """Only Docker transport is doubled; baseline ownership/boundary stay real."""

    def __init__(self, root):
        self.root = root
        self.inspections = 0
        self.after_death = None
        self.record = {
            "Id": CID, "Image": IMAGE,
            "Config": {"User": f"{os.getuid()}:{os.getgid()}", "Labels": {
                baseline.LABEL: "owned-nonce", baseline.LABEL + ".source": baseline.SOURCE}},
            "HostConfig": {
                "ReadonlyRootfs": True, "Privileged": False, "NetworkMode": "bridge",
                "CapDrop": ["ALL"], "SecurityOpt": ["no-new-privileges"],
                "Memory": 6442450944, "MemorySwap": 6442450944,
                "PidsLimit": 512, "NanoCpus": 2000000000, "LogConfig": {"Type": "none"}},
            "Mounts": [{"Type": "bind", "Source": str(root / "work"),
                        "Destination": "/work", "RW": True}],
            "State": {"Status": "running", "Running": True, "Pid": 987, "ExitCode": 0},
        }

    def persist(self):
        (self.root / "fixture-docker.json").write_text(json.dumps(self.record))

    def __call__(self, args, **kwargs):
        if args[:1] == ["inspect"]:
            if args != ["inspect", CID]:
                raise subprocess.CalledProcessError(1, ["docker", *args], output="No such container")
            self.inspections += 1
            if self.inspections == 2 and self.after_death:
                self.after_death()
            return "[" + (self.root / "fixture-docker.json").read_text() + "]"
        raise AssertionError(f"Unexpected parent-side Docker mutation: {args}")


@unittest.skipUnless(os.name == "posix" and os.getuid() > 0 and os.getgid() > 0,
                     "Native SIGKILL and baseline identity require non-root POSIX")
class InterruptionTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(proof, "Missing bounded interruption proof implementation")
        self.area = HERE / ("case-" + uuid.uuid4().hex)
        self.area.mkdir(mode=0o700)
        self.addCleanup(shutil.rmtree, self.area)
        self.output = self.area / "output"
        self.receipts = self.area / "proof"
        self.sdk = self.area / "baseline-sdk.tar.gz"
        self.children = []
        self.requests = []
        self.mode = "normal"
        self.prepare = True
        self.docker = FixtureDocker(self.output)
        self.addCleanup(self.reap)

    def reap(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=3)

    def prepare_files(self):
        self.output.mkdir(mode=0o700)
        (self.output / "work").mkdir()
        plan = {
            "source": baseline.SOURCE, "image": IMAGE, "nonce": "owned-nonce",
            "network": "bridge", "work": str(self.output / "work"),
            "command": ["fixture-only"], "uid": os.getuid(), "gid": os.getgid(),
            "memory_bytes": 6442450944, "cpus": 2, "pids": 512,
            "accepted_test_counts": None,
        }
        baseline.save(self.output / "prepare-plan.json", plan)
        baseline.save(self.output / "prepare-start-intent.json", {"cid": CID})
        baseline.save(self.output / "image-receipt.json",
                      {"id": IMAGE, "base": baseline.BASE, "sdk": baseline.SDK,
                       "sdk_sha512": baseline.SDK_SHA512, "sdk_size": baseline.SDK_SIZE})
        (self.output / "prepare.cid").write_text(CID)
        self.docker.persist()

    def launch_fixture(self, argv, **kwargs):
        self.requests.append(list(argv))
        module = str(Path(baseline.__file__).resolve())
        if argv == [sys.executable, "-B", module, "run", "--sdk-archive",
                    str(self.sdk), "--output", str(self.output)]:
            if self.prepare:
                self.prepare_files()
            argv = [sys.executable, "-B", str(HERE / "native_fixture.py"),
                    "run", str(self.output), self.mode]
        elif argv == [sys.executable, "-B", module, "cleanup", "--output", str(self.output)]:
            argv = [sys.executable, "-B", str(HERE / "native_fixture.py"),
                    "cleanup", str(self.output), self.mode]
        else:
            raise AssertionError(f"Unsupported launcher/cleanup API: {argv}")
        child = REAL_POPEN(argv, **kwargs)
        self.children.append(child)
        if argv[3] == "run" and self.mode != "early":
            deadline = time.monotonic() + 2
            while not (self.area / "fixture-launcher-ready").exists():
                if child.poll() is not None or time.monotonic() >= deadline:
                    raise AssertionError("Native fixture did not enter its bounded sleep")
                time.sleep(.005)
        return child

    def run_proof(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "radical/aspire"}), \
                patch.object(baseline.platform, "system", return_value="Linux"), \
                patch.object(baseline.platform, "machine", return_value="x86_64"), \
                patch.object(baseline, "docker", self.docker), \
                patch.object(proof.subprocess, "Popen", side_effect=self.launch_fixture), \
                patch.object(proof, "READY_SECONDS", .4), \
                patch.object(proof, "POLL_SECONDS", .01):
            return proof.run(self.output, self.sdk, self.receipts)

    def result(self):
        return json.loads((self.receipts / "interruption.json").read_text())

    def test_native_owned_hung_launcher_dies_minus_nine_unrelated_child_survives(self):
        # Removing the actual handle.kill()/wait or accepting a live launcher breaks this.
        unrelated = REAL_POPEN([sys.executable, "-B", "-c", "import time; time.sleep(20)"])
        self.children.append(unrelated)
        result = self.run_proof()
        self.assertEqual(self.children[1].returncode, -signal.SIGKILL)
        self.assertIsNone(unrelated.poll())
        self.assertEqual(result["launcher_returncode"], -9)
        self.assertEqual(result["shell_equivalent_returncode"], 137)
        self.assertNotEqual(result["cleanup_pid"], result["launcher_pid"])
        self.assertEqual(result["cleanup_returncode"], 0)
        self.assertTrue(result["before"]["State"]["Running"])
        self.assertTrue(result["after_launcher_death"]["State"]["Running"])
        self.assertEqual(result["after_cleanup"]["Id"], CID)
        self.assertEqual(result["after_cleanup"]["State"]["Pid"], 0)
        self.assertFalse(result["after_cleanup"]["State"]["Running"])
        self.assertIsNone(result["accepted_test_counts"])
        self.assertEqual(result["status"], "owned-launcher-interruption-cleanup-confirmed")
        fresh = json.loads((self.output / "fixture-cleanup-process.json").read_text())
        self.assertEqual(fresh["pid"], result["cleanup_pid"])
        self.assertNotEqual(fresh["pid"], os.getpid())
        self.assertEqual(json.loads((self.output / "prepare-stopped.json").read_text())["cid"], CID)

    def test_unsupported_host_rejects_before_uid_or_spawn(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "radical/aspire"}), \
                patch.object(baseline.platform, "system", return_value="Darwin"), \
                patch.object(baseline, "host_identity", side_effect=AssertionError("UID touched")), \
                patch.object(proof.subprocess, "Popen", side_effect=AssertionError("spawned")):
            with self.assertRaisesRegex(ValueError, "Linux"):
                proof.run(self.output, self.sdk, self.receipts)
        self.assertFalse(self.receipts.exists())

    def test_native_identity_failure_prevents_any_child_or_kill(self):
        with patch("os.geteuid", return_value=0):
            with self.assertRaisesRegex(ValueError, "identity"):
                self.run_proof()
        self.assertEqual(self.children, [])
        self.assertEqual(self.requests, [])

    def test_native_work_owner_mismatch_is_rejected_before_proof_kill(self):
        # A fake claimed host identity must not override actual on-disk ownership.
        with patch.object(baseline, "host_identity", return_value=(os.getuid() + 1, os.getgid())):
            with self.assertRaisesRegex(ValueError, "owner"):
                self.run_proof()
        self.assertIsNone(self.result()["launcher_returncode"])
        self.assertEqual(self.result()["status"], "error")

    def test_foreign_repository_rejected_before_spawn(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "microsoft/aspire"}), \
                patch.object(proof.subprocess, "Popen", side_effect=AssertionError("spawned")):
            with self.assertRaisesRegex(ValueError, "radical/aspire"):
                proof.run(self.output, self.sdk, self.receipts)

    def test_changed_frozen_module_rejected_before_spawn(self):
        foreign = self.area / "foreign_baseline.py"
        foreign.write_text("not the frozen authority\n")
        with patch.object(baseline, "__file__", str(foreign)):
            with self.assertRaisesRegex(ValueError, "frozen v4"):
                self.run_proof()
        self.assertEqual(self.requests, [])

    def test_bounded_original_image_build_log_is_preserved_without_truncation(self):
        original = self.prepare_files
        def with_build_log():
            original()
            path = self.output / "image-receipt.json"
            value = json.loads(path.read_text())
            value["build_output"] = "x" * 70000
            path.write_text(json.dumps(value))
        with patch.object(self, "prepare_files", side_effect=with_build_log):
            try:
                result = self.run_proof()
            except ValueError as error:
                self.fail(f"Supported bounded baseline image receipt was rejected: {error}")
        retained = json.loads((self.receipts / "protected-before.json").read_text())
        self.assertEqual(json.loads(retained["image-receipt.json"])["build_output"], "x" * 70000)
        self.assertEqual(result["status"], "owned-launcher-interruption-cleanup-confirmed")

    def test_cli_rejects_pid_source_command_or_test_selection(self):
        for flag in ("--pid", "--source", "--command", "--test"):
            result = subprocess.run(
                [sys.executable, "-B", str(HERE / "baseline_interruption.py"), flag, "foreign"],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=3)
            self.assertEqual(result.returncode, 2)
            self.assertIn(b"unrecognized arguments", result.stderr)

    def test_absent_plan_cid_or_intent_never_produces_interruption_claim(self):
        for missing in ("prepare-plan.json", "prepare.cid", "prepare-start-intent.json"):
            with self.subTest(missing=missing):
                original = self.prepare_files
                def prepare_without():
                    original()
                    (self.output / missing).unlink()
                with patch.object(self, "prepare_files", side_effect=prepare_without):
                    with self.assertRaisesRegex((ValueError, TimeoutError), "prepare|readiness"):
                        self.run_proof()
                self.assertIsNone(self.result()["launcher_returncode"])
                self.assertEqual(self.result()["status"], "error")
                self.assertIsNotNone(self.result()["error"])
                self.assertIsNone(self.result()["accepted_test_counts"])
                self.reset_case()

    def reset_case(self):
        self.reap()
        self.children.clear()
        shutil.rmtree(self.output, ignore_errors=True)
        shutil.rmtree(self.receipts, ignore_errors=True)
        (self.area / "fixture-launcher-ready").unlink(missing_ok=True)

    def test_foreign_ownership_cid_intent_and_boundary_fail_before_proof_kill(self):
        for mutation in ("source", "nonce", "network", "cid", "intent", "user", "mount", "caps"):
            with self.subTest(mutation=mutation):
                original = self.prepare_files
                def changed():
                    original()
                    if mutation in {"source", "nonce", "network"}:
                        path = self.output / "prepare-plan.json"
                        value = json.loads(path.read_text())
                        value[mutation] = "foreign"
                        path.write_text(json.dumps(value))
                    elif mutation == "cid":
                        (self.output / "prepare.cid").write_text("f" * 64)
                    elif mutation == "intent":
                        (self.output / "prepare-start-intent.json").write_text(
                            json.dumps({"cid": "f" * 64}))
                    else:
                        if mutation == "user":
                            self.docker.record["Config"]["User"] = "0:0"
                        elif mutation == "mount":
                            self.docker.record["Mounts"][0]["Source"] = "/foreign"
                        else:
                            self.docker.record["HostConfig"]["CapDrop"] = []
                        self.docker.persist()
                with patch.object(self, "prepare_files", side_effect=changed):
                    with self.assertRaises((ValueError, subprocess.CalledProcessError)):
                        self.run_proof()
                self.assertIsNone(self.result()["launcher_returncode"])
                self.assertEqual(self.result()["status"], "error")
                self.assertIsNotNone(self.result()["error"])
                self.reset_case()
                self.docker = FixtureDocker(self.output)

    def test_early_launcher_exit_is_not_interruption(self):
        self.mode = "early"
        self.prepare = False
        with self.assertRaisesRegex(ValueError, "launcher exited"):
            self.run_proof()
        self.assertEqual(self.result()["early_launcher_returncode"], 23)
        self.assertIsNone(self.result()["launcher_returncode"])
        self.assertEqual(self.result()["status"], "error")

    def test_running_never_observed_is_bounded_error(self):
        self.docker.record["State"].update(Status="created", Running=False, Pid=0)
        with self.assertRaisesRegex(TimeoutError, "readiness"):
            self.run_proof()
        self.assertIsNone(self.result()["launcher_returncode"])

    def test_container_not_running_after_real_launcher_death_blocks_claim_and_cleanup(self):
        def ended():
            self.docker.record["State"].update(Status="exited", Running=False, Pid=0)
            self.docker.persist()
        self.docker.after_death = ended
        with self.assertRaisesRegex(ValueError, "still active"):
            self.run_proof()
        result = self.result()
        self.assertEqual(result["launcher_returncode"], -9)
        self.assertIsNone(result["cleanup_pid"])
        self.assertEqual(result["status"], "error")

    def test_changed_plan_after_launcher_death_blocks_claim(self):
        def changed():
            path = self.output / "prepare-plan.json"
            value = json.loads(path.read_text())
            value["command"] = ["changed-after-launch"]
            path.write_text(json.dumps(value))
        self.docker.after_death = changed
        with self.assertRaisesRegex(ValueError, "identity changed"):
            self.run_proof()
        self.assertEqual(self.result()["launcher_returncode"], -9)
        self.assertIsNone(self.result()["cleanup_pid"])

    def test_cleanup_deadline_is_error_and_fresh_owned_child_is_reaped(self):
        self.mode = "cleanup-hangs"
        with patch.object(proof, "CLEANUP_SECONDS", .15):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_proof()
        result = self.result()
        self.assertEqual(result["launcher_returncode"], -9)
        self.assertEqual(result.get("final_cleanup_returncode"), -signal.SIGTERM)
        self.assertEqual(result["error"]["type"], "TimeoutExpired")
        self.assertEqual(result["status"], "error")
        self.assertIsNotNone(self.children[-1].poll())

    def test_cleanup_error_preserves_kill_exit_and_failure_not_success(self):
        self.mode = "cleanup-fails"
        with self.assertRaises(subprocess.CalledProcessError) as caught:
            self.run_proof()
        self.assertEqual(caught.exception.returncode, 41)
        result = self.result()
        self.assertEqual(result["launcher_returncode"], -9)
        self.assertEqual(result["cleanup_returncode"], 41)
        self.assertEqual(result["status"], "error")
        self.assertIsNone(result["accepted_test_counts"])
        self.assertIn("fixture cleanup failed", (self.receipts / "cleanup.log").read_text())

    def test_zero_cleanup_exit_without_stopped_container_is_error(self):
        self.mode = "cleanup-noop"
        with self.assertRaisesRegex(ValueError, "stopped"):
            self.run_proof()
        self.assertEqual(self.result()["cleanup_returncode"], 0)
        self.assertEqual(self.result()["status"], "error")

    def test_foreign_stop_receipt_is_not_confirmation(self):
        self.mode = "cleanup-foreign-receipt"
        with self.assertRaisesRegex(ValueError, "Cleanup receipt"):
            self.run_proof()
        self.assertEqual(self.result()["cleanup_returncode"], 0)
        self.assertEqual(self.result()["status"], "error")
        self.assertIsNone(self.result()["accepted_test_counts"])

    def test_post_cleanup_identity_and_boundary_are_reverified(self):
        self.mode = "cleanup-boundary"
        with self.assertRaisesRegex(ValueError, "boundary"):
            self.run_proof()
        self.assertEqual(self.result()["status"], "error")
        # Retain the actual rejected record, not only the validation message.
        self.assertTrue(self.result().get("after_cleanup", {}).get("HostConfig", {}).get("Privileged"))
        self.assertTrue((self.receipts / "after-cleanup.json").exists())


if __name__ == "__main__":
    unittest.main()
