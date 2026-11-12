#!/usr/bin/env python3
"""Release Guard: a required-but-non-delaying inspection status.

Run ONLY by .github/workflows/release-guard.yml and release-guard-defer.yml, from the
base branch's copy of this file. It never reads, builds or runs anything from a PR: it
uses the GitHub API for the PR's changed-file NAMES and its labels, and nothing else.

  release_guard.py event   one PR (pull_request_target): post the status
  release_guard.py queue   fallback when `event` errored: queue it, never block it
  release_guard.py defer   every open PR (schedule): pass long-queued ones

The `release-guard` commit status on the PR head:
  success  OK        ordinary PR, posted at once
  pending  QUEUED    sensitive PR, for DEFER_MINUTES
  success  DEFERRED  sensitive PR after the wait
  failure  BLOCKED   label release-guard-block is on

Only statuses created by github-actions[bot] are read, because only those satisfy the
ruleset (integration 15368). Standard library only; stdlib urllib, no third-party code.
"""

# fmt: off
# One file shared verbatim by every xekoms repo, whose formatters use different line
# lengths, so it is hand-formatted to 88 columns and the formatter leaves it alone.
import datetime as dt
import itertools
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

UTC = dt.timezone(dt.timedelta(0))
CONTEXT = "release-guard"
ACTIONS_BOT = "github-actions[bot]"
BLOCK_LABEL = "release-guard-block"
SENSITIVE_LABEL = "needs-bradley"
MARKER = "<!-- release-guard:inspection-queued -->"

OK = "No sensitive paths"
QUEUED = "Release Guard inspection queued"
DEFERRED = "Deferred \u2013 follow-up required (Release Guard)"
BLOCKED = "Release Guard P0/P1 finding \u2013 see PR comments"

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
            sys.exit(
                f"::error::release-guard: {method} {path.split('?')[0]} "
                f"returned HTTP {error.code}."
            )
        except (urllib.error.URLError, OSError) as error:
            sys.exit(
                f"::error::release-guard: {method} failed ({type(error).__name__})."
            )

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
        if s.get("state") == "pending" and s.get("description") == QUEUED
    ]
    return min(times) if times else None


def defer_minutes():
    value = os.environ.get("DEFER_MINUTES", "20")
    return int(value) if value.isdigit() and 1 <= int(value) <= 120 else 20


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


def event():
    api = Api()
    number, sha, run_url = (
        os.environ["PR_NUMBER"],
        os.environ["HEAD_SHA"],
        os.environ["RUN_URL"],
    )
    if not (number.isdigit() and re.fullmatch(r"[0-9a-f]{40}", sha)):
        sys.exit(
            "::error::release-guard: the event's PR number or head SHA is malformed."
        )
    pr = api.call("GET", f"/pulls/{number}")
    labels = {label["name"] for label in pr.get("labels", [])}
    if BLOCK_LABEL in labels:
        api.post_status(sha, "failure", BLOCKED, run_url)
        return
    reasons, sample, count = set(), [], 0
    for item in api.pages(f"/pulls/{number}/files"):  # the API caps this at 3000 files
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
    if SENSITIVE_LABEL in labels:
        reasons.add(f"{SENSITIVE_LABEL} label")
    if not reasons:
        api.post_status(sha, "success", OK, run_url)
        return
    statuses = api.guard_statuses(sha)
    since = queued_since(statuses)
    now = dt.datetime.now(UTC)
    if since and now - since >= dt.timedelta(minutes=defer_minutes()):
        api.post_status(sha, "success", DEFERRED, run_url)
        return
    latest = statuses[0] if statuses else None
    if not (
        latest
        and latest.get("state") == "pending"
        and latest.get("description") == QUEUED
    ):
        api.post_status(
            sha, "pending", QUEUED, run_url
        )  # keeps the first queue time as the clock
    upsert_comment(api, number, sha, reasons, sample)


def queue():
    """Fallback after an error: queue the PR; the scheduled run passes it (D9)."""
    api = Api()
    sha = os.environ["HEAD_SHA"]
    if re.fullmatch(r"[0-9a-f]{40}", sha):
        api.post_status(sha, "pending", QUEUED, os.environ["RUN_URL"])


def defer():
    api = Api()
    run_url = os.environ["RUN_URL"]
    now = dt.datetime.now(UTC)
    wait = dt.timedelta(minutes=defer_minutes())
    deferred = 0
    for pr in api.pages("/pulls?state=open", limit=10):
        sha = (pr.get("head") or {}).get("sha", "")
        if not re.fullmatch(r"[0-9a-f]{40}", sha):
            continue
        if BLOCK_LABEL in {label["name"] for label in pr.get("labels", [])}:
            continue
        statuses = api.guard_statuses(sha)
        if (
            not statuses
            or statuses[0].get("state") != "pending"
            or statuses[0].get("description") != QUEUED
        ):
            continue
        since = queued_since(statuses)
        if since and now - since >= wait:
            api.post_status(sha, "success", DEFERRED, run_url)
            deferred += 1
    print(f"release-guard: deferred {deferred} PR(s).")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) == 2 else ""
    {"event": event, "queue": queue, "defer": defer}.get(
        mode, lambda: sys.exit("usage: release_guard.py event|queue|defer")
    )()
