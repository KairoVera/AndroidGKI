#!/usr/bin/env python3
"""Land the in-tree pin promotion on the dev branch via the GitHub Contents
API, authenticated with the repo-scoped fine-grained PAT (GH_TOKEN).

Why the write-back bypasses `git push` entirely
-----------------------------------------------
actions/checkout authenticates every git request by writing
    http.https://github.com/.extraheader = AUTHORIZATION: basic <GITHUB_TOKEN>
into a per-credential config file and pulling it in via `includeIf.gitdir`.
An injected Authorization header takes precedence over credentials embedded
in a URL, so even a `git push` whose URL carries the promote PAT still went
out under GITHUB_TOKEN. GITHUB_TOKEN is a GitHub App that can never push a
commit touching .github/workflows/ (the server answers: "refusing to allow a
GitHub App to create or update workflow ... without `workflows` permission"),
and that `workflows` scope is not grantable to GITHUB_TOKEN via any
`permissions:` key (the schema rejects it).

The Contents API is plain HTTPS with the PAT in the Authorization header —
the same proven channel the nightly-release job already uses — and the PAT
carries the `workflows` + `contents` write scopes, so the commit lands. The
new commit sits on the branch tip as read at commit time; a concurrent edit
surfaces as a 409 and the next update run simply re-promotes.

Usage: commit_promoted_pins.py <message> <file...>
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

REPO = os.environ["GITHUB_REPOSITORY"]
BRANCH = os.environ["GITHUB_REF_NAME"]
TOK = os.environ.get("GH_TOKEN") or os.environ.get("CI_PROMOTE_TOKEN") or ""
API = f"https://api.github.com/repos/{REPO}"


def call(method, url, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
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
        raise SystemExit(f"Contents API {method} {url} -> {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Contents API {method} {url} -> network: {e.reason}")


def blob_sha(path):
    """sha of `path` on BRANCH, or None when the file is new (no update sha
    needed for a create). A 404 is the expected 'new file' case."""
    q = urllib.parse.quote(path)
    try:
        return call("GET", f"{API}/contents/{q}?ref={BRANCH}")["sha"]
    except SystemExit as e:
        if " -> 404" in str(e):
            return None
        raise


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit("usage: commit_promoted_pins.py <message> <file...>")
    msg, files = args[0], args[1:]
    if not TOK:
        sys.exit("ERROR: no PAT available for the pin write-back (GH_TOKEN empty)")

    entries = []
    for path in files:
        entry = {
            "path": path,
            "content": base64.b64encode(open(path, "rb").read()).decode(),
        }
        sha = blob_sha(path)
        if sha:  # updating an existing file requires its current sha
            entry["sha"] = sha
        entries.append(entry)
        print(f"  {'update' if sha else 'create'} {path}")

    # One commit carrying every promoted file (Contents API nested form).
    res = call("PUT", f"{API}/contents", {
        "message": msg,
        "branch": BRANCH,
        "contents": entries,
    })
    print(f"Commit {res['commit']['sha']} on {BRANCH}: {len(entries)} file(s).")


if __name__ == "__main__":
    main()
