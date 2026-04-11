"""Unit tests for aws_exe_sys/common/sops.py with mocked subprocess calls."""

import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest
from botocore.exceptions import ClientError

from aws_exe_sys.common import sops
from aws_exe_sys.common.sops import SopsKeyExpired, fetch_sops_key_ssm


class TestEncryptEnv:
    @patch("aws_exe_sys.common.sops._run_cmd")
    def test_encrypt_with_provided_key(self, mock_run_cmd):
        mock_run_cmd.return_value = ""

        encrypted_path, key_used = sops.encrypt_env(
            {"KEY1": "val1", "KEY2": "val2"},
            sops_key="age1abc123",
        )
        assert key_used == "age1abc123"
        # Verify sops was called with --encrypt
        call_args = mock_run_cmd.call_args[0][0]
        assert "sops" in call_args
        assert "--encrypt" in call_args
        assert "--age" in call_args

    @patch("aws_exe_sys.common.sops._generate_age_key")
    @patch("aws_exe_sys.common.sops._run_cmd")
    def test_encrypt_auto_gen_key(self, mock_run_cmd, mock_gen_key):
        mock_gen_key.return_value = ("age1publickey", "AGE-SECRET-KEY-CONTENT", "/tmp/test.key")
        mock_run_cmd.return_value = ""

        encrypted_path, key_used = sops.encrypt_env({"KEY": "val"})
        assert key_used == "age1publickey"
        mock_gen_key.assert_called_once()


class TestDecryptEnv:
    @patch("aws_exe_sys.common.sops._run_cmd")
    def test_decrypt_with_key_string(self, mock_run_cmd):
        mock_run_cmd.return_value = json.dumps({"KEY1": "val1", "KEY2": "val2"})

        result = sops.decrypt_env("/tmp/encrypted.json", "AGE-SECRET-KEY-1ABC")
        assert result == {"KEY1": "val1", "KEY2": "val2"}
        call_args = mock_run_cmd.call_args[0][0]
        assert "--decrypt" in call_args

    @patch("aws_exe_sys.common.sops._run_cmd")
    @patch("os.path.isfile", return_value=True)
    def test_decrypt_with_key_file(self, mock_isfile, mock_run_cmd):
        mock_run_cmd.return_value = json.dumps({"KEY": "val"})

        result = sops.decrypt_env("/tmp/encrypted.json", "/tmp/key.file")
        assert result == {"KEY": "val"}
        # Should set SOPS_AGE_KEY_FILE env var
        env_arg = mock_run_cmd.call_args[1].get("env", {})
        assert "SOPS_AGE_KEY_FILE" in env_arg


class TestRepackageOrder:
    @patch("aws_exe_sys.common.sops.encrypt_env")
    def test_repackage_creates_files(self, mock_encrypt):
        with tempfile.TemporaryDirectory() as tmpdir:
            enc_file = os.path.join(tmpdir, "mock_enc.json")
            with open(enc_file, "w") as f:
                f.write("{}")
            mock_encrypt.return_value = (enc_file, "age1key")

            result_dir = sops.repackage_order(
                code_dir=tmpdir,
                env_vars={"APP_ENV": "staging", "DB_PASS": "secret123"},
            )

            assert result_dir == tmpdir

            # Check secrets.enc.json exists
            assert os.path.exists(os.path.join(tmpdir, "secrets.enc.json"))

            # Check env_vars.env has var names
            env_file = os.path.join(tmpdir, "env_vars.env")
            assert os.path.exists(env_file)
            with open(env_file) as f:
                lines = f.read().strip().split("\n")
            assert "APP_ENV" in lines
            assert "DB_PASS" in lines

    @patch("aws_exe_sys.common.sops.encrypt_env")
    def test_repackage_passes_all_env_vars_to_encrypt(self, mock_encrypt):
        with tempfile.TemporaryDirectory() as tmpdir:
            enc_file = os.path.join(tmpdir, "mock_enc.json")
            with open(enc_file, "w") as f:
                f.write("{}")
            mock_encrypt.return_value = (enc_file, "age1key")

            sops.repackage_order(
                code_dir=tmpdir,
                env_vars={"KEY1": "val1", "KEY2": "val2"},
            )

            # Verify encrypt_env received the exact dict
            call_args = mock_encrypt.call_args[0][0]
            assert call_args == {"KEY1": "val1", "KEY2": "val2"}


class TestRunCmd:
    @patch("subprocess.run")
    def test_success(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(
            returncode=0, stdout="output", stderr=""
        )
        result = sops._run_cmd(["echo", "hello"])
        assert result == "output"

    @patch("subprocess.run")
    def test_failure_raises(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(
            returncode=1, stdout="", stderr="error message"
        )
        with pytest.raises(RuntimeError, match="Command failed"):
            sops._run_cmd(["bad", "cmd"])


class TestFetchSopsKeySsm:
    """Tests for fetch_sops_key_ssm — domain exception on expired/missing key."""

    @patch("aws_exe_sys.common.sops.boto3.client")
    def test_success_returns_value(self, mock_client_factory):
        mock_ssm = MagicMock()
        mock_client_factory.return_value = mock_ssm
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "AGE-SECRET-KEY-1ABC"}
        }

        result = fetch_sops_key_ssm("/aws-exe-sys/sops-keys/run-1/000")

        assert result == "AGE-SECRET-KEY-1ABC"
        mock_ssm.get_parameter.assert_called_once_with(
            Name="/aws-exe-sys/sops-keys/run-1/000", WithDecryption=True
        )

    @patch("aws_exe_sys.common.sops.boto3.client")
    def test_raises_domain_error_on_missing(self, mock_client_factory):
        """When the SSM parameter has been expired by the tier policy and
        swept by SSM, get_parameter raises ParameterNotFound. We convert that
        to a SopsKeyExpired domain exception so the worker can respond with
        a fast, specific callback instead of a generic crash.
        """
        mock_ssm = MagicMock()
        mock_client_factory.return_value = mock_ssm

        # Simulate the botocore exception class that boto3 attaches to the
        # client under ssm.exceptions.ParameterNotFound.
        class ParameterNotFound(ClientError):
            pass

        mock_ssm.exceptions.ParameterNotFound = ParameterNotFound
        mock_ssm.get_parameter.side_effect = ParameterNotFound(
            {"Error": {"Code": "ParameterNotFound", "Message": "Parameter not found."}},
            "GetParameter",
        )

        with pytest.raises(SopsKeyExpired) as exc:
            fetch_sops_key_ssm("/aws-exe-sys/sops-keys/run-expired/000")

        assert "/aws-exe-sys/sops-keys/run-expired/000" in str(exc.value)
