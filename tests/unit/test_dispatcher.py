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
        "timeout_seconds": 3600,
    }
    defaults.update(overrides)
    return SimplePayload(**defaults)


class TestPayloadToDict:
    def test_all_11_fields_present(self):
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
            "timeout_seconds",
            "callback_url",
            "callback_token",
            "execution_mode",
        }
        assert d["trigger_id"] == "trg-001"
        assert d["timeout_seconds"] == "3600"
        assert d["sops_type"] == "ssm"

    def test_none_values_become_empty_string(self):
        payload = _valid_payload(sops_type=None, sops_path=None)
        d = _payload_to_dict(payload)
        assert d["sops_type"] == ""
        assert d["sops_path"] == ""

    def test_absent_callback_fields_become_empty_string(self):
        payload = _valid_payload()
        d = _payload_to_dict(payload)
        assert d["callback_url"] == ""
        assert d["callback_token"] == ""

    def test_absent_execution_mode_becomes_empty_string(self):
        payload = _valid_payload()
        d = _payload_to_dict(payload)
        assert d["execution_mode"] == ""

    def test_execution_mode_direct_rides_as_plain_string(self):
        payload = _valid_payload(execution_target="codebuild", execution_mode="direct")
        d = _payload_to_dict(payload)
        assert d["execution_mode"] == "direct"

    def test_callback_fields_present(self):
        payload = _valid_payload(
            callback_url="https://caller.example.com/callback",
            callback_token="tok-abc",
        )
        d = _payload_to_dict(payload)
        assert d["callback_url"] == "https://caller.example.com/callback"
        assert d["callback_token"] == "tok-abc"


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
    def test_passes_all_11_fields_in_payload(self, mock_boto3, monkeypatch):
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
        assert sent_payload["timeout_seconds"] == "3600"
        assert sent_payload["callback_url"] == ""
        assert sent_payload["callback_token"] == ""
        assert sent_payload["execution_mode"] == ""
        assert len(sent_payload) == 11


class TestDispatchToCodeBuild:
    @patch("aws_exe_sys.init_job.dispatcher.uuid.uuid4")
    @patch("aws_exe_sys.init_job.dispatcher.boto3")
    def test_starts_standard_workflow(self, mock_boto3, mock_uuid4, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123:stateMachine:xe")
        mock_uuid4.return_value.hex = "unique123"

        mock_client = MagicMock()
        mock_client.start_execution.return_value = {"executionArn": "arn:aws:states:us-east-1:123:execution:xe:run"}
        mock_boto3.client.return_value = mock_client

        payload = _valid_payload(execution_target="codebuild")
        response = dispatch_to_codebuild(payload)

        assert "executionArn" in response
        mock_boto3.client.assert_called_once_with("stepfunctions")
        mock_client.start_execution.assert_called_once()
        mock_client.start_build.assert_not_called()

    @patch("aws_exe_sys.init_job.dispatcher.uuid.uuid4")
    @patch("aws_exe_sys.init_job.dispatcher.boto3")
    def test_passes_11_plain_string_fields_plus_derived_timeouts(self, mock_boto3, mock_uuid4, monkeypatch):
        state_machine_arn = "arn:aws:states:us-east-1:123:stateMachine:xe"
        monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN", state_machine_arn)
        mock_uuid4.return_value.hex = "unique123"

        mock_client = MagicMock()
        mock_client.start_execution.return_value = {"executionArn": "execution-arn"}
        mock_boto3.client.return_value = mock_client

        payload = _valid_payload(execution_target="codebuild", sops_type=None, sops_path=None)
        dispatch_to_codebuild(payload)

        call_kwargs = mock_client.start_execution.call_args.kwargs
        assert call_kwargs["stateMachineArn"] == state_machine_arn
        assert call_kwargs["name"] == "aws-exe-unique123"
        sent_payload = json.loads(call_kwargs["input"])
        assert set(sent_payload) == {
            "trigger_id",
            "s3_package_uri",
            "sops_type",
            "sops_path",
            "commands_b64",
            "done_endpoint",
            "execution_target",
            "timeout_seconds",
            "callback_url",
            "callback_token",
            "execution_mode",
            "build_timeout_minutes",
            "sfn_timeout_seconds",
        }
        payload_fields = {k: v for k, v in sent_payload.items()
                          if k not in ("build_timeout_minutes", "sfn_timeout_seconds")}
        assert all(isinstance(value, str) for value in payload_fields.values())
        assert sent_payload["sops_type"] == ""
        assert sent_payload["sops_path"] == ""
        # Derived numeric workflow inputs follow timeout_seconds:
        # ceil(3600/60) + 3 margin minutes; 3600 + 300 queued + 300 margin.
        assert sent_payload["build_timeout_minutes"] == 63
        assert sent_payload["sfn_timeout_seconds"] == 4200

    @patch("aws_exe_sys.init_job.dispatcher.boto3")
    def test_missing_state_machine_configuration_fails_loudly(self, mock_boto3, monkeypatch):
        monkeypatch.delenv("AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN", raising=False)

        with pytest.raises(KeyError, match="AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN"):
            dispatch_to_codebuild(_valid_payload(execution_target="codebuild"))

        mock_boto3.client.assert_not_called()


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
    def test_routes_to_codebuild_workflow(self, mock_boto3, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN", "arn:aws:states:us-east-1:123:stateMachine:xe")
        mock_client = MagicMock()
        mock_client.start_execution.return_value = {"executionArn": "execution-arn"}
        mock_boto3.client.return_value = mock_client

        payload = _valid_payload(execution_target="codebuild")
        result = dispatch(payload)
        assert "executionArn" in result
        mock_client.start_execution.assert_called_once()
        mock_client.start_build.assert_not_called()

    def test_unknown_target_raises(self):
        payload = _valid_payload(execution_target="ecs")
        with pytest.raises(ValueError, match="Unknown execution_target"):
            dispatch(payload)
