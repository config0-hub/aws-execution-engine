"""Unit tests for aws_exe_sys/init_job/handler.py (rewritten for SimplePayload)."""

import base64
import json
from unittest.mock import patch

from aws_exe_sys.init_job.handler import handler


def _b64_cmds(cmds: list[str]) -> str:
    return base64.b64encode(json.dumps(cmds).encode()).decode()


def _valid_payload_dict(**overrides) -> dict:
    defaults = {
        "trigger_id": "trg-001",
        "s3_package_uri": "s3://my-bucket/exec/trg-001/exec.zip",
        "sops_type": None,
        "sops_path": None,
        "commands_b64": _b64_cmds(["echo hello"]),
        "done_endpoint": "s3://done-bucket/trg-001/result.json",
        "execution_target": "lambda",
        "timeout_seconds": 3600,
    }
    defaults.update(overrides)
    return defaults


# -- Direct invoke tests ---------------------------------------------------


class TestHandlerDirectInvoke:
    @patch("aws_exe_sys.init_job.handler.dispatch")
    @patch("aws_exe_sys.init_job.handler.validate_payload_resources", return_value=[])
    def test_valid_payload_returns_ok(self, _mock_validate, _mock_dispatch):
        event = _valid_payload_dict()
        resp = handler(event)
        assert resp["status"] == "ok"
        assert resp["trigger_id"] == "trg-001"
        assert "statusCode" not in resp

    @patch("aws_exe_sys.init_job.handler.dispatch")
    @patch("aws_exe_sys.init_job.handler.validate_payload_resources", return_value=[])
    def test_dispatch_called_with_payload(self, _mock_validate, mock_dispatch):
        event = _valid_payload_dict()
        handler(event)
        mock_dispatch.assert_called_once()
        payload = mock_dispatch.call_args[0][0]
        assert payload.trigger_id == "trg-001"
        assert payload.execution_target == "lambda"

    def test_invalid_payload_returns_error(self):
        event = _valid_payload_dict(trigger_id="", execution_target="bad")
        resp = handler(event)
        assert resp["status"] == "error"
        assert "trigger_id" in resp["error"]
        assert "statusCode" not in resp

    @patch("aws_exe_sys.init_job.handler.validate_payload_resources")
    def test_resource_validation_failure_returns_error(self, mock_validate):
        mock_validate.return_value = ["S3 object not found: s3://my-bucket/exec/trg-001/exec.zip"]
        event = _valid_payload_dict()
        resp = handler(event)
        assert resp["status"] == "error"
        assert "S3 object not found" in resp["errors"][0]

    @patch("aws_exe_sys.init_job.handler.dispatch", side_effect=RuntimeError("Lambda invoke failed"))
    @patch("aws_exe_sys.init_job.handler.validate_payload_resources", return_value=[])
    def test_dispatch_error_returns_error(self, _mock_validate, _mock_dispatch):
        event = _valid_payload_dict()
        resp = handler(event)
        assert resp["status"] == "error"
        assert "Lambda invoke failed" in resp["error"]


# -- SNS unwrapping tests --------------------------------------------------


class TestHandlerSNS:
    @patch("aws_exe_sys.init_job.handler.dispatch")
    @patch("aws_exe_sys.init_job.handler.validate_payload_resources", return_value=[])
    def test_sns_event_unwrapped(self, _mock_validate, _mock_dispatch):
        payload = _valid_payload_dict()
        event = {"Records": [{"Sns": {"Message": json.dumps(payload)}}]}
        resp = handler(event)
        assert resp["status"] == "ok"
        assert resp["trigger_id"] == "trg-001"
        assert "statusCode" not in resp


# -- API Gateway tests -----------------------------------------------------


class TestHandlerAPIGateway:
    @patch("aws_exe_sys.init_job.handler.dispatch")
    @patch("aws_exe_sys.init_job.handler.validate_payload_resources", return_value=[])
    def test_apigw_post_returns_200(self, _mock_validate, _mock_dispatch):
        payload = _valid_payload_dict()
        event = {"httpMethod": "POST", "body": json.dumps(payload)}
        resp = handler(event)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "ok"
        assert body["trigger_id"] == "trg-001"

    def test_apigw_get_returns_405(self):
        event = {"httpMethod": "GET", "body": "{}"}
        resp = handler(event)
        assert resp["statusCode"] == 405

    def test_apigw_invalid_payload_returns_400(self):
        payload = _valid_payload_dict(trigger_id="")
        event = {"httpMethod": "POST", "body": json.dumps(payload)}
        resp = handler(event)
        assert resp["statusCode"] == 400
        body = json.loads(resp["body"])
        assert "trigger_id" in body["error"]

    @patch("aws_exe_sys.init_job.handler.validate_payload_resources")
    def test_apigw_resource_validation_failure_returns_400(self, mock_validate):
        mock_validate.return_value = ["SSM parameter not found: /some/path"]
        payload = _valid_payload_dict()
        event = {"httpMethod": "POST", "body": json.dumps(payload)}
        resp = handler(event)
        assert resp["statusCode"] == 400
        body = json.loads(resp["body"])
        assert "SSM parameter not found" in body["errors"][0]

    @patch("aws_exe_sys.init_job.handler.dispatch", side_effect=RuntimeError("crash"))
    @patch("aws_exe_sys.init_job.handler.validate_payload_resources", return_value=[])
    def test_apigw_dispatch_error_returns_500(self, _mock_validate, _mock_dispatch):
        payload = _valid_payload_dict()
        event = {"httpMethod": "POST", "body": json.dumps(payload)}
        resp = handler(event)
        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert "crash" in body["error"]

    @patch("aws_exe_sys.init_job.handler.dispatch")
    @patch("aws_exe_sys.init_job.handler.validate_payload_resources", return_value=[])
    def test_apigw_v2_post_returns_200(self, _mock_validate, _mock_dispatch):
        payload = _valid_payload_dict()
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps(payload),
        }
        resp = handler(event)
        assert resp["statusCode"] == 200
