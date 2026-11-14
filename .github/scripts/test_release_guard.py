"""Unit tests for release_guard.py: python3 .github/scripts/test_release_guard.py

Standard library only. A fake GitHub API keeps commit statuses, PRs and comments in
memory, so nothing here touches the network.
"""

# fmt: off
# Shared verbatim by every xekoms repo, like release_guard.py: hand-formatted to 88.
import contextlib
import datetime as dt
import importlib.util
import os
import pathlib
import unittest
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("rg", HERE / "release_guard.py")
rg = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rg)

SHA = "a" * 40
OTHER = "b" * 40
NOW = dt.datetime(2026, 11, 14, 12, 0, tzinfo=rg.UTC)
RUN = "https://example.invalid/run/1"


def stamp(minutes_ago):
    return (NOW - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeApi(rg.Api):
    def __init__(self, prs=(), files=None, fail_comments=0):
        self.prs = {pr["number"]: pr for pr in prs}
        self.files = files or {}
        self.statuses = {}  # sha -> newest-first list
        self.comments = {}  # PR number -> list
        self.fail_comments = fail_comments
        self.comment_calls = 0

    def add_status(self, sha, state, description, minutes_ago):
        self.statuses.setdefault(sha, []).insert(
            0,
            {
                "context": rg.CONTEXT,
                "state": state,
                "description": description,
                "created_at": stamp(minutes_ago),
                "creator": {"login": rg.ACTIONS_BOT},
            },
        )

    def latest(self, sha):
        top = self.statuses.get(sha, [{}])[0]
        return top.get("state"), top.get("description")

    def call(self, method, path, body=None):
        path = path.split("?")[0]
        if method == "GET" and path.startswith("/pulls/") and path.count("/") == 2:
            return self.prs[int(path.split("/")[2])]
        if method == "POST" and path.startswith("/statuses/"):
            sha = path.split("/")[2]
            self.add_status(sha, body["state"], body["description"], 0)
            return {}
        if method in {"POST", "PATCH"} and "comments" in path:
            self.comment_calls += 1
            if self.fail_comments:
                self.fail_comments -= 1
                raise rg.ApiError(f"{method} {path} returned HTTP 502.")
            if method == "POST":
                number = int(path.split("/")[2])
                author = {"login": rg.ACTIONS_BOT}
                self.comments.setdefault(number, []).append(
                    {"id": number, "user": author, "body": body["body"]}
                )
            else:
                for comments in self.comments.values():
                    for comment in comments:
                        comment["body"] = body["body"]
            return {}
        raise AssertionError(f"unexpected call {method} {path}")

    def pages(self, path, limit=30):
        path = path.split("?")[0]
        if path == "/pulls":
            return list(self.prs.values())
        if path.endswith("/files"):
            number = int(path.split("/")[2])
            return [{"filename": name} for name in self.files.get(number, [])]
        if path.endswith("/comments"):
            return list(self.comments.get(int(path.split("/")[2]), []))
        if path.endswith("/statuses"):
            return list(self.statuses.get(path.split("/")[2], []))
        raise AssertionError(f"unexpected page {path}")


def pr(number, sha=SHA, labels=()):
    names = [{"name": name} for name in labels]
    return {"number": number, "head": {"sha": sha}, "labels": names}


def no_sleep(_seconds):
    pass


def run(api, mode, **env):
    """Run one entry point against `api` with `env`; returns the SystemExit or None."""
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.object(rg, "Api", lambda: api))
        stack.enter_context(mock.patch.object(rg.time, "sleep", no_sleep))
        stack.enter_context(mock.patch.dict(os.environ, {"RUN_URL": RUN, **env}))
        try:
            getattr(rg, mode)()
        except SystemExit as stop:
            return stop
    return None


class CommentRetryTests(unittest.TestCase):
    """Finding 1: a sensitive PR never passes without its inspection comment."""

    def setUp(self):
        patch = mock.patch.object(rg.time, "sleep", no_sleep)
        patch.start()
        self.addCleanup(patch.stop)

    def test_transient_comment_failure_is_retried(self):
        api = FakeApi([pr(1)], {1: ["supabase/migrations/1.sql"]}, fail_comments=2)
        self.assertTrue(rg.settle(api, SHA, [api.prs[1]], RUN, NOW))
        self.assertEqual(api.comment_calls, 3)
        self.assertIn(rg.MARKER, api.comments[1][0]["body"])
        self.assertEqual(api.latest(SHA), ("pending", rg.QUEUED))

    def test_persistent_failure_stays_pending_even_after_the_wait(self):
        api = FakeApi([pr(1)], {1: ["supabase/x.sql"]}, fail_comments=99)
        api.add_status(SHA, "pending", rg.QUEUED, 60)
        self.assertFalse(rg.settle(api, SHA, [api.prs[1]], RUN, NOW))
        self.assertEqual(api.comment_calls, rg.COMMENT_ATTEMPTS)
        self.assertEqual(api.latest(SHA), ("pending", rg.RETRYING))

    def test_next_tick_posts_comment_then_defers(self):
        api = FakeApi([pr(1)], {1: ["supabase/x.sql"]}, fail_comments=99)
        api.add_status(SHA, "pending", rg.QUEUED, 60)
        rg.settle(api, SHA, [api.prs[1]], RUN, NOW)
        api.fail_comments = 0
        self.assertTrue(rg.settle(api, SHA, [api.prs[1]], RUN, NOW))
        self.assertIn(rg.MARKER, api.comments[1][0]["body"])
        self.assertEqual(api.latest(SHA), ("success", rg.DEFERRED))

    def test_event_exits_nonzero_when_comment_fails(self):
        api = FakeApi([pr(1)], {1: [".env.production"]}, fail_comments=99)
        stop = run(api, "event", PR_NUMBER="1", HEAD_SHA=SHA)
        self.assertIn("could not be posted", str(stop.code))
        self.assertEqual(api.latest(SHA), ("pending", rg.RETRYING))

    def test_defer_reports_unsettled_heads(self):
        api = FakeApi([pr(1)], {1: ["supabase/x.sql"]}, fail_comments=99)
        api.add_status(SHA, "pending", rg.QUEUED, 60)
        self.assertIsNotNone(run(api, "defer"))
        self.assertEqual(api.latest(SHA), ("pending", rg.RETRYING))

    def test_queue_fallback_keeps_retrying_and_failure(self):
        for state, description in (("pending", rg.RETRYING), ("failure", rg.BLOCKED)):
            api = FakeApi()
            api.add_status(SHA, state, description, 1)
            run(api, "queue", HEAD_SHA=SHA)
            self.assertEqual(api.latest(SHA), (state, description))


class LabelReconcileTests(unittest.TestCase):
    """Finding 2: a token-added owner-review label is picked up by the defer run."""

    @staticmethod
    def owner_label():
        label = "owner-review-test"
        return label, mock.patch.object(
            rg, "OWNER_LABEL_SHA256", rg.hashlib.sha256(label.encode()).hexdigest()
        )

    def test_defer_queues_a_passed_pr_that_gained_the_owner_label(self):
        label, patch = self.owner_label()
        api = FakeApi([pr(1, labels=[label])], {1: [".github/ai-review/prompt.md"]})
        api.add_status(SHA, "success", rg.OK, 5)
        with patch:
            self.assertIsNone(run(api, "defer"))
        self.assertEqual(api.latest(SHA), ("pending", rg.QUEUED))
        self.assertIn("owner-review label", api.comments[1][0]["body"])

    def test_defer_blocks_a_pr_that_gained_the_block_label(self):
        api = FakeApi([pr(1, labels=[rg.BLOCK_LABEL])], {1: ["README.md"]})
        api.add_status(SHA, "success", rg.OK, 5)
        self.assertIsNone(run(api, "defer"))
        self.assertEqual(api.latest(SHA), ("failure", rg.BLOCKED))

    def test_defer_does_not_repost_an_unchanged_status(self):
        api = FakeApi([pr(1)], {1: ["README.md"]})
        api.add_status(SHA, "success", rg.OK, 5)
        self.assertIsNone(run(api, "defer"))
        self.assertEqual(len(api.statuses[SHA]), 1)

    def test_workflows_rerun_on_labels_and_ai_review(self):
        workflows = HERE.parent / "workflows"
        guard = (workflows / "release-guard.yml").read_text()
        defer = (workflows / "release-guard-defer.yml").read_text()
        self.assertIn("labeled, unlabeled", guard)
        self.assertIn("workflows: [ai-review]", defer)
        self.assertIn("issues: write", defer)
        for text in (guard, defer):
            lines = [line.strip() for line in text.splitlines()]
            runs_on = [line for line in lines if line.startswith("runs-on:")]
            self.assertEqual(len(runs_on), 1)
            self.assertIn("self-hosted", runs_on[0])  # never a GitHub-hosted default
            self.assertNotIn("ubuntu", runs_on[0])
            self.assertNotIn("${{ secrets", text)
            self.assertNotIn("head.ref", text)
            self.assertNotIn("runs-on: ubuntu", text)


class SharedHeadTests(unittest.TestCase):
    """Finding 3: PRs on one head commit share a status; the strictest one wins."""

    @staticmethod
    def two_prs(labels_one=(), labels_two=(), files_two=("README.md",)):
        return FakeApi(
            [pr(1, labels=labels_one), pr(2, labels=labels_two)],
            {1: ["README.md"], 2: list(files_two)},
        )

    def run_event(self, api, number):
        self.assertIsNone(run(api, "event", PR_NUMBER=str(number), HEAD_SHA=SHA))

    def test_pass_on_one_pr_cannot_overwrite_block_on_another(self):
        api = self.two_prs(labels_one=[rg.BLOCK_LABEL])
        self.run_event(api, 1)
        self.run_event(api, 2)
        self.assertEqual(api.latest(SHA), ("failure", rg.BLOCKED))

    def test_ordinary_pr_cannot_pass_a_queued_sibling(self):
        api = self.two_prs(files_two=["supabase/x.sql"])
        self.run_event(api, 1)
        self.assertEqual(api.latest(SHA), ("pending", rg.QUEUED))
        self.assertIn(rg.MARKER, api.comments[2][0]["body"])
        self.assertNotIn(1, api.comments)

    def test_unblocking_one_pr_returns_to_the_rules(self):
        api = self.two_prs(labels_one=[rg.BLOCK_LABEL])
        self.run_event(api, 1)
        api.prs[1]["labels"] = []
        self.run_event(api, 2)
        self.assertEqual(api.latest(SHA), ("success", rg.OK))

    def test_defer_does_not_pass_a_commit_another_pr_blocks(self):
        api = self.two_prs(labels_one=[rg.BLOCK_LABEL], files_two=["supabase/x.sql"])
        api.add_status(SHA, "pending", rg.QUEUED, 60)
        self.assertIsNone(run(api, "defer"))
        self.assertEqual(api.latest(SHA), ("failure", rg.BLOCKED))

    def test_other_heads_are_independent(self):
        api = FakeApi([pr(1, labels=[rg.BLOCK_LABEL]), pr(2, sha=OTHER)], {2: ["a.py"]})
        self.assertIsNone(run(api, "defer"))
        self.assertEqual(api.latest(SHA), ("failure", rg.BLOCKED))
        self.assertEqual(api.latest(OTHER), ("success", rg.OK))


class CategoryTests(unittest.TestCase):
    def test_examples(self):
        self.assertEqual(rg.categories("src/useAuth.ts"), {"auth"})
        self.assertEqual(rg.categories("docs/login.md"), set())
        self.assertIn("deploy config", rg.categories(".github/workflows/x.yml"))


if __name__ == "__main__":
    unittest.main()
