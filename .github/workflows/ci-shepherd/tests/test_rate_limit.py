from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from helpers import reconciliation_decision, reconciliation_evidence
from test_live import FakeService
import live
import pilot_github
import receipts
import round as contracts


class WindowOpener:
    """Authenticated HTTP responses with independent core and task windows."""

    def __init__(self):
        self.service = FakeService()
        self.now = datetime(2026, 10, 4, 4, 17, 52, tzinfo=timezone.utc)
        self.reset = self.now + timedelta(seconds=59)
        self.remaining = 60
        self.requests, self.sleeps, self.task_reads = [], [], []
        self.after_sleep = None
        self.error_status = 403
        self.download = None
        for index in range(8):
            task = self.service.task_value(f"historic-{index}", "Earlier unrelated work")
            task["state"] = task["sessions"][0]["state"] = "cancelled"
            self.service.tasks[task["id"]] = task

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)
        if self.after_sleep:
            self.after_sleep(self)

    def open(self, request, timeout):
        url = urlparse(request.full_url)
        if url.hostname == "fixture.blob.core.windows.net":
            return self.response(self.download, {})
        endpoint = url.path[1:] + ("?" + url.query if url.query else "")
        method = request.get_method()
        self.requests.append((method, endpoint, self.now))
        task_route = endpoint.startswith("agents/")
        if self.now >= self.reset:
            self.remaining, self.reset = 60, self.now + timedelta(seconds=60)
        headers = {"X-RateLimit-Resource": "mission_control" if task_route else "core",
                   "X-RateLimit-Limit": "60" if task_route else "5000",
                   "X-RateLimit-Remaining": str(max(0, self.remaining - 1)) if task_route else "4999",
                   "X-RateLimit-Reset": str(int(self.reset.timestamp())),
                   "Date": format_datetime(self.now, usegmt=True), "X-GitHub-Request-Id": "TEST:REQUEST"}
        if task_route:
            if self.remaining == 0:
                headers["Retry-After"] = "59"
                headers["Location"] = "https://fixture.blob.core.windows.net/secret?sig=credential"
                raise HTTPError(request.full_url, self.error_status, "fixture", headers, io.BytesIO(b"credential arbitrary body"))
            self.remaining -= 1
            if method == "GET":
                self.task_reads.append(endpoint)
        body = None if request.data is None else json.loads(request.data)
        result = self.service.transport(method, endpoint, body)
        if isinstance(result.payload, bytes):
            self.download = result.payload
            headers["Location"] = "https://fixture.blob.core.windows.net/download"
            raise HTTPError(request.full_url, 302, "fixture", headers, io.BytesIO())
        return self.response(json.dumps(result.payload).encode(), headers, result.status)

    @staticmethod
    def response(raw, headers, status=200):
        response = io.BytesIO(raw)
        response.headers, response.status = headers, status
        return response


class RateLimitTests(unittest.TestCase):
    def test_large_check_pages_are_complete_without_expanding_packet_limit(self):
        opener = WindowOpener()
        first_page = None

        def page(method, endpoint, body):
            nonlocal first_page
            number = int(parse_qs(urlparse(endpoint).query)["page"][0])
            start = (number - 1) * 100
            payload = {"total_count": 452, "check_runs": [
                {"id": index, "output": {"summary": "CI check metadata " * 220}}
                for index in range(start + 1, min(start + 100, 452) + 1)]}
            if number == 1:
                first_page = json.dumps(payload)
            return live.Response(payload, {})

        opener.service.transport = page
        transport = pilot_github.PilotTransport("credential")
        transport.opener = opener
        checks = live.API(transport).pages(
            f"repos/{live.REPOSITORY}/commits/{live.INITIAL_HEAD}/check-runs",
            key="check_runs", require_total_count=True)
        self.assertEqual(list(range(1, 453)), [check["id"] for check in checks])
        self.assertEqual(5, len(opener.requests))
        self.assertGreater(len(first_page.encode()), contracts.MAX_JSON_BYTES)
        with self.assertRaisesRegex(ValueError, "JSON exceeds size limit"):
            contracts.loads(first_page)

    def test_api_json_accepts_exact_eight_mib_boundary(self):
        bound = 8 * 1024 * 1024
        value = {"value": "x" * (bound - len(json.dumps({"value": ""}).encode()))}
        self.assertEqual(bound, len(json.dumps(value).encode()))
        opener = WindowOpener()
        opener.service.transport = lambda *_: live.Response(value, {})
        response = live.HTTPTransport("credential", opener=opener)("GET", f"repos/{live.REPOSITORY}", None)
        self.assertEqual(value, response.payload)
        self.assertEqual(1, len(opener.requests))

    def test_api_json_over_boundary_preserves_read_failure_and_write_uncertainty(self):
        bound = 8 * 1024 * 1024
        value = {"value": "x" * (bound + 1 - len(json.dumps({"value": ""}).encode()))}
        self.assertEqual(bound + 1, len(json.dumps(value).encode()))
        for method, endpoint, body, exception in (
                ("GET", f"repos/{live.REPOSITORY}", None, live.IncompleteInventory),
                ("POST", f"agents/repos/{live.REPOSITORY}/tasks", {}, live.LostResponse)):
            with self.subTest(method=method):
                opener = WindowOpener()
                opener.service.transport = lambda *_: live.Response(value, {})
                transport = live.HTTPTransport("credential", write=True, opener=opener)
                with self.assertRaisesRegex(exception, "API JSON response exceeds 8388608-byte limit"):
                    transport(method, endpoint, body)
                self.assertEqual(1, len(opener.requests))

    def test_large_api_json_keeps_duplicate_constant_and_syntax_validation(self):
        prefix = '{"padding":"' + "x" * contracts.MAX_JSON_BYTES + '",'
        for suffix in ('"value":1,"value":2}', '"value":NaN}', '"value":}'):
            with self.subTest(suffix=suffix):
                opener = WindowOpener()
                raw = (prefix + suffix).encode()
                with patch.object(opener, "open", return_value=WindowOpener.response(raw, {})) as opened:
                    with self.assertRaisesRegex(live.IncompleteInventory, "unavailable or invalid"):
                        live.HTTPTransport("credential", opener=opener)("GET", f"repos/{live.REPOSITORY}", None)
                    opened.assert_called_once()

    def test_fixed_compare_endpoint_is_allowed_but_traversal_is_not(self):
        opener = WindowOpener()
        transport = live.HTTPTransport("credential", opener=opener)
        failure = None
        try:
            response = transport("GET", f"repos/{live.REPOSITORY}/compare/{live.INITIAL_HEAD}...{'c' * 40}", None)
        except ValueError as error:
            failure = str(error)
        self.assertIsNone(failure)
        self.assertEqual(response.payload["status"], "ahead")
        with self.assertRaises(ValueError):
            transport("GET", f"repos/{live.REPOSITORY}/../user", None)

    def setup_flow(self):
        opener = WindowOpener()
        transport = live.HTTPTransport("credential", write=True, opener=opener)
        transport.clock_fn, transport.sleep_fn = opener.clock, opener.sleep
        github = live.FixtureGitHub(transport, opener.service.run, write=True)
        scope = receipts.TrialScope(live.ROOT, None)
        packet = contracts.prepare_reconciliation(github, live.ROOT, live.ROOT, opener.service.run, opener.clock, scope)
        decision = reconciliation_decision(packet, "repair-pr", {"feedbackIds": ["ci-10-20"]})
        executor = live.ExistingPRExecutor(github, packet, github.context)
        return opener, transport, github, scope, packet, decision, executor

    def apply(self, flow):
        opener, _, github, scope, packet, decision, executor = flow
        return contracts.apply_reconciliation(packet, decision, opener.service.run, github, opener.clock, scope,
                                              executor=executor, dry_run=False, evidence=reconciliation_evidence(decision))

    def test_full_guards_with_eight_terminal_tasks_pace_separate_window(self):
        flow = self.setup_flow()
        opener = flow[0]
        failure = None
        try:
            result = self.apply(flow)
        except ValueError as error:
            failure = str(error)
        self.assertIsNone(failure, "Repeated complete guards exhausted mission_control before one task POST: " + str(failure))
        self.assertEqual(result["outcome"], "confirmed")
        self.assertEqual(len(opener.service.posts()), 1)
        self.assertTrue(opener.sleeps)
        self.assertLessEqual(sum(opener.sleeps), 180)
        self.assertGreater(len(opener.task_reads), 60)
        self.assertEqual({path.rsplit("/", 1)[-1] for path in opener.task_reads if "historic-" in path},
                         {f"historic-{index}" for index in range(8)})
        collections = sum("is_archived=false" in path for path in opener.task_reads)
        self.assertEqual(
            [sum(path.endswith(f"/historic-{index}") for path in opener.task_reads) for index in range(8)],
            [collections] * 8,
        )
        record = receipts.parse_body(opener.service.comments[0]["body"])
        self.assertEqual(record["repairBatches"], 1)
        for method, endpoint, observed in opener.requests:
            if method == "POST":
                self.assertLess(observed, live.issue_pr.timestamp(flow[4]["validUntil"]), endpoint)

    def test_wait_expiry_rollback_and_changed_authority_make_zero_task_posts(self):
        for change in ("expiry", "rollback", "head", "hands-off"):
            with self.subTest(change=change):
                flow = self.setup_flow()
                opener = flow[0]
                def after_wait(value):
                    if change == "expiry":
                        value.now = live.issue_pr.timestamp(flow[4]["validUntil"])
                    elif change == "rollback":
                        value.now -= timedelta(seconds=value.sleeps[-1] + 1)
                    elif change == "head":
                        value.service.pr["head"]["sha"] = "c" * 40
                    else:
                        value.service.pr["labels"].append({"name": "shepherd-hands-off"})
                opener.after_sleep = after_wait
                with self.assertRaisesRegex(ValueError, "expired|rollback|backwards|basis changed|hands-off"):
                    self.apply(flow)
                self.assertTrue(opener.sleeps)
                self.assertEqual(opener.service.posts(), [])

    def test_known_exhausted_post_is_rejected_without_send_or_sleep(self):
        opener = WindowOpener()
        opener.remaining = 1
        transport = live.HTTPTransport("credential", write=True, opener=opener)
        transport.clock_fn, transport.sleep_fn = opener.clock, opener.sleep
        transport("GET", "agents/repos/radical/aspire/tasks?is_archived=false", None)
        transport("GET", "repos/radical/aspire/pulls/121", None)
        with self.assertRaises(live.RejectedEffect):
            transport("POST", "agents/repos/radical/aspire/tasks", {})
        self.assertEqual([method for method, _, _ in opener.requests], ["GET", "GET"])
        self.assertEqual(opener.sleeps, [])

    def test_wait_budget_or_incomplete_wait_blocks_before_next_get(self):
        for scenario in ("over-budget", "early-wakeup"):
            with self.subTest(scenario=scenario):
                opener = WindowOpener()
                opener.remaining = 1
                if scenario == "over-budget":
                    opener.reset = opener.now + timedelta(seconds=181)
                transport = live.HTTPTransport("credential", opener=opener,
                                               clock_fn=opener.clock,
                                               sleep_fn=opener.sleep if scenario == "over-budget" else lambda _: None)
                transport("GET", "agents/repos/radical/aspire/tasks?is_archived=false", None)
                with self.assertRaisesRegex(live.IncompleteInventory, "exceeds|did not reach"):
                    transport("GET", "agents/repos/radical/aspire/tasks?is_archived=true", None)
                self.assertEqual(len(opener.requests), 1)

    def test_unexpected_403_or_429_never_retries_reads_or_writes(self):
        for method, status in ((method, status) for method in ("GET", "POST") for status in (403, 429)):
            with self.subTest(method=method, status=status):
                opener = WindowOpener()
                opener.remaining = 0
                opener.error_status = status
                transport = live.HTTPTransport("credential", write=True, opener=opener,
                                               clock_fn=opener.clock, sleep_fn=opener.sleep)
                with self.assertRaises(ValueError) as caught:
                    transport(method, "agents/repos/radical/aspire/tasks", None if method == "GET" else {})
                self.assertIn("mission_control", str(caught.exception))
                self.assertEqual(len(opener.requests), 1)
                self.assertEqual(opener.sleeps, [])

    def test_missing_mission_quota_field_is_not_a_successful_read(self):
        opener = WindowOpener()
        original_open = opener.open
        def incomplete(request, timeout):
            response = original_open(request, timeout)
            response.headers.pop("X-RateLimit-Reset")
            return response
        opener.open = incomplete
        transport = live.HTTPTransport("credential", opener=opener)
        with self.assertRaisesRegex(live.IncompleteInventory, "invalid"):
            transport("GET", "agents/repos/radical/aspire/tasks?is_archived=false", None)
        self.assertEqual(len(opener.requests), 1)

    def test_403_diagnostics_expose_only_safe_route_and_headers(self):
        opener = WindowOpener()
        opener.remaining = 0
        transport = live.HTTPTransport("credential", opener=opener)
        with self.assertRaises(live.IncompleteInventory) as caught:
            transport("GET", "agents/repos/radical/aspire/tasks?is_archived=false", None)
        message = str(caught.exception)
        self.assertIn("GET agents/repos/radical/aspire/tasks", message)
        self.assertIn("HTTP 403", message)
        self.assertIn("mission_control", message)
        self.assertIn("TEST:REQUEST", message)
        self.assertNotIn("credential", message)
        self.assertNotIn("blob.core", message)
        self.assertNotIn("arbitrary body", message)

    def test_admission_wait_and_network_diagnostics_never_echo_credential(self):
        for failure in ("admission", "wait", "network"):
            with self.subTest(failure=failure):
                opener = WindowOpener()
                transport = live.HTTPTransport("radical", write=True, opener=opener,
                                               clock_fn=opener.clock, sleep_fn=opener.sleep)
                if failure == "network":
                    def unavailable(request, timeout):
                        raise URLError("radical signed storage URL")
                    opener.open = unavailable
                else:
                    transport.mission_quota = {"remaining": 0, "reset": int(opener.now.timestamp()) + 3600}
                method = "POST" if failure == "admission" else "GET"
                with self.assertRaises(ValueError) as caught:
                    transport(method, "agents/repos/radical/aspire/tasks", {} if method == "POST" else None)
                self.assertNotIn("radical", str(caught.exception))
                self.assertEqual(opener.requests, [])
                self.assertEqual(opener.sleeps, [])

    def test_invalid_download_redirect_never_echoes_signed_url(self):
        opener = WindowOpener()
        def redirect(request, timeout):
            headers = {"Location": "https://fixture.blob.core.windows.net:credential/download?sig=credential"}
            raise HTTPError(request.full_url, 302, "fixture", headers, io.BytesIO())
        opener.open = redirect
        transport = live.HTTPTransport("credential", opener=opener)
        with self.assertRaises(ValueError) as caught:
            transport("GET", "repos/radical/aspire/actions/jobs/20/logs", None)
        self.assertNotIn("credential", str(caught.exception))
        self.assertNotIn("blob.core", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
