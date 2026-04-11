"""AWS SSM Parameter Store credential provider."""

import base64
import binascii
import json
from typing import Dict, Optional

import boto3

from .base import CredentialProvider


class AwsSsmProvider(CredentialProvider):
    """Fetch secrets from AWS Systems Manager Parameter Store.

    Contract: each SSM parameter value is a base64-encoded JSON dict of
    env var key/value pairs. This matches the engine's historical use of
    SSM as a keyring (see ``docs/VARIABLES.md`` for the full rationale).

    Raises:
        ValueError: if the parameter value is not valid base64, does not
            decode to a JSON dict, or the decoded dict is the wrong shape.
            The error message names the SSM path so operators can identify
            the misconfigured parameter.
    """

    scheme = "aws_ssm"

    def fetch(
        self, path: str, region: Optional[str] = None,
    ) -> Dict[str, str]:
        client = boto3.client("ssm", region_name=region)
        resp = client.get_parameter(Name=path, WithDecryption=True)
        value = resp["Parameter"]["Value"]
        try:
            raw = base64.b64decode(value, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(
                f"SSM parameter {path!r} is not valid base64. "
                f"AwsSsmProvider expects a base64-encoded JSON dict of "
                f"env var key/value pairs (underlying error: {exc})."
            ) from exc
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"SSM parameter {path!r} decoded from base64 but the result "
                f"is not valid JSON (underlying error: {exc})."
            ) from exc
        if not isinstance(decoded, dict):
            raise ValueError(
                f"SSM parameter {path!r} must be a JSON dict of env var "
                f"key/value pairs; got {type(decoded).__name__}."
            )
        return {str(k): str(v) for k, v in decoded.items()}
