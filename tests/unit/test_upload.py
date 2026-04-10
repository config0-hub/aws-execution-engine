"""Unit tests for src/init_job/upload.py."""

import os
import tempfile

import boto3
import pytest
from moto import mock_aws

from src.init_job.upload import upload_orders


@pytest.fixture
def aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_EXE_SYS_INTERNAL_BUCKET", "test-internal")


@pytest.fixture
def s3_bucket(aws_env):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-internal")
        yield s3


class TestUploadOrders:
    def test_uploads_exec_zip(self, s3_bucket):
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
            f.write(b"fake zip content")
            zip_path = f.name

        try:
            repackaged = [
                {"order_num": "0001", "zip_path": zip_path, "order_name": "test"},
            ]
            upload_orders(repackaged, "run-1", "test-internal")

            # Verify it was uploaded
            resp = s3_bucket.get_object(
                Bucket="test-internal",
                Key="tmp/exec/run-1/0001/exec.zip",
            )
            assert resp["Body"].read() == b"fake zip content"
        finally:
            os.unlink(zip_path)

    def test_uploads_multiple_orders(self, s3_bucket):
        zip_paths = []
        try:
            for i in range(3):
                with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as f:
                    f.write(f"zip-{i}".encode())
                    zip_paths.append(f.name)

            repackaged = [
                {"order_num": f"000{i+1}", "zip_path": p, "order_name": f"order-{i}"}
                for i, p in enumerate(zip_paths)
            ]
            upload_orders(repackaged, "run-1", "test-internal")

            for i in range(3):
                resp = s3_bucket.get_object(
                    Bucket="test-internal",
                    Key=f"tmp/exec/run-1/000{i+1}/exec.zip",
                )
                assert resp["Body"].read() == f"zip-{i}".encode()
        finally:
            for p in zip_paths:
                os.unlink(p)
