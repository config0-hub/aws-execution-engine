"""AWS CodeBuild execution target.

Starts a CodeBuild project build with environment overrides that point
the builder at the SOPS-encrypted exec bundle in S3.
"""

import os
from typing import Any

import boto3


class CodeBuildTarget:
    """Execution target that starts a CodeBuild project build."""

    name = "codebuild"

    def dispatch(self, order: Any, run_id: str, internal_bucket: str) -> str:
        codebuild_client = boto3.client("codebuild")
        project_name = os.environ["AWS_EXE_SYS_CODEBUILD_PROJECT"]

        env_overrides = [
            {
                "name": "S3_LOCATION",
                "value": order.get("s3_location", ""),
                "type": "PLAINTEXT",
            },
            {
                "name": "INTERNAL_BUCKET",
                "value": internal_bucket,
                "type": "PLAINTEXT",
            },
        ]
        if order.get("sops_key_ssm_path"):
            env_overrides.append({
                "name": "SOPS_KEY_SSM_PATH",
                "value": order["sops_key_ssm_path"],
                "type": "PLAINTEXT",
            })

        resp = codebuild_client.start_build(
            projectName=project_name,
            environmentVariablesOverride=env_overrides,
        )
        return resp.get("build", {}).get("id", "")
