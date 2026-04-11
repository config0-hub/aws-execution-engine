"""Unit tests for aws_exe_sys/common/bundler.py."""

import os
import tempfile
from unittest.mock import patch

import pytest

from aws_exe_sys.common.bundler import OrderBundler


class TestBuildEnv:
    def test_merges_all_sources(self):
        bundler = OrderBundler(
            run_id="run-1",
            order_id="deploy-vpc",
            order_num="001",
            trace_id="abc123",
            flow_id="user:abc123-exec",
            env_vars={"APP_ENV": "staging"},
            ssm_values={"DB_PASS": "secret123"},
            secret_values={"API_KEY": "key456"},
            callback_url="https://callback.example.com",
        )
        env = bundler.build_env()

        # User env vars
        assert env["APP_ENV"] == "staging"
        # SSM values
        assert env["DB_PASS"] == "secret123"
        # Secrets Manager values
        assert env["API_KEY"] == "key456"
        # Callback
        assert env["CALLBACK_URL"] == "https://callback.example.com"
        # Introspection fields
        assert env["TRACE_ID"] == "abc123"
        assert env["RUN_ID"] == "run-1"
        assert env["ORDER_ID"] == "deploy-vpc"
        assert env["ORDER_NUM"] == "001"
        assert env["FLOW_ID"] == "user:abc123-exec"

    def test_ssm_overwrites_env_vars_on_collision(self):
        bundler = OrderBundler(
            env_vars={"DB_PASS": "from_env"},
            ssm_values={"DB_PASS": "from_ssm"},
        )
        env = bundler.build_env()
        assert env["DB_PASS"] == "from_ssm"

    def test_secrets_overwrite_ssm_on_collision(self):
        bundler = OrderBundler(
            ssm_values={"API_KEY": "from_ssm"},
            secret_values={"API_KEY": "from_secrets"},
        )
        env = bundler.build_env()
        assert env["API_KEY"] == "from_secrets"

    def test_callback_url_omitted_when_empty(self):
        bundler = OrderBundler(env_vars={"KEY": "val"})
        env = bundler.build_env()
        assert "CALLBACK_URL" not in env

    def test_cmds_serialized_as_json(self):
        bundler = OrderBundler(cmds=["echo hello", "terraform plan"])
        env = bundler.build_env()
        assert env["CMDS"] == '["echo hello", "terraform plan"]'

    def test_cmds_omitted_when_empty(self):
        bundler = OrderBundler()
        env = bundler.build_env()
        assert "CMDS" not in env

    def test_introspection_fields_default_to_empty(self):
        bundler = OrderBundler()
        env = bundler.build_env()
        assert env["TRACE_ID"] == ""
        assert env["RUN_ID"] == ""
        assert env["ORDER_ID"] == ""
        assert env["ORDER_NUM"] == ""
        assert env["FLOW_ID"] == ""


class TestSecretSources:
    def test_returns_sorted_keys(self):
        bundler = OrderBundler(
            ssm_values={"ZZZ_SSM": "val1", "AAA_SSM": "val2"},
            secret_values={"MMM_SECRET": "val3"},
        )
        sources = bundler.secret_sources()
        assert sources == ["AAA_SSM", "MMM_SECRET", "ZZZ_SSM"]

    def test_empty_when_no_credentials(self):
        bundler = OrderBundler(env_vars={"APP": "val"})
        assert bundler.secret_sources() == []


class TestRepackage:
    @patch("aws_exe_sys.common.bundler.sops.repackage_order")
    def test_calls_sops_with_full_env(self, mock_repackage):
        mock_repackage.return_value = "/tmp/code"

        bundler = OrderBundler(
            run_id="run-1",
            trace_id="abc123",
            env_vars={"APP": "staging"},
            ssm_values={"DB_PASS": "secret"},
            callback_url="https://callback.url",
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_repackage.return_value = tmpdir
            bundler.repackage(tmpdir)

            call_args = mock_repackage.call_args
            env_dict = call_args[0][1]
            assert env_dict["APP"] == "staging"
            assert env_dict["DB_PASS"] == "secret"
            assert env_dict["CALLBACK_URL"] == "https://callback.url"
            assert env_dict["TRACE_ID"] == "abc123"
            assert env_dict["RUN_ID"] == "run-1"

    @patch("aws_exe_sys.common.bundler.sops.repackage_order")
    def test_writes_secrets_src(self, mock_repackage):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_repackage.return_value = tmpdir

            bundler = OrderBundler(
                ssm_values={"DB_PASS": "secret"},
                secret_values={"API_KEY": "key"},
            )
            bundler.repackage(tmpdir)

            secrets_src = os.path.join(tmpdir, "secrets.src")
            assert os.path.exists(secrets_src)
            with open(secrets_src) as f:
                lines = f.read().strip().split("\n")
            assert "API_KEY" in lines
            assert "DB_PASS" in lines

    @patch("aws_exe_sys.common.bundler.sops.repackage_order")
    def test_no_secrets_src_when_no_credentials(self, mock_repackage):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_repackage.return_value = tmpdir

            bundler = OrderBundler(env_vars={"APP": "val"})
            bundler.repackage(tmpdir)

            secrets_src = os.path.join(tmpdir, "secrets.src")
            assert not os.path.exists(secrets_src)

    @patch("aws_exe_sys.common.bundler.sops.repackage_order")
    def test_passes_sops_key(self, mock_repackage):
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_repackage.return_value = tmpdir

            bundler = OrderBundler(env_vars={"K": "V"})
            bundler.repackage(tmpdir, sops_key="age1customkey")

            call_kwargs = mock_repackage.call_args[1]
            assert call_kwargs["sops_key"] == "age1customkey"
