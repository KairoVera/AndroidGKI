#!/usr/bin/env python3
"""Land the in-tree pin promotion on the dev branch via the GitHub Git Data
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

The Git Data API is plain HTTPS with the PAT in the Authorization header —
the same proven channel nightly-release already uses — and the PAT carries
the `workflows` + `contents` write scopes, so the commit lands. The commit
sits on the branch tip as read at commit time; a concurrent edit surfaces as
a 409 and the next update run simply re-promotes.

Endpoint note: the multi-file form is POST /repos/{owner}/{repo}/commits
with a `files` array (base64 `content`, `path`). There is NO REST endpoint
`PUT /repos/{owner}/{repo}/contents` without a path — that URL does not
exist and returns 404; the single-file PUT/POST is per-`path` only.

Usage: commit_promoted_pins.py <message> <file...>
"""
import base64
import json
import os
import sys
import urllib.error
import urllib.request

REPO = os.environ["GITHUB_REPOSITORY"]
BRANCH = os.environ["GITHUB_REF_NAME"]
# CI_PROMOTE_TOKEN is what the "Commit promoted pins" step declares, so it
# wins; GH_TOKEN is only a fallback for local ad-hoc runs. In the workflow
# step GH_TOKEN is not set, so a GITHUB_TOKEN app token can never leak in.
TOK = os.environ.get("CI_PROMOTE_TOKEN") or os.environ.get("GH_TOKEN") or ""
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
        # A 409 here means the branch moved between our checkout and the
        # commit — let the next update run retry rather than force anything.
        if e.code == 409:
            print(f"409: branch {BRANCH} moved; next update run will re-promote.")
            return None
        raise SystemExit(f"Git Data API {method} {url} -> {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Git Data API {method} {url} -> network: {e.reason}")


def main():
    args = sys.argv[1:]
    if len(args) < 2:
        sys.exit("usage: commit_promoted_pins.py <message> <file...>")
    msg, files = args[0], args[1:]
    if not TOK:
        sys.exit("ERROR: no PAT available for the pin write-back (GH_TOKEN empty)")

    files_body = []
    for path in files:
        with open(path, "rb") as fh:
            content = base64.b64encode(fh.read()).decode()
        files_body.append({"content": content, "path": path})
        print(f"  commit {path}")

    res = call("POST", f"{API}/commits", {
        "message": msg,
        "branch": BRANCH,
        "files": files_body,
    })
    if res is None:
        return  # 409 retry case, handled above
    print(f"Commit {res['sha']} on {BRANCH}: {len(files_body)} file(s).")


if __name__ == "__main__":
    main()
