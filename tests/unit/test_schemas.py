"""Unit tests for aws_exe_sys/common/schemas.py — versioned ResultV1 schema."""

from __future__ import annotations

import json

import boto3
import pytest
from moto import mock_aws

from aws_exe_sys.common import s3 as s3_ops
from aws_exe_sys.common.schemas import SCHEMA_VERSION_CURRENT, ResultV1


def test_result_v1_round_trip():
    """ResultV1 -> dict -> JSON -> dict -> ResultV1 preserves all fields."""
    original = ResultV1(status="succeeded", log="ok")
    payload = json.dumps(original.to_dict())
    parsed = ResultV1.from_dict(json.loads(payload))
    assert parsed == original
    assert parsed.status == "succeeded"
    assert parsed.log == "ok"
    assert parsed.schema_version == SCHEMA_VERSION_CURRENT


def test_result_v1_includes_schema_version():
    """to_dict() always emits schema_version='v1' even when not passed."""
    result = ResultV1(status="succeeded", log="ok")
    payload = result.to_dict()
    assert payload["schema_version"] == "v1"
    assert payload["schema_version"] == SCHEMA_VERSION_CURRENT


def test_result_from_dict_rejects_missing_version():
    """from_dict() raises ValueError mentioning v1 when schema_version absent."""
    with pytest.raises(ValueError) as exc_info:
        ResultV1.from_dict({"status": "ok", "log": "x"})
    assert "v1" in str(exc_info.value)


def test_result_from_dict_rejects_wrong_version():
    """from_dict() raises ValueError when schema_version is not v1."""
    with pytest.raises(ValueError) as exc_info:
        ResultV1.from_dict(
            {"status": "ok", "log": "x", "schema_version": "v2"}
        )
    assert "v2" in str(exc_info.value)


def test_write_result_includes_schema_version(monkeypatch):
    """s3_ops.write_result writes a payload that carries schema_version."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")

    with mock_aws():
        s3_client = boto3.client("s3", region_name="us-east-1")
        s3_client.create_bucket(Bucket="test-internal")

        key = s3_ops.write_result(
            bucket="test-internal",
            run_id="run-1",
            order_num="0001",
            status="succeeded",
            log="all good",
            s3_client=s3_client,
        )
        obj = s3_client.get_object(Bucket="test-internal", Key=key)
        body = json.loads(obj["Body"].read().decode("utf-8"))

        assert body["schema_version"] == "v1"
        assert body["status"] == "succeeded"
        assert body["log"] == "all good"
