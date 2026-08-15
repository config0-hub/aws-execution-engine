"""Integration tests for init_job handler — full pipeline with mocked AWS SDK.

Exercises: handler() → normalize_event() → SimplePayload.from_dict() →
validate() → validate_payload_resources() → dispatch().

Only boto3.client calls are mocked; all Python logic runs for real.
"""

import base64
import json
from unittest.mock import MagicMock, patch

from aws_exe_sys.init_job.handler import handler


def _b64_cmds(cmds: list[str]) -> str:
    return base64.b64encode(json.dumps(cmds).encode()).decode()


def _valid_event(**overrides) -> dict:
    """Build a valid direct-invoke event dict with the 8 required fields."""
    defaults = {
        "trigger_id": "trg-int-001",
        "s3_package_uri": "s3://test-bucket/exec/trg-int-001/exec.zip",
        "sops_type": "ssm",
        "sops_path": "/exe-sys/sops-keys/run1/001",
        "commands_b64": _b64_cmds(["echo hello"]),
        "done_endpoint": "s3://done-bucket/trg-int-001/result.json",
        "execution_target": "lambda",
        "timeout_seconds": 3600,
    }
    defaults.update(overrides)
    return defaults


def _mock_boto3_client(service_mocks: dict):
    """Return a side_effect function that dispatches boto3.client() by service name."""

    def _client(service_name, **kwargs):
        if service_name in service_mocks:
            return service_mocks[service_name]
        return MagicMock()

    return _client


class TestInitValidPayloadDispatch:
    """Valid payload → resource validation passes → dispatch succeeds."""

    def test_dispatch_to_lambda(self, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "my-worker-fn")

        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {}

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "age-key"}}

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        with (
            patch("aws_exe_sys.init_job.validate.boto3") as mock_val_boto3,
            patch("aws_exe_sys.init_job.dispatcher.boto3") as mock_disp_boto3,
        ):
            mock_val_boto3.client.side_effect = _mock_boto3_client({"s3": mock_s3, "ssm": mock_ssm})
            mock_disp_boto3.client.return_value = mock_lambda

            result = handler(_valid_event(execution_target="lambda"))

        assert result["status"] == "ok"
        assert result["trigger_id"] == "trg-int-001"
        mock_lambda.invoke.assert_called_once()
        call_kwargs = mock_lambda.invoke.call_args[1]
        assert call_kwargs["FunctionName"] == "my-worker-fn"
        assert call_kwargs["InvocationType"] == "Event"
        sent = json.loads(call_kwargs["Payload"].decode())
        assert sent["trigger_id"] == "trg-int-001"
        assert len(sent) == 10
        assert sent["callback_url"] == ""
        assert sent["callback_token"] == ""

    def test_dispatch_to_codebuild(self, monkeypatch):
        state_machine_arn = "arn:aws:states:us-east-1:123:stateMachine:xe"
        monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN", state_machine_arn)

        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {}

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "age-key"}}

        mock_stepfunctions = MagicMock()
        mock_stepfunctions.start_execution.return_value = {"executionArn": "execution-arn"}

        with (
            patch("aws_exe_sys.init_job.validate.boto3") as mock_val_boto3,
            patch("aws_exe_sys.init_job.dispatcher.boto3") as mock_disp_boto3,
        ):
            mock_val_boto3.client.side_effect = _mock_boto3_client({"s3": mock_s3, "ssm": mock_ssm})
            mock_disp_boto3.client.return_value = mock_stepfunctions

            result = handler(_valid_event(execution_target="codebuild"))

        assert result == {"status": "ok", "trigger_id": "trg-int-001"}
        mock_stepfunctions.start_execution.assert_called_once()
        call_kwargs = mock_stepfunctions.start_execution.call_args.kwargs
        assert call_kwargs["stateMachineArn"] == state_machine_arn
        sent = json.loads(call_kwargs["input"])
        # The SFN input is the 10 SimplePayload fields plus the two derived
        # timeout fields the state machine consumes.
        assert set(sent) == set(_valid_event()) | {
            "callback_url",
            "callback_token",
            "build_timeout_minutes",
            "sfn_timeout_seconds",
        }
        assert all(
            isinstance(sent[field], str) for field in _valid_event()
        )
        assert sent["callback_url"] == ""
        assert sent["callback_token"] == ""
        assert isinstance(sent["build_timeout_minutes"], int)
        assert isinstance(sent["sfn_timeout_seconds"], int)
        mock_stepfunctions.start_build.assert_not_called()


class TestInitInvalidPayload:
    """Invalid payloads → PayloadValidationError → error response."""

    def test_missing_trigger_id(self):
        result = handler(_valid_event(trigger_id=""))
        assert result["status"] == "error"
        assert "trigger_id" in result["error"]

    def test_invalid_s3_uri(self):
        result = handler(_valid_event(s3_package_uri="not-an-s3-uri"))
        assert result["status"] == "error"
        assert "s3_package_uri" in result["error"]

    def test_invalid_sops_type(self):
        result = handler(_valid_event(sops_type="invalid"))
        assert result["status"] == "error"
        assert "sops_type" in result["error"]

    def test_ssm_sops_type_missing_path(self):
        result = handler(_valid_event(sops_type="ssm", sops_path=None))
        assert result["status"] == "error"
        assert "sops_path" in result["error"]

    def test_invalid_commands_b64(self):
        result = handler(_valid_event(commands_b64="not-base64!!!"))
        assert result["status"] == "error"
        assert "commands_b64" in result["error"]

    def test_empty_commands_array(self):
        result = handler(_valid_event(commands_b64=_b64_cmds([])))
        assert result["status"] == "error"
        assert "commands_b64" in result["error"]

    def test_invalid_done_endpoint(self):
        result = handler(_valid_event(done_endpoint="http://not-s3"))
        assert result["status"] == "error"
        assert "done_endpoint" in result["error"]

    def test_invalid_execution_target(self):
        result = handler(_valid_event(execution_target="ecs"))
        assert result["status"] == "error"
        assert "execution_target" in result["error"]

    def test_multiple_validation_errors(self):
        result = handler(_valid_event(trigger_id="", s3_package_uri="bad", execution_target="x"))
        assert result["status"] == "error"
        assert "trigger_id" in result["error"]
        assert "s3_package_uri" in result["error"]


class TestInitResourceValidation:
    """Resource validation failures (S3 object missing, SSM param missing)."""

    def test_s3_object_not_found(self, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "my-worker-fn")

        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.head_object.side_effect = ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")

        with patch("aws_exe_sys.init_job.validate.boto3") as mock_val_boto3:
            mock_val_boto3.client.side_effect = _mock_boto3_client({"s3": mock_s3})

            result = handler(_valid_event())

        assert result["status"] == "error"
        assert "errors" in result
        assert any("S3 object not found" in e for e in result["errors"])

    def test_ssm_param_not_found(self, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "my-worker-fn")

        from botocore.exceptions import ClientError

        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {}

        mock_ssm = MagicMock()
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}}, "GetParameter"
        )

        with patch("aws_exe_sys.init_job.validate.boto3") as mock_val_boto3:
            mock_val_boto3.client.side_effect = _mock_boto3_client({"s3": mock_s3, "ssm": mock_ssm})

            result = handler(_valid_event())

        assert result["status"] == "error"
        assert "errors" in result
        assert any("SSM parameter not found" in e for e in result["errors"])


class TestInitNoSopsValidation:
    """When sops_type is None, SSM validation is skipped."""

    def test_no_sops_skips_ssm_check(self, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "my-worker-fn")

        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {}

        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        with (
            patch("aws_exe_sys.init_job.validate.boto3") as mock_val_boto3,
            patch("aws_exe_sys.init_job.dispatcher.boto3") as mock_disp_boto3,
        ):
            mock_val_boto3.client.side_effect = _mock_boto3_client({"s3": mock_s3})
            mock_disp_boto3.client.return_value = mock_lambda

            result = handler(_valid_event(sops_type=None, sops_path=None))

        assert result["status"] == "ok"
        mock_lambda.invoke.assert_called_once()


class TestInitApiGatewayFormat:
    """API Gateway v1 and v2 event wrapping."""

    def test_apigw_v2_post(self, monkeypatch):
        monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "my-worker-fn")

        mock_s3 = MagicMock()
        mock_s3.head_object.return_value = {}
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {"Parameter": {"Value": "key"}}
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"StatusCode": 202}

        apigw_event = {
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps(_valid_event()),
        }

        with (
            patch("aws_exe_sys.init_job.validate.boto3") as mock_val_boto3,
            patch("aws_exe_sys.init_job.dispatcher.boto3") as mock_disp_boto3,
        ):
            mock_val_boto3.client.side_effect = _mock_boto3_client({"s3": mock_s3, "ssm": mock_ssm})
            mock_disp_boto3.client.return_value = mock_lambda

            result = handler(apigw_event)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "ok"

    def test_apigw_v2_get_rejected(self):
        apigw_event = {
            "requestContext": {"http": {"method": "GET"}},
        }
        result = handler(apigw_event)
        assert result["statusCode"] == 405
