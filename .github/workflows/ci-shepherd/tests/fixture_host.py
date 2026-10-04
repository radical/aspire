"""Test-only subprocess shim; no production endpoint or capability override."""

import os
from datetime import datetime
from pathlib import Path
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen

sys.path.insert(0, os.environ["TEST_SOURCE"])
import hosted
import live
import round as contracts
from github import Response


class Loopback:
    def open(self, request, timeout):
        url = urlparse(request.full_url)
        rewritten = Request("http://127.0.0.1:" + os.environ["TEST_HTTP_PORT"] + url.path + ("?" + url.query if url.query else ""),
                            data=request.data, headers=dict(request.headers), method=request.get_method())
        return urlopen(rewritten, timeout=timeout)


original = live.HTTPTransport


class FixtureTransport(original):
    def __init__(self, token, *, write=False):
        super().__init__(token, write=write, opener=Loopback())

    def __call__(self, method, endpoint, body):
        if method == "GET" and (endpoint.endswith("/logs") or endpoint.endswith("/zip")):
            with self.opener.open(Request("https://api.github.com/" + endpoint), timeout=30) as response:
                return Response(response.read(live.MAX_BYTES + 1), {}, response.status)
        return super().__call__(method, endpoint, body)


live.HTTPTransport = FixtureTransport
if os.environ.get("TEST_CLOCK"):
    observed = datetime.fromisoformat(os.environ["TEST_CLOCK"])
    live.clock = lambda: observed
hosted.prepare.__kwdefaults__["host_check"] = lambda run: None
hosted.apply.__kwdefaults__["host_check"] = lambda run: None
if Path(sys.argv[1]).name == "hosted.py":
    sys.exit(hosted.main(sys.argv[2:]))
sys.exit(contracts.main(sys.argv[2:]))
