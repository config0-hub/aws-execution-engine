"""Unit tests for aws_exe_sys/init_job/validate.py (resource validation)."""

import base64
import json

import boto3
from moto import mock_aws
import pytest

from aws_exe_sys.common.payload import SimplePayload
from aws_exe_sys.init_job.validate import validate_payload_resources


@pytest.fixture(autouse=True)
def _aws_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")


def _b64_cmds(cmds: list[str]) -> str:
    return base64.b64encode(json.dumps(cmds).encode()).decode()


def _valid_payload(**overrides) -> SimplePayload:
    defaults = {
        "trigger_id": "trg-001",
        "s3_package_uri": "s3://test-bucket/exec/trg-001/exec.zip",
        "sops_type": None,
        "sops_path": None,
        "commands_b64": _b64_cmds(["echo hello"]),
        "done_endpoint": "s3://done-bucket/trg-001/result.json",
        "execution_target": "lambda",
        "timeout_seconds": 3600,
    }
    defaults.update(overrides)
    return SimplePayload(**defaults)


@mock_aws
class TestValidatePayloadResourcesAllValid:
    def test_s3_exists_no_sops_returns_empty(self):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        s3.put_object(Bucket="test-bucket", Key="exec/trg-001/exec.zip", Body=b"data")

        payload = _valid_payload()
        errors = validate_payload_resources(payload)
        assert errors == []

    def test_s3_exists_and_ssm_exists_returns_empty(self):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        s3.put_object(Bucket="test-bucket", Key="exec/trg-001/exec.zip", Body=b"data")

        ssm = boto3.client("ssm", region_name="us-east-1")
        ssm.put_parameter(
            Name="/exe-sys/sops-keys/run1/001",
            Value="private-key-content",
            Type="SecureString",
        )

        payload = _valid_payload(
            sops_type="ssm",
            sops_path="/exe-sys/sops-keys/run1/001",
        )
        errors = validate_payload_resources(payload)
        assert errors == []


@mock_aws
class TestValidatePayloadResourcesS3Failure:
    def test_s3_object_missing_returns_error(self):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        # Do NOT put the object

        payload = _valid_payload()
        errors = validate_payload_resources(payload)
        assert len(errors) == 1
        assert "S3 object not found" in errors[0]

    def test_s3_bucket_missing_returns_error(self):
        # Don't create the bucket at all
        payload = _valid_payload(s3_package_uri="s3://nonexistent-bucket/key")
        errors = validate_payload_resources(payload)
        assert len(errors) == 1
        assert "S3 object not found" in errors[0]


@mock_aws
class TestValidatePayloadResourcesSSMFailure:
    def test_ssm_parameter_missing_returns_error(self):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        s3.put_object(Bucket="test-bucket", Key="exec/trg-001/exec.zip", Body=b"data")

        payload = _valid_payload(
            sops_type="ssm",
            sops_path="/exe-sys/sops-keys/missing/001",
        )
        errors = validate_payload_resources(payload)
        assert len(errors) == 1
        assert "SSM parameter not found" in errors[0]


@mock_aws
class TestValidatePayloadResourcesSSMSkipped:
    def test_sops_type_none_skips_ssm_check(self):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        s3.put_object(Bucket="test-bucket", Key="exec/trg-001/exec.zip", Body=b"data")

        payload = _valid_payload(sops_type=None, sops_path=None)
        errors = validate_payload_resources(payload)
        assert errors == []

    def test_sops_type_kms_skips_ssm_check(self):
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        s3.put_object(Bucket="test-bucket", Key="exec/trg-001/exec.zip", Body=b"data")

        payload = _valid_payload(sops_type="kms", sops_path="/some/kms/key")
        errors = validate_payload_resources(payload)
        assert errors == []
