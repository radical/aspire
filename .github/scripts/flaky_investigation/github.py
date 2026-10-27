"""Small GitHub REST adapter. Remote writes are never retried implicitly."""

import base64
import io
import json
from pathlib import PurePosixPath
import re
import subprocess
import zipfile
from urllib.parse import urlencode

from contracts import REPOSITORY, integer, require, sha, timestamp, validate_checkpoint


TRAILER = "\n\nCo-authored-by: Copilot App <223556219+Copilot@users.noreply.github.com>\n"


class GitHubAPIError(subprocess.CalledProcessError):
    """Expose captured diagnostics while preserving existing failure handling."""

    def __str__(self):
        if not self.stderr:
            return super().__str__()
        diagnostic = self.stderr.decode("utf-8", errors="replace")
        diagnostic = re.sub(r"(?:gh[pousr]_|github_pat_)[A-Za-z0-9_]+", "[REDACTED]", diagnostic)
        diagnostic = re.sub(r"(?i)([?&](?:sig|token|access_token)=)[^&\s\"']+", r"\1[REDACTED]", diagnostic)
        return f"{super().__str__()}\nGitHub API diagnostic: {json.dumps(diagnostic.strip()[:4000])}"


class GitHub:
    def api(self, endpoint, body=None, method=None, binary=False, timeout=120):
        command = ["gh", "api", f"repos/{REPOSITORY}/{endpoint}"]
        if method:
            command += ["--method", method]
        if body is not None:
            command += ["--input", "-"]
        try:
            result = subprocess.run(command, input=json.dumps(body).encode() if body is not None else None,
                                    capture_output=True, check=True, timeout=timeout)
        except subprocess.CalledProcessError as error:
            raise GitHubAPIError(error.returncode, error.cmd, output=error.stdout, stderr=error.stderr) from error
        if binary:
            return result.stdout
        return json.loads(result.stdout) if result.stdout else None

    def optional(self, endpoint):
        try:
            return self.api(endpoint)
        except subprocess.CalledProcessError as error:
            if b"HTTP 404" not in error.stderr:
                raise
            return None

    def pages(self, endpoint, key=None):
        result = []
        for page in range(1, 101):
            value = self.api(f"{endpoint}{'&' if '?' in endpoint else '?'}per_page=100&page={page}")
            items = value[key] if key else value
            result.extend(items)
            if len(items) < 100:
                return result
        raise ValueError("Pagination exceeds bounded collection limit")

    def file(self, path, revision):
        sha(revision)
        value = self.api(f"contents/{path}?ref={revision}")
        require(value.get("type") == "file" and value.get("encoding") == "base64"
                and value.get("size", 0) <= 1000000, "Expected a bounded regular repository file")
        return base64.b64decode(value["content"]).decode("utf-8")

    def tree(self, revision):
        value = self.api(f"git/trees/{sha(revision)}?recursive=1")
        require(not value.get("truncated"), "Repository tree was truncated")
        return {item["path"]: item for item in value["tree"]
                if item["type"] == "blob" and item["mode"] == "100644"}

    def commit(self, parent, files, message):
        entries = []
        for path, content in files.items():
            blob = self.api("git/blobs", {"encoding": "utf-8", "content": content})
            entries.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})
        body = {"tree": entries}
        if parent:
            body["base_tree"] = self.api(f"git/commits/{parent}")["tree"]["sha"]
        tree = self.api("git/trees", body)
        return self.api("git/commits", {
            "message": message + TRAILER, "tree": tree["sha"], "parents": [parent] if parent else [],
        })["sha"]

    def create_ref(self, branch, revision):
        return self.api("git/refs", {"ref": f"refs/heads/{branch}", "sha": sha(revision)})

    def checkpoint(self, state, previous):
        revision = self.commit(previous, {"state.json": json.dumps(state, indent=2) + "\n"},
                               f"ci: checkpoint flaky investigation {state['issue']} ({state['phase']})")
        branch = state_branch(state["issue"])
        if previous:
            # A concurrent writer produces a sibling commit. A nonforced update
            # rejects it instead of overwriting a newer checkpoint.
            self.api(f"git/refs/heads/{branch}", {"sha": revision, "force": False}, "PATCH")
        else:
            self.create_ref(branch, revision)
        return revision

    def load(self, issue, revision=None):
        if revision is None:
            ref = self.optional(f"git/ref/heads/{state_branch(issue)}")
            if ref is None:
                return None, None
            revision = ref["object"]["sha"]
        state = json.loads(self.file("state.json", revision))
        validate_checkpoint(state, issue)
        return state, revision

    def dispatch(self, workflow, branch, inputs):
        # GitHub's return_run_details response is the receipt, not a latest-run
        # lookup. Older servers returning 204 are an ambiguous write, not success.
        require(re.fullmatch(r"ankj/flaky-test-[1-9][0-9]*-(?:agent|run)-[1-9][0-9]*", branch),
                "Dispatch requires a scoped investigation branch")
        value = self.api(f"actions/workflows/{workflow}/dispatches", {
            "ref": branch, "inputs": inputs, "return_run_details": True,
        })
        require(isinstance(value, dict) and type(value.get("workflow_run_id")) is int,
                "Dispatch has no run-ID receipt; inspect the persisted intent, do not retry")
        return value["workflow_run_id"]

    def dispatch_candidates(self, owned, title, requested_at, now):
        # REST exposes display_title, not dispatch inputs. Both workflows embed
        # the full immutable intent SHA, e.g. "Flaky reproduction <issue> [<sha>]".
        # A complete bounded response is required; never choose the newest run.
        # https://docs.github.com/en/rest/actions/workflow-runs#list-workflow-runs-for-a-workflow
        query = urlencode({
            "branch": owned["branch"], "event": "workflow_dispatch", "head_sha": owned["sha"],
            "created": requested_at.replace(microsecond=0).isoformat() + ".." + now.isoformat(),
            "per_page": 100,
        })
        value = self.api(f"actions/workflows/{owned['workflow']}/runs?{query}")
        require(isinstance(value, dict), "Malformed dispatch discovery response")
        count = integer(value.get("total_count"), 100, minimum=0)
        runs = value.get("workflow_runs")
        require(isinstance(runs, list) and len(runs) == count, "Incomplete dispatch discovery response")
        seen = set()
        for run in runs:
            correlated_run(run, owned, title, requested_at, now)
            require(run["id"] not in seen, "Duplicate dispatch discovery record")
            seen.add(run["id"])
        return runs

    def artifact(self, run_id, name, maximum=15000000):
        artifacts = self.pages(f"actions/runs/{run_id}/artifacts", "artifacts")
        matches = [item for item in artifacts if not item["expired"] and (
            item["name"] == name or (name == "repo-memory-default"
                                    and re.fullmatch(r"[0-9a-f]{8}-repo-memory-default", item["name"])))]
        require(len(matches) == 1, f"Expected one unexpired artifact: {name}")
        artifact = matches[0]
        require(artifact["size_in_bytes"] <= maximum, "Artifact exceeds compressed size budget")
        data = self.api(f"actions/artifacts/{artifact['id']}/zip", binary=True)
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            require(sum(item.file_size for item in archive.infolist()) <= maximum * 4,
                    "Artifact exceeds expanded size budget")
            result = {}
            for item in archive.infolist():
                path = PurePosixPath(item.filename)
                require(not path.is_absolute() and ".." not in path.parts
                        and item.filename not in result, "Invalid or duplicate artifact path")
                if not item.is_dir():
                    result[item.filename] = archive.read(item)
            return result


def state_branch(issue):
    return f"ankj/flaky-test-{issue}-state"


def owned_run(run, owned):
    require(isinstance(run, dict) and isinstance(run.get("head_repository"), dict)
            and run.get("id") == owned["id"] and run.get("run_attempt") == 1
            and run.get("head_sha") == owned["sha"]
            and run.get("head_branch") == owned["branch"]
            and run.get("event") == "workflow_dispatch"
            and run.get("path") == f".github/workflows/{owned['workflow']}"
            and run.get("head_repository", {}).get("full_name") == REPOSITORY,
            "Run identity/attempt does not match the owned dispatch")
    return run


def dispatch_title(kind, issue_url, revision):
    require(kind in {"agent", "worker"}, "Unknown dispatch kind")
    return f"Flaky {'investigator' if kind == 'agent' else 'reproduction'} {issue_url} [{sha(revision)}]"


def correlated_run(run, owned, title, requested_at, now):
    require(isinstance(run, dict), "Malformed dispatch discovery record")
    run_id = integer(run.get("id"), 10 ** 15)
    owned_run(run, {**owned, "id": owned.get("id", run_id)})
    require(isinstance(run.get("repository"), dict)
            and run["repository"].get("full_name") == REPOSITORY
            and run.get("display_title") == title, "Dispatch repository/intent correlation mismatch")
    # GitHub serializes created_at to seconds; the persisted intent has
    # microseconds. A run created in that same second must remain discoverable.
    require(requested_at.replace(microsecond=0) <= timestamp(run.get("created_at")) <= now,
            "Dispatch creation time is outside the intent window")
    require(isinstance(run.get("status"), str) and run["status"] in
            {"queued", "in_progress", "completed", "waiting", "pending", "requested"},
            "Unknown discovered run status")
    return run
