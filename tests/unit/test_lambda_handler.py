"""Unit tests for aws_exe_sys/common/lambda_handler.py."""

import json
from unittest.mock import MagicMock

from aws_exe_sys.common.lambda_handler import normalize_event, apigw_response, lambda_handler


# ── normalize_event ─────────────────────────────────────────────────


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

    def test_apigw_v1_post(self):
        payload = {"job_parameters_b64": "abc"}
        event = {"httpMethod": "POST", "body": json.dumps(payload)}
        assert normalize_event(event) == payload

    def test_apigw_v1_get_rejected(self):
        event = {"httpMethod": "GET", "body": "{}"}
        result = normalize_event(event)
        assert "_apigw_error" in result
        assert "GET" in result["_apigw_error"]

    def test_apigw_v1_dict_body(self):
        payload = {"job_parameters_b64": "abc"}
        event = {"httpMethod": "POST", "body": payload}
        assert normalize_event(event) == payload

    def test_apigw_v1_empty_body(self):
        event = {"httpMethod": "POST", "body": ""}
        assert normalize_event(event) == {}

    def test_apigw_v2_post(self):
        payload = {"job_parameters_b64": "abc"}
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "body": json.dumps(payload),
        }
        assert normalize_event(event) == payload

    def test_apigw_v2_get_rejected(self):
        event = {
            "requestContext": {"http": {"method": "GET"}},
            "body": "{}",
        }
        result = normalize_event(event)
        assert "_apigw_error" in result

    def test_apigw_v2_empty_body(self):
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "body": "",
        }
        assert normalize_event(event) == {}


# ── apigw_response ──────────────────────────────────────────────────


class TestApigwResponse:
    def test_wraps_body(self):
        resp = apigw_response(200, {"status": "ok"})
        assert resp["statusCode"] == 200
        assert resp["headers"]["Content-Type"] == "application/json"
        assert json.loads(resp["body"]) == {"status": "ok"}

    def test_error_status(self):
        resp = apigw_response(500, {"status": "error", "error": "boom"})
        assert resp["statusCode"] == 500


# ── lambda_handler ──────────────────────────────────────────────────


class TestLambdaHandler:
    def test_direct_invoke_success(self):
        process_fn = MagicMock(return_value={"status": "ok", "run_id": "r1"})
        event = {"job_parameters_b64": "abc123", "trace_id": "t1"}

        result = lambda_handler(process_fn, event, extra_fields=["trace_id"])
        assert result == {"status": "ok", "run_id": "r1"}
        process_fn.assert_called_once_with("abc123", trace_id="t1")

    def test_direct_invoke_missing_b64(self):
        process_fn = MagicMock()
        result = lambda_handler(process_fn, {}, extra_fields=[])
        assert result["status"] == "error"
        assert "Missing" in result["error"]
        process_fn.assert_not_called()

    def test_direct_invoke_exception(self):
        process_fn = MagicMock(side_effect=RuntimeError("boom"))
        event = {"job_parameters_b64": "abc123"}
        result = lambda_handler(process_fn, event, extra_fields=[])
        assert result["status"] == "error"
        assert "boom" in result["error"]
        assert "statusCode" not in result

    def test_apigw_success_returns_200(self):
        process_fn = MagicMock(return_value={"status": "ok"})
        payload = {"job_parameters_b64": "abc123"}
        event = {"httpMethod": "POST", "body": json.dumps(payload)}

        result = lambda_handler(process_fn, event, extra_fields=[])
        assert result["statusCode"] == 200
        assert json.loads(result["body"])["status"] == "ok"

    def test_apigw_error_returns_400(self):
        process_fn = MagicMock(return_value={"status": "error", "errors": ["bad"]})
        payload = {"job_parameters_b64": "abc123"}
        event = {"httpMethod": "POST", "body": json.dumps(payload)}

        result = lambda_handler(process_fn, event, extra_fields=[])
        assert result["statusCode"] == 400

    def test_apigw_method_not_allowed_returns_405(self):
        process_fn = MagicMock()
        event = {"httpMethod": "GET", "body": "{}"}

        result = lambda_handler(process_fn, event, extra_fields=[])
        assert result["statusCode"] == 405
        process_fn.assert_not_called()

    def test_apigw_missing_b64_returns_400(self):
        process_fn = MagicMock()
        event = {"httpMethod": "POST", "body": "{}"}

        result = lambda_handler(process_fn, event, extra_fields=[])
        assert result["statusCode"] == 400

    def test_apigw_exception_returns_500(self):
        process_fn = MagicMock(side_effect=RuntimeError("crash"))
        payload = {"job_parameters_b64": "abc123"}
        event = {"httpMethod": "POST", "body": json.dumps(payload)}

        result = lambda_handler(process_fn, event, extra_fields=[])
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "crash" in body["error"]

    def test_sns_event_processed(self):
        process_fn = MagicMock(return_value={"status": "ok"})
        payload = {"job_parameters_b64": "abc123", "run_id": "r1"}
        event = {"Records": [{"Sns": {"Message": json.dumps(payload)}}]}

        result = lambda_handler(process_fn, event, extra_fields=["run_id"])
        assert result["status"] == "ok"
        process_fn.assert_called_once_with("abc123", run_id="r1")

    def test_extra_fields_extracted(self):
        process_fn = MagicMock(return_value={"status": "ok"})
        event = {
            "job_parameters_b64": "abc",
            "trace_id": "t1",
            "run_id": "r1",
            "done_endpt": "s3://bucket/done",
        }

        lambda_handler(process_fn, event, extra_fields=["trace_id", "run_id", "done_endpt"])
        process_fn.assert_called_once_with("abc", trace_id="t1", run_id="r1", done_endpt="s3://bucket/done")

    def test_missing_extra_fields_default_to_empty_string(self):
        process_fn = MagicMock(return_value={"status": "ok"})
        event = {"job_parameters_b64": "abc"}

        lambda_handler(process_fn, event, extra_fields=["trace_id", "run_id"])
        process_fn.assert_called_once_with("abc", trace_id="", run_id="")
