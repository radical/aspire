"""Behavioral checks for the deliberately unwired trusted-baseline experiment."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


if importlib.util.find_spec("baseline"):
    import baseline
else:
    baseline = None


class Docker:
    def __init__(self):
        self.calls = []
        self.record = None
        self.exit_code = 0
        self.fail_stop = False
        self.fail_start = False
        self.fail_attach_after_exit = False

    def __call__(self, arguments, timeout=30, limit=1048576):
        self.calls.append(arguments)
        if arguments[:1] == ["create"]:
            value = lambda flag: arguments[arguments.index(flag) + 1]
            self.name = Path(value("--cidfile")).stem
            cid = "a" * 64
            Path(value("--cidfile")).write_text(cid)
            self.record = {
                "Id": cid, "Image": "sha256:" + "b" * 64,
                "Config": {"Labels": dict(item.split("=", 1) for index, item in enumerate(arguments)
                                          if index and arguments[index - 1] == "--label"),
                           "User": value("--user")},
                "State": {"Status": "created", "Running": False, "Pid": 0, "ExitCode": self.exit_code},
                "HostConfig": {
                    "ReadonlyRootfs": True, "Privileged": False,
                    "NetworkMode": value("--network"), "CapDrop": ["ALL"],
                    "SecurityOpt": ["no-new-privileges"],
                    "Memory": 6442450944, "MemorySwap": 6442450944,
                    "PidsLimit": 512, "NanoCpus": 2000000000,
                    "LogConfig": {"Type": "none"},
                },
                "Mounts": [{"Type": "bind", "Source": str(self.work), "Destination": "/work", "RW": True}],
            }
            return cid
        if arguments[:1] == ["inspect"]:
            return json.dumps([self.record])
        if arguments[:1] == ["start"]:
            # A fresh host process must already have enough durable identity.
            plan = json.loads((self.root / f"{self.name}-plan.json").read_text())
            assert plan["image"] == self.record["Image"]
            assert (self.root / f"{self.name}.cid").read_text() == self.record["Id"]
            if self.fail_start:
                raise subprocess.CalledProcessError(125, ["docker", *arguments], output="start failed")
            self.record["State"].update(Status="exited", Running=False, Pid=0, ExitCode=self.exit_code)
            if self.exit_code:
                raise subprocess.CalledProcessError(self.exit_code, ["docker", *arguments], output="test failed")
            if self.fail_attach_after_exit:
                raise subprocess.CalledProcessError(125, ["docker", *arguments], output="attach failed")
            return "finished"
        if arguments[:1] == ["kill"]:
            if not self.fail_stop:
                self.record["State"].update(Status="exited", Running=False, Pid=0)
            return ""
        if arguments[:1] == ["wait"]:
            if self.record["State"]["Status"] == "created":
                raise TimeoutError("docker wait cannot finish for a never-started container")
            return str(self.exit_code)
        raise AssertionError(arguments)


@unittest.skipUnless(
    os.name == "posix" and os.getuid() > 0 and os.getgid() > 0,
    "Linux-container ownership tests require a non-root POSIX host identity")
class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(baseline, "Trusted-baseline execution component is missing")
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.work = self.root / "work"
        self.work.mkdir()
        self.docker = Docker()
        self.docker.root, self.docker.work = self.root, self.work

    def stage(self, **kwargs):
        return baseline.Stage(self.root, self.work, "test", "sha256:" + "b" * 64,
                              ["bash", "-c", "exit 0"], network="none",
                              docker=self.docker, **kwargs)

    def test_persists_identity_before_start_and_confirms_whole_container_stop(self):
        result = self.stage().execute(20)
        self.assertTrue(result["stopped"])
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(json.loads((self.root / "test-stopped.json").read_text())["cid"], "a" * 64)
        create = self.docker.calls[0]
        self.assertEqual(create[create.index("--network") + 1], "none")
        self.assertEqual(create[create.index("--mount") + 1],
                         f"type=bind,src={self.work},dst=/work")
        self.assertEqual(create.count("--mount"), 1)

    def test_container_uid_gid_match_checkout_owner_required_by_bootstrap_chmod(self):
        installer = self.work / "dotnet-install.sh"
        installer.write_text("#!/bin/sh\n")
        installer.chmod(0o600)
        owner = installer.stat()
        stage = self.stage()
        stage.create()
        create = self.docker.calls[0]
        self.assertEqual(create[create.index("--user") + 1], f"{owner.st_uid}:{owner.st_gid}")
        plan = json.loads((self.root / "test-plan.json").read_text())
        self.assertEqual((plan["uid"], plan["gid"]), (owner.st_uid, owner.st_gid))
        installer.chmod(0o700)
        self.assertEqual(installer.stat().st_mode & 0o777, 0o700)

    def test_root_or_mismatched_host_identity_is_rejected_before_container_creation(self):
        uid, gid = os.getuid(), os.getgid()
        for real_uid, effective_uid, real_gid, effective_gid in (
                (0, 0, gid, gid), (uid, uid, 0, 0),
                (uid, uid + 1, gid, gid), (uid, uid, gid, gid + 1)):
            with self.subTest(identity=(real_uid, effective_uid, real_gid, effective_gid)), \
                    patch("os.getuid", return_value=real_uid), \
                    patch("os.geteuid", return_value=effective_uid), \
                    patch("os.getgid", return_value=real_gid), \
                    patch("os.getegid", return_value=effective_gid):
                with self.assertRaisesRegex(ValueError, "identity"):
                    self.stage()
        self.assertEqual(self.docker.calls, [])

    def test_foreign_owned_work_is_rejected_without_chown_or_container_creation(self):
        uid = os.getuid() + 1
        with patch("os.getuid", return_value=uid), patch("os.geteuid", return_value=uid):
            with self.assertRaisesRegex(ValueError, "owner"):
                self.stage().create()
        self.assertEqual(self.docker.calls, [])

    def test_cleanup_rejects_changed_container_user_without_killing_it(self):
        self.stage().create()
        self.docker.record["Config"]["User"] = "0:0"
        self.docker.record["State"].update(Status="running", Running=True, Pid=987)
        with self.assertRaisesRegex(ValueError, "identity"):
            baseline.cleanup(self.root, docker=self.docker)
        self.assertEqual([call for call in self.docker.calls if call[0] == "kill"], [])

    def test_fresh_process_cleanup_uses_only_protected_owned_cid(self):
        self.stage().create()
        self.docker.record["State"].update(Status="running", Running=True, Pid=987)
        baseline.cleanup(self.root, docker=self.docker)
        self.assertEqual([call for call in self.docker.calls if call[0] == "kill"],
                         [["kill", "a" * 64]])
        self.assertFalse(self.docker.record["State"]["Running"])

    def test_interruption_before_start_is_cleanable_without_waiting_for_start(self):
        self.stage().create()
        baseline.cleanup(self.root, docker=self.docker)
        self.assertTrue((self.root / "test-stopped.json").exists())

    def test_failed_docker_start_is_not_a_zero_exit_test(self):
        self.docker.fail_start = True
        with self.assertRaises((ValueError, subprocess.CalledProcessError)):
            self.stage().execute(20)

    def test_attach_transport_failure_cannot_be_a_success_shaped_fallback(self):
        self.docker.fail_attach_after_exit = True
        with self.assertRaises(ValueError):
            self.stage().execute(20)

    def test_reported_target_failure_retains_the_exact_container_exit(self):
        self.docker.exit_code = 2
        self.assertEqual(self.stage().execute(20)["exit_code"], 2)

    def test_stopped_container_is_not_restarted_by_duplicate_execution(self):
        stage = self.stage()
        stage.execute(20)
        with self.assertRaises(ValueError):
            stage.execute(20)
        self.assertEqual(len([call for call in self.docker.calls if call[0] == "start"]), 1)

    def test_foreign_container_is_never_cancelled(self):
        self.stage().create()
        self.docker.record["Config"]["Labels"] = {}
        self.docker.record["State"].update(Status="running", Running=True, Pid=987)
        with self.assertRaisesRegex(ValueError, "identity"):
            baseline.cleanup(self.root, docker=self.docker)
        self.assertEqual([call for call in self.docker.calls if call[0] == "kill"], [])

    def test_extra_mount_and_privilege_are_rejected_before_start(self):
        for field in ("mount", "privilege", "network", "user"):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                self.docker.root = root
                stage = baseline.Stage(root, self.work, "test", "sha256:" + "b" * 64,
                                       ["true"], network="none", docker=self.docker)
                stage.create()
                if field == "mount":
                    self.docker.record["Mounts"].append({"Source": "/secret"})
                elif field == "privilege":
                    self.docker.record["HostConfig"]["Privileged"] = True
                elif field == "network":
                    self.docker.record["HostConfig"]["NetworkMode"] = "host"
                else:
                    self.docker.record["Config"]["User"] = "0"
                with self.assertRaises(ValueError):
                    stage.execute(20)
                self.assertEqual([call for call in self.docker.calls if call[0] == "start"], [])

    def test_unconfirmed_cleanup_is_not_success(self):
        stage = self.stage()
        stage.create()
        self.docker.record["State"].update(Status="running", Running=True, Pid=987)
        self.docker.fail_stop = True
        with self.assertRaisesRegex(ValueError, "stopped"):
            stage.stop()
        self.assertFalse((self.root / "test-stopped.json").exists())

    def test_attach_failure_still_confirms_stop_and_preserves_failure(self):
        stage = self.stage()
        stage.create()
        real = self.docker
        def disconnected(arguments, **kwargs):
            if arguments[0] == "start":
                real.record["State"].update(Status="running", Running=True, Pid=987)
                raise TimeoutError("attach interrupted")
            return real(arguments, **kwargs)
        stage.docker = disconnected
        with self.assertRaisesRegex(TimeoutError, "attach interrupted"):
            stage.execute(20)
        self.assertTrue((self.root / "test-stopped.json").exists())

    def test_output_import_rejects_nonregular_or_linked_files_and_root_symlink(self):
        cases = ("root", "file", "hardlink", "fifo", "size")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                results = root / "results"
                results.mkdir()
                source = root / "source"
                source.write_text("untrusted")
                if case == "root":
                    results.rmdir()
                    results.symlink_to(root, target_is_directory=True)
                elif case == "file":
                    (results / "out.trx").symlink_to(source)
                elif case == "hardlink":
                    os.link(source, results / "out.trx")
                elif case == "fifo":
                    os.mkfifo(results / "out.trx")
                else:
                    (results / "out.trx").write_bytes(b"x" * 33)
                with self.assertRaises(ValueError):
                    baseline.import_outputs(results, root / "import", stopped=True, byte_limit=32)

    def test_private_owner_only_output_is_importable_after_confirmed_stop(self):
        report = self.work / "original.trx"
        report.write_text(self.trx())
        report.chmod(0o600)
        baseline.import_outputs(self.work, self.root / "import", stopped=True)
        self.assertEqual((self.root / "import" / "original.trx").read_text(), self.trx())

    def test_guest_ownership_preflight_rejects_foreign_installer_before_bootstrap(self):
        guard = getattr(baseline, "OWNERSHIP_CHECK", "")
        self.assertTrue(guard, "Guest ownership preflight is missing")
        installer = self.work / "eng" / "common" / "dotnet-install.sh"
        installer.parent.mkdir(parents=True)
        installer.write_text("#!/bin/sh\n")
        installer.chmod(0o600)
        self.assertEqual((installer.stat().st_uid, installer.stat().st_gid), baseline.host_identity())
        subprocess.run(["bash", "-euc", guard + "chmod +x eng/common/dotnet-install.sh"],
                       cwd=self.work, check=True, capture_output=True)
        self.assertTrue(installer.stat().st_mode & 0o100)
        installer.unlink()
        installer.symlink_to("/bin/sh")
        result = subprocess.run(["bash", "-euc", guard], cwd=self.work, capture_output=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"ownership", result.stderr)

    def test_import_bounds_empty_directories_as_well_as_file_bytes(self):
        for index in range(129):
            (self.work / str(index)).mkdir()
        with self.assertRaises(ValueError):
            baseline.import_outputs(self.work, self.root / "import", stopped=True)

    def test_command_capture_bounds_flood_and_timeout(self):
        with self.assertRaisesRegex(ValueError, "output"):
            baseline.command([sys.executable, "-c", "print('x'*100000)"], limit=256)
        with self.assertRaises(TimeoutError):
            baseline.command([sys.executable, "-c", "import time; time.sleep(20)"], timeout=.05)

    @staticmethod
    def trx(outcome="Passed", method="TestProjectStartsAndStopsCleanly"):
        return (f'<TestRun><TestDefinitions><UnitTest id="a"><TestMethod '
                f'className="Aspire.Hosting.Tests.SlimTestProgramTests" name="{method}"/>'
                f'</UnitTest></TestDefinitions><Results><UnitTestResult testId="a" '
                f'executionId="b" outcome="{outcome}"/></Results></TestRun>')

    def test_pipeline_separates_preparation_network_and_imports_only_after_stop(self):
        output = self.root / "run"
        def prepared_source(root):
            work = root / "work"
            work.mkdir()
            (work / "source").mkdir()
            (work / "results").mkdir()
            (work / "results" / "original.trx").write_text(self.trx())
            self.docker.root, self.docker.work = root, work
            return work
        real_stage = baseline.Stage
        def stage(*args, **kwargs):
            return real_stage(*args, **kwargs, docker=self.docker)
        with patch.object(baseline, "authorize"), \
                patch.object(baseline, "source", side_effect=prepared_source), \
                patch.object(baseline, "image", return_value="sha256:" + "b" * 64), \
                patch.object(baseline, "Stage", side_effect=stage):
            baseline.run(output, self.root / "sdk.tar.gz")
        prepare = json.loads((output / "prepare-plan.json").read_text())
        test = json.loads((output / "test-plan.json").read_text())
        self.assertEqual((prepare["network"], test["network"]), ("bridge", "none"))
        self.assertEqual(test["source"], "934fbbbe0942da5025abdfefa93edca1ad9ec633")
        self.assertTrue((output / "test-stopped.json").exists())
        observed = json.loads((output / "observations.json").read_text())
        self.assertEqual(observed["untrusted_outcomes"], ["Passed"])
        self.assertIsNone(observed["accepted_test_counts"])

    @unittest.skipUnless(
        (Path(__file__).resolve().parents[3] / ".dotnet" / "dotnet").is_file(),
        "Project operand regression requires an already provisioned repository .NET SDK")
    def test_preparation_project_resolves_from_the_source_not_the_msbuild_consumer(self):
        source = self.root / "source tree"
        project = source / baseline.PROJECT
        project.parent.mkdir(parents=True)
        project.write_text(
            '<Project><Target Name="Probe"><WriteLinesToFile '
            'File="$(MSBuildProjectDirectory)/resolved.txt" Lines="$(MSBuildProjectFullPath)" '
            'Overwrite="true"/></Target></Project>')
        consumer = self.root / "arcade" / "Build.proj"
        consumer.parent.mkdir()
        # Arcade materializes -projects as ProjectToBuild, then invokes MSBuild
        # from its own tools/Build.proj, not the shell's working directory.
        consumer.write_text(
            '<Project><ItemGroup><ProjectToBuild Include="$(Projects)"/></ItemGroup>'
            '<Target Name="Build"><MSBuild Projects="@(ProjectToBuild)" '
            'Targets="Probe"/></Target></Project>')
        build = source / "build.sh"
        build.write_text(
            '#!/bin/bash\nset -eu\n'
            'while [[ "$1" != -projects ]]; do shift; done\n'
            'exec "$OPERAND_DOTNET" msbuild "$OPERAND_CONSUMER" -nologo "-p:Projects=$2"\n')
        build.chmod(0o700)
        with patch.object(baseline, "authorize"), \
                patch.object(baseline, "source", return_value=self.work), \
                patch.object(baseline, "image", return_value="sha256:" + "b" * 64), \
                patch.object(baseline, "Stage") as stage:
            stage.return_value.execute.return_value = {"exit_code": 1}
            with self.assertRaisesRegex(ValueError, "restore/build"):
                baseline.run(self.root / "operand-run", self.root / "sdk.tar.gz")
        script = stage.call_args.args[4][2]
        command = next(line for line in script.splitlines() if line.startswith("./build.sh "))
        result = subprocess.run(
            ["bash", "-euc", command], cwd=source, capture_output=True, text=True, timeout=30,
            env={**os.environ,
                 "OPERAND_DOTNET": str(Path(__file__).resolve().parents[3] / ".dotnet" / "dotnet"),
                 "OPERAND_CONSUMER": str(consumer)})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        resolved = Path((project.parent / "resolved.txt").read_text().strip())
        self.assertEqual(resolved.resolve(), project.resolve())

    def test_sdk_type_size_and_checksum_reject_before_image_build(self):
        archive = self.root / "sdk.tar.gz"
        archive.write_bytes(b"not the vendor SDK archive")
        for size in (207623687, archive.stat().st_size):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory, \
                    patch.object(baseline, "SDK_SIZE", size):
                with self.assertRaisesRegex(ValueError, "SDK"):
                    baseline.image(Path(directory), archive)

    def test_source_bootstrap_rejects_an_unenrolled_sdk(self):
        source_root = self.root / "source-fixture"
        source_root.mkdir()
        def git(arguments, **kwargs):
            checkout = source_root / "work" / "source"
            if "init" in arguments:
                checkout.mkdir()
                (checkout / "global.json").write_text(json.dumps({
                    "sdk": {"version": "10.0.100"}, "tools": {"dotnet": "10.0.100"}}))
            return "934fbbbe0942da5025abdfefa93edca1ad9ec633\n" if "rev-parse" in arguments else ""
        with patch.object(baseline, "command", side_effect=git):
            with self.assertRaisesRegex(ValueError, "SDK"):
                baseline.source(source_root)


class BaselineContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        # Parser contracts run on Windows too; POSIX no-follow file imports are
        # exercised separately by the native filesystem tests.
        reader = patch.object(baseline, "read_regular", side_effect=lambda path, maximum: path.read_bytes())
        reader.start()
        self.addCleanup(reader.stop)

    def test_missing_skipped_empty_or_foreign_trx_is_not_an_execution(self):
        with self.assertRaisesRegex(ValueError, "report"):
            baseline.observations(self.root, 0)
        for outcome, method in (("NotExecuted", "TestProjectStartsAndStopsCleanly"),
                                ("Passed", "Foreign"), ("", "TestProjectStartsAndStopsCleanly")):
            with self.subTest(outcome=outcome):
                (self.root / "original.trx").write_text(BaselineTests.trx(outcome, method))
                with self.assertRaises(ValueError):
                    baseline.observations(self.root, 0)

    def test_test_bytes_remain_untrusted_even_when_selected_record_passes(self):
        (self.root / "original.trx").write_text(BaselineTests.trx())
        result = baseline.observations(self.root, 0)
        self.assertEqual(result["untrusted_outcomes"], ["Passed"])
        self.assertIsNone(result["accepted_test_counts"])
        with self.assertRaises(ValueError):
            baseline.observations(self.root, 2)

    def test_import_never_runs_before_confirmed_stop(self):
        with self.assertRaisesRegex(ValueError, "stop"):
            baseline.import_outputs(self.root, self.root / "import", stopped=False)

    def test_source_bootstrap_is_not_executed_for_wrong_authority_or_platform(self):
        for repository, system, machine in (
                ("microsoft/aspire", "Linux", "x86_64"),
                ("radical/aspire", "Darwin", "arm64"),
                ("radical/aspire", "Linux", "aarch64")):
            with self.subTest(repository=repository, system=system), \
                    patch.dict(os.environ, {"GITHUB_REPOSITORY": repository}), \
                    patch("platform.system", return_value=system), \
                    patch("platform.machine", return_value=machine):
                with self.assertRaises(ValueError):
                    baseline.authorize()

    def test_windows_is_rejected_before_unavailable_unix_identity_calls(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "radical/aspire"}), \
                patch("platform.system", return_value="Windows"), \
                patch("os.getuid", side_effect=AssertionError("Unix UID read on Windows"), create=True):
            with self.assertRaisesRegex(ValueError, "Linux"):
                baseline.authorize()

    def test_root_linux_host_is_rejected_before_any_work_is_created(self):
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "radical/aspire"}), \
                patch("platform.system", return_value="Linux"), \
                patch("platform.machine", return_value="x86_64"), \
                patch("os.getuid", return_value=0, create=True), \
                patch("os.geteuid", return_value=0, create=True), \
                patch("os.getgid", return_value=1001, create=True), \
                patch("os.getegid", return_value=1001, create=True):
            with self.assertRaisesRegex(ValueError, "identity"):
                baseline.authorize()


if __name__ == "__main__":
    unittest.main()
