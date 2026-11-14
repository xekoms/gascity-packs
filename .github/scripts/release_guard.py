#!/usr/bin/env python3
"""Release Guard: a required-but-non-delaying inspection status.

Run ONLY by .github/workflows/release-guard.yml and release-guard-defer.yml, from the
base branch's copy of this file. It never reads, builds or runs anything from a PR: it
uses the GitHub API for the PR's changed-file NAMES and its labels, and nothing else.

  release_guard.py event   one PR (pull_request_target): post the status
  release_guard.py queue   fallback when `event` errored: queue it, never block it
  release_guard.py defer   every open PR (schedule): re-check labels, pass long-queued

The `release-guard` commit status on the PR head:
  success  OK        ordinary PR, posted at once
  pending  QUEUED    sensitive PR, for DEFER_MINUTES
  pending  RETRYING  sensitive PR whose inspection comment could not be posted yet
  success  DEFERRED  sensitive PR after the wait, once its comment is posted
  failure  BLOCKED   label release-guard-block is on

A commit status belongs to a commit, not a PR, so every open PR whose head is that
commit is weighed and the most restrictive result is posted: failure > pending >
success. A label added with a job token starts no workflow run, so the scheduled
`defer` run re-checks every open PR's current labels and files, not only queued ones.

Only statuses created by github-actions[bot] are read, because only those satisfy the
ruleset (integration 15368). Standard library only; stdlib urllib, no third-party code.
"""

# fmt: off
# One file shared verbatim by every xekoms repo, whose formatters use different line
# lengths, so it is hand-formatted to 88 columns and the formatter leaves it alone.
import datetime as dt
import hashlib
import itertools
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

UTC = dt.timezone(dt.timedelta(0))
CONTEXT = "release-guard"
ACTIONS_BOT = "github-actions[bot]"
BLOCK_LABEL = "release-guard-block"
# The owner-review label, kept as a SHA-256 so his name stays out of every repo.
OWNER_LABEL_SHA256 = "aada8fcebdfb3db54e50ec34877f9fca45522d8527d7b7a5ccfe20eb56527a55"
MARKER = "<!-- release-guard:inspection-queued -->"

OK = "No sensitive paths"
QUEUED = "Release Guard inspection queued"
DEFERRED = "Deferred \u2013 follow-up required (Release Guard)"
BLOCKED = "Release Guard P0/P1 finding \u2013 see PR comments"
RETRYING = "Release Guard inspection comment not posted yet \u2013 retrying"
QUEUED_STATES = {QUEUED, RETRYING}
RANK = {"success": 0, "pending": 1, "failure": 2}
COMMENT_ATTEMPTS = 3

# Words inside one path segment. Segments are split on punctuation and camelCase, so
# `useAuth.ts`, `auth-callback.tsx`, `SignInForm.tsx` and `login_page.py` all match.
WORDS = {
    "auth": {
        "auth",
        "authn",
        "authz",
        "authentication",
        "authorization",
        "authorize",
        "oauth",
        "login",
        "logout",
        "logins",
        "signin",
        "signup",
        "session",
        "sessions",
        "password",
        "passwords",
        "passwd",
        "credential",
        "credentials",
        "jwt",
        "sso",
        "saml",
        "oidc",
        "mfa",
        "totp",
    },
    "access rules": {
        "rls",
        "role",
        "roles",
        "rbac",
        "permission",
        "permissions",
        "policy",
        "policies",
        "acl",
        "acls",
        "grant",
        "grants",
    },
    "secrets": {"secret", "secrets", "vault"},
    "schema": {"schema", "schemas", "migration", "migrations", "alembic", "prisma"},
    "deploy config": {
        "infra",
        "terraform",
        "k8s",
        "kubernetes",
        "helm",
        "deploy",
        "deployment",
    },
}
# Two adjacent words: `sign-in`, `sign_up`, `SignIn`, `log-in`.
PAIRS = {("sign", "in"), ("sign", "up"), ("log", "in"), ("log", "out")}
# Whole-path patterns (lower-cased path).
PATHS = [
    ("schema", re.compile(r"(^|/)supabase/")),
    ("schema", re.compile(r"\.sql$")),
    ("secrets", re.compile(r"(^|/)\.env(\.|$)")),
    ("secrets", re.compile(r"\.(pem|key|p12|pfx|jks)$")),
    ("secrets", re.compile(r"(^|/)(\.npmrc|\.pypirc|id_rsa|id_ed25519)$")),
    ("deploy config", re.compile(r"(^|/)\.github/(workflows|actions)/")),
    ("deploy config", re.compile(r"(^|/)\.github/codeowners$|(^|/)codeowners$")),
    (
        "deploy config",
        re.compile(
            r"(^|/)(vercel\.json|netlify\.toml|fly\.toml|render\.ya?ml|procfile)$"
        ),
    ),
    ("deploy config", re.compile(r"(^|/)wrangler\.(toml|json|jsonc)$")),
    (
        "deploy config",
        re.compile(
            r"(^|/)(dockerfile[^/]*|[^/]*\.dockerfile|docker-compose[^/]*|compose\.ya?ml)$"
        ),
    ),
    ("deploy config", re.compile(r"\.tf$|\.tfvars$")),
]
# Prose and tests are not inspected for words: a doc or a test about sign-in
# changes nothing
# that runs. Secret-looking files are still caught there by PATHS.
PROSE_OR_TESTS = re.compile(
    r"\.(md|mdx|markdown|rst|txt|adoc)$"
    r"|(^|/)(docs?|handoff|history|examples?|tests?|__tests__|spec|fixtures|\.agents|\.claude|\.continue|\.cursor|skills)/"
    r"|(^|/)test_[^/]*$|[._-](test|spec)\.[a-z0-9]+$"
)
SPLIT = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")


def categories(path):
    found = set()
    low = path.lower()
    for name, pattern in PATHS:
        if pattern.search(low):
            found.add(name)
    if PROSE_OR_TESTS.search(low) and not low.startswith(".github/"):
        return {name for name in found if name == "secrets"}
    for segment in path.split("/"):
        words = [w.lower() for w in SPLIT.findall(segment)]
        for name, vocabulary in WORDS.items():
            if any(w in vocabulary for w in words):
                found.add(name)
        if any(pair in PAIRS for pair in itertools.pairwise(words)):
            found.add("auth")
    return found


class ApiError(Exception):
    """One failed GitHub API call. The message names the call, never the token."""


class Api:
    def __init__(self):
        self.base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
        self.token = os.environ["GH_TOKEN"]
        self.repo = os.environ["REPO"]
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repo):
            sys.exit("::error::release-guard: REPO is not owner/name.")

    def call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            f"{self.base}/repos/{self.repo}{path}",
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            raise ApiError(
                f"{method} {path.split('?')[0]} returned HTTP {error.code}."
            ) from None
        except (urllib.error.URLError, OSError) as error:
            raise ApiError(f"{method} failed ({type(error).__name__}).") from None

    def pages(self, path, limit=30):
        joiner = "&" if "?" in path else "?"
        for page in range(1, limit + 1):
            items = self.call("GET", f"{path}{joiner}per_page=100&page={page}")
            yield from items
            if len(items) < 100:
                return

    def post_status(self, sha, state, description, target_url):
        self.call(
            "POST",
            f"/statuses/{sha}",
            {
                "state": state,
                "context": CONTEXT,
                "description": description[:140],
                "target_url": target_url,
            },
        )
        print(f"release-guard: posted '{state}' ({description}) on {sha}.")

    def guard_statuses(self, sha):
        """This check's statuses on one commit from Actions, newest first."""
        return [
            s
            for s in self.pages(f"/commits/{sha}/statuses", limit=5)
            if s.get("context") == CONTEXT
            and (s.get("creator") or {}).get("login") == ACTIONS_BOT
        ]


def parse_time(stamp):
    return dt.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


def queued_since(statuses):
    """When this commit was first queued for inspection, or None."""
    times = [
        parse_time(s["created_at"])
        for s in statuses
        if s.get("state") == "pending" and s.get("description") in QUEUED_STATES
    ]
    return min(times) if times else None


def defer_minutes():
    value = os.environ.get("DEFER_MINUTES", "20")
    return int(value) if value.isdigit() and 1 <= int(value) <= 120 else 20


def is_sha(value):
    return bool(re.fullmatch(r"[0-9a-f]{40}", value or ""))


def safe_path(path):
    """A changed path as inert text: no markdown, no mentions, no newlines."""
    return re.sub(r"[^A-Za-z0-9._/+-]", "?", path)[:120]


def upsert_comment(api, number, sha, reasons, sample):
    lines = [
        MARKER,
        "**Release Guard inspection queued** (`release-guard:inspection-queued`)",
        "",
        f"Head `{sha}` touches sensitive areas: {', '.join(sorted(reasons))}.",
        (
            f"`release-guard` stays pending for about {defer_minutes()} minutes, then "
            "passes as \u201cDeferred \u2013 follow-up required\u201d. "
            "Merging is never blocked by the wait."
        ),
        (
            "Release Guard picks this up on its next check-in and opens a Linear "
            f"inspection issue. A P0/P1 finding adds the `{BLOCK_LABEL}` label, "
            "which fails the check until it is removed."
        ),
        "",
    ]
    if sample:
        lines += ["Matched paths (first 15):", ""] + [
            f"- `{safe_path(p)}`" for p in sample[:15]
        ]
    body = "\n".join(lines)
    for comment in api.pages(f"/issues/{number}/comments", limit=10):
        if (comment.get("user") or {}).get("login") == ACTIONS_BOT and MARKER in (
            comment.get("body") or ""
        ):
            if f"`{sha}`" not in comment["body"]:
                api.call("PATCH", f"/issues/comments/{comment['id']}", {"body": body})
            return
    api.call("POST", f"/issues/{number}/comments", {"body": body})


def comment_with_retry(api, number, sha, reasons, sample):
    """True once PR `number` carries the marker comment for `sha` (bounded retry)."""
    for attempt in range(1, COMMENT_ATTEMPTS + 1):
        try:
            upsert_comment(api, number, sha, reasons, sample)
            return True
        except ApiError as error:
            print(
                f"::warning::release-guard: inspection comment on PR #{number}, "
                f"attempt {attempt}/{COMMENT_ATTEMPTS}: {error}"
            )
            if attempt < COMMENT_ATTEMPTS:
                time.sleep(2**attempt)
    return False


def verdict(api, pr):
    """One open PR's own result: (state, reasons, sample) before any wait."""
    labels = {label["name"] for label in pr.get("labels", [])}
    if BLOCK_LABEL in labels:
        return "failure", set(), []
    reasons, sample, count = set(), [], 0
    for item in api.pages(f"/pulls/{pr['number']}/files"):  # the API caps it at 3000
        count += 1
        for path in {item.get("filename", ""), item.get("previous_filename") or ""} - {
            ""
        }:
            hit = categories(path)
            if hit:
                reasons |= hit
                sample.append(path)
    if count >= 3000:
        reasons.add("over 3000 files")
    if any(
        hashlib.sha256(name.encode()).hexdigest() == OWNER_LABEL_SHA256
        for name in labels
    ):
        reasons.add("owner-review label")
    return ("pending" if reasons else "success"), reasons, sample


def open_prs_by_head(api):
    """Every open PR, grouped by head commit."""
    heads = {}
    for pr in api.pages("/pulls?state=open", limit=10):
        sha = (pr.get("head") or {}).get("sha", "")
        if is_sha(sha) and str(pr.get("number", "")).isdigit():
            heads.setdefault(sha, []).append(pr)
    return heads


def settle(api, sha, prs, run_url, now):
    """Post the most restrictive result over every open PR on one head commit.

    failure (any PR blocked) > pending (any PR queued) > success. A queued commit
    passes as DEFERRED only after the wait AND once every queued PR carries the
    marker comment. Returns False when a comment could not be posted: the status
    is then left pending (RETRYING) and the next scheduled run tries again.
    """
    verdicts = [(pr["number"], *verdict(api, pr)) for pr in prs]
    statuses = api.guard_statuses(sha)
    latest = statuses[0] if statuses else {}

    def post(state, description):
        if (latest.get("state"), latest.get("description")) != (state, description):
            api.post_status(sha, state, description, run_url)

    worst = max((v[1] for v in verdicts), key=RANK.__getitem__, default="success")
    if worst != "pending":
        post(worst, BLOCKED if worst == "failure" else OK)
        return True
    posted = [
        comment_with_retry(api, number, sha, reasons, sample)
        for number, state, reasons, sample in verdicts
        if state == "pending"
    ]
    if not all(posted):
        post("pending", RETRYING)
        return False
    since = queued_since(statuses)
    if since and now - since >= dt.timedelta(minutes=defer_minutes()):
        post("success", DEFERRED)
    elif not (
        latest.get("state") == "pending" and latest.get("description") == QUEUED
    ):
        api.post_status(sha, "pending", QUEUED, run_url)  # first one is the clock
    return True


def event():
    api = Api()
    number, sha, run_url = (
        os.environ["PR_NUMBER"],
        os.environ["HEAD_SHA"],
        os.environ["RUN_URL"],
    )
    if not (number.isdigit() and is_sha(sha)):
        sys.exit(
            "::error::release-guard: the event's PR number or head SHA is malformed."
        )
    pr = api.call("GET", f"/pulls/{number}")
    prs = {p["number"]: p for p in open_prs_by_head(api).get(sha, [])}
    prs[pr["number"]] = pr  # this PR with its current labels, even if just closed
    if not settle(api, sha, list(prs.values()), run_url, dt.datetime.now(UTC)):
        sys.exit(
            "::error::release-guard: the inspection comment could not be posted after "
            f"{COMMENT_ATTEMPTS} attempts. The check stays pending, never passes "
            "without it, and the scheduled release-guard-defer run retries it."
        )


def queue():
    """Fallback after an error: queue the PR; the scheduled run settles it (D9).

    Never downgrades a failure (another PR on the same commit may be blocked)."""
    api = Api()
    sha = os.environ["HEAD_SHA"]
    if not is_sha(sha):
        return
    statuses = api.guard_statuses(sha)
    latest = statuses[0] if statuses else {}
    if latest.get("state") == "failure" or (
        latest.get("state") == "pending" and latest.get("description") in QUEUED_STATES
    ):
        print(f"release-guard: kept '{latest['state']}' on {sha}.")
        return
    api.post_status(sha, "pending", QUEUED, os.environ["RUN_URL"])


def defer():
    api = Api()
    run_url = os.environ["RUN_URL"]
    now = dt.datetime.now(UTC)
    heads = open_prs_by_head(api)
    unsettled = 0
    for sha, prs in heads.items():
        try:
            if not settle(api, sha, prs, run_url, now):
                unsettled += 1
        except ApiError as error:
            print(f"::warning::release-guard: {sha}: {error}")
            unsettled += 1
    print(f"release-guard: settled {len(heads) - unsettled} of {len(heads)} head(s).")
    if unsettled:
        sys.exit(
            f"::error::release-guard: {unsettled} head(s) left pending (a comment or "
            "API call failed); the next scheduled run retries them."
        )


def main(mode):
    try:
        {"event": event, "queue": queue, "defer": defer}.get(
            mode, lambda: sys.exit("usage: release_guard.py event|queue|defer")
        )()
    except ApiError as error:
        sys.exit(f"::error::release-guard: {error}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) == 2 else "")
