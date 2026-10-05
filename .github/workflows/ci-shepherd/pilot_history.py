"""Sealed descriptive PR history, never worker or billing authority."""

from github import IncompleteInventory, Response
import issue_pr

# GET /graphql returns the schema, not a query result. This fixed POST is a
# read; no caller-supplied query text or mutation enters the transport.
# https://docs.github.com/en/graphql/reference/objects#copilotworkstartedevent
QUERY = """query ShepherdWorkHistory($owner:String!,$name:String!,$number:Int!,$after:String) {
  repository(owner:$owner,name:$name) {
    databaseId nameWithOwner
    pullRequest(number:$number) {
      id number
      timelineItems(first:100,after:$after,itemTypes:[COPILOT_WORK_STARTED_EVENT,COPILOT_WORK_FINISHED_EVENT,COPILOT_WORK_FINISHED_FAILURE_EVENT]) {
        nodes {
          __typename
          ... on CopilotWorkStartedEvent { id createdAt actor { login } sessionId }
          ... on CopilotWorkFinishedEvent { id createdAt actor { login } sessionId }
          ... on CopilotWorkFinishedFailureEvent { id createdAt actor { login } sessionId }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}"""
TYPES = {"CopilotWorkStartedEvent": "started", "CopilotWorkFinishedEvent": "finished",
         "CopilotWorkFinishedFailureEvent": "failed"}
MAX_PAGES = 10


def body(repository, number, after=None):
    owner, name = repository.split("/")
    return {"query": QUERY, "variables": {"owner": owner, "name": name, "number": number, "after": after}}


def validate_request(value, binding):
    if not isinstance(value, dict) or set(value) != {"query", "variables"} or value["query"] != QUERY:
        raise ValueError("only the sealed PR history query is allowed")
    variables = value["variables"]
    if not isinstance(variables, dict) or set(variables) != {"owner", "name", "number", "after"}:
        raise ValueError("closed history variables required")
    owner, name = binding.repository.split("/")
    if (variables["owner"] != owner or variables["name"] != name
            or type(variables["number"]) is not int or variables["number"] <= 0
            or variables["number"] == 121
            or binding.subject is not None and variables["number"] != binding.subject):
        raise ValueError("history subject binding mismatch")
    if variables["after"] is not None:
        issue_pr.text(variables["after"], "history cursor", 1024)


def read(transport, binding, number, node):
    events, ids, cursors, after = [], set(), set(), None
    try:
        for _ in range(MAX_PAGES):
            response = transport("POST", "graphql", body(binding.repository, number, after))
            if (not isinstance(response, Response) or response.status != 200
                    or not isinstance(response.payload, dict) or response.payload.get("errors")):
                raise ValueError("history API error")
            repository = response.payload["data"]["repository"]
            pr = repository["pullRequest"]
            if (type(repository["databaseId"]) is not int or repository["databaseId"] != binding.repository_id
                    or repository["nameWithOwner"] != binding.repository
                    or pr["id"] != node or type(pr["number"]) is not int or pr["number"] != number):
                raise ValueError("history repository/PR identity mismatch")
            timeline = pr["timelineItems"]
            if not isinstance(timeline["nodes"], list) or len(timeline["nodes"]) > 100:
                raise ValueError("history nodes unavailable")
            for event in timeline["nodes"]:
                kind = TYPES[event["__typename"]]
                issue_pr.text(event["id"], "history event ID")
                issue_pr.timestamp(event["createdAt"])
                if event["id"] in ids:
                    raise ValueError("duplicate history event")
                actor = event["actor"]
                if actor is not None:
                    issue_pr.text(actor["login"], "history actor")
                session = event["sessionId"]
                if session is not None:
                    issue_pr.text(session, "history session")
                ids.add(event["id"])
                events.append({"id": event["id"], "state": kind, "at": event["createdAt"],
                               "actor": None if actor is None else actor["login"], "sessionId": session})
            page = timeline["pageInfo"]
            if type(page["hasNextPage"]) is not bool:
                raise ValueError("history pagination unavailable")
            cursor = page["endCursor"]
            if timeline["nodes"] and cursor is None:
                raise ValueError("history end cursor unavailable")
            if cursor is not None:
                issue_pr.text(cursor, "history cursor", 1024)
                if cursor in cursors:
                    raise ValueError("history cursor repeated")
                cursors.add(cursor)
            if not page["hasNextPage"]:
                return {"complete": True, "events": events}
            if not timeline["nodes"] or cursor is None:
                raise ValueError("history next page unavailable")
            after = cursor
        raise ValueError("history page bound exceeded")
    except (ValueError, KeyError, TypeError) as error:
        raise IncompleteInventory("PR work history unavailable or incomplete") from error


def describe(history):
    if not history["complete"]:
        return "PR Copilot history: unknown (read unavailable/incomplete); not execution or task evidence."
    events = history["events"]
    counts = {kind: sum(event["state"] == kind for event in events) for kind in TYPES.values()}
    lines = [f"PR Copilot history: {counts['started']} starts, {counts['finished']} finishes, "
             f"{counts['failed']} failures; descriptive only, not current execution or task IDs."]
    # A bounded tail keeps waiting logs readable; old unmatched starts are
    # historical context, never a permanent running-worker blocker.
    for event in events[-5:]:
        lines.append(f"  {event['at']}: {event['actor'] or 'unknown actor'} {event['state']}; "
                     f"session {event['sessionId'] or 'unknown'}.")
    return "\n".join(lines)
