"""Tests for src.common.code_source — fetch_ssm_values base64 JSON dict contract."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from src.common.code_source import fetch_ssm_values


def _encode_dict(d: dict) -> str:
    """Helper: encode a dict as base64 JSON string (simulates SSM stored value)."""
    return base64.b64encode(json.dumps(d).encode()).decode()


class TestFetchSsmValues:
    """Tests for fetch_ssm_values with base64-encoded JSON dict values."""

    def test_empty_paths_returns_empty_dict(self):
        result = fetch_ssm_values([])
        assert result == {}

    @patch("src.common.code_source.boto3")
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

    @patch("src.common.code_source.boto3")
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

    @patch("src.common.code_source.boto3")
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

    @patch("src.common.code_source.boto3")
    def test_region_none_passes_through(self, mock_boto3):
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        mock_client.get_parameter.return_value = {
            "Parameter": {"Value": _encode_dict({"X": "1"})}
        }

        fetch_ssm_values(["/path"])

        mock_boto3.client.assert_called_once_with("ssm", region_name=None)
