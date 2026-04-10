"""Insert orders into DynamoDB and write initial job event."""

import base64
import json
import time
from typing import Dict, List

from src.common import dynamodb
from src.common.models import Job, JOB_ORDER_NAME, QUEUED


def insert_orders(
    job: Job,
    run_id: str,
    flow_id: str,
    trace_id: str,
    repackaged_orders: List[Dict],
    internal_bucket: str,
    dynamodb_resource=None,
) -> None:
    """Insert all orders into the DynamoDB orders table and write initial job event."""
    now = int(time.time())
    ttl = now + 86400  # 1 day

    for i, order in enumerate(job.orders):
        order_info = repackaged_orders[i]
        order_num = order_info["order_num"]
        order_name = order_info["order_name"]

        # Build git b64 if using git source
        git_b64 = None
        if not order.s3_location:
            commit = order.commit_hash or job.commit_hash
            git_data = {
                "repo": order.git_repo or job.git_repo,
                "token_location": job.git_token_location,
                "folder": order.git_folder or "",
            }
            if job.git_ssh_key_location:
                git_data["ssh_key_location"] = job.git_ssh_key_location
            if commit:
                git_data["commit_hash"] = commit
            git_b64 = base64.b64encode(json.dumps(git_data).encode()).decode()

        s3_location = f"s3://{internal_bucket}/tmp/exec/{run_id}/{order_num}/exec.zip"

        order_data = {
            "trace_id": trace_id,
            "flow_id": flow_id,
            "order_name": order_name,
            "cmds": order.cmds,
            "status": QUEUED,
            "queue_id": order.queue_id or order_num,
            "s3_location": s3_location,
            "callback_url": order_info["callback_url"],
            "execution_target": order.execution_target,
            "dependencies": order.dependencies or [],
            "must_succeed": order.must_succeed,
            "timeout": order.timeout,
            "created_at": now,
            "last_update": now,
            "ttl": ttl,
        }
        if git_b64:
            order_data["git_b64"] = git_b64

        sops_key_ssm_path = order_info.get("sops_key_ssm_path")
        if sops_key_ssm_path:
            order_data["sops_key_ssm_path"] = sops_key_ssm_path

        dynamodb.put_order(
            run_id=run_id,
            order_num=order_num,
            order_data=order_data,
            dynamodb_resource=dynamodb_resource,
        )

    # Write initial job-level event
    dynamodb.put_event(
        trace_id=trace_id,
        order_name=JOB_ORDER_NAME,
        event_type="job_started",
        status="running",
        extra_fields={
            "flow_id": flow_id,
            "run_id": run_id,
            "order_count": len(job.orders),
        },
        dynamodb_resource=dynamodb_resource,
    )
