#!/usr/bin/env python3
"""Publish a rendered dashboard frame to GitHub.

Works for a public repo (device fetches the plain Pages/raw URL) and for a private
repo (device fetches through the API with a read-only token) - the publish step is
identical either way, which is why this does not need to know which you chose.

Auth comes from the environment, never from a file in this repo:
    GH_TOKEN   fine-grained PAT, contents:write on the target repo (read for the device)
    GH_REPO    owner/name
    GH_PATH    path in the repo, default dash.png
    GH_BRANCH  default main

Uses the Contents API rather than git, so there is no local clone, no history to
rebase, and one PUT per frame. Exit code is non-zero on any failure, so a cron job
can tell a push that worked from one that silently did not.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"


def _req(method: str, url: str, token: str, payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body.strip() else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"message": body[:300]}


def _git_push(token: str, repo: str, branch: str, path: str, content: str,
              commit_msg: str) -> str:
    """One orphan-commit push. Raises SystemExit carrying the server's message."""
    status, ref = _req("GET", f"{API}/repos/{repo}/git/ref/heads/{branch}", token)
    base_tree = None
    if status == 200 and ref.get("object", {}).get("sha"):
        st, commit = _req("GET", f"{API}/repos/{repo}/git/commits/{ref['object']['sha']}", token)
        if st == 200:
            base_tree = commit.get("tree", {}).get("sha")

    st, blob = _req("POST", f"{API}/repos/{repo}/git/blobs", token,
                    {"content": content, "encoding": "base64"})
    if st not in (200, 201):
        raise SystemExit(f"blob failed: {st} {blob.get('message', '')}")

    tree_payload = {"tree": [{"path": path, "mode": "100644", "type": "blob",
                              "sha": blob["sha"]}]}
    if base_tree:
        tree_payload["base_tree"] = base_tree      # keep the rest of the repo
    st, tree = _req("POST", f"{API}/repos/{repo}/git/trees", token, tree_payload)
    if st not in (200, 201):
        raise SystemExit(f"tree failed: {st} {tree.get('message', '')}")

    # No parents: an orphan commit, so the branch stays exactly one commit deep.
    st, new_commit = _req("POST", f"{API}/repos/{repo}/git/commits", token,
                          {"message": commit_msg, "tree": tree["sha"], "parents": []})
    if st not in (200, 201):
        raise SystemExit(f"commit failed: {st} {new_commit.get('message', '')}")

    st, res = _req("PATCH", f"{API}/repos/{repo}/git/refs/heads/{branch}", token,
                   {"sha": new_commit["sha"], "force": True})
    if st not in (200, 201):
        raise SystemExit(f"ref update failed: {st} {res.get('message', '')}")
    return f"https://raw.githubusercontent.com/{repo}/{branch}/{path}"


def publish(local_png: Path, single_commit: bool = True, attempts: int = 3) -> str:
    """Upload the frame. With single_commit, the branch is force-updated to an orphan
    commit so history never grows - which matters when a cron publishes every few
    minutes (a fresh ~120 KB commit every 5 min is gigabytes a year, an orphan commit
    is a constant ~120 KB forever). Falls back to the Contents API if the git API
    path is unavailable."""
    token = os.environ.get("GH_TOKEN", "").strip()
    repo = os.environ.get("GH_REPO", "").strip()
    path = os.environ.get("GH_PATH", "dash.png").strip().lstrip("/")
    branch = os.environ.get("GH_BRANCH", "main").strip()
    if not token or "/" not in repo:
        raise SystemExit("set GH_TOKEN and GH_REPO=owner/name")
    if not local_png.exists():
        raise SystemExit(f"no such frame: {local_png}")

    content = base64.b64encode(local_png.read_bytes()).decode()
    commit_msg = f"dashboard: update {path}"

    if single_commit:
        failure = ""
        for attempt in range(1, attempts + 1):
            try:
                url = _git_push(token, repo, branch, path, content, commit_msg)
                print(f"pushed {local_png.name} ({local_png.stat().st_size} bytes) -> "
                      f"{repo}:{path} @ {branch} (single commit, attempt {attempt})")
                return url
            except SystemExit as exc:
                failure = str(exc)
                # 401/403/404 mean the token or the repo is wrong; retrying cannot help.
                if any(f" {code} " in failure for code in ("401", "403", "404")):
                    raise
                if attempt < attempts:
                    delay = 2 * attempt
                    sys.stderr.write(f"push attempt {attempt}/{attempts} failed ({failure}); "
                                     f"retrying in {delay}s\n")
                    time.sleep(delay)
        sys.stderr.write(f"single-commit push failed {attempts}x ({failure}); "
                         f"falling back to the Contents API\n")

    content_url = f"{API}/repos/{repo}/contents/{path}"
    status, meta = _req("GET", f"{content_url}?ref={branch}", token)

    payload = {
        "message": f"dashboard: update {path}",
        "content": base64.b64encode(local_png.read_bytes()).decode(),
        "branch": branch,
    }
    if status == 200 and isinstance(meta, dict) and meta.get("sha"):
        payload["sha"] = meta["sha"]          # update in place
    elif status not in (200, 404):
        raise SystemExit(f"cannot read {path} on {repo}: {status} {meta.get('message', '')}")

    status, res = 0, {}
    for attempt in range(1, 3):
        status, res = _req("PUT", content_url, token, payload)
        if status in (200, 201):
            break
        if attempt < 2:
            sys.stderr.write(f"push failed: {status} {res.get('message', '')}; retrying\n")
            time.sleep(2 * attempt)
    if status not in (200, 201):
        raise SystemExit(f"push failed: {status} {res.get('message', '')}")

    commit = (res.get("commit") or {}).get("sha", "")[:8]
    print(f"pushed {local_png.name} ({local_png.stat().st_size} bytes) -> {repo}:{path} @ {branch} [{commit}]")
    return res.get("content", {}).get("html_url", "")


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "dash.png").resolve()
    url = publish(target)
    if url:
        print(f"public url: {url}")