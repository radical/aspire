from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
import io
import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

from helpers import reconciliation_decision, reconciliation_evidence
from test_live import FakeService, HTTPFixture
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
    def test_truncated_http_get_recovers_through_real_response_reader(self):
        service = FakeService()
        calls = []
        value = {"jobs": [{"id": 20}]}
        complete = json.dumps(value).encode()

        def jobs(method, endpoint, body):
            calls.append((method, endpoint, body))
            raw = complete[:10] if len(calls) == 1 else complete
            return live.Response(raw, {"Content-Length": str(len(complete))})

        service.transport = jobs
        fixture = HTTPFixture(service)
        self.addCleanup(fixture.close)
        sleeps = []
        path = f"repos/{live.REPOSITORY}/actions/runs/10/attempts/1/jobs"
        with self.assertLogs("live", level="WARNING") as logs:
            response = live.HTTPTransport("credential", opener=fixture, sleep_fn=sleeps.append)(
                "GET", path, None)
        self.assertEqual(value, response.payload)
        self.assertEqual([("GET", path, None)] * 2, calls)
        self.assertEqual([1], sleeps)
        self.assertIn(f"received=10 expected={len(complete)}", logs.output[0])

    def test_valid_json_prefix_is_not_accepted_as_a_complete_http_body(self):
        opener = WindowOpener()
        responses = [WindowOpener.response(b"{}", {"Content-Length": "20"}) for _ in range(3)]
        with patch.object(opener, "open", side_effect=responses) as opened:
            with self.assertLogs("live", level="WARNING"):
                with self.assertRaisesRegex(live.IncompleteInventory, "incomplete.*3 attempts.*received=2 expected=20"):
                    live.HTTPTransport("credential", opener=opener, sleep_fn=opener.sleep)(
                        "GET", f"repos/{live.REPOSITORY}", None)
        self.assertEqual(3, opened.call_count)

    def test_truncated_write_receipt_remains_uncertain(self):
        for method, path in (
                ("POST", f"agents/repos/{live.REPOSITORY}/tasks"),
                ("PATCH", f"repos/{live.REPOSITORY}/issues/comments/501")):
            with self.subTest(method=method):
                opener = WindowOpener()
                with patch.object(opener, "open", return_value=WindowOpener.response(
                        b"{}", {"Content-Length": "20"})) as opened:
                    with self.assertRaisesRegex(live.LostResponse, "incomplete.*no retry.*received=2 expected=20"):
                        live.HTTPTransport("credential", write=True, opener=opener, sleep_fn=opener.sleep)(
                            method, path, {})
                self.assertEqual(1, opened.call_count)
                self.assertEqual([], opener.sleeps)

    def test_complete_malformed_json_reports_position_without_retry_or_body(self):
        opener = WindowOpener()
        raw = b'{"secret":"credential", "jobs":'
        with patch.object(opener, "open", return_value=WindowOpener.response(
                raw, {"Content-Length": str(len(raw))})) as opened:
            with self.assertRaisesRegex(
                    live.IncompleteInventory,
                    f"malformed JSON.*offset={len(raw)}.*received={len(raw)} expected={len(raw)}") as error:
                live.HTTPTransport("credential", opener=opener, sleep_fn=opener.sleep)(
                    "GET", f"repos/{live.REPOSITORY}", None)
        self.assertEqual(1, opened.call_count)
        self.assertEqual([], opener.sleeps)
        self.assertNotIn("credential", str(error.exception))

    def test_malformed_body_without_length_is_not_assumed_to_be_truncated(self):
        opener = WindowOpener()
        with patch.object(opener, "open", return_value=WindowOpener.response(b'{"jobs":', {})) as opened:
            with self.assertRaisesRegex(live.IncompleteInventory, "offset=8.*received=8 expected=unknown"):
                live.HTTPTransport("credential", opener=opener, sleep_fn=opener.sleep)(
                    "GET", f"repos/{live.REPOSITORY}", None)
        self.assertEqual(1, opened.call_count)
        self.assertEqual([], opener.sleeps)

    def test_exact_length_valid_json_and_unframed_json_remain_supported(self):
        for headers in ({}, {"Content-Length": "2"}):
            with self.subTest(headers=headers):
                opener = WindowOpener()
                with patch.object(opener, "open", return_value=WindowOpener.response(b"{}", headers)) as opened:
                    response = live.HTTPTransport("credential", opener=opener)(
                        "GET", f"repos/{live.REPOSITORY}", None)
                self.assertEqual({}, response.payload)
                self.assertEqual(1, opened.call_count)

    def test_invalid_or_overlong_framing_is_not_retried(self):
        for length in ("invalid", "-1", "0", "1"):
            with self.subTest(length=length):
                opener = WindowOpener()
                with patch.object(opener, "open", return_value=WindowOpener.response(
                        b"{}", {"Content-Length": length})) as opened:
                    with self.assertRaises(live.IncompleteInventory):
                        live.HTTPTransport("credential", opener=opener, sleep_fn=opener.sleep)(
                            "GET", f"repos/{live.REPOSITORY}", None)
                self.assertEqual(1, opened.call_count)
                self.assertEqual([], opener.sleeps)

    def test_empty_http_body_recovers_through_real_response_reader(self):
        service = FakeService()
        requests = []
        value = {"total_count": 1, "jobs": [{"id": 20}]}

        def jobs(method, endpoint, body):
            requests.append((method, endpoint, body))
            return live.Response(b"" if len(requests) == 1 else value, {})

        service.transport = jobs
        fixture = HTTPFixture(service)
        self.addCleanup(fixture.close)
        sleeps = []
        path = f"repos/{live.REPOSITORY}/actions/runs/10/attempts/1/jobs"
        with self.assertLogs("live", level="WARNING"):
            response = live.HTTPTransport("credential", opener=fixture, sleep_fn=sleeps.append)(
                "GET", path, None)
        self.assertEqual(value, response.payload)
        self.assertEqual([("GET", path, None)] * 2, requests)
        self.assertEqual([1], sleeps)

    def test_empty_successful_get_retries_the_same_page_before_returning_jobs(self):
        opener = WindowOpener()
        endpoint = f"repos/{live.REPOSITORY}/actions/runs/10/attempts/1/jobs?per_page=100&page=2"
        value = {"total_count": 1, "jobs": [{"id": 20, "conclusion": "failure"}]}
        responses = [WindowOpener.response(b"", {"X-GitHub-Request-Id": "EMPTY:1",
                                                "Location": "https://untrusted.example/credential"}),
                     WindowOpener.response(b"", {"X-GitHub-Request-Id": "EMPTY:2"}),
                     WindowOpener.response(json.dumps(value).encode(), {})]
        with patch.object(opener, "open", side_effect=responses) as opened:
            with self.assertLogs("live", level="WARNING") as logs:
                response = live.HTTPTransport("credential", opener=opener, sleep_fn=opener.sleep)(
                    "GET", endpoint, None)
        self.assertEqual(value, response.payload)
        self.assertEqual([1, 2], opener.sleeps)
        self.assertEqual([("GET", "https://api.github.com/" + endpoint)] * 3,
                         [(call.args[0].get_method(), call.args[0].full_url)
                          for call in opened.call_args_list])
        self.assertTrue(all(item.closed for item in responses))
        self.assertEqual(2, len(logs.output))
        self.assertIn("EMPTY:1", logs.output[0])
        self.assertNotIn("credential", "\n".join(logs.output))

    def test_empty_get_retry_rechecks_task_quota_before_resending(self):
        opener = WindowOpener()
        reset = opener.now + timedelta(seconds=5)
        headers = {"X-RateLimit-Resource": "mission_control", "X-RateLimit-Limit": "60",
                   "X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(reset.timestamp()))}
        responses = [WindowOpener.response(b"", headers),
                     WindowOpener.response(b'{"tasks":[]}', {})]
        transport = live.HTTPTransport("credential", opener=opener, sleep_fn=opener.sleep, clock_fn=opener.clock)
        with patch.object(opener, "open", side_effect=responses):
            with self.assertLogs("live", level="WARNING"):
                response = transport("GET", f"agents/repos/{live.REPOSITORY}/tasks", None)
        self.assertEqual({"tasks": []}, response.payload)
        self.assertEqual([1, 5], opener.sleeps)
        self.assertGreater(opener.now, reset)
        self.assertEqual(1, transport.quota_waits)

    def test_empty_non_200_get_is_not_retried(self):
        opener = WindowOpener()
        with patch.object(opener, "open", return_value=WindowOpener.response(b"", {}, 204)) as opened:
            with self.assertRaisesRegex(live.IncompleteInventory, "empty.*no retry"):
                live.HTTPTransport("credential", opener=opener, sleep_fn=opener.sleep)(
                    "GET", f"repos/{live.REPOSITORY}", None)
        self.assertEqual(1, opened.call_count)
        self.assertEqual([], opener.sleeps)

    def test_persistently_empty_get_stops_after_three_attempts(self):
        opener = WindowOpener()
        responses = [WindowOpener.response(b"", {"X-GitHub-Request-Id": f"EMPTY:{index}"})
                     for index in range(3)]
        with patch.object(opener, "open", side_effect=responses) as opened:
            with self.assertLogs("live", level="WARNING"):
                with self.assertRaisesRegex(live.IncompleteInventory, "empty.*3 attempts.*EMPTY:2"):
                    live.HTTPTransport("credential", opener=opener, sleep_fn=opener.sleep)(
                        "GET", f"repos/{live.REPOSITORY}", None)
        self.assertEqual(3, opened.call_count)
        self.assertEqual([1, 2], opener.sleeps)
        self.assertTrue(all(item.closed for item in responses))

    def test_empty_write_response_remains_uncertain_without_retry(self):
        for method, endpoint in (
                ("POST", f"agents/repos/{live.REPOSITORY}/tasks"),
                ("PATCH", f"repos/{live.REPOSITORY}/issues/comments/501")):
            with self.subTest(method=method):
                opener = WindowOpener()
                response = WindowOpener.response(b"", {})
                with patch.object(opener, "open", return_value=response) as opened:
                    with self.assertRaisesRegex(live.LostResponse, "empty.*no retry"):
                        live.HTTPTransport("credential", write=True, opener=opener, sleep_fn=opener.sleep)(
                            method, endpoint, {})
                self.assertEqual(1, opened.call_count)
                self.assertEqual([], opener.sleeps)
                self.assertTrue(response.closed)

    def test_retry_does_not_skip_or_duplicate_inventory_pages(self):
        opener = WindowOpener()
        path = f"repos/{live.REPOSITORY}/actions/runs/10/attempts/1/jobs"
        first = {"total_count": 2, "jobs": [{"id": 20}]}
        second = {"total_count": 2, "jobs": [{"id": 21}]}
        responses = [
            WindowOpener.response(json.dumps(first).encode(), {
                "Link": f'<https://api.github.com/{path}?per_page=100&page=2>; rel="next"'}),
            WindowOpener.response(b"", {}),
            WindowOpener.response(json.dumps(second).encode(), {}),
        ]
        with patch.object(opener, "open", side_effect=responses) as opened:
            with self.assertLogs("live", level="WARNING"):
                values = live.API(live.HTTPTransport(
                    "credential", opener=opener, sleep_fn=opener.sleep)).pages(
                        path, key="jobs", require_total_count=True)
        self.assertEqual([{"id": 20}, {"id": 21}], values)
        self.assertEqual([["1"], ["2"], ["2"]],
                         [parse_qs(urlparse(call.args[0].full_url).query)["page"]
                          for call in opened.call_args_list])

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
