"""Unit tests for aws_exe_sys/common/vcs/github.py — GitHub provider layer."""

import json
import subprocess
from unittest.mock import patch

import pytest
import responses

from aws_exe_sys.common.vcs.github import GitHubProvider, GITHUB_API_BASE


@pytest.fixture
def github():
    return GitHubProvider()


# ---------------------------------------------------------------------------
# get_comments — pagination
# ---------------------------------------------------------------------------


class TestGetComments:
    @responses.activate
    def test_single_page(self, github):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[
                {"id": 1, "body": "first"},
                {"id": 2, "body": "second"},
            ],
            status=200,
        )

        comments = github.get_comments("org/repo", 42, "token")
        assert len(comments) == 2
        assert comments[0]["id"] == 1

    @responses.activate
    def test_pagination(self, github):
        # Full page of 100
        page1 = [{"id": i, "body": f"comment {i}"} for i in range(100)]
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=page1,
            status=200,
        )
        # Partial second page
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[{"id": 200, "body": "page 2"}],
            status=200,
        )

        comments = github.get_comments("org/repo", 42, "token")
        assert len(comments) == 101
        assert len(responses.calls) == 2

    @responses.activate
    def test_empty(self, github):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[],
            status=200,
        )

        comments = github.get_comments("org/repo", 42, "token")
        assert comments == []


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


class TestCreateComment:
    @responses.activate
    def test_create_comment(self, github):
        responses.add(
            responses.POST,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json={"id": 12345, "body": "test comment"},
            status=201,
        )

        comment_id = github.create_comment("org/repo", 42, "test comment", "token123")
        assert comment_id == 12345

        req = responses.calls[0].request
        assert "Bearer token123" in req.headers["Authorization"]
        assert json.loads(req.body)["body"] == "test comment"


class TestUpdateComment:
    @responses.activate
    def test_success(self, github):
        responses.add(
            responses.PATCH,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/comments/12345",
            json={"id": 12345, "body": "updated"},
            status=200,
        )
        assert github.update_comment("org/repo", 12345, "updated", "token") is True

    @responses.activate
    def test_failure(self, github):
        responses.add(
            responses.PATCH,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/comments/12345",
            json={"message": "Not Found"},
            status=404,
        )
        assert github.update_comment("org/repo", 12345, "updated", "token") is False


class TestDeleteComment:
    @responses.activate
    def test_success(self, github):
        responses.add(
            responses.DELETE,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/comments/12345",
            status=204,
        )
        assert github.delete_comment("org/repo", 12345, "token") is True

    @responses.activate
    def test_failure(self, github):
        responses.add(
            responses.DELETE,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/comments/99999",
            status=404,
        )
        assert github.delete_comment("org/repo", 99999, "token") is False


# ---------------------------------------------------------------------------
# find_comment_by_tag — whole-body substring search
# ---------------------------------------------------------------------------


class TestFindCommentByTag:
    @responses.activate
    def test_finds_tag_anywhere_in_body(self, github):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[
                {"id": 1, "body": "unrelated"},
                {"id": 2, "body": "contains #tag-123 in the middle\nmore text"},
            ],
            status=200,
        )
        assert github.find_comment_by_tag("org/repo", 42, "#tag-123", "token") == 2

    @responses.activate
    def test_not_found(self, github):
        responses.add(
            responses.GET,
            f"{GITHUB_API_BASE}/repos/org/repo/issues/42/comments",
            json=[{"id": 1, "body": "no match"}],
            status=200,
        )
        assert github.find_comment_by_tag("org/repo", 42, "#missing", "token") is None


# ---------------------------------------------------------------------------
# get_clone_url — URL construction, no hardcoded github.com
# ---------------------------------------------------------------------------


class TestGetCloneUrl:
    def test_anonymous_url(self, github):
        assert github.get_clone_url("org/repo") == "https://github.com/org/repo.git"

    def test_token_url_uses_x_access_token(self, github):
        url = github.get_clone_url("org/repo", token="ghp_abc")
        assert url == "https://x-access-token:ghp_abc@github.com/org/repo.git"

    def test_https_host_is_configurable(self):
        """A subclass can override ``https_host`` for GitHub Enterprise."""
        class GheProvider(GitHubProvider):
            https_host = "ghe.example.com"
            ssh_host = "ghe.example.com"

        ghe = GheProvider()
        assert ghe.get_clone_url("org/repo") == "https://ghe.example.com/org/repo.git"
        assert (
            ghe.get_clone_url("org/repo", token="tok")
            == "https://x-access-token:tok@ghe.example.com/org/repo.git"
        )


# ---------------------------------------------------------------------------
# clone — HTTPS primary, SSH fallback, public, with commit checkout
# ---------------------------------------------------------------------------


class TestClone:
    def test_clone_https_token_url(self, github, tmp_path):
        """HTTPS clone with token calls git with the token-embedded URL."""
        dest = str(tmp_path / "repo")
        with patch("aws_exe_sys.common.vcs.github.subprocess.run") as mock_run:
            github.clone("org/repo", dest=dest, token="ghp_secret")

        assert mock_run.call_count == 1
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert cmd[0] == "git"
        assert cmd[1] == "clone"
        assert "https://x-access-token:ghp_secret@github.com/org/repo.git" in cmd
        assert dest in cmd
        assert kwargs["check"] is True

    def test_clone_https_anonymous(self, github, tmp_path):
        """No token, no SSH key → unauthenticated HTTPS (public repo)."""
        dest = str(tmp_path / "pub")
        with patch("aws_exe_sys.common.vcs.github.subprocess.run") as mock_run:
            github.clone("public/repo", dest=dest)

        assert mock_run.call_count == 1
        cmd = mock_run.call_args.args[0]
        assert "https://github.com/public/repo.git" in cmd
        # Anonymous URL must not leak any token placeholder.
        assert "x-access-token" not in " ".join(cmd)

    def test_clone_ssh(self, github, tmp_path):
        """SSH-only path uses GIT_SSH_COMMAND env var and git@host: URL."""
        dest = str(tmp_path / "ssh-repo")
        with patch("aws_exe_sys.common.vcs.github.subprocess.run") as mock_run:
            github.clone_ssh("org/repo", dest=dest, ssh_key_path="/tmp/key")

        assert mock_run.call_count == 1
        args, kwargs = mock_run.call_args
        cmd = args[0]
        assert "git@github.com:org/repo.git" in cmd
        env = kwargs["env"]
        assert "GIT_SSH_COMMAND" in env
        assert "/tmp/key" in env["GIT_SSH_COMMAND"]

    def test_clone_https_fallback_to_ssh_on_failure(self, github, tmp_path):
        """When HTTPS clone fails and an SSH key is provided, fall back to SSH."""
        dest = str(tmp_path / "fallback")
        calls: list = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            if len(calls) == 1:
                # First call = HTTPS, fail it
                raise subprocess.CalledProcessError(1, cmd, stderr="auth failed")
            # Second call = SSH, succeed
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("aws_exe_sys.common.vcs.github.subprocess.run", side_effect=fake_run):
            github.clone(
                "org/repo", dest=dest, token="bad", ssh_key_path="/tmp/key",
            )

        assert len(calls) == 2
        https_cmd = calls[0][0]
        ssh_cmd = calls[1][0]
        assert "https://x-access-token:bad@github.com/org/repo.git" in https_cmd
        assert "git@github.com:org/repo.git" in ssh_cmd

    def test_clone_with_commit_hash_checks_out(self, github, tmp_path):
        """After clone, commit_hash triggers a git checkout."""
        dest = str(tmp_path / "commit")
        with patch("aws_exe_sys.common.vcs.github.subprocess.run") as mock_run:
            github.clone("org/repo", dest=dest, token="tok", commit_hash="abc123")

        # Call 1 = clone, call 2 = checkout
        assert mock_run.call_count == 2
        clone_cmd = mock_run.call_args_list[0].args[0]
        checkout_cmd = mock_run.call_args_list[1].args[0]
        assert clone_cmd[:3] == ["git", "clone", "--depth"]
        assert clone_cmd[3] == "2"  # depth=2 when commit_hash set
        assert checkout_cmd == ["git", "checkout", "abc123"]

    def test_clone_commit_not_in_shallow_falls_back_to_fetch(
        self, github, tmp_path,
    ):
        """If the commit isn't in the shallow clone, fall back to git fetch."""
        dest = str(tmp_path / "deep")
        calls: list = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            if cmd[:3] == ["git", "checkout", "abc123"] and len(
                [c for c in calls if c[:3] == ["git", "checkout", "abc123"]]
            ) == 1:
                raise subprocess.CalledProcessError(1, cmd, stderr="not found")
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch("aws_exe_sys.common.vcs.github.subprocess.run", side_effect=fake_run):
            github.clone(
                "org/repo", dest=dest, token="t", commit_hash="abc123",
            )

        # clone + checkout (fail) + fetch + checkout (succeed)
        assert len(calls) == 4
        assert calls[0][:2] == ["git", "clone"]
        assert calls[1] == ["git", "checkout", "abc123"]
        assert calls[2][:3] == ["git", "fetch", "--depth"]
        assert calls[3] == ["git", "checkout", "abc123"]
