import unittest

from helpers import subject
from github import GitHub, Response, IncompleteInventory, LostResponse


class GitHubTests(unittest.TestCase):
    def client(self, responses, **kwargs):
        calls = []
        def transport(method, endpoint, payload):
            calls.append((method, endpoint, payload))
            return responses.pop(0)
        return GitHub(transport, "owner/repo", {"id": 100, "login": "shepherd[bot]"}, **kwargs), calls

    def test_paginated_comments_require_proven_completion(self):
        client, calls = self.client([
            Response([{"id": 1}], {"Link": '<https://api.github.com/repos/owner/repo/issues/7/comments?per_page=100&page=2>; rel="next"'}),
            Response([{"id": 2}], {}),
        ])
        self.assertEqual(client.get_pages("repos/owner/repo/issues/7/comments"), [{"id": 1}, {"id": 2}])
        self.assertEqual(len(calls), 2)
        client, _ = self.client([Response([{}] * 100, {})], max_pages=1)
        with self.assertRaises(IncompleteInventory):
            client.get_pages("repos/owner/repo/issues/7/comments")

    def test_truncated_error_cyclic_or_foreign_pagination_fails(self):
        for response in (
            Response({"items": []}, {}),
            Response([], {}, status=403),
            Response([], {"Link": '<https://evil.example/repos/owner/repo/issues/7/comments?page=2>; rel="next"'}),
            Response([], {"Link": '<https://api.github.com/repos/other/repo/issues/7/comments?page=2>; rel="next"'}),
            Response([], {"Link": '<https://api.github.com/repos/owner/repo/issues/7/comments?per_page=100&page=1>; rel="next"'}),
            Response([], {"Link": "malformed"}),
            Response([], {"Link": '<https://api.github.com/repos/owner/repo/issues/7/comments?per_page=100&page=9>; rel="next"'}),
        ):
            client, _ = self.client([response])
            with self.subTest(response=response), self.assertRaises(ValueError):
                client.get_pages("repos/owner/repo/issues/7/comments")

    def test_closed_read_endpoints_reject_unlisted_paths(self):
        client, calls = self.client([])
        for path in ("user", "repos/other/repo/issues/7", "repos/owner/repo/contents/secrets",
                     "repos/owner/repo/issues/7/comments/../8", "repos/owner/repo/issues/7?state=all"):
            with self.subTest(path=path), self.assertRaises(ValueError):
                client.get(path)
        self.assertEqual(calls, [])

    def test_exact_status_endpoints_and_veto_before_transport(self):
        import receipts
        from helpers import FakeClock, observation
        record = receipts.new_record(observation(), FakeClock()(), receipts.TrialScope(subject(), None))
        body = receipts.render_record(record)
        client, calls = self.client([Response({"id": 501}, {}, status=201), Response({"id": 501}, {})], write_enabled=True)
        guards = []
        client.publish_status(subject(), body, None, lambda: guards.append("create"))
        client.publish_status(subject(), body, 501, lambda: guards.append("edit"))
        self.assertEqual(guards, ["create", "edit"])
        self.assertEqual([(method, endpoint) for method, endpoint, _ in calls], [
            ("POST", "repos/owner/repo/issues/7/comments"), ("PATCH", "repos/owner/repo/issues/comments/501"),
        ])
        self.assertEqual([payload for _, _, payload in calls], [{"body": body}, {"body": body}])
        def veto():
            raise ValueError("hands-off")
        with self.assertRaisesRegex(ValueError, "hands-off"):
            client.publish_status(subject(), body, 501, veto)
        self.assertEqual(len(calls), 2)

    def test_write_errors_and_invalid_returned_ids_are_uncertain_with_no_retry(self):
        import receipts
        from helpers import FakeClock, observation
        body = receipts.render_record(receipts.new_record(observation(), FakeClock()(), receipts.TrialScope(subject(), None)))
        for response in (Response({"id": 501}, {}, status=503), Response({"id": True}, {}, status=201),
                         Response({}, {}, status=201), Response({"id": 501}, {}, status=200)):
            client, calls = self.client([response], write_enabled=True)
            with self.subTest(response=response), self.assertRaises(LostResponse):
                client.publish_status(subject(), body, None, lambda: None)
            self.assertEqual(len(calls), 1)

    def test_status_write_requires_capability_host_body_and_guard(self):
        client, calls = self.client([])
        with self.assertRaises(ValueError):
            client.publish_status(subject(), "arbitrary", None, lambda: None)
        self.assertEqual(calls, [])
        client, calls = self.client([], write_enabled=True)
        with self.assertRaises(ValueError):
            client.publish_status(subject(), "agent body", None, lambda: None)
        self.assertEqual(calls, [])


if __name__ == "__main__":
    unittest.main()
