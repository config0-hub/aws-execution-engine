"""Unit tests for aws_exe_sys/init_job/handler.py."""

import base64
import json
from unittest.mock import patch, MagicMock

import pytest

from aws_exe_sys.common.models import Job, Order
from aws_exe_sys.common.lambda_handler import normalize_event
from aws_exe_sys.init_job.handler import handler, process_job_and_insert_orders


def _make_job_b64(**kwargs):
    defaults = {
        "git_repo": "org/repo",
        "git_token_location": "aws:::ssm:/token",
        "username": "testuser",
        "orders": [{"cmds": ["echo hi"], "timeout": 300}],
    }
    defaults.update(kwargs)
    return base64.b64encode(json.dumps(defaults).encode()).decode()


# ── normalize_event ──────────────────────────────────────────────

class TestNormalizeEvent:
    def test_direct_invoke_passthrough(self):
        event = {"job_parameters_b64": "abc", "trace_id": "t1"}
        assert normalize_event(event) == event

    def test_sns_unwraps_message(self):
        payload = {"job_parameters_b64": "abc"}
        event = {"Records": [{"Sns": {"Message": json.dumps(payload)}}]}
        assert normalize_event(event) == payload

    def test_sns_dict_message(self):
        payload = {"job_parameters_b64": "abc"}
        event = {"Records": [{"Sns": {"Message": payload}}]}
        assert normalize_event(event) == payload

    def test_apigw_post_unwraps_body(self):
        payload = {"job_parameters_b64": "abc"}
        event = {"httpMethod": "POST", "body": json.dumps(payload)}
        assert normalize_event(event) == payload

    def test_apigw_dict_body(self):
        payload = {"job_parameters_b64": "abc"}
        event = {"httpMethod": "POST", "body": payload}
        assert normalize_event(event) == payload

    def test_apigw_get_rejected(self):
        event = {"httpMethod": "GET", "body": "{}"}
        result = normalize_event(event)
        assert "_apigw_error" in result

    def test_apigw_empty_body(self):
        event = {"httpMethod": "POST", "body": ""}
        assert normalize_event(event) == {}


# ── handler (direct invoke) ──────────────────────────────────────

class TestHandlerDirectInvoke:
    def test_missing_job_parameters_returns_error(self):
        resp = handler({})
        assert resp["status"] == "error"
        assert "Missing" in resp["error"]
        assert "statusCode" not in resp  # not wrapped

    @patch("aws_exe_sys.init_job.handler.process_job_and_insert_orders")
    def test_valid_request_returns_ok(self, mock_process):
        mock_process.return_value = {
            "status": "ok",
            "run_id": "run-1",
            "trace_id": "abc",
            "flow_id": "user:abc-exec",
            "done_endpt": "s3://done/run-1/done",
        }

        event = {"job_parameters_b64": _make_job_b64()}
        resp = handler(event)
        assert resp["status"] == "ok"
        assert resp["run_id"] == "run-1"
        assert "statusCode" not in resp

    @patch("aws_exe_sys.init_job.handler.process_job_and_insert_orders")
    def test_exception_returns_error(self, mock_process):
        mock_process.side_effect = RuntimeError("boom")

        resp = handler({"job_parameters_b64": _make_job_b64()})
        assert resp["status"] == "error"
        assert "boom" in resp["error"]
        assert "statusCode" not in resp


# ── handler (SNS) ────────────────────────────────────────────────

class TestHandlerSNS:
    @patch("aws_exe_sys.init_job.handler.process_job_and_insert_orders")
    def test_sns_event_processed(self, mock_process):
        mock_process.return_value = {"status": "ok", "run_id": "r1"}

        payload = {"job_parameters_b64": _make_job_b64()}
        event = {"Records": [{"Sns": {"Message": json.dumps(payload)}}]}
        resp = handler(event)
        assert resp["status"] == "ok"
        assert "statusCode" not in resp


# ── handler (API Gateway) ────────────────────────────────────────

class TestHandlerAPIGateway:
    @patch("aws_exe_sys.init_job.handler.process_job_and_insert_orders")
    def test_apigw_post_returns_200(self, mock_process):
        mock_process.return_value = {"status": "ok", "run_id": "r1"}

        payload = {"job_parameters_b64": _make_job_b64()}
        event = {"httpMethod": "POST", "body": json.dumps(payload)}
        resp = handler(event)
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["status"] == "ok"

    def test_apigw_get_returns_405(self):
        event = {"httpMethod": "GET", "body": "{}"}
        resp = handler(event)
        assert resp["statusCode"] == 405

    def test_apigw_missing_payload_returns_400(self):
        event = {"httpMethod": "POST", "body": "{}"}
        resp = handler(event)
        assert resp["statusCode"] == 400
        body = json.loads(resp["body"])
        assert "Missing" in body["error"]

    @patch("aws_exe_sys.init_job.handler.process_job_and_insert_orders")
    def test_apigw_error_returns_400(self, mock_process):
        mock_process.return_value = {"status": "error", "errors": ["bad"]}

        payload = {"job_parameters_b64": _make_job_b64()}
        event = {"httpMethod": "POST", "body": json.dumps(payload)}
        resp = handler(event)
        assert resp["statusCode"] == 400

    @patch("aws_exe_sys.init_job.handler.process_job_and_insert_orders")
    def test_apigw_exception_returns_500(self, mock_process):
        mock_process.side_effect = RuntimeError("crash")

        payload = {"job_parameters_b64": _make_job_b64()}
        event = {"httpMethod": "POST", "body": json.dumps(payload)}
        resp = handler(event)
        assert resp["statusCode"] == 500
        body = json.loads(resp["body"])
        assert "crash" in body["error"]


class TestProcessJobAndInsertOrders:
    @patch("aws_exe_sys.init_job.handler.s3_ops.write_init_trigger")
    @patch("aws_exe_sys.init_job.handler.insert_orders")
    @patch("aws_exe_sys.init_job.handler.upload_orders")
    @patch("aws_exe_sys.init_job.handler.repackage_orders")
    @patch("aws_exe_sys.init_job.handler.validate_orders")
    def test_full_flow(
        self, mock_validate, mock_repackage, mock_upload,
        mock_insert, mock_trigger, monkeypatch,
    ):
        monkeypatch.setenv("AWS_EXE_SYS_INTERNAL_BUCKET", "internal")
        monkeypatch.setenv("AWS_EXE_SYS_DONE_BUCKET", "done")

        mock_validate.return_value = []
        mock_repackage.return_value = [
            {"order_num": "0001", "order_name": "test", "zip_path": "/tmp/x.zip",
             "callback_url": "https://cb"},
        ]

        result = process_job_and_insert_orders(_make_job_b64())

        assert result["status"] == "ok"
        assert "run_id" in result
        assert "trace_id" in result
        assert "flow_id" in result

        mock_validate.assert_called_once()
        mock_repackage.assert_called_once()
        mock_upload.assert_called_once()
        mock_insert.assert_called_once()
        mock_trigger.assert_called_once()
