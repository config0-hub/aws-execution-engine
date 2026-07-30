"""Unit tests for aws_exe_sys/init_job/dispatcher.py."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from aws_exe_sys.common.payload import SimplePayload
from aws_exe_sys.init_job.dispatcher import (
    _payload_to_dict,
    dispatch,
    dispatch_to_codebuild,
    dispatch_to_lambda,
)


def _b64_cmds(cmds: list[str]) -> str:
    return base64.b64encode(json.dumps(cmds).encode()).decode()


def _valid_payload(**overrides) -> SimplePayload:
    defaults = {
        "trigger_id": "trg-001",
        "s3_package_uri": "s3://my-bucket/exec/trg-001/exec.zip",
        "sops_type": "ssm",
        "sops_path": "/exe-sys/sops-keys/run1/001",
        "commands_b64": _b64_cmds(["echo hello"]),
        "done_endpoint": "s3://done-bucket/trg-001/result.json",
        "execution_target": "lambda",
    }
    defaults.update(overrides)
    return SimplePayload(**defaults)


class TestPayloadToDict:
    def test_all_7_fields_present(self):
        payload = _valid_payload()
        d = _payload_to_dict(payload)
        assert set(d.keys()) == {
            "trigger_id",
            "s3_package_uri",
            "sops_type",
            "sops_path",
            "commands_b64",
            "done_endpoint",
            "execution_target",
        }
        assert d["trigger_id"] == "trg-001"
        assert d["sops_type"] == "ssm"

    def test_none_values_become_empty_string(self):
        payload = _valid_payload(sops_type=None, sops_path=None)
        d = _payload_to_dict(payload)
        assert d["sops_type"] == ""
        assert d["sops_path"] == ""


class TestDispatchToLambda:
    @patch("aws_exe_sys.init_job.dispatcher.boto3")
    def test_invokes_worker_lambda(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "my-worker-fn")

        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}
        mock_boto3.client.return_value = mock_client

        payload = _valid_payload(execution_target="lambda")
        response = dispatch_to_lambda(payload)

        assert response["StatusCode"] == 202
        mock_boto3.client.assert_called_once_with("lambda")
        mock_client.invoke.assert_called_once()

    @patch("aws_exe_sys.init_job.dispatcher.boto3")
    def test_passes_all_7_fields_in_payload(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "my-worker-fn")

        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}
        mock_boto3.client.return_value = mock_client

        payload = _valid_payload(execution_target="lambda")
        dispatch_to_lambda(payload)

        call_kwargs = mock_client.invoke.call_args[1]
        assert call_kwargs["FunctionName"] == "my-worker-fn"
        assert call_kwargs["InvocationType"] == "Event"

        sent_payload = json.loads(call_kwargs["Payload"].decode())
        assert sent_payload["trigger_id"] == "trg-001"
        assert sent_payload["s3_package_uri"] == "s3://my-bucket/exec/trg-001/exec.zip"
        assert sent_payload["sops_type"] == "ssm"
        assert sent_payload["sops_path"] == "/exe-sys/sops-keys/run1/001"
        assert sent_payload["done_endpoint"] == "s3://done-bucket/trg-001/result.json"
        assert sent_payload["execution_target"] == "lambda"
        assert sent_payload["commands_b64"] == payload.commands_b64
        assert len(sent_payload) == 7


class TestDispatchToCodeBuild:
    @patch("aws_exe_sys.init_job.dispatcher.boto3")
    def test_starts_codebuild_project(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_PROJECT", "my-cb-project")

        mock_client = MagicMock()
        mock_client.start_build.return_value = {"build": {"id": "build-123"}}
        mock_boto3.client.return_value = mock_client

        payload = _valid_payload(execution_target="codebuild")
        response = dispatch_to_codebuild(payload)

        assert "build" in response
        mock_boto3.client.assert_called_once_with("codebuild")
        mock_client.start_build.assert_called_once()

    @patch("aws_exe_sys.init_job.dispatcher.boto3")
    def test_passes_all_7_fields_as_env_vars(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_PROJECT", "my-cb-project")

        mock_client = MagicMock()
        mock_client.start_build.return_value = {"build": {"id": "build-123"}}
        mock_boto3.client.return_value = mock_client

        payload = _valid_payload(execution_target="codebuild")
        dispatch_to_codebuild(payload)

        call_kwargs = mock_client.start_build.call_args[1]
        assert call_kwargs["projectName"] == "my-cb-project"

        env_vars = call_kwargs["environmentVariablesOverride"]
        env_names = {e["name"] for e in env_vars}
        expected = {
            "TRIGGER_ID",
            "S3_PACKAGE_URI",
            "SOPS_TYPE",
            "SOPS_PATH",
            "COMMANDS_B64",
            "DONE_ENDPOINT",
            "EXECUTION_TARGET",
        }
        assert env_names == expected

        # Verify values
        env_map = {e["name"]: e["value"] for e in env_vars}
        assert env_map["TRIGGER_ID"] == "trg-001"
        assert env_map["S3_PACKAGE_URI"] == "s3://my-bucket/exec/trg-001/exec.zip"
        assert all(e["type"] == "PLAINTEXT" for e in env_vars)


class TestDispatchRouting:
    @patch("aws_exe_sys.init_job.dispatcher.boto3")
    def test_routes_to_lambda(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "my-worker-fn")
        mock_client = MagicMock()
        mock_client.invoke.return_value = {"StatusCode": 202}
        mock_boto3.client.return_value = mock_client

        payload = _valid_payload(execution_target="lambda")
        result = dispatch(payload)
        assert result["StatusCode"] == 202
        mock_client.invoke.assert_called_once()

    @patch("aws_exe_sys.init_job.dispatcher.boto3")
    def test_routes_to_codebuild(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_PROJECT", "my-cb-project")
        mock_client = MagicMock()
        mock_client.start_build.return_value = {"build": {"id": "build-1"}}
        mock_boto3.client.return_value = mock_client

        payload = _valid_payload(execution_target="codebuild")
        result = dispatch(payload)
        assert "build" in result
        mock_client.start_build.assert_called_once()

    def test_unknown_target_raises(self):
        payload = _valid_payload(execution_target="ecs")
        with pytest.raises(ValueError, match="Unknown execution_target"):
            dispatch(payload)
