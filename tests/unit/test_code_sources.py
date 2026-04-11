"""Unit tests for the code source registry (P2-3).

The architectural point of this registry is that a third-party source
(OCI artifacts, local tarballs, HTTP downloads…) can be registered at
runtime and ``repackage_orders`` will dispatch to it with no code
changes in the call site. These tests prove that end-to-end.
"""

import os
import shutil
import tempfile
from typing import Any, List
from unittest.mock import patch

import pytest

from aws_exe_sys.common.code_sources import (
    CodeSource,
    CommandsOnlyCodeSource,
    GitCodeSource,
    S3CodeSource,
    UnknownCodeSourceError,
    detect_kind,
    list_code_sources,
    new_sources,
    register_code_source,
)
from aws_exe_sys.common.code_sources import registry as code_source_registry
from aws_exe_sys.common.models import Job, Order, SsmJob, SsmOrder


@pytest.fixture
def clean_registry():
    """Snapshot and restore the code source registry across tests."""
    saved = dict(code_source_registry._FACTORIES)
    try:
        yield
    finally:
        code_source_registry._FACTORIES.clear()
        code_source_registry._FACTORIES.update(saved)


def _order(**overrides) -> Order:
    defaults = {"cmds": ["echo hi"], "timeout": 300}
    defaults.update(overrides)
    return Order(**defaults)


def _job(orders: List[Order], **overrides) -> Job:
    defaults = {
        "username": "test",
        "git_repo": "org/repo",
        "git_token_location": "aws:::ssm:/git/token",
    }
    defaults.update(overrides)
    return Job(orders=orders, **defaults)


# ---------------------------------------------------------------------------
# Built-in sources are registered at import time
# ---------------------------------------------------------------------------


class TestBuiltinRegistration:
    def test_all_three_kinds_registered(self):
        kinds = list_code_sources()
        assert "git" in kinds
        assert "s3" in kinds
        assert "commands_only" in kinds

    def test_new_sources_returns_fresh_instances(self):
        """Each call returns a brand-new instance — state does not leak."""
        a = new_sources()
        b = new_sources()
        assert a["git"] is not b["git"]

    def test_protocol_runtime_check(self):
        """Built-in sources satisfy the CodeSource Protocol structurally."""
        assert isinstance(GitCodeSource(), CodeSource)
        assert isinstance(S3CodeSource(), CodeSource)
        assert isinstance(CommandsOnlyCodeSource(), CodeSource)


# ---------------------------------------------------------------------------
# GitCodeSource — clones and shares clone dirs across orders
# ---------------------------------------------------------------------------


class TestGitSourceClones:
    @patch("aws_exe_sys.common.code_sources.git.extract_folder")
    @patch("aws_exe_sys.common.code_sources.git.clone_repo")
    @patch(
        "aws_exe_sys.common.code_sources.git.resolve_git_credentials",
        return_value=("tok", None),
    )
    def test_git_source_clones(self, _mock_creds, mock_clone, mock_extract):
        """A git order routes through clone_repo + extract_folder."""
        mock_clone.return_value = "/tmp/cloned"
        mock_extract.return_value = "/tmp/extracted-a"

        source = GitCodeSource()
        job = _job(orders=[])
        order = _order(order_name="a", git_folder="infra/vpc")

        result = source.fetch(order, job)

        assert result == "/tmp/extracted-a"
        mock_clone.assert_called_once()
        clone_kwargs = mock_clone.call_args.kwargs
        assert clone_kwargs["repo"] == "org/repo"
        assert clone_kwargs["token"] == "tok"
        assert clone_kwargs["provider"] == "github"
        mock_extract.assert_called_once_with("/tmp/cloned", "infra/vpc")

    @patch("aws_exe_sys.common.code_sources.git.extract_folder")
    @patch("aws_exe_sys.common.code_sources.git.clone_repo")
    @patch(
        "aws_exe_sys.common.code_sources.git.resolve_git_credentials",
        return_value=("tok", None),
    )
    def test_git_source_shares_clones_same_repo_and_commit(
        self, _mock_creds, mock_clone, mock_extract,
    ):
        """Two orders on the same commit → one clone, two extract calls."""
        mock_clone.return_value = "/tmp/shared-clone"
        mock_extract.side_effect = ["/tmp/ex-a", "/tmp/ex-b"]

        source = GitCodeSource()
        job = _job(orders=[], commit_hash="abc123")
        order_a = _order(order_name="a", git_folder="vpc")
        order_b = _order(order_name="b", git_folder="rds")

        source.fetch(order_a, job)
        source.fetch(order_b, job)

        assert mock_clone.call_count == 1
        assert mock_extract.call_count == 2

    @patch("aws_exe_sys.common.code_sources.git.extract_folder")
    @patch("aws_exe_sys.common.code_sources.git.clone_repo")
    @patch(
        "aws_exe_sys.common.code_sources.git.resolve_git_credentials",
        return_value=("tok", None),
    )
    def test_git_source_clones_separately_per_commit(
        self, _mock_creds, mock_clone, mock_extract,
    ):
        """Two orders on the same repo but different commits → two clones."""
        mock_clone.side_effect = ["/tmp/c1", "/tmp/c2"]
        mock_extract.side_effect = ["/tmp/e1", "/tmp/e2"]

        source = GitCodeSource()
        job = _job(orders=[])
        a = _order(order_name="a", commit_hash="aaa")
        b = _order(order_name="b", commit_hash="bbb")

        source.fetch(a, job)
        source.fetch(b, job)

        assert mock_clone.call_count == 2

    def test_git_source_cleanup_wipes_clones(self):
        """cleanup() removes all cached clone directories from disk."""
        source = GitCodeSource()
        d1 = tempfile.mkdtemp(prefix="aws-exe-sys-git-test-")
        d2 = tempfile.mkdtemp(prefix="aws-exe-sys-git-test-")
        source._clones[("org/a", None)] = d1
        source._clones[("org/b", "sha")] = d2

        source.cleanup()

        assert not os.path.exists(d1)
        assert not os.path.exists(d2)
        assert source._clones == {}

    def test_git_source_uses_job_git_provider(self):
        """GitCodeSource forwards job.git_provider to clone_repo."""
        with patch(
            "aws_exe_sys.common.code_sources.git.clone_repo", return_value="/tmp/c",
        ) as mock_clone, patch(
            "aws_exe_sys.common.code_sources.git.extract_folder", return_value="/tmp/e",
        ), patch(
            "aws_exe_sys.common.code_sources.git.resolve_git_credentials",
            return_value=("t", None),
        ):
            source = GitCodeSource()
            job = _job(orders=[], git_provider="bitbucket")
            source.fetch(_order(git_folder="x"), job)

        assert mock_clone.call_args.kwargs["provider"] == "bitbucket"

    def test_git_source_detect(self):
        src = GitCodeSource()
        # git_repo on job
        assert src.detect(_order(), _job(orders=[])) is True
        # s3_location wins over git
        assert src.detect(
            _order(s3_location="s3://b/k"), _job(orders=[])
        ) is False
        # no repo at all
        assert src.detect(_order(), _job(orders=[], git_repo="")) is False


# ---------------------------------------------------------------------------
# S3CodeSource — downloads and extracts zip
# ---------------------------------------------------------------------------


class TestS3SourceDownloads:
    @patch("aws_exe_sys.common.code_sources.s3.fetch_code_s3")
    def test_s3_source_downloads(self, mock_fetch):
        mock_fetch.return_value = "/tmp/s3-extracted"

        source = S3CodeSource()
        result = source.fetch(
            _order(s3_location="s3://bucket/code.zip"),
            _job(orders=[]),
        )

        assert result == "/tmp/s3-extracted"
        mock_fetch.assert_called_once_with("s3://bucket/code.zip")

    def test_s3_source_detect(self):
        src = S3CodeSource()
        assert src.detect(_order(s3_location="s3://b/k"), _job(orders=[])) is True
        assert src.detect(_order(), _job(orders=[])) is False

    def test_s3_source_cleanup_is_noop(self):
        S3CodeSource().cleanup()  # should not raise


# ---------------------------------------------------------------------------
# CommandsOnlyCodeSource — empty workspace
# ---------------------------------------------------------------------------


class TestCommandsOnlySource:
    def test_commands_only_creates_empty_dir(self):
        source = CommandsOnlyCodeSource()
        job = SsmJob(username="test", orders=[])
        order = SsmOrder(cmds=["echo hi"], timeout=60)

        result = source.fetch(order, job)
        try:
            assert os.path.isdir(result)
            assert os.listdir(result) == []
        finally:
            shutil.rmtree(result, ignore_errors=True)

    def test_commands_only_detect(self):
        src = CommandsOnlyCodeSource()
        # no s3, no git → yes
        assert src.detect(
            SsmOrder(cmds=[], timeout=0),
            SsmJob(username="u", orders=[]),
        ) is True
        # has git → no
        assert src.detect(_order(), _job(orders=[])) is False
        # has s3 → no
        assert src.detect(
            _order(s3_location="s3://b/k"), _job(orders=[]),
        ) is False


# ---------------------------------------------------------------------------
# detect_kind — picks the right source for each order
# ---------------------------------------------------------------------------


class TestDetectKind:
    def test_dispatches_git_s3_commands(self, clean_registry):
        sources = new_sources()
        job = _job(orders=[])

        git_order = _order(git_folder="vpc")
        s3_order = _order(s3_location="s3://b/k")

        assert detect_kind(git_order, job, sources) == "git"
        assert detect_kind(s3_order, job, sources) == "s3"

        # Commands-only needs a job with no git_repo
        ssm_job = SsmJob(username="u", orders=[])
        ssm_order = SsmOrder(cmds=[], timeout=0)
        assert detect_kind(ssm_order, ssm_job, sources) == "commands_only"

    def test_detect_kind_raises_when_no_match(self, clean_registry):
        # Empty registry → nothing matches anything
        code_source_registry._FACTORIES.clear()
        sources = new_sources()

        with pytest.raises(UnknownCodeSourceError):
            detect_kind(_order(), _job(orders=[]), sources)


# ---------------------------------------------------------------------------
# Third-party source registration — the architectural point
# ---------------------------------------------------------------------------


class _FakeTarballSource:
    """Minimal stub proving third-party sources plug in."""

    kind = "fake_tarball"

    def __init__(self) -> None:
        self.fetch_calls: List[Any] = []

    def detect(self, order: Any, job: Any) -> bool:
        return bool(getattr(order, "env_vars", None) and "TARBALL_URL" in order.env_vars)

    def fetch(self, order: Any, job: Any) -> str:
        self.fetch_calls.append(order.env_vars["TARBALL_URL"])
        return tempfile.mkdtemp(prefix="aws-exe-sys-fake-tar-")

    def cleanup(self) -> None:
        pass


class _BadSourceNoKind:
    """Missing the required kind class attribute."""

    def detect(self, order, job):
        return False

    def fetch(self, order, job):
        return ""

    def cleanup(self):
        pass


class TestRegisterThirdPartySource:
    def test_register_third_party_source(self, clean_registry):
        """Register a tarball stub and verify detect_kind routes to it."""
        register_code_source(_FakeTarballSource)

        assert "fake_tarball" in list_code_sources()

        sources = new_sources()
        assert "fake_tarball" in sources

        # An order tagged via env_vars gets dispatched to the new source.
        order = _order(
            order_name="a",
            env_vars={"TARBALL_URL": "https://example.com/code.tar.gz"},
            s3_location="",  # defeat s3 matcher
        )
        # Use a job with no git_repo so the git matcher also declines.
        job = _job(orders=[], git_repo="")

        # Detection prefers more specific sources. Our fake is registered
        # after the built-ins, so we need either an order that the
        # built-ins decline or explicit ordering. An order without git,
        # without s3, and with TARBALL_URL in env_vars — the built-ins
        # will match "commands_only" first (registered before fake).
        # Re-register fake at the front so it wins.
        code_source_registry._FACTORIES.clear()
        register_code_source(_FakeTarballSource)  # highest priority
        register_code_source(S3CodeSource)
        register_code_source(GitCodeSource)
        register_code_source(CommandsOnlyCodeSource)

        sources = new_sources()
        kind = detect_kind(order, job, sources)
        assert kind == "fake_tarball"

        fake = sources["fake_tarball"]
        result = fake.fetch(order, job)
        try:
            assert os.path.isdir(result)
            assert fake.fetch_calls == ["https://example.com/code.tar.gz"]
        finally:
            shutil.rmtree(result, ignore_errors=True)

    def test_register_source_without_kind_raises(self, clean_registry):
        with pytest.raises(ValueError, match="kind"):
            register_code_source(_BadSourceNoKind)

    def test_register_source_explicit_kind(self, clean_registry):
        """Explicit kind= overrides / supplies the registry key."""
        register_code_source(_BadSourceNoKind, kind="explicit")
        assert "explicit" in list_code_sources()


# ---------------------------------------------------------------------------
# End-to-end: repackage_orders dispatches through the registry
# ---------------------------------------------------------------------------


class TestRepackageDispatchesThroughRegistry:
    """Prove that repackage_orders hits the registry, not direct imports."""

    @patch("aws_exe_sys.init_job.repackage.store_sops_key_ssm", return_value="/sops/k")
    @patch(
        "aws_exe_sys.init_job.repackage._generate_age_key",
        return_value=("pub", "priv", "/tmp/k"),
    )
    @patch("aws_exe_sys.common.code_sources.s3.fetch_code_s3")
    @patch("aws_exe_sys.common.code_sources.git.clone_repo")
    @patch(
        "aws_exe_sys.common.code_sources.git.resolve_git_credentials",
        return_value=("tok", None),
    )
    @patch("aws_exe_sys.common.code_sources.git.extract_folder")
    @patch("aws_exe_sys.init_job.repackage.fetch_ssm_values", return_value={})
    @patch("aws_exe_sys.init_job.repackage.fetch_secret_values", return_value={})
    @patch("aws_exe_sys.init_job.repackage.s3_ops.generate_callback_presigned_url")
    @patch("aws_exe_sys.init_job.repackage.OrderBundler")
    def test_mixed_git_and_s3_dispatch(
        self,
        MockBundler, mock_presign, _mock_secrets, _mock_ssm,
        mock_extract, _mock_resolve, mock_clone, mock_s3,
        _mock_gen_key, _mock_store_ssm,
    ):
        """Two orders — one git, one S3 — each goes to the right source."""
        from aws_exe_sys.init_job.repackage import repackage_orders
        from unittest.mock import MagicMock

        mock_presign.return_value = "https://cb.example/presign"
        MockBundler.return_value = MagicMock()

        # Create real temp dirs so zip_directory has something to walk.
        git_code = tempfile.mkdtemp(prefix="aws-exe-sys-git-test-")
        s3_code = tempfile.mkdtemp(prefix="aws-exe-sys-s3-test-")
        clone_dir = tempfile.mkdtemp(prefix="aws-exe-sys-clone-test-")

        with open(os.path.join(git_code, "main.tf"), "w") as f:
            f.write("g")
        with open(os.path.join(s3_code, "main.tf"), "w") as f:
            f.write("s")

        mock_clone.return_value = clone_dir
        mock_extract.return_value = git_code
        mock_s3.return_value = s3_code

        job = _job(orders=[
            _order(order_name="git-one", git_folder="vpc"),
            _order(order_name="s3-one", s3_location="s3://b/k"),
        ])

        try:
            results = repackage_orders(
                job=job,
                run_id="run-1",
                trace_id="t",
                flow_id="f",
                internal_bucket="internal",
            )

            assert len(results) == 2
            assert results[0]["order_name"] == "git-one"
            assert results[1]["order_name"] == "s3-one"
            mock_clone.assert_called_once()
            mock_s3.assert_called_once_with("s3://b/k")
        finally:
            shutil.rmtree(git_code, ignore_errors=True)
            shutil.rmtree(s3_code, ignore_errors=True)
            shutil.rmtree(clone_dir, ignore_errors=True)
