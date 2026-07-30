"""Unit tests for aws_exe_sys/common/result_writer.py using moto."""

import json

import boto3
from moto import mock_aws
import pytest

from aws_exe_sys.common.result_writer import (
    ExecutionResult,
    StepResult,
    _parse_s3_uri,
    write_result,
)


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")


@pytest.fixture
def s3_client(aws_env):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket="done-bucket")
        yield client


class TestParseS3URI:
    def test_valid_uri(self):
        bucket, key = _parse_s3_uri("s3://my-bucket/path/to/result.json")
        assert bucket == "my-bucket"
        assert key == "path/to/result.json"

    def test_invalid_uri_no_scheme(self):
        with pytest.raises(ValueError, match="Invalid S3 URI"):
            _parse_s3_uri("my-bucket/key")

    def test_invalid_uri_no_key(self):
        with pytest.raises(ValueError, match="Invalid S3 URI"):
            _parse_s3_uri("s3://my-bucket")


class TestExecutionResult:
    def test_to_dict_excludes_none(self):
        r = ExecutionResult(trigger_id="t-1", status="succeeded")
        d = r.to_dict()
        assert "error" not in d
        assert d["trigger_id"] == "t-1"
        assert d["steps"] == []

    def test_to_dict_includes_error(self):
        r = ExecutionResult(trigger_id="t-1", status="failed", error="boom")
        d = r.to_dict()
        assert d["error"] == "boom"

    def test_to_dict_with_steps(self):
        step = StepResult(
            step_name="step-0",
            status="succeeded",
            exit_code=0,
            duration_seconds=1.23,
            output="hello\n",
        )
        r = ExecutionResult(trigger_id="t-1", status="succeeded", steps=[step])
        d = r.to_dict()
        assert len(d["steps"]) == 1
        assert d["steps"][0]["step_name"] == "step-0"
        assert d["steps"][0]["output"] == "hello\n"


class TestWriteResult:
    def test_write_and_read_back(self, s3_client):
        step = StepResult(
            step_name="step-0",
            status="succeeded",
            exit_code=0,
            duration_seconds=0.5,
            output="all good",
        )
        result = ExecutionResult(
            trigger_id="trg-42",
            status="succeeded",
            steps=[step],
        )

        key = write_result(
            "s3://done-bucket/trg-42/result.json",
            result,
            s3_client=s3_client,
        )
        assert key == "trg-42/result.json"

        obj = s3_client.get_object(Bucket="done-bucket", Key=key)
        body = json.loads(obj["Body"].read().decode())
        assert body["trigger_id"] == "trg-42"
        assert body["status"] == "succeeded"
        assert len(body["steps"]) == 1
        assert body["steps"][0]["output"] == "all good"
        # Plain text output, not base64
        assert body["steps"][0]["output"] == "all good"

    def test_write_failed_result(self, s3_client):
        result = ExecutionResult(
            trigger_id="trg-err",
            status="failed",
            error="command not found",
        )
        key = write_result(
            "s3://done-bucket/trg-err/result.json",
            result,
            s3_client=s3_client,
        )

        obj = s3_client.get_object(Bucket="done-bucket", Key=key)
        body = json.loads(obj["Body"].read().decode())
        assert body["status"] == "failed"
        assert body["error"] == "command not found"

    def test_invalid_done_endpoint(self, s3_client):
        result = ExecutionResult(trigger_id="t", status="succeeded")
        with pytest.raises(ValueError, match="Invalid S3 URI"):
            write_result("not-an-s3-uri", result, s3_client=s3_client)
