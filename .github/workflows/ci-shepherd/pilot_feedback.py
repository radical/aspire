"""Fresh resolution evidence for the exact REST review-comment inventory."""

import re

from github import IncompleteInventory, Response
import issue_pr

# Comment.thread avoids a second, nested paginated thread/comment inventory.
# fullDatabaseId is GitHub's BigInt string, e.g. "4187037077", not a node ID.
# https://docs.github.com/en/graphql/reference
QUERY = """query ShepherdReviewFeedback($owner:String!,$name:String!,$number:Int!,$ids:[ID!]!) {
  repository(owner:$owner,name:$name) {
    databaseId nameWithOwner
    pullRequest(number:$number) { id number headRefOid }
  }
  nodes(ids:$ids) {
    ... on PullRequestReviewComment {
      id fullDatabaseId
      thread { id isResolved pullRequest { id } }
    }
  }
}"""
MAX_BATCH = 100
MAX_COMMENTS = 1000


def body(repository, number, identities):
    owner, name = repository.split("/")
    return {"query": QUERY, "variables": {"owner": owner, "name": name, "number": number, "ids": identities}}


def validate_request(value, binding):
    if not isinstance(value, dict) or set(value) != {"query", "variables"} or value["query"] != QUERY:
        raise ValueError("only the sealed review-feedback query is allowed")
    variables = value["variables"]
    if not isinstance(variables, dict) or set(variables) != {"owner", "name", "number", "ids"}:
        raise ValueError("closed review-feedback variables required")
    owner, name = binding.repository.split("/")
    if (variables["owner"] != owner or variables["name"] != name
            or type(variables["number"]) is not int or variables["number"] <= 0
            or variables["number"] == 121
            or binding.subject is not None and variables["number"] != binding.subject):
        raise ValueError("review-feedback subject binding mismatch")
    identities = variables["ids"]
    if not isinstance(identities, list) or not 0 < len(identities) <= MAX_BATCH:
        raise ValueError("bounded review-comment node IDs required")
    for identity in identities:
        issue_pr.text(identity, "review-comment node", 1024)
    if len(set(identities)) != len(identities):
        raise ValueError("duplicate review-comment node")


def resolved(transport, binding, number, node, head, comments):
    try:
        if not 0 < len(comments) <= MAX_COMMENTS:
            raise ValueError("review-comment inventory bound exceeded")
        expected = {}
        database_ids = set()
        for comment in comments:
            issue_pr.positive(comment["id"], "review-comment ID")
            issue_pr.text(comment["node_id"], "review-comment node", 1024)
            if comment["node_id"] in expected or comment["id"] in database_ids:
                raise ValueError("duplicate review-comment identity")
            expected[comment["node_id"]] = comment["id"]
            database_ids.add(comment["id"])
        identities, result, threads = list(expected), set(), {}
        for start in range(0, len(identities), MAX_BATCH):
            batch = identities[start:start + MAX_BATCH]
            request = body(binding.repository, number, batch)
            validate_request(request, binding)
            response = transport("POST", "graphql", request)
            if (not isinstance(response, Response) or response.status != 200
                    or not isinstance(response.payload, dict) or response.payload.get("errors")):
                raise ValueError("review-feedback API error")
            data = response.payload["data"]
            repository = data["repository"]
            pr = repository["pullRequest"]
            if (type(repository["databaseId"]) is not int or repository["databaseId"] != binding.repository_id
                    or repository["nameWithOwner"] != binding.repository
                    or pr["id"] != node or type(pr["number"]) is not int or pr["number"] != number
                    or pr["headRefOid"] != head):
                raise ValueError("review-feedback repository/PR/head changed")
            values = data["nodes"]
            if not isinstance(values, list) or len(values) != len(batch):
                raise ValueError("review-feedback nodes incomplete")
            seen = set()
            for value in values:
                identity = value["id"]
                if identity not in batch or identity in seen:
                    raise ValueError("review-feedback node identity mismatch")
                database_id = value["fullDatabaseId"]
                if not isinstance(database_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", database_id):
                    raise ValueError("review-feedback database ID unavailable")
                if int(database_id) != expected[identity]:
                    raise ValueError("review-feedback REST identity mismatch")
                thread = value["thread"]
                issue_pr.text(thread["id"], "review thread", 1024)
                if type(thread["isResolved"]) is not bool or thread["pullRequest"]["id"] != node:
                    raise ValueError("review-feedback thread identity/state unavailable")
                if thread["id"] in threads and threads[thread["id"]] != thread["isResolved"]:
                    raise ValueError("review-feedback thread changed during inventory")
                threads[thread["id"]] = thread["isResolved"]
                seen.add(identity)
                if thread["isResolved"]:
                    result.add(expected[identity])
        return result
    except (ValueError, KeyError, TypeError) as error:
        raise IncompleteInventory("Review-thread resolution unavailable/incomplete.") from error
