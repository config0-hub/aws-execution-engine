"""Resource validation for SimplePayload fields."""

import logging
from urllib.parse import urlparse

import boto3
from botocore.exceptions import ClientError

from aws_exe_sys.common.payload import SimplePayload

logger = logging.getLogger("init_job.validate")


def validate_payload_resources(payload: SimplePayload) -> list[str]:
    """Check that referenced AWS resources exist.

    Returns a list of error strings (empty if all resources are reachable).
    """
    errors: list[str] = []

    # Verify S3 package object exists
    parsed = urlparse(payload.s3_package_uri)
    bucket = parsed.netloc
    key = parsed.path.lstrip("/")

    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError:
        errors.append(f"S3 object not found: {payload.s3_package_uri}")

    # Verify SSM parameter exists when sops_type is "ssm"
    if payload.sops_type == "ssm" and payload.sops_path:
        ssm = boto3.client("ssm")
        try:
            ssm.get_parameter(Name=payload.sops_path, WithDecryption=False)
        except ClientError:
            errors.append(f"SSM parameter not found: {payload.sops_path}")

    return errors
