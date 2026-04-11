"""AWS Secrets Manager credential provider."""

import json
from typing import Dict, Optional

import boto3

from .base import CredentialProvider


def _path_to_env_key(path: str) -> str:
    """Derive an env var key from a Secrets Manager path.

    Takes the last segment, upper-cases it, and replaces dashes with
    underscores. e.g. ``"prod/github-token"`` -> ``"GITHUB_TOKEN"``.
    """
    return path.rsplit("/", 1)[-1].upper().replace("-", "_")


class AwsSecretsManagerProvider(CredentialProvider):
    """Fetch secrets from AWS Secrets Manager.

    A secret's ``SecretString`` is interpreted in two ways:

    1. If it parses as a JSON dict, each key becomes an env var. This
       matches the common Secrets Manager convention of packing many
       fields (e.g. RDS credentials) into one secret.
    2. Otherwise (plain string, JSON list, scalar) the whole value is
       assigned to a single env var named after the path's last segment.
    """

    scheme = "aws_secretsmanager"

    def fetch(
        self, path: str, region: Optional[str] = None,
    ) -> Dict[str, str]:
        client = boto3.client("secretsmanager", region_name=region)
        resp = client.get_secret_value(SecretId=path)
        value = resp["SecretString"]
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            return {str(k): str(v) for k, v in parsed.items()}
        return {_path_to_env_key(path): value}
