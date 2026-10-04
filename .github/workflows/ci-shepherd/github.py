"""Injectable, endpoint-allowlisted transport. No credentials or live CLI."""

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlparse

from issue_pr import positive, validate_subject


class IncompleteInventory(ValueError):
    pass


class LostResponse(ValueError):
    """A write may have happened. Never automatically retry its POST/PATCH."""


class RejectedEffect(ValueError):
    """Transport positively establishes that the effect did not happen."""


@dataclass(frozen=True)
class Response:
    payload: object
    headers: dict
    status: int = 200


class GitHub:
    def __init__(self, transport, repository, actor, *, max_pages=10, write_enabled=False):
        validate_subject({"repository": repository, "kind": "issue", "number": 1})
        positive(max_pages, "pagination limit")
        self.transport = transport
        self.repository = repository
        self.actor = actor
        self.max_pages = max_pages
        self.write_enabled = write_enabled

    def _read_path(self, endpoint):
        if not isinstance(endpoint, str):
            raise ValueError("invalid read endpoint")
        parsed = urlparse(endpoint)
        prefix = "repos/" + re.escape(self.repository)
        if parsed.scheme or parsed.netloc or parsed.fragment or not re.fullmatch(
            prefix + r"/(?:issues/[1-9][0-9]*(?:/comments)?|pulls/[1-9][0-9]*)", parsed.path,
        ):
            raise ValueError("read endpoint is not allowed")
        query = parse_qs(parsed.query, strict_parsing=True)
        if set(query) - {"page", "per_page"} or any(len(values) != 1 for values in query.values()):
            raise ValueError("read query is not allowed")
        for key, values in query.items():
            if not re.fullmatch(r"[1-9][0-9]*", values[0]) or (key == "per_page" and values != ["100"]):
                raise ValueError("invalid paging query")
        if query and not parsed.path.endswith("/comments"):
            raise ValueError("paging only supports comment inventories")
        return parsed.path

    def get(self, endpoint):
        self._read_path(endpoint)
        response = self.transport("GET", endpoint, None)
        if not isinstance(response, Response) or response.status != 200:
            raise IncompleteInventory("GitHub read unavailable; inventory incomplete")
        return response.payload

    def get_pages(self, endpoint):
        path = self._read_path(endpoint)
        if not path.endswith("/comments") or "?" in endpoint:
            raise ValueError("unsupported paged inventory")
        items, seen = [], set()
        current = path + "?per_page=100&page=1"
        # GitHub returns Link: <https://api.github.com/repos/o/r/issues/7/comments
        # ?per_page=100&page=2>; rel="next". A full page without Link is probed
        # once more rather than treated as a complete inventory.
        # https://docs.github.com/en/rest/using-the-rest-api/using-pagination-in-the-rest-api
        for page in range(1, self.max_pages + 1):
            if current in seen:
                raise IncompleteInventory("cyclic pagination")
            seen.add(current)
            self._read_path(current)
            response = self.transport("GET", current, None)
            if not isinstance(response, Response) or response.status != 200 or not isinstance(response.payload, list):
                raise IncompleteInventory("GitHub page unavailable or malformed")
            if len(response.payload) > 100 or not isinstance(response.headers, dict) or not all(isinstance(key, str) for key in response.headers):
                raise IncompleteInventory("invalid GitHub page")
            items.extend(response.payload)
            links = [value for key, value in response.headers.items() if key.casefold() == "link"]
            if len(links) > 1:
                raise IncompleteInventory("ambiguous pagination header")
            following = None
            if links:
                if not isinstance(links[0], str):
                    raise IncompleteInventory("malformed pagination header")
                matches = []
                for part in links[0].split(","):
                    match = re.fullmatch(r'\s*<([^>]+)>;\s*rel="(next|prev|first|last)"\s*', part)
                    if not match:
                        raise IncompleteInventory("malformed pagination header")
                    if match[2] == "next":
                        matches.append(match[1])
                if len(matches) > 1:
                    raise IncompleteInventory("ambiguous next page")
                if matches:
                    parsed = urlparse(matches[0])
                    if parsed.scheme != "https" or parsed.netloc != "api.github.com" or parsed.path != "/" + path or parsed.fragment:
                        raise IncompleteInventory("foreign pagination endpoint")
                    following = parsed.path[1:] + "?" + parsed.query
                    self._read_path(following)
                    query = parse_qs(parsed.query)
                    if query.get("per_page", ["100"]) != ["100"] or int(query.get("page", ["0"])[0]) != page + 1:
                        raise IncompleteInventory("non-sequential pagination")
            if following is None and len(response.payload) < 100:
                return items
            current = following or path + f"?per_page=100&page={page + 1}"
        raise IncompleteInventory("pagination limit; inventory incomplete")

    def publish_status(self, root, body, comment_id, guard):
        import receipts

        validate_subject(root)
        if self.write_enabled is not True or root["repository"] != self.repository:
            raise ValueError("hosted writer capability required")
        # Only a deterministic renderer may produce this payload; arbitrary API
        # bodies, labels, tasks, merge and repair endpoints are not exposed.
        record = receipts.parse_body(body)
        if record["root"] != root or body != receipts.render_record(record):
            raise ValueError("status body/root is not host-rendered")
        if not callable(guard):
            raise ValueError("fresh mutation guard required")
        if comment_id is None:
            method, endpoint = "POST", f"repos/{self.repository}/issues/{root['number']}/comments"
        else:
            positive(comment_id, "owned status comment id")
            method, endpoint = "PATCH", f"repos/{self.repository}/issues/comments/{comment_id}"
        guard()
        # https://docs.github.com/en/rest/issues/comments
        # Transport errors after sending must be classified LostResponse by the
        # transport; even HTTP errors here never trigger an automatic retry.
        response = self.transport(method, endpoint, {"body": body})
        if not isinstance(response, Response) or response.status != (201 if method == "POST" else 200):
            raise LostResponse("status publication result unavailable")
        if not isinstance(response.payload, dict):
            raise LostResponse("status publication returned no identity")
        result = response.payload
        if type(result.get("id")) is not int or result["id"] <= 0 or (comment_id is not None and result["id"] != comment_id):
            raise LostResponse("status publication identity unavailable")
        return {"id": result["id"]}
