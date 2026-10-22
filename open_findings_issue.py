"""
Opens a GitHub issue for each newly-added file under docs/missy-findings/,
so Missy's daily standing audit (which runs as a scheduled Routine with no
GitHub API access of its own - see docs/decisions.md, 2026-09-21) actually
surfaces to the repo owner as a real, emailed notification instead of a
file sitting quietly in docs/ that nobody looks at.

Triggered on push to main touching docs/missy-findings/**.md (see
.github/workflows/missy-findings-issue.yml). Uses the workflow's own
GITHUB_TOKEN, same pattern as check_reminders.py's open_github_issue() -
this repo's owner already gets GitHub's own notification email for a new
issue in a repo they're watching, so this needs no separate email service.

Only opens an issue for files ADDED in this push (git diff --diff-filter=A
against the previous commit) - a findings file is written once per day and
never modified afterward, so "added" is the right signal for "new day's
audit landed," not "modified" (which would also fire on unrelated pushes
that happen to touch an old findings file, e.g. a rebase).

Fails loudly (sys.exit(1)) on any GitHub API error - a findings file that
silently fails to notify anyone is worse than useless, the same reasoning
check_reminders.py's own docstring gives for its own failure mode.
"""

import os
import subprocess
import sys

import requests

REQUEST_TIMEOUT = 30
FINDINGS_DIR = "docs/missy-findings"


def added_findings_files():
    # Diffs against the push event's own "before" SHA (passed in as
    # BEFORE_SHA), not a hardcoded HEAD~1 - a push can contain more than
    # one commit (e.g. scripts/commit_and_push.sh's own "residual commit"
    # fallback), and HEAD~1 would only see the last of those, silently
    # missing the findings file if it landed in an earlier commit of the
    # same push.
    before_sha = os.environ.get("BEFORE_SHA")
    result = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=A", before_sha, "HEAD", "--", FINDINGS_DIR],
        capture_output=True, text=True, check=True,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def existing_issue_number(repo, token, title):
    resp = requests.get(
        "https://api.github.com/search/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        params={"q": f'repo:{repo} is:issue in:title "{title}"'},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    items = resp.json().get("items", [])
    for item in items:
        if item.get("title") == title:
            return item["number"]
    return None


def open_issue(repo, token, path):
    date = os.path.basename(path).removesuffix(".md")
    title = f"\U0001f9d0 Missy's findings for {date}"

    if existing_issue_number(repo, token, title) is not None:
        print(f"Issue already exists for {path}, skipping")
        return

    body = open(path, encoding="utf-8").read()
    body += (
        f"\n\n---\nOpened automatically from `{path}` by open_findings_issue.py "
        "(runs on every push touching docs/missy-findings/). See docs/decisions.md "
        "(2026-09-21) for why this exists instead of Missy opening issues directly."
    )

    resp = requests.post(
        f"https://api.github.com/repos/{repo}/issues",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
        json={"title": title, "body": body, "labels": ["missy-finding"]},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    print(f"Opened {resp.json()['html_url']} for {path}")


def main():
    token = os.environ.get("GITHUB_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    before_sha = os.environ.get("BEFORE_SHA")
    if not token or not repo or not before_sha:
        print("ERROR: GITHUB_TOKEN, GITHUB_REPOSITORY, and BEFORE_SHA must be set", file=sys.stderr)
        sys.exit(1)

    files = added_findings_files()
    if not files:
        print("No newly-added findings files in this push - nothing to do")
        return

    failures = []
    for path in files:
        try:
            open_issue(repo, token, path)
        except requests.RequestException as e:
            print(f"ERROR: failed to open issue for {path}: {e}", file=sys.stderr)
            failures.append(path)

    if failures:
        print(f"\nERROR: failed to open issue(s) for: {failures}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
