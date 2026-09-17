#!/usr/bin/env python3
"""Land the in-tree pin promotion on the dev branch via the GitHub Contents
API, authenticated with the repo-scoped fine-grained PAT (CI_PROMOTE_TOKEN).

Why not `git push` (discovered by earlier iterations)
-----------------------------------------------------
actions/checkout injects GITHUB_TOKEN through a per-credential config file
(`includeIf.gitdir` -> `http.https://github.com/.extraheader`), and that
injected Authorization header beats any credential embedded in a URL. Pushes
therefore still went out as the GitHub App token, which the server refuses
for commits touching .github/workflows/.

Why not the "obvious" commit endpoints
--------------------------------------
- POST /repos/{owner}/{repo}/commits (create a commit with `files[]`) is
  NOT on GitHub's list of endpoints available to fine-grained personal
  access tokens, so it answers 404 (GitHub hides permission gaps behind
  404 rather than 403, to prevent token probing).
- PUT /repos/{owner}/{repo}/contents without a path is not an endpoint.

What works
----------
PUT /repos/{owner}/{repo}/contents/{path} ("create or update file
contents") IS on the fine-grained list and needs Contents: write, plus
Workflows: write when the path sits under .github/workflows/ — both are
granted to CI_PROMOTE_TOKEN. A nested `contents` array in the same body
commits multiple files in ONE commit. Existing files must be updated with
their current blob sha (from `git rev-parse HEAD:<path>`) or the API
rejects with 409; brand-new files (the pins/history record) omit it.

Usage: commit_promoted_pins.py <message> <file...>
"""
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = os.environ["GITHUB_REPOSITORY"]
BRANCH = os.environ["GITHUB_REF_NAME"]
# CI_PROMOTE_TOKEN is what the "Commit promoted pins" step declares, so it
# wins; GH_TOKEN is only a fallback for local ad-hoc runs. In the workflow
# step GH_TOKEN is not set, so an App token can never sneak in.
TOK = os.environ.get("CI_PROMOTE_TOKEN") or os.environ.get("GH_TOKEN") or ""
API = f"https://api.github.com/repos/{REPO}"


def old_blob_sha(path):
    """Current blob sha of `path` in the checked-out commit, or None.

    The API refuses an update whose supplied sha no longer matches the
    file in the repo, so every pre-existing file must carry its sha; a
    new file (the timestamped pins/history record) has no repo-side blob
    and must NOT carry one.
    """
    r = subprocess.run(
        ["git", "rev-parse", f"HEAD:{path}"],
        capture_output=True, text=True,
    )
    return r.stdout.strip() if r.returncode == 0 else None


def call(method, url, body):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method=method,
        headers={
            "Authorization": f"Bearer {TOK}",
            "Accept": "application/vnd.github+json",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:500]
        # 409 = the branch's file moved between checkout and this commit
        # (someone else pushed). Nothing to force: the NEXT update run
        # re-promotes from the fresh tip.
        if e.code == 409:
            print(f"409: {BRANCH} moved under us; next update run re-promotes.")
            return None
        raise SystemExit(f"Contents API {method} {url} -> {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Contents API {method} {url} -> network: {e.reason}")


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit("usage: commit_promoted_pins.py <message> <file...>")
    msg, files = args[0], args[1:]
    if not TOK:
        sys.exit("ERROR: CI_PROMOTE_TOKEN empty; cannot land the pin write-back")

    entries = []
    for path in files:
        entry = {
            "path": path,
            "content": base64.b64encode(open(path, "rb").read()).decode(),
        }
        sha = old_blob_sha(path)
        if sha:
            entry["sha"] = sha
        entries.append(entry)
        print(f"  commit {path} (repo blob {sha[:12] + '…' if sha else 'new file'})")

    # One request, one commit: the first file rides as the top-level
    # `content` (its path forms the URL); the rest ride in the nested
    # `contents` array.
    top, rest = entries[0], entries[1:]
    body = {"message": msg, "branch": BRANCH, "content": top["content"]}
    if top.get("sha"):
        body["sha"] = top["sha"]
    if rest:
        body["contents"] = rest

    res = call("PUT", f"{API}/contents/{urllib.parse.quote(top['path'], safe='')}", body)
    if res is None:
        return  # 409 handled above
    print(f"Commit {res['commit']['sha'][:12]} on {BRANCH}: {len(entries)} file(s).")


if __name__ == "__main__":
    main()
