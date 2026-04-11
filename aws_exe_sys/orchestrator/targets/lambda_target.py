"""AWS Lambda execution target.

Invokes the worker Lambda asynchronously with a JSON payload pointing
at the SOPS-encrypted exec bundle in S3.
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


class LambdaTarget:
    """Execution target that invokes the worker Lambda asynchronously."""

    name = "lambda"

    def dispatch(self, order: Any, run_id: str, internal_bucket: str) -> str:
        lambda_client = boto3.client("lambda")
        function_name = os.environ["AWS_EXE_SYS_WORKER_LAMBDA"]

        payload = {
            "s3_location": order.get("s3_location", ""),
            "internal_bucket": internal_bucket,
            # Plaintext callback_url so the worker can finalize the
            # order even if the SOPS key (which carries the in-bundle
            # CALLBACK_URL) has expired in SSM.
            "callback_url": order.get("callback_url", ""),
            # Run identity — plumbed so the worker's callback fallback
            # can write to DynamoDB directly when the presigned S3 PUT
            # is unreachable. Without these, the fallback is a no-op.
            "run_id": run_id,
            "order_num": order.get("order_num", ""),
        }
        if order.get("sops_key_ssm_path"):
            payload["sops_key_ssm_path"] = order["sops_key_ssm_path"]

        resp = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="Event",  # async
            Payload=json.dumps(payload, default=_json_default).encode(),
        )
        return resp.get("ResponseMetadata", {}).get("RequestId", "")
