"""AWS SSM Run Command execution target.

Sends an SSM SendCommand to an existing EC2 instance (either by
instance IDs or by tag selectors) with the order's commands, env
vars, and callback URL.
"""

import json
import os
from decimal import Decimal
from typing import Any

import boto3


def _json_default(obj: Any) -> Any:
    if isinstance(obj, Decimal):
        return int(obj) if obj == int(obj) else float(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class SsmTarget:
    """Execution target that issues an SSM SendCommand."""

    name = "ssm"

    def dispatch(self, order: Any, run_id: str, internal_bucket: str) -> str:
        ssm_client = boto3.client("ssm")
        document_name = (
            order.get("ssm_document_name")
            or os.environ["AWS_EXE_SYS_SSM_DOCUMENT"]
        )

        parameters = {
            "Commands": [
                json.dumps(order.get("cmds", []), default=_json_default),
            ],
            "CallbackUrl": [order.get("callback_url", "")],
            "Timeout": [str(order.get("timeout", 300))],
        }

        # NOTE: env vars are not read from DynamoDB. They are baked
        # into the SOPS-encrypted zip bundle at repackage time and
        # unpacked by the worker on the target instance. See
        # aws_exe_sys/ssm_config/insert.py for the rationale.

        s3_location = order.get("s3_location", "")
        if s3_location:
            parameters["S3Location"] = [s3_location]

        ssm_targets = order.get("ssm_targets", {})
        send_kwargs = {
            "DocumentName": document_name,
            "Parameters": parameters,
            "TimeoutSeconds": int(order.get("timeout", 300)),
            "Comment": (
                f"aws-exe-sys run_id={run_id} "
                f"order={order.get('order_num', '')}"
            ),
        }

        if ssm_targets.get("instance_ids"):
            send_kwargs["InstanceIds"] = ssm_targets["instance_ids"]
        elif ssm_targets.get("tags"):
            send_kwargs["Targets"] = [
                {"Key": f"tag:{k}", "Values": [v] if isinstance(v, str) else v}
                for k, v in ssm_targets["tags"].items()
            ]

        resp = ssm_client.send_command(**send_kwargs)
        return resp.get("Command", {}).get("CommandId", "")
