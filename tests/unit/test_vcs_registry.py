"""Unit tests for the VCS provider registry (P2-2).

The architectural point of this registry is that a third-party provider
(Bitbucket, GitLab, Gitea…) can be registered at runtime and ``clone_repo``
will dispatch to it. This test proves that end-to-end.
"""

from typing import List, Optional
from unittest.mock import patch

import pytest

from aws_exe_sys.common import code_source
from aws_exe_sys.common.vcs import (
    GitHubProvider,
    UnknownVcsProviderError,
    VcsProvider,
    get_provider,
    list_providers,
    register_provider,
)
from aws_exe_sys.common.vcs import registry as vcs_registry


@pytest.fixture
def clean_registry():
    """Snapshot/restore the VCS registry so tests don't leak providers."""
    saved = dict(vcs_registry._PROVIDERS)
    try:
        yield
    finally:
        vcs_registry._PROVIDERS.clear()
        vcs_registry._PROVIDERS.update(saved)


# ---------------------------------------------------------------------------
# Built-in provider is wired at import time
# ---------------------------------------------------------------------------


class TestBuiltinRegistration:
    def test_github_is_registered(self):
        providers = list_providers()
        assert "github" in providers
        assert isinstance(providers["github"], GitHubProvider)

    def test_get_provider_github(self):
        assert isinstance(get_provider("github"), GitHubProvider)

    def test_unknown_raises(self):
        with pytest.raises(UnknownVcsProviderError) as exc:
            get_provider("nonexistent")
        assert "nonexistent" in str(exc.value)


# ---------------------------------------------------------------------------
# Third-party provider registration — the architectural point
# ---------------------------------------------------------------------------


class _BitbucketStub(VcsProvider):
    """Minimal Bitbucket stub that records all clone calls."""

    name = "bitbucket"
    https_host = "bitbucket.org"
    ssh_host = "bitbucket.org"

    def __init__(self):
        self.clone_calls: List[dict] = []
        self.ssh_calls: List[dict] = []

    def get_clone_url(self, repo: str, token: Optional[str] = None) -> str:
        if token:
            return f"https://x-token-auth:{token}@{self.https_host}/{repo}.git"
        return f"https://{self.https_host}/{repo}.git"

    def clone(self, repo, dest, token="", commit_hash=None, ssh_key_path=None):
        self.clone_calls.append({
            "repo": repo,
            "dest": dest,
            "token": token,
            "commit_hash": commit_hash,
            "ssh_key_path": ssh_key_path,
        })

    def clone_ssh(self, repo, dest, ssh_key_path, commit_hash=None):
        self.ssh_calls.append({
            "repo": repo,
            "dest": dest,
            "ssh_key_path": ssh_key_path,
            "commit_hash": commit_hash,
        })

    # Comment CRUD — not exercised here, but the ABC requires them.
    def create_comment(self, repo, pr_number, body, token):
        return 1

    def update_comment(self, repo, comment_id, body, token):
        return True

    def delete_comment(self, repo, comment_id, token):
        return True

    def find_comment_by_tag(self, repo, pr_number, tag, token):
        return None

    def get_comments(self, repo, pr_number, token):
        return []


class TestRegisterBitbucketStub:
    def test_register_bitbucket_stub(self, clean_registry):
        """Register a Bitbucket stub and confirm ``clone_repo`` routes to it.

        This is the architectural point of P2-2: callers set
        ``job.git_provider="bitbucket"`` and the VCS abstraction takes
        care of the rest, with no code changes inside code_source.
        """
        bitbucket = _BitbucketStub()
        register_provider(bitbucket)

        # Registry picks up the new provider.
        assert "bitbucket" in list_providers()
        assert get_provider("bitbucket") is bitbucket

        # get_clone_url uses the Bitbucket host + auth convention.
        assert (
            bitbucket.get_clone_url("team/repo", token="tok")
            == "https://x-token-auth:tok@bitbucket.org/team/repo.git"
        )

        # code_source.clone_repo dispatches to the registered provider.
        with patch(
            "aws_exe_sys.common.code_source.tempfile.mkdtemp",
            return_value="/tmp/bb-clone",
        ):
            result = code_source.clone_repo(
                repo="team/repo",
                token="bb-token",
                commit_hash="abc",
                provider="bitbucket",
            )

        assert result == "/tmp/bb-clone"
        assert len(bitbucket.clone_calls) == 1
        call = bitbucket.clone_calls[0]
        assert call["repo"] == "team/repo"
        assert call["dest"] == "/tmp/bb-clone"
        assert call["token"] == "bb-token"
        assert call["commit_hash"] == "abc"

    def test_register_provider_without_name_raises(self, clean_registry):
        class Bad(VcsProvider):
            def get_clone_url(self, repo, token=None):
                return ""

            def clone(self, *args, **kwargs):
                pass

            def clone_ssh(self, *args, **kwargs):
                pass

            def create_comment(self, *args, **kwargs):
                return 0

            def update_comment(self, *args, **kwargs):
                return False

            def delete_comment(self, *args, **kwargs):
                return False

            def find_comment_by_tag(self, *args, **kwargs):
                return None

            def get_comments(self, *args, **kwargs):
                return []

        with pytest.raises(ValueError, match="name"):
            register_provider(Bad())

    def test_clone_repo_unknown_provider_raises(self):
        """``code_source.clone_repo`` with an unknown provider surfaces the
        registry error — no silent fallback to GitHub."""
        with pytest.raises(UnknownVcsProviderError):
            code_source.clone_repo(
                repo="team/repo",
                provider="gitlab_self_hosted_nope",
            )


# ---------------------------------------------------------------------------
# code_source.clone_repo default routes to GitHub
# ---------------------------------------------------------------------------


class TestCloneRepoDefault:
    def test_default_provider_is_github(self):
        """``clone_repo`` with no ``provider=`` argument uses GitHub."""
        with patch(
            "aws_exe_sys.common.code_source.tempfile.mkdtemp",
            return_value="/tmp/gh-clone",
        ), patch(
            "aws_exe_sys.common.vcs.github.subprocess.run",
        ) as mock_run:
            result = code_source.clone_repo(repo="org/repo", token="tok")

        assert result == "/tmp/gh-clone"
        # git clone was called exactly once
        assert mock_run.call_count == 1
        cmd = mock_run.call_args.args[0]
        assert "https://x-access-token:tok@github.com/org/repo.git" in cmd
