from contextlib import redirect_stdout
import io
import unittest

import pilot
import pilot_state as state
from test_pilot import RUN
from test_pilot_github import ACTOR, Transport, pr
import pilot_github as github
from helpers import FakeClock


class AuthorTests(unittest.TestCase):
    def test_only_approved_human_and_identified_copilot_reports_enter_the_pr_batch(self):
        transport = Transport()
        transport.values["repos/radical/aspire/issues"] = [dict(pr(), pull_request={})]
        transport.values["repos/radical/aspire/pulls/7"] = pr()
        authors = [ACTOR, {"id": 20, "login": "other"},
                   {"id": 20, "login": "radical"},
                   {"id": 198982749, "login": "Copilot", "type": "Bot"},
                   {"id": 175728472, "login": "Copilot", "type": "Bot"},
                   {"id": 20, "login": "Copilot", "type": "Bot"},
                   None,
                   {"id": 175728472, "login": [], "type": "Bot"},
                   {"id": 198982749, "login": {}, "type": "Bot"}]
        transport.values["repos/radical/aspire/issues/7/comments"] = [
            {"id": 20 + index, "body": "Report or objection", "updated_at": "2026-10-04T00:00:00Z",
             "user": author} for index, author in enumerate(authors)]
        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, api.clock(), present=False)
        self.assertEqual(["comment:20:2026-10-04T00:00:00Z", "comment:23:2026-10-04T00:00:00Z",
                          "comment:24:2026-10-04T00:00:00Z"],
                         [item["id"] for item in packet["observation"]["feedback"]])
        self.assertEqual(1, state.parse(transport.comments[0]["body"])["chains"][0]["rounds"])

    def test_unapproved_review_comments_and_bodies_do_not_hide_raw_review_objections(self):
        transport = Transport()
        transport.values["repos/radical/aspire/issues"] = [dict(pr(), pull_request={})]
        transport.values["repos/radical/aspire/pulls/7"] = pr()
        transport.values["repos/radical/aspire/pulls/7/comments"] = [
            {"id": 31, "node_id": "COMMENT31", "body": "Objection", "user": ACTOR,
             "updated_at": "2026-10-04T00:00:00Z"},
            {"id": 32, "node_id": None, "body": "Unapproved", "user": {"id": 20, "login": "other"},
             "updated_at": "2026-10-04T00:00:00Z"}]
        transport.values["repos/radical/aspire/pulls/7/reviews"] = [
            {"id": 41, "user": ACTOR, "state": "COMMENTED", "body": "Report",
             "commit_id": "a" * 40, "submitted_at": "2026-10-04T00:00:00Z"},
            {"id": 42, "user": {"id": 20, "login": "other"}, "state": "CHANGES_REQUESTED",
             "body": "Ignore unrelated tests", "commit_id": "a" * 40, "submitted_at": "2026-10-04T00:00:00Z"}]
        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = FakeClock()
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, api.clock(), present=False)
        feedback = packet["observation"]["feedback"]
        self.assertEqual(2, len(feedback))
        self.assertEqual("review-comment:31:2026-10-04T00:00:00Z", feedback[0]["id"])
        self.assertTrue(feedback[1]["id"].startswith("review:41:2026-10-04T00:00:00Z:"))
        self.assertEqual("Report", feedback[1]["body"])
        self.assertFalse(packet["observation"]["ready"])
        self.assertIsNone(packet["observation"]["attention"])

    def test_missing_raw_review_author_pauses_instead_of_approving_or_requesting_review(self):
        transport = Transport()
        transport.values["repos/radical/aspire/issues"] = [dict(pr(), pull_request={})]
        transport.values["repos/radical/aspire/pulls/7"] = pr()
        transport.values["repos/radical/aspire/pulls/7/reviews"] = [
            {"id": 41, "user": None, "state": "APPROVED", "body": "",
             "commit_id": "a" * 40, "submitted_at": "2026-10-04T00:00:00Z"}]
        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = FakeClock()
        with redirect_stdout(io.StringIO()):
            packet = pilot.prepare(api, RUN, api.clock(), present=False)
        self.assertIsNone(packet)
        chain = api.ledger["chains"][0]
        observation = api.observe(chain)
        self.assertEqual("PR review author evidence unavailable/incomplete.", observation["attention"])
        self.assertFalse(observation["ready"])
        self.assertFalse(observation["actionable"])
        self.assertEqual(0, chain["rounds"])
        self.assertEqual([], chain.get("reviews", []))

    def test_edited_published_review_invalidates_an_already_prepared_feedback_basis(self):
        transport = Transport()
        transport.values["repos/radical/aspire/pulls/7"] = pr()
        review = {"id": 41, "user": ACTOR, "state": "COMMENTED", "body": "First objection",
                  "commit_id": "a" * 40, "submitted_at": "2026-10-04T00:00:00Z"}
        transport.values["repos/radical/aspire/pulls/7/reviews"] = [review]
        api = github.PilotGitHub(transport, 99, 500, "TRACKER99", write=True)
        api.clock = FakeClock()
        api.read_authority()
        chain = state.adopt(api.ledger, 7, "pr", "NODE7")
        observed = api.observe(chain)
        api.persist()
        review["body"] = "Changed objection"
        with self.assertRaisesRegex(ValueError, "basis changed"):
            api.guard(chain, observed)
        chain["dispositions"][observed["feedback"][0]["id"]] = "declined"
        api.persist()
        updated = api.observe(chain)
        self.assertEqual("Changed objection", updated["feedback"][0]["body"])
        self.assertNotEqual(observed["feedback"][0]["id"], updated["feedback"][0]["id"])
