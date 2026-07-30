"""Unit tests for aws_exe_sys/common/sops.py — engine-side SOPS operations."""

import json
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError
import pytest

from aws_exe_sys.common import sops
from aws_exe_sys.common.sops import (
    SopsKeyExpired,
    decrypt_env,
    decrypt_with_kms,
    delete_sops_key_ssm,
    fetch_sops_key_ssm,
    handle_sops,
)


class TestRunCmd:
    @patch("subprocess.run")
    def test_success(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=0, stdout="output", stderr="")
        result = sops._run_cmd(["echo", "hello"])
        assert result == "output"

    @patch("subprocess.run")
    def test_failure_raises(self, mock_subprocess):
        mock_subprocess.return_value = MagicMock(returncode=1, stdout="", stderr="error message")
        with pytest.raises(RuntimeError, match="Command failed"):
            sops._run_cmd(["bad", "cmd"])


class TestFetchSopsKeySsm:
    @patch("aws_exe_sys.common.sops.boto3.client")
    def test_success_returns_value(self, mock_client_factory):
        mock_ssm = MagicMock()
        mock_client_factory.return_value = mock_ssm
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "AGE-SECRET-KEY-1ABC"}}

        result = fetch_sops_key_ssm("/exe-sys/sops-keys/run-1/000")

        assert result == "AGE-SECRET-KEY-1ABC"
        mock_ssm.get_parameter.assert_called_once_with(Name="/exe-sys/sops-keys/run-1/000", WithDecryption=True)

    @patch("aws_exe_sys.common.sops.boto3.client")
    def test_raises_domain_error_on_missing(self, mock_client_factory):
        mock_ssm = MagicMock()
        mock_client_factory.return_value = mock_ssm

        class ParameterNotFound(ClientError):
            pass

        mock_ssm.exceptions.ParameterNotFound = ParameterNotFound
        mock_ssm.get_parameter.side_effect = ParameterNotFound(
            {"Error": {"Code": "ParameterNotFound", "Message": "Parameter not found."}},
            "GetParameter",
        )

        with pytest.raises(SopsKeyExpired) as exc:
            fetch_sops_key_ssm("/exe-sys/sops-keys/run-expired/000")

        assert "/exe-sys/sops-keys/run-expired/000" in str(exc.value)

    @patch("aws_exe_sys.common.sops.boto3.client")
    def test_raises_domain_error_on_access_denied(self, mock_client_factory):
        mock_ssm = MagicMock()
        mock_client_factory.return_value = mock_ssm

        class ParameterNotFound(ClientError):
            pass

        mock_ssm.exceptions.ParameterNotFound = ParameterNotFound
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}},
            "GetParameter",
        )

        with pytest.raises(SopsKeyExpired):
            fetch_sops_key_ssm("/exe-sys/sops-keys/run-1/000")

    @patch("aws_exe_sys.common.sops.boto3.client")
    def test_reraises_unexpected_client_error(self, mock_client_factory):
        mock_ssm = MagicMock()
        mock_client_factory.return_value = mock_ssm

        class ParameterNotFound(ClientError):
            pass

        mock_ssm.exceptions.ParameterNotFound = ParameterNotFound
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "Something broke"}},
            "GetParameter",
        )

        with pytest.raises(ClientError, match="InternalError"):
            fetch_sops_key_ssm("/exe-sys/sops-keys/run-1/000")


class TestDeleteSopsKeySsm:
    @patch("aws_exe_sys.common.sops.boto3.client")
    def test_deletes_parameter(self, mock_client_factory):
        mock_ssm = MagicMock()
        mock_client_factory.return_value = mock_ssm

        delete_sops_key_ssm("/exe-sys/sops-keys/run-1/000")

        mock_ssm.delete_parameter.assert_called_once_with(Name="/exe-sys/sops-keys/run-1/000")

    @patch("aws_exe_sys.common.sops.boto3.client")
    def test_ignores_already_deleted(self, mock_client_factory):
        mock_ssm = MagicMock()
        mock_client_factory.return_value = mock_ssm

        class ParameterNotFound(ClientError):
            pass

        mock_ssm.exceptions.ParameterNotFound = ParameterNotFound
        mock_ssm.delete_parameter.side_effect = ParameterNotFound(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "DeleteParameter",
        )

        # Should not raise
        delete_sops_key_ssm("/exe-sys/sops-keys/run-1/000")


class TestDecryptEnv:
    @patch("aws_exe_sys.common.sops._run_cmd")
    def test_decrypt_with_key_string(self, mock_run_cmd):
        mock_run_cmd.return_value = json.dumps({"KEY1": "val1", "KEY2": "val2"})

        result = decrypt_env("/tmp/encrypted.json", "AGE-SECRET-KEY-1ABC")

        assert result == {"KEY1": "val1", "KEY2": "val2"}
        call_args = mock_run_cmd.call_args[0][0]
        assert "--decrypt" in call_args

    @patch("aws_exe_sys.common.sops._run_cmd")
    @patch("os.path.isfile", return_value=True)
    def test_decrypt_with_key_file(self, mock_isfile, mock_run_cmd):
        mock_run_cmd.return_value = json.dumps({"KEY": "val"})

        result = decrypt_env("/tmp/encrypted.json", "/tmp/key.file")

        assert result == {"KEY": "val"}
        env_arg = mock_run_cmd.call_args[1].get("env", {})
        assert "SOPS_AGE_KEY_FILE" in env_arg


class TestDecryptWithKms:
    @patch("aws_exe_sys.common.sops._run_cmd")
    def test_decrypt_returns_env_vars(self, mock_run_cmd):
        mock_run_cmd.return_value = json.dumps({"AWS_ACCESS_KEY_ID": "AKIA...", "DB_PASS": "secret"})

        result = decrypt_with_kms("/tmp/secrets.enc.json")

        assert result == {"AWS_ACCESS_KEY_ID": "AKIA...", "DB_PASS": "secret"}
        call_args = mock_run_cmd.call_args[0][0]
        assert call_args == [
            "sops",
            "--decrypt",
            "--input-type",
            "json",
            "--output-type",
            "json",
            "/tmp/secrets.enc.json",
        ]
        # No env override should be passed (KMS ARN is in the file)
        if len(mock_run_cmd.call_args[0]) > 1:
            assert mock_run_cmd.call_args[0][1] is None
        else:
            assert mock_run_cmd.call_args[1].get("env") is None

    @patch("aws_exe_sys.common.sops._run_cmd")
    def test_decrypt_raises_on_failure(self, mock_run_cmd):
        mock_run_cmd.side_effect = RuntimeError("Command failed: sops --decrypt\nKMS error")

        with pytest.raises(RuntimeError, match="KMS error"):
            decrypt_with_kms("/tmp/secrets.enc.json")


class TestHandleSops:
    """Tests for the top-level handle_sops dispatcher."""

    def test_none_sops_type_returns_empty_dict(self):
        result = handle_sops("/tmp/workdir", sops_type=None)
        assert result == {}

    def test_omitted_sops_type_returns_empty_dict(self):
        result = handle_sops("/tmp/workdir")
        assert result == {}

    @patch("aws_exe_sys.common.sops.delete_sops_key_ssm")
    @patch("aws_exe_sys.common.sops.decrypt_env")
    @patch("aws_exe_sys.common.sops.fetch_sops_key_ssm")
    def test_ssm_path_fetch_decrypt_delete(self, mock_fetch, mock_decrypt, mock_delete):
        mock_fetch.return_value = "AGE-SECRET-KEY-1ABC"
        mock_decrypt.return_value = {"DB_PASS": "secret", "API_KEY": "abc123"}

        result = handle_sops(
            "/tmp/workdir",
            sops_type="ssm",
            sops_path="/exe-sys/sops-keys/run-1/000",
        )

        assert result == {"DB_PASS": "secret", "API_KEY": "abc123"}
        mock_fetch.assert_called_once_with("/exe-sys/sops-keys/run-1/000")
        mock_decrypt.assert_called_once_with("/tmp/workdir/secrets.enc.json", "AGE-SECRET-KEY-1ABC")
        mock_delete.assert_called_once_with("/exe-sys/sops-keys/run-1/000")

    @patch("aws_exe_sys.common.sops.fetch_sops_key_ssm")
    def test_ssm_path_raises_sops_key_expired(self, mock_fetch):
        mock_fetch.side_effect = SopsKeyExpired("Key expired")

        with pytest.raises(SopsKeyExpired, match="Key expired"):
            handle_sops(
                "/tmp/workdir",
                sops_type="ssm",
                sops_path="/exe-sys/sops-keys/run-expired/000",
            )

    @patch("aws_exe_sys.common.sops.delete_sops_key_ssm")
    @patch("aws_exe_sys.common.sops.decrypt_env")
    @patch("aws_exe_sys.common.sops.fetch_sops_key_ssm")
    def test_ssm_deletion_failure_is_raised(self, mock_fetch, mock_decrypt, mock_delete):
        mock_fetch.return_value = "AGE-SECRET-KEY-1ABC"
        mock_decrypt.return_value = {"KEY": "value"}
        mock_delete.side_effect = ClientError(
            {"Error": {"Code": "InternalError", "Message": "SSM down"}},
            "DeleteParameter",
        )

        with pytest.raises(ClientError):
            handle_sops(
                "/tmp/workdir",
                sops_type="ssm",
                sops_path="/exe-sys/sops-keys/run-1/000",
            )

        mock_delete.assert_called_once()

    @patch("aws_exe_sys.common.sops.decrypt_with_kms")
    def test_kms_path(self, mock_decrypt_kms):
        mock_decrypt_kms.return_value = {"AWS_ACCESS_KEY_ID": "AKIA...", "SECRET": "xyz"}

        result = handle_sops("/tmp/workdir", sops_type="kms")

        assert result == {"AWS_ACCESS_KEY_ID": "AKIA...", "SECRET": "xyz"}
        mock_decrypt_kms.assert_called_once_with("/tmp/workdir/secrets.enc.json")

    def test_unknown_sops_type_raises(self):
        with pytest.raises(ValueError, match="Unknown sops_type"):
            handle_sops("/tmp/workdir", sops_type="pgp")
