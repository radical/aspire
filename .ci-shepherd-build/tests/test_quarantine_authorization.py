from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from ci_shepherd.quarantine_authorization import (
    authorize_quarantine_start,
    create_quarantine_grant,
    write_quarantine_grant,
)
from ci_shepherd.quarantine import record_quarantine_session_event
from ci_shepherd.repository_policy import load_repository_policy_document


def retry_identity(
    run_id: int,
    attempt: int,
    job_id: int,
) -> dict[str, object]:
    return {
        "runId": run_id,
        "attempt": attempt,
        "jobId": job_id,
        "headSha": f"{run_id:040x}",
        "workflow": "CI",
        "jobName": "Tests / unit (ubuntu-latest)",
        "lane": "unit",
        "os": "ubuntu-latest",
    }


def repository_policy_identity(repository: str) -> dict[str, object]:
    repositories = [repository]
    if repository != "microsoft/aspire":
        repositories.append("microsoft/aspire")
    policy = load_repository_policy_document(
        {
            "schemaVersion": 1,
            "policyVersion": "test-v1",
            "repositories": repositories,
            "retryTestResults": {
                "aggregateJobSuffixes": ["Aggregate Results"],
                "artifactNames": ["Combined-Results"],
                "trxPathPattern": (
                    r"^(?P<os>[^/]+)/testresults/"
                    r"(?P<lane>.+)_net[^_]+_[^/]+\.trx$"
                ),
                "jobNamePattern": (
                    r"^(?:.* / )?(?P<lane>[^/]+) "
                    r"\((?P<os>[^()]+)\)$"
                ),
                "trustedEvents": ["push", "workflow_dispatch"],
                "requireHeadRepositoryMatch": True,
            },
            "quarantinePullRequest": {
                "baseRef": "main",
                "allowedHeadRepositories": [repository],
                "requiredApprovingReviews": 1,
            },
        }
    )
    return {**policy.as_public_dict(), "digest": policy.digest}


class QuarantineAuthorizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.state_dir = self.root / "state"
        self.request_path = self.root / "request.json"
        self.authorization_path = self.root / "authorization.json"
        repository_policy = repository_policy_identity("radical/aspire")
        self.request = {
            "schemaVersion": 1,
            "repository": "radical/aspire",
            "snapshotId": "snapshot:radical/aspire:2026-08-30T00:00:00Z",
            "repositoryPolicy": repository_policy,
            "repositoryPolicyDigest": repository_policy["digest"],
            "batchId": "quarantine:1",
            "sourceRevision": "a" * 40,
            "sourceTreeDigest": "sha256:" + "b" * 64,
            "tests": [
                {
                    "testName": "Tests.One",
                    "issueNumber": 1,
                    "issueUrl": "https://github.com/radical/aspire/issues/1",
                    "evidenceClass": "A",
                    "evidenceReason": (
                        "the exact test failed and later passed in the same "
                        "run, commit, and job lane"
                    ),
                    "evidenceIds": [
                        "run:200:attempt:1:job:901:test-results",
                        "run:200:attempt:2:job:902:test-results",
                    ],
                    "failureOccurrenceId": "occurrence:1:200:1:901:1",
                    "recoveryCoverageId": (
                        "coverage:run:200:attempt:2:job:902:test:Tests.One"
                    ),
                    "failureIdentity": retry_identity(200, 1, 901),
                    "recoveryIdentity": retry_identity(200, 2, 902),
                    "sourceLocation": {"file": "OneTests.cs", "line": 10},
                    "sourceValidation": {
                        "fileSemanticDigest": "sha256:" + "c" * 64,
                        "fileQuarantines": [],
                    },
                },
                {
                    "testName": "Tests.Two",
                    "issueNumber": 2,
                    "issueUrl": "https://github.com/radical/aspire/issues/2",
                    "evidenceClass": "A",
                    "evidenceReason": (
                        "the exact test failed and later passed in the same "
                        "run, commit, and job lane"
                    ),
                    "evidenceIds": [
                        "run:201:attempt:1:job:903:test-results",
                        "run:201:attempt:2:job:904:test-results",
                    ],
                    "failureOccurrenceId": "occurrence:2:201:1:903:1",
                    "recoveryCoverageId": (
                        "coverage:run:201:attempt:2:job:904:test:Tests.Two"
                    ),
                    "failureIdentity": retry_identity(201, 1, 903),
                    "recoveryIdentity": retry_identity(201, 2, 904),
                    "sourceLocation": {"file": "TwoTests.cs", "line": 20},
                    "sourceValidation": {
                        "fileSemanticDigest": "sha256:" + "d" * 64,
                        "fileQuarantines": [],
                    },
                },
            ],
        }
        self.now = datetime(2026, 8, 30, tzinfo=timezone.utc)
        self._write_request()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_exact_grant_authorizes_one_batch(self) -> None:
        self._write_grant()

        grant = json.loads(self.authorization_path.read_text(encoding="utf-8"))
        self.assertEqual(
            self.request["repositoryPolicyDigest"],
            grant["repositoryPolicyDigest"],
        )

        result = authorize_quarantine_start(
            request_path=self.request_path,
            authorization_path=self.authorization_path,
            state_dir=self.state_dir,
            batch_id="quarantine:1",
            now=self.now + timedelta(minutes=1),
        )

        self.assertEqual(self.request, result.request)
        self.assertTrue(result.grant_id.startswith("quarantine-grant:"))
        self.assertEqual("2026-08-30T00:15:00Z", result.expires_at)

    def test_plan_wrapper_is_bound_and_returns_its_proposal(self) -> None:
        plan = {
            "schemaVersion": 1,
            "repository": "radical/aspire",
            "proposal": self.request,
        }
        self.request_path.write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        self._write_grant()

        result = authorize_quarantine_start(
            request_path=self.request_path,
            authorization_path=self.authorization_path,
            state_dir=self.state_dir,
            batch_id="quarantine:1",
            now=self.now + timedelta(minutes=1),
        )

        self.assertEqual(self.request, result.request)

    def test_grant_rejects_repository_policy_content_tampering(self) -> None:
        policy = self.request["repositoryPolicy"]
        assert isinstance(policy, dict)
        retry_results = policy["retryTestResults"]
        assert isinstance(retry_results, dict)
        retry_results["artifactNames"] = ["Substituted-Results"]
        self._write_request()

        with self.assertRaisesRegex(ValueError, "repositoryPolicy digest"):
            self._write_grant()

    def test_grant_rejects_repository_policy_digest_substitution(self) -> None:
        self.request["repositoryPolicyDigest"] = "sha256:" + "f" * 64
        self._write_request()

        with self.assertRaisesRegex(
            ValueError,
            "repositoryPolicyDigest does not match repositoryPolicy",
        ):
            self._write_grant()

    def test_one_byte_plan_change_is_rejected(self) -> None:
        self._write_grant()
        self.request_path.write_bytes(self.request_path.read_bytes() + b" ")

        with self.assertRaisesRegex(ValueError, "digest"):
            authorize_quarantine_start(
                request_path=self.request_path,
                authorization_path=self.authorization_path,
                state_dir=self.state_dir,
                batch_id="quarantine:1",
                now=self.now + timedelta(minutes=1),
            )

    def test_different_state_directory_is_rejected(self) -> None:
        self._write_grant()

        with self.assertRaisesRegex(ValueError, "stateDirectory"):
            authorize_quarantine_start(
                request_path=self.request_path,
                authorization_path=self.authorization_path,
                state_dir=self.root / "other-state",
                batch_id="quarantine:1",
                now=self.now + timedelta(minutes=1),
            )

    def test_expired_grant_is_rejected(self) -> None:
        self._write_grant()

        with self.assertRaisesRegex(ValueError, "not currently valid"):
            authorize_quarantine_start(
                request_path=self.request_path,
                authorization_path=self.authorization_path,
                state_dir=self.state_dir,
                batch_id="quarantine:1",
                now=self.now + timedelta(minutes=16),
            )

    def test_production_repository_is_denied(self) -> None:
        self.request["repository"] = "microsoft/aspire"
        for test in self.request["tests"]:
            test["issueUrl"] = test["issueUrl"].replace(
                "github.com/radical/aspire/",
                "github.com/microsoft/aspire/",
            )
        self._write_request()

        with self.assertRaisesRegex(ValueError, "forbidden"):
            create_quarantine_grant(
                request_path=self.request_path,
                state_dir=self.state_dir,
                batch_id="quarantine:1",
                issued_at=self.now,
            )
        self.assertFalse(self.authorization_path.exists())

    def test_grant_requires_a_source_bound_request(self) -> None:
        self.request.pop("sourceRevision", None)
        self._write_request()

        with self.assertRaisesRegex(ValueError, "sourceRevision"):
            create_quarantine_grant(
                request_path=self.request_path,
                state_dir=self.state_dir,
                batch_id="quarantine:1",
                issued_at=self.now,
            )

    def test_grant_requires_class_a_evidence_for_every_test(self) -> None:
        self.request["tests"][0].pop("evidenceClass", None)
        self._write_request()

        with self.assertRaisesRegex(ValueError, "evidenceClass"):
            create_quarantine_grant(
                request_path=self.request_path,
                state_dir=self.state_dir,
                batch_id="quarantine:1",
                issued_at=self.now,
            )

    def test_grant_binds_failure_and_recovery_logs(self) -> None:
        self.request["tests"][0]["evidenceIds"] = [
            "run:200:attempt:1:job:901:test-results"
        ]
        self._write_request()

        with self.assertRaisesRegex(ValueError, "evidenceIds"):
            create_quarantine_grant(
                request_path=self.request_path,
                state_dir=self.state_dir,
                batch_id="quarantine:1",
                issued_at=self.now,
            )

    def test_grant_requires_a_source_baseline_for_every_test(self) -> None:
        self.request["tests"][0].pop("sourceLocation", None)
        self._write_request()

        with self.assertRaisesRegex(ValueError, "sourceLocation"):
            create_quarantine_grant(
                request_path=self.request_path,
                state_dir=self.state_dir,
                batch_id="quarantine:1",
                issued_at=self.now,
            )

    def test_grant_output_must_not_be_a_symlink(self) -> None:
        target = self.root / "target.json"
        target.write_text("unchanged", encoding="utf-8")
        self.authorization_path.symlink_to(target)
        grant = create_quarantine_grant(
            request_path=self.request_path,
            state_dir=self.state_dir,
            batch_id="quarantine:1",
            issued_at=self.now,
        )

        with self.assertRaisesRegex(ValueError, "must not be a symlink"):
            write_quarantine_grant(self.authorization_path, grant)

        self.assertEqual("unchanged", target.read_text(encoding="utf-8"))

    def test_changed_test_set_is_rejected_even_with_matching_batch(self) -> None:
        self._write_grant()
        changed = deepcopy(self.request)
        changed["tests"].append(
            {
                **deepcopy(self.request["tests"][0]),
                "testName": "Tests.Three",
                "issueNumber": 3,
                "issueUrl": "https://github.com/radical/aspire/issues/3",
                "evidenceIds": [
                    "run:202:attempt:1:job:905:test-results",
                    "run:202:attempt:2:job:906:test-results",
                ],
                "failureOccurrenceId": "occurrence:3:202:1:905:1",
                "recoveryCoverageId": (
                    "coverage:run:202:attempt:2:job:906:test:Tests.Three"
                ),
                "failureIdentity": retry_identity(202, 1, 905),
                "recoveryIdentity": retry_identity(202, 2, 906),
                "sourceLocation": {"file": "ThreeTests.cs", "line": 30},
            }
        )
        self.request_path.write_text(
            json.dumps(changed, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "digest"):
            authorize_quarantine_start(
                request_path=self.request_path,
                authorization_path=self.authorization_path,
                state_dir=self.state_dir,
                batch_id="quarantine:1",
                now=self.now + timedelta(minutes=1),
            )

    def test_retry_identity_mismatch_is_rejected_before_grant(self) -> None:
        self.request["tests"][0]["recoveryIdentity"]["os"] = (
            "windows-latest"
        )
        self._write_request()

        with self.assertRaisesRegex(ValueError, "retry identity"):
            create_quarantine_grant(
                request_path=self.request_path,
                state_dir=self.state_dir,
                batch_id="quarantine:1",
                issued_at=self.now,
            )

    def test_consumed_grant_cannot_restart_after_failure(self) -> None:
        self._write_grant()
        authorized = authorize_quarantine_start(
            request_path=self.request_path,
            authorization_path=self.authorization_path,
            state_dir=self.state_dir,
            batch_id="quarantine:1",
            now=self.now + timedelta(minutes=1),
        )
        recorded_at = "2026-08-30T00:01:00Z"
        record_quarantine_session_event(
            self.state_dir,
            authorized.request,
            status="started",
            recorded_at=recorded_at,
            session_id="session-1",
            authorization_grant_id=authorized.grant_id,
            authorization_expires_at=authorized.expires_at,
        )
        record_quarantine_session_event(
            self.state_dir,
            authorized.request,
            status="failed",
            recorded_at="2026-08-30T00:02:00Z",
            session_id="session-1",
            failure_reason="Worker failed before changing the checkout.",
        )

        with self.assertRaisesRegex(ValueError, "already been consumed"):
            record_quarantine_session_event(
                self.state_dir,
                authorized.request,
                status="started",
                recorded_at="2026-08-30T00:03:00Z",
                session_id="session-2",
                authorization_grant_id=authorized.grant_id,
                authorization_expires_at=authorized.expires_at,
            )

    def test_cli_requires_and_consumes_the_exact_grant(self) -> None:
        plan = {
            "schemaVersion": 1,
            "repository": "radical/aspire",
            "snapshotId": self.request["snapshotId"],
            "proposal": self.request,
        }
        self.request_path.write_text(
            json.dumps(plan, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        script_root = Path(__file__).resolve().parents[1] / "scripts"
        grant_process = subprocess.run(
            [
                sys.executable,
                str(script_root / "authorize_quarantine.py"),
                "--request",
                str(self.request_path),
                "--state-dir",
                str(self.state_dir),
                "--batch-id",
                "quarantine:1",
                "--output",
                str(self.authorization_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            "quarantine:1",
            json.loads(grant_process.stdout)["allowedBatchId"],
        )
        recorded_at = datetime.now(timezone.utc).isoformat()
        base_command = [
            sys.executable,
            str(script_root / "quarantine_session.py"),
            "--state-dir",
            str(self.state_dir),
            "--request",
            str(self.request_path),
            "--batch-id",
            "quarantine:1",
            "--recorded-at",
            recorded_at,
        ]
        subprocess.run(
            [
                *base_command,
                "--authorization",
                str(self.authorization_path),
                "--status",
                "started",
                "--session-id",
                "session-1",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(
            [
                *base_command,
                "--status",
                "failed",
                "--session-id",
                "session-1",
                "--failure-reason",
                "Worker stopped before changing the checkout.",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        replay = subprocess.run(
            [
                *base_command,
                "--authorization",
                str(self.authorization_path),
                "--status",
                "started",
                "--session-id",
                "session-2",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        self.assertNotEqual(0, replay.returncode)
        self.assertIn("already been consumed", replay.stderr)

    def _write_request(self) -> None:
        self.request_path.write_text(
            json.dumps(self.request, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    def _write_grant(self) -> None:
        grant = create_quarantine_grant(
            request_path=self.request_path,
            state_dir=self.state_dir,
            batch_id="quarantine:1",
            issued_at=self.now,
        )
        write_quarantine_grant(self.authorization_path, grant)


if __name__ == "__main__":
    unittest.main()
