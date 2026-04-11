"""Tests for aws_exe_sys.common.code_source — fetch_ssm_values base64 JSON dict contract."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from aws_exe_sys.common.code_source import fetch_secret_values, fetch_ssm_values


def _encode_dict(d: dict) -> str:
    """Helper: encode a dict as base64 JSON string (simulates SSM stored value)."""
    return base64.b64encode(json.dumps(d).encode()).decode()


class TestFetchSsmValues:
    """Tests for fetch_ssm_values with base64-encoded JSON dict values."""

    def test_empty_paths_returns_empty_dict(self):
        result = fetch_ssm_values([])
        assert result == {}

    @patch("aws_exe_sys.common.credentials.aws_ssm.boto3")
    def test_single_path_decodes_base64_json_dict(self, mock_boto3):
        creds = {
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        }
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_parameter.return_value = {
            "Parameter": {"Value": _encode_dict(creds)}
        }

        result = fetch_ssm_values(["/exec-engine-test/aws-creds"], region="us-east-1")

        assert result == creds
        mock_boto3.client.assert_called_once_with("ssm", region_name="us-east-1")
        mock_client.get_parameter.assert_called_once_with(
            Name="/exec-engine-test/aws-creds", WithDecryption=True
        )

    @patch("aws_exe_sys.common.credentials.aws_ssm.boto3")
    def test_multiple_paths_merge_decoded_dicts(self, mock_boto3):
        creds_1 = {
            "AWS_ACCESS_KEY_ID": "AKIAIOSFODNN7EXAMPLE",
            "AWS_SECRET_ACCESS_KEY": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
            "AWS_DEFAULT_REGION": "us-east-1",
        }
        creds_2 = {
            "GITHUB_TOKEN": "ghp_xxxxxxxxxxxxxxxxxxxx",
        }
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_parameter.side_effect = [
            {"Parameter": {"Value": _encode_dict(creds_1)}},
            {"Parameter": {"Value": _encode_dict(creds_2)}},
        ]

        result = fetch_ssm_values(
            ["/creds/aws", "/creds/github"], region="us-east-1"
        )

        assert result == {**creds_1, **creds_2}
        assert mock_client.get_parameter.call_count == 2

    @patch("aws_exe_sys.common.credentials.aws_ssm.boto3")
    def test_later_path_overrides_earlier_keys(self, mock_boto3):
        """When two SSM params contain the same key, the later one wins."""
        first = {"KEY": "first_value"}
        second = {"KEY": "second_value"}
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_parameter.side_effect = [
            {"Parameter": {"Value": _encode_dict(first)}},
            {"Parameter": {"Value": _encode_dict(second)}},
        ]

        result = fetch_ssm_values(["/a", "/b"])

        assert result["KEY"] == "second_value"

    @patch("aws_exe_sys.common.credentials.aws_ssm.boto3")
    def test_region_none_passes_through(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_parameter.return_value = {
            "Parameter": {"Value": _encode_dict({"X": "1"})}
        }

        fetch_ssm_values(["/path"])

        mock_boto3.client.assert_called_once_with("ssm", region_name=None)

    @patch("aws_exe_sys.common.credentials.aws_ssm.boto3")
    def test_raises_helpful_error_on_plain_string(self, mock_boto3):
        """A non-base64 value in SSM must raise ValueError naming the path.

        Storing a plain token in SSM (e.g. "ghp_xxx") is a common operator
        mistake. Without the guard, base64.b64decode raises binascii.Error,
        which gives no hint that the value was supposed to be a base64 JSON
        dict. We raise a targeted ValueError instead.
        """
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_parameter.return_value = {
            "Parameter": {"Value": "ghp_plain_token_not_base64_!!!"}
        }

        with pytest.raises(ValueError) as exc:
            fetch_ssm_values(["/creds/bad"], region="us-east-1")

        msg = str(exc.value)
        assert "/creds/bad" in msg
        assert "base64" in msg.lower()


class TestFetchSecretValues:
    """Tests for fetch_secret_values — JSON dict expansion and plain-string fallback."""

    @patch("aws_exe_sys.common.credentials.aws_secretsmanager.boto3")
    def test_json_dict_expands_to_multiple_keys(self, mock_boto3):
        """If the SecretString parses to a dict, each key becomes an env var.

        This matches the Secrets Manager convention of storing many fields
        (e.g. RDS credentials) in a single secret as JSON. Previously the
        whole blob was assigned to a single key named after the path.
        """
        secret_payload = json.dumps({
            "DB_HOST": "db.example.internal",
            "DB_USER": "config0",
            "DB_PASSWORD": "s3cret!",
        })
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {"SecretString": secret_payload}

        result = fetch_secret_values(["prod/rds/main"], region="us-east-1")

        assert result == {
            "DB_HOST": "db.example.internal",
            "DB_USER": "config0",
            "DB_PASSWORD": "s3cret!",
        }

    @patch("aws_exe_sys.common.credentials.aws_secretsmanager.boto3")
    def test_plain_string_uses_path_key(self, mock_boto3):
        """A non-JSON SecretString preserves the legacy behavior: single key from path."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {
            "SecretString": "ghp_literal_token_value"
        }

        result = fetch_secret_values(["prod/github-token"])

        assert result == {"GITHUB_TOKEN": "ghp_literal_token_value"}

    @patch("aws_exe_sys.common.credentials.aws_secretsmanager.boto3")
    def test_json_list_uses_path_key(self, mock_boto3):
        """A JSON value that is not a dict (list, scalar) falls back to path-key."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.return_value = {
            "SecretString": '["a", "b"]'
        }

        result = fetch_secret_values(["prod/things-list"])

        # Not a dict — treated as raw string and assigned to path-derived key.
        assert result == {"THINGS_LIST": '["a", "b"]'}

    @patch("aws_exe_sys.common.credentials.aws_secretsmanager.boto3")
    def test_multiple_secrets_merge(self, mock_boto3):
        """Two secrets with dict payloads merge into a single env dict."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_secret_value.side_effect = [
            {"SecretString": json.dumps({"DB_HOST": "h1"})},
            {"SecretString": json.dumps({"API_KEY": "k1"})},
        ]

        result = fetch_secret_values(["prod/db", "prod/api"])

        assert result == {"DB_HOST": "h1", "API_KEY": "k1"}
