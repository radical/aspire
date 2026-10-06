"""Fresh resolution evidence for the exact REST review-comment inventory."""

import re

from github import IncompleteInventory, Response
import issue_pr

# PullRequestReviewComment.thread is unavailable for some authorized credentials.
# Traverse the public PR connection and paginate comments independently.
# fullDatabaseId is a BigInt string, e.g. "4187037077", not a node ID.
# https://docs.github.com/en/graphql/reference/objects#pullrequest
QUERY = """query ShepherdReviewFeedback($owner:String!,$name:String!,$number:Int!,$after:String) {
  repository(owner:$owner,name:$name) {
    databaseId nameWithOwner
    pullRequest(number:$number) {
      id number headRefOid
      reviewThreads(first:100,after:$after) {
        nodes {
          id isResolved pullRequest { id }
          comments(first:100) {
            nodes { id fullDatabaseId }
            pageInfo { hasNextPage endCursor }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""
COMMENTS_QUERY = """query ShepherdReviewThreadComments($owner:String!,$name:String!,$number:Int!,$thread:ID!,$after:String!) {
  repository(owner:$owner,name:$name) {
    databaseId nameWithOwner
    pullRequest(number:$number) { id number headRefOid }
  }
  node(id:$thread) {
    ... on PullRequestReviewThread {
      id isResolved pullRequest { id }
      comments(first:100,after:$after) {
        nodes { id fullDatabaseId }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""
MAX_BATCH = 100
MAX_COMMENTS = 1000
MAX_PAGES = 10
MAX_REQUESTS = 20


def body(repository, number, after=None, *, thread=None):
    owner, name = repository.split("/")
    variables = {"owner": owner, "name": name, "number": number, "after": after}
    if thread is not None:
        variables["thread"] = thread
    return {"query": QUERY if thread is None else COMMENTS_QUERY, "variables": variables}


def validate_request(value, binding):
    if (not isinstance(value, dict) or set(value) != {"query", "variables"}
            or value["query"] not in (QUERY, COMMENTS_QUERY)):
        raise ValueError("only the sealed review-feedback query is allowed")
    variables = value["variables"]
    keys = {"owner", "name", "number", "after"}
    if value["query"] == COMMENTS_QUERY:
        keys.add("thread")
    if not isinstance(variables, dict) or set(variables) != keys:
        raise ValueError("closed review-feedback variables required")
    owner, name = binding.repository.split("/")
    if (variables["owner"] != owner or variables["name"] != name
            or type(variables["number"]) is not int or variables["number"] <= 0
            or variables["number"] == 121
            or binding.subject is not None and variables["number"] != binding.subject):
        raise ValueError("review-feedback subject binding mismatch")
    if variables["after"] is not None:
        issue_pr.text(variables["after"], "review-feedback cursor", 1024)
    if value["query"] == COMMENTS_QUERY:
        issue_pr.text(variables["thread"], "review thread", 1024)
        issue_pr.text(variables["after"], "review-comment cursor", 1024)


def next_page(connection, cursors):
    if not isinstance(connection["nodes"], list) or len(connection["nodes"]) > MAX_BATCH:
        raise ValueError("review-feedback page unavailable")
    page = connection["pageInfo"]
    if type(page["hasNextPage"]) is not bool:
        raise ValueError("review-feedback pagination unavailable")
    cursor = page["endCursor"]
    if cursor is not None:
        issue_pr.text(cursor, "review-feedback cursor", 1024)
        if cursor in cursors:
            raise ValueError("review-feedback cursor repeated")
        cursors.add(cursor)
    if connection["nodes"] and cursor is None:
        raise ValueError("review-feedback end cursor unavailable")
    if page["hasNextPage"]:
        if not connection["nodes"] or cursor is None:
            raise ValueError("review-feedback next page unavailable")
        return cursor
    return None


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
        result, matched, threads, seen_comments, seen_database_ids = set(), set(), set(), set(), set()
        requests = 0

        def read(request):
            nonlocal requests
            requests += 1
            if requests > MAX_REQUESTS:
                raise ValueError("review-feedback request bound exceeded")
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
            return data

        after, thread_cursors = None, set()
        for _ in range(MAX_PAGES):
            pr = read(body(binding.repository, number, after))["repository"]["pullRequest"]
            connection = pr["reviewThreads"]
            following = next_page(connection, thread_cursors)
            for thread in connection["nodes"]:
                identity, is_resolved = thread["id"], thread["isResolved"]
                issue_pr.text(identity, "review thread", 1024)
                if (identity in threads or len(threads) >= MAX_COMMENTS
                        or type(is_resolved) is not bool or thread["pullRequest"]["id"] != node):
                    raise ValueError("review-feedback thread identity/state unavailable")
                threads.add(identity)
                comment_cursors = set()
                for page_index in range(MAX_PAGES):
                    comments_page = thread["comments"]
                    comment_after = next_page(comments_page, comment_cursors)
                    for value in comments_page["nodes"]:
                        comment_id, database_id = value["id"], value["fullDatabaseId"]
                        issue_pr.text(comment_id, "review-comment node", 1024)
                        if not isinstance(database_id, str) or not re.fullmatch(r"[1-9][0-9]{0,19}", database_id):
                            raise ValueError("review-feedback database ID unavailable")
                        if (comment_id in seen_comments or database_id in seen_database_ids
                                or len(seen_comments) >= MAX_COMMENTS):
                            raise ValueError("review-feedback comment identity/bound exceeded")
                        seen_comments.add(comment_id)
                        seen_database_ids.add(database_id)
                        if comment_id in expected:
                            if int(database_id) != expected[comment_id]:
                                raise ValueError("review-feedback REST identity mismatch")
                            matched.add(comment_id)
                            if is_resolved:
                                result.add(expected[comment_id])
                        elif int(database_id) in database_ids:
                            raise ValueError("review-feedback REST node mismatch")
                    if comment_after is None:
                        break
                    if page_index == MAX_PAGES - 1:
                        raise ValueError("review-comment page bound exceeded")
                    thread = read(body(binding.repository, number, comment_after, thread=identity))["node"]
                    if (thread["id"] != identity or thread["isResolved"] is not is_resolved
                            or thread["pullRequest"]["id"] != node):
                        raise ValueError("review-feedback thread changed during inventory")
            if following is None:
                if matched != set(expected):
                    raise ValueError("review-feedback nodes incomplete")
                return result
            after = following
        raise ValueError("review-thread page bound exceeded")
    except (ValueError, KeyError, TypeError) as error:
        raise IncompleteInventory("Review-thread resolution unavailable/incomplete.") from error
