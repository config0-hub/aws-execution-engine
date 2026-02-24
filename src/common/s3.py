"""S3 operations for exec zips, presigned URLs, and result files."""

import json
import os
from typing import Optional

import boto3
from botocore.config import Config


def _get_client(s3_client=None):
    """Get an S3 client with v4 signatures for regional bucket support."""
    if s3_client is None:
        s3_client = boto3.client("s3", config=Config(signature_version="s3v4"))
    return s3_client


def upload_exec_zip(
    bucket: str,
    run_id: str,
    order_num: str,
    file_path: str,
    s3_client=None,
) -> str:
    """Upload exec.zip to tmp/exec/<run_id>/<order_num>/exec.zip."""
    client = _get_client(s3_client)
    key = f"tmp/exec/{run_id}/{order_num}/exec.zip"
    client.upload_file(file_path, bucket, key)
    return key


def generate_callback_presigned_url(
    bucket: str,
    run_id: str,
    order_num: str,
    expiry: int = 7200,
    s3_client=None,
) -> str:
    """Generate a presigned PUT URL for the callback result.json."""
    client = _get_client(s3_client)
    key = f"tmp/callbacks/runs/{run_id}/{order_num}/result.json"
    url = client.generate_presigned_url(
        "put_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expiry,
    )
    return url


def read_result(
    bucket: str,
    run_id: str,
    order_num: str,
    s3_client=None,
) -> Optional[dict]:
    """Read and parse result.json. Returns None if not found."""
    client = _get_client(s3_client)
    key = f"tmp/callbacks/runs/{run_id}/{order_num}/result.json"
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        body = response["Body"].read().decode("utf-8")
        return json.loads(body)
    except client.exceptions.NoSuchKey:
        return None


def write_result(
    bucket: str,
    run_id: str,
    order_num: str,
    status: str,
    log: str,
    s3_client=None,
) -> str:
    """Write result.json directly to S3 (used by watchdog, not workers)."""
    client = _get_client(s3_client)
    key = f"tmp/callbacks/runs/{run_id}/{order_num}/result.json"
    payload = json.dumps({"status": status, "log": log})
    client.put_object(Bucket=bucket, Key=key, Body=payload)
    return key


def write_init_trigger(
    bucket: str,
    run_id: str,
    s3_client=None,
) -> str:
    """Write the init trigger result.json for order 0000."""
    client = _get_client(s3_client)
    key = f"tmp/callbacks/runs/{run_id}/0000/result.json"
    payload = json.dumps({"status": "init", "log": ""})
    client.put_object(Bucket=bucket, Key=key, Body=payload)
    return key


def write_done_endpoint(
    bucket: str,
    run_id: str,
    status: str,
    summary: dict,
    s3_client=None,
) -> str:
    """Write <run_id>/done to the done bucket."""
    client = _get_client(s3_client)
    key = f"{run_id}/done"
    payload = json.dumps({"status": status, "summary": summary})
    client.put_object(Bucket=bucket, Key=key, Body=payload)
    return key


def check_result_exists(
    bucket: str,
    run_id: str,
    order_num: str,
    s3_client=None,
) -> bool:
    """Check if result.json exists for a given order."""
    client = _get_client(s3_client)
    key = f"tmp/callbacks/runs/{run_id}/{order_num}/result.json"
    try:
        client.head_object(Bucket=bucket, Key=key)
        return True
    except client.exceptions.ClientError:
        return False
