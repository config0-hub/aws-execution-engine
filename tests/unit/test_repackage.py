"""Unit tests for src/init_job/repackage.py."""

import os
import shutil
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from src.common.models import Job, Order
from src.init_job.repackage import repackage_orders
from src.common.code_source import (
    extract_folder,
    group_git_orders,
    fetch_ssm_values,
    fetch_secret_values,
    zip_directory,
)


def _make_job(orders=None, **kwargs):
    defaults = {
        "git_repo": "org/repo",
        "git_token_location": "aws:::ssm:/token",
        "username": "testuser",
    }
    defaults.update(kwargs)
    return Job(orders=orders or [], **defaults)


def _make_order(**kwargs):
    defaults = {
        "cmds": ["echo hello"],
        "timeout": 300,
    }
    defaults.update(kwargs)
    return Order(**defaults)


class TestZipDirectory:
    def test_creates_zip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create some files
            with open(os.path.join(tmpdir, "test.txt"), "w") as f:
                f.write("hello")
            os.makedirs(os.path.join(tmpdir, "sub"))
            with open(os.path.join(tmpdir, "sub", "nested.txt"), "w") as f:
                f.write("nested")

            zip_path = os.path.join(tmpdir, "output.zip")
            zip_directory(tmpdir, zip_path)
            assert os.path.exists(zip_path)
            assert os.path.getsize(zip_path) > 0


class TestExtractFolder:
    def test_copies_entire_clone(self):
        with tempfile.TemporaryDirectory() as clone_dir:
            with open(os.path.join(clone_dir, "main.tf"), "w") as f:
                f.write("resource {}")

            result = extract_folder(clone_dir)
            try:
                assert os.path.isdir(result)
                assert os.path.exists(os.path.join(result, "main.tf"))
            finally:
                shutil.rmtree(result, ignore_errors=True)

    def test_copies_subfolder(self):
        with tempfile.TemporaryDirectory() as clone_dir:
            os.makedirs(os.path.join(clone_dir, "vpc"))
            with open(os.path.join(clone_dir, "vpc", "main.tf"), "w") as f:
                f.write("vpc resource")
            with open(os.path.join(clone_dir, "root.txt"), "w") as f:
                f.write("root")

            result = extract_folder(clone_dir, "vpc")
            try:
                assert os.path.exists(os.path.join(result, "main.tf"))
                # root.txt should NOT be present (only vpc/ contents copied)
                assert not os.path.exists(os.path.join(result, "root.txt"))
            finally:
                shutil.rmtree(result, ignore_errors=True)

    def test_excludes_git_dir(self):
        with tempfile.TemporaryDirectory() as clone_dir:
            os.makedirs(os.path.join(clone_dir, ".git"))
            with open(os.path.join(clone_dir, ".git", "HEAD"), "w") as f:
                f.write("ref: refs/heads/main")
            with open(os.path.join(clone_dir, "main.tf"), "w") as f:
                f.write("resource {}")

            result = extract_folder(clone_dir)
            try:
                assert os.path.exists(os.path.join(result, "main.tf"))
                assert not os.path.exists(os.path.join(result, ".git"))
            finally:
                shutil.rmtree(result, ignore_errors=True)

    def test_missing_folder_raises(self):
        with tempfile.TemporaryDirectory() as clone_dir:
            with pytest.raises(FileNotFoundError, match="nonexistent"):
                extract_folder(clone_dir, "nonexistent")


class TestGroupGitOrders:
    def test_groups_same_repo(self):
        job = _make_job(orders=[
            _make_order(order_name="a", git_folder="vpc"),
            _make_order(order_name="b", git_folder="rds"),
        ])
        git_groups, s3_indices = group_git_orders(job.orders, job)
        assert len(git_groups) == 1
        assert len(s3_indices) == 0
        key = ("org/repo", None)
        assert len(git_groups[key]) == 2

    def test_different_repos_separate_groups(self):
        job = _make_job(orders=[
            _make_order(order_name="a", git_repo="org/repo-a"),
            _make_order(order_name="b", git_repo="org/repo-b"),
        ])
        git_groups, s3_indices = group_git_orders(job.orders, job)
        assert len(git_groups) == 2

    def test_same_repo_different_commits_separate(self):
        job = _make_job(orders=[
            _make_order(order_name="a", commit_hash="abc123"),
            _make_order(order_name="b", commit_hash="def456"),
        ])
        git_groups, s3_indices = group_git_orders(job.orders, job)
        assert len(git_groups) == 2

    def test_job_level_commit_hash_groups_orders(self):
        job = _make_job(
            commit_hash="abc123",
            orders=[
                _make_order(order_name="a", git_folder="vpc"),
                _make_order(order_name="b", git_folder="rds"),
            ],
        )
        git_groups, s3_indices = group_git_orders(job.orders, job)
        assert len(git_groups) == 1
        key = ("org/repo", "abc123")
        assert len(git_groups[key]) == 2

    def test_s3_orders_separated(self):
        job = _make_job(orders=[
            _make_order(order_name="git-order", git_folder="vpc"),
            _make_order(order_name="s3-order", s3_location="s3://bucket/code.zip"),
        ])
        git_groups, s3_indices = group_git_orders(job.orders, job)
        assert len(git_groups) == 1
        assert s3_indices == [1]


class TestRepackageOrders:
    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run-1/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY-CONTENT", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.init_job.repackage.clone_repo")
    @patch("src.init_job.repackage.fetch_ssm_values")
    @patch("src.init_job.repackage.fetch_secret_values")
    @patch("src.init_job.repackage.s3_ops.generate_callback_presigned_url")
    @patch("src.init_job.repackage.OrderBundler")
    def test_repackage_produces_correct_structure(
        self, MockBundler, mock_presign, mock_secrets,
        mock_ssm, mock_clone, mock_resolve_creds,
        mock_gen_key, mock_store_ssm,
    ):
        with tempfile.TemporaryDirectory() as clone_dir:
            mock_clone.return_value = clone_dir
            mock_ssm.return_value = {"DB_PASS": "secret"}
            mock_secrets.return_value = {}
            mock_presign.return_value = "https://presigned.url"

            bundler_instance = MagicMock()
            MockBundler.return_value = bundler_instance

            with open(os.path.join(clone_dir, "main.tf"), "w") as f:
                f.write("resource {}")

            job = _make_job(orders=[
                _make_order(order_name="deploy-vpc", ssm_paths=["/db/pass"]),
            ])

            results = repackage_orders(
                job=job,
                run_id="run-1",
                trace_id="abc123",
                flow_id="user:abc123-exec",
                internal_bucket="test-bucket",
            )

            assert len(results) == 1
            r = results[0]
            assert r["order_num"] == "0001"
            assert r["order_name"] == "deploy-vpc"
            assert r["callback_url"] == "https://presigned.url"
            assert os.path.exists(r["zip_path"])

            # Single clone call
            mock_clone.assert_called_once()

            # Verify bundler was created with correct args
            MockBundler.assert_called_once()
            call_kwargs = MockBundler.call_args[1]
            assert call_kwargs["run_id"] == "run-1"
            assert call_kwargs["trace_id"] == "abc123"
            assert call_kwargs["ssm_values"] == {"DB_PASS": "secret"}

    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run-1/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY-CONTENT", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.init_job.repackage.fetch_code_s3")
    @patch("src.init_job.repackage.fetch_ssm_values")
    @patch("src.init_job.repackage.fetch_secret_values")
    @patch("src.init_job.repackage.s3_ops.generate_callback_presigned_url")
    @patch("src.init_job.repackage.OrderBundler")
    def test_s3_code_source(
        self, MockBundler, mock_presign, mock_secrets,
        mock_ssm, mock_s3, mock_resolve_creds,
        mock_gen_key, mock_store_ssm,
    ):
        with tempfile.TemporaryDirectory() as code_dir:
            mock_s3.return_value = code_dir
            mock_ssm.return_value = {}
            mock_secrets.return_value = {}
            mock_presign.return_value = "https://presigned.url"
            MockBundler.return_value = MagicMock()

            with open(os.path.join(code_dir, "script.sh"), "w") as f:
                f.write("#!/bin/bash")

            job = _make_job(orders=[
                _make_order(s3_location="s3://bucket/code.zip"),
            ])

            results = repackage_orders(
                job=job,
                run_id="run-1",
                trace_id="abc",
                flow_id="user:abc-exec",
                internal_bucket="test-bucket",
            )

            assert len(results) == 1
            mock_s3.assert_called_once_with("s3://bucket/code.zip")

    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run-1/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY-CONTENT", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.init_job.repackage.clone_repo")
    @patch("src.init_job.repackage.fetch_ssm_values")
    @patch("src.init_job.repackage.fetch_secret_values")
    @patch("src.init_job.repackage.s3_ops.generate_callback_presigned_url")
    @patch("src.init_job.repackage.OrderBundler")
    def test_multiple_orders_same_repo_clones_once(
        self, MockBundler, mock_presign, mock_secrets,
        mock_ssm, mock_clone, mock_resolve_creds,
        mock_gen_key, mock_store_ssm,
    ):
        with tempfile.TemporaryDirectory() as clone_dir:
            mock_clone.return_value = clone_dir
            mock_ssm.return_value = {}
            mock_secrets.return_value = {}
            mock_presign.return_value = "https://presigned.url"
            MockBundler.return_value = MagicMock()

            # Create folder structure in clone
            os.makedirs(os.path.join(clone_dir, "vpc"), exist_ok=True)
            os.makedirs(os.path.join(clone_dir, "rds"), exist_ok=True)
            with open(os.path.join(clone_dir, "vpc", "main.tf"), "w") as f:
                f.write("vpc")
            with open(os.path.join(clone_dir, "rds", "main.tf"), "w") as f:
                f.write("rds")

            job = _make_job(orders=[
                _make_order(order_name="order-a", git_folder="vpc"),
                _make_order(order_name="order-b", git_folder="rds"),
            ])

            results = repackage_orders(
                job=job,
                run_id="run-1",
                trace_id="abc",
                flow_id="user:abc-exec",
                internal_bucket="bucket",
            )

            assert len(results) == 2
            assert results[0]["order_num"] == "0001"
            assert results[1]["order_num"] == "0002"

            # KEY: only one clone despite two orders
            mock_clone.assert_called_once()

    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run-1/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY-CONTENT", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.init_job.repackage.clone_repo")
    @patch("src.init_job.repackage.fetch_ssm_values")
    @patch("src.init_job.repackage.fetch_secret_values")
    @patch("src.init_job.repackage.s3_ops.generate_callback_presigned_url")
    @patch("src.init_job.repackage.OrderBundler")
    def test_different_repos_clone_separately(
        self, MockBundler, mock_presign, mock_secrets,
        mock_ssm, mock_clone, mock_resolve_creds,
        mock_gen_key, mock_store_ssm,
    ):
        created_dirs = []

        def clone_side_effect(repo, token=None, commit_hash=None, ssh_key_path=None):
            d = tempfile.mkdtemp(prefix="aws-exe-sys-test-")
            with open(os.path.join(d, "main.tf"), "w") as f:
                f.write(f"repo={repo}")
            created_dirs.append(d)
            return d

        mock_clone.side_effect = clone_side_effect
        mock_ssm.return_value = {}
        mock_secrets.return_value = {}
        mock_presign.return_value = "https://presigned.url"
        MockBundler.return_value = MagicMock()

        job = _make_job(orders=[
            _make_order(order_name="order-a", git_repo="org/repo-a"),
            _make_order(order_name="order-b", git_repo="org/repo-b"),
        ])

        results = repackage_orders(
            job=job,
            run_id="run-1",
            trace_id="abc",
            flow_id="user:abc-exec",
            internal_bucket="bucket",
        )

        assert len(results) == 2
        # Two different repos => two clones
        assert mock_clone.call_count == 2

        for d in created_dirs:
            shutil.rmtree(d, ignore_errors=True)

    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run-1/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY-CONTENT", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.init_job.repackage.clone_repo")
    @patch("src.init_job.repackage.fetch_ssm_values")
    @patch("src.init_job.repackage.fetch_secret_values")
    @patch("src.init_job.repackage.s3_ops.generate_callback_presigned_url")
    @patch("src.init_job.repackage.OrderBundler")
    def test_same_repo_different_commits_clone_separately(
        self, MockBundler, mock_presign, mock_secrets,
        mock_ssm, mock_clone, mock_resolve_creds,
        mock_gen_key, mock_store_ssm,
    ):
        created_dirs = []

        def clone_side_effect(repo, token=None, commit_hash=None, ssh_key_path=None):
            d = tempfile.mkdtemp(prefix="aws-exe-sys-test-")
            with open(os.path.join(d, "main.tf"), "w") as f:
                f.write(f"commit={commit_hash}")
            created_dirs.append(d)
            return d

        mock_clone.side_effect = clone_side_effect
        mock_ssm.return_value = {}
        mock_secrets.return_value = {}
        mock_presign.return_value = "https://presigned.url"
        MockBundler.return_value = MagicMock()

        job = _make_job(orders=[
            _make_order(order_name="order-a", commit_hash="abc123"),
            _make_order(order_name="order-b", commit_hash="def456"),
        ])

        results = repackage_orders(
            job=job,
            run_id="run-1",
            trace_id="abc",
            flow_id="user:abc-exec",
            internal_bucket="bucket",
        )

        assert len(results) == 2
        # Same repo, different commits => two clones
        assert mock_clone.call_count == 2

        for d in created_dirs:
            shutil.rmtree(d, ignore_errors=True)

    @patch("src.init_job.repackage.store_sops_key_ssm", return_value="/aws-exe-sys/sops-keys/run-1/0001")
    @patch("src.init_job.repackage._generate_age_key", return_value=("age1pubkey", "AGE-SECRET-KEY-CONTENT", "/tmp/mock.key"))
    @patch("src.init_job.repackage.resolve_git_credentials", return_value=("mock-token", None))
    @patch("src.init_job.repackage.clone_repo")
    @patch("src.init_job.repackage.fetch_ssm_values")
    @patch("src.init_job.repackage.fetch_secret_values")
    @patch("src.init_job.repackage.s3_ops.generate_callback_presigned_url")
    @patch("src.init_job.repackage.OrderBundler")
    def test_job_level_commit_hash_groups_orders(
        self, MockBundler, mock_presign, mock_secrets,
        mock_ssm, mock_clone, mock_resolve_creds,
        mock_gen_key, mock_store_ssm,
    ):
        with tempfile.TemporaryDirectory() as clone_dir:
            mock_clone.return_value = clone_dir
            mock_ssm.return_value = {}
            mock_secrets.return_value = {}
            mock_presign.return_value = "https://presigned.url"
            MockBundler.return_value = MagicMock()

            os.makedirs(os.path.join(clone_dir, "vpc"), exist_ok=True)
            os.makedirs(os.path.join(clone_dir, "rds"), exist_ok=True)
            with open(os.path.join(clone_dir, "vpc", "main.tf"), "w") as f:
                f.write("vpc")
            with open(os.path.join(clone_dir, "rds", "main.tf"), "w") as f:
                f.write("rds")

            job = _make_job(
                commit_hash="abc123",
                orders=[
                    _make_order(order_name="order-a", git_folder="vpc"),
                    _make_order(order_name="order-b", git_folder="rds"),
                ],
            )

            results = repackage_orders(
                job=job,
                run_id="run-1",
                trace_id="abc",
                flow_id="user:abc-exec",
                internal_bucket="bucket",
            )

            assert len(results) == 2
            # Same repo + same job-level commit => single clone
            mock_clone.assert_called_once()
            call_kwargs = mock_clone.call_args
            assert call_kwargs[1]["commit_hash"] == "abc123"
