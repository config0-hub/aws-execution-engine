"""Unit tests for src/common/s3.py using moto."""

import json
import os
import tempfile

import boto3
import pytest
from moto import mock_aws
from unittest.mock import patch, MagicMock

from src.common import s3


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
        client.create_bucket(Bucket="test-internal")
        client.create_bucket(Bucket="test-done")
        yield client


class TestUploadExecZip:
    def test_upload(self, s3_client):
        # Create a temp file
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(b"fake zip content")
            temp_path = f.name

        try:
            key = s3.upload_exec_zip(
                "test-internal", "run-1", "001", temp_path,
                s3_client=s3_client,
            )
            assert key == "tmp/exec/run-1/001/exec.zip"

            # Verify the file exists in S3
            obj = s3_client.get_object(Bucket="test-internal", Key=key)
            assert obj["Body"].read() == b"fake zip content"
        finally:
            os.unlink(temp_path)


class TestPresignedUrl:
    def test_generate_callback_presigned_url(self, s3_client):
        url = s3.generate_callback_presigned_url(
            "test-internal", "run-1", "001",
            s3_client=s3_client,
        )
        assert "test-internal" in url
        assert "tmp/callbacks/runs/run-1/001/result.json" in url

    def test_custom_expiry(self, s3_client):
        url = s3.generate_callback_presigned_url(
            "test-internal", "run-1", "001", expiry=3600,
            s3_client=s3_client,
        )
        assert url is not None


class TestReadResult:
    def test_read_existing_result(self, s3_client):
        key = "tmp/callbacks/runs/run-1/001/result.json"
        payload = json.dumps({"status": "succeeded", "log": "all good"})
        s3_client.put_object(Bucket="test-internal", Key=key, Body=payload)

        result = s3.read_result(
            "test-internal", "run-1", "001",
            s3_client=s3_client,
        )
        assert result is not None
        assert result["status"] == "succeeded"
        assert result["log"] == "all good"

    def test_read_nonexistent_result(self, s3_client):
        result = s3.read_result(
            "test-internal", "nonexistent", "001",
            s3_client=s3_client,
        )
        assert result is None


class TestWriteResult:
    def test_write_result(self, s3_client):
        key = s3.write_result(
            "test-internal", "run-1", "001",
            "succeeded", "all good",
            s3_client=s3_client,
        )
        assert key == "tmp/callbacks/runs/run-1/001/result.json"

        obj = s3_client.get_object(Bucket="test-internal", Key=key)
        body = json.loads(obj["Body"].read().decode())
        assert body["status"] == "succeeded"
        assert body["log"] == "all good"


class TestWriteInitTrigger:
    def test_write_init_trigger(self, s3_client):
        key = s3.write_init_trigger(
            "test-internal", "run-1",
            s3_client=s3_client,
        )
        assert key == "tmp/callbacks/runs/run-1/0000/result.json"

        obj = s3_client.get_object(Bucket="test-internal", Key=key)
        body = json.loads(obj["Body"].read().decode())
        assert body["status"] == "init"


class TestWriteDoneEndpoint:
    def test_write_done(self, s3_client):
        summary = {"succeeded": 3, "failed": 0}
        key = s3.write_done_endpoint(
            "test-done", "run-1", "succeeded", summary,
            s3_client=s3_client,
        )
        assert key == "run-1/done"

        obj = s3_client.get_object(Bucket="test-done", Key=key)
        body = json.loads(obj["Body"].read().decode())
        assert body["status"] == "succeeded"
        assert body["summary"]["succeeded"] == 3


class TestCheckResultExists:
    def test_exists(self, s3_client):
        key = "tmp/callbacks/runs/run-1/001/result.json"
        s3_client.put_object(Bucket="test-internal", Key=key, Body=b'{"status":"ok"}')

        assert s3.check_result_exists(
            "test-internal", "run-1", "001",
            s3_client=s3_client,
        ) is True

    def test_not_exists(self, s3_client):
        assert s3.check_result_exists(
            "test-internal", "run-1", "999",
            s3_client=s3_client,
        ) is False
