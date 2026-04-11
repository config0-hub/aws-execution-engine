"""Insert SSM orders into DynamoDB and write initial job event."""

import time
from typing import Dict, List

from aws_exe_sys.common import dynamodb
from aws_exe_sys.common.models import JOB_ORDER_NAME, QUEUED
from aws_exe_sys.common.models import SsmJob


def insert_ssm_orders(
    job: SsmJob,
    run_id: str,
    flow_id: str,
    trace_id: str,
    repackaged_orders: List[Dict],
    internal_bucket: str,
    dynamodb_resource=None,
) -> None:
    """Insert all SSM orders into the DynamoDB orders table and write initial job event."""
    now = int(time.time())
    ttl = now + 86400  # 1 day

    for i, order in enumerate(job.orders):
        order_info = repackaged_orders[i]
        order_num = order_info["order_num"]
        order_name = order_info["order_name"]

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
            "execution_target": "ssm",
            "dependencies": order.dependencies or [],
            "must_succeed": order.must_succeed,
            "timeout": order.timeout,
            "created_at": now,
            "last_update": now,
            "ttl": ttl,
            "ssm_targets": order.ssm_targets,
        }
        if order.ssm_document_name:
            order_data["ssm_document_name"] = order.ssm_document_name
        # NOTE: env_dict is intentionally NOT persisted to DynamoDB. Environment
        # variables are already baked into the SOPS-encrypted zip bundle at
        # repackage time; storing them on the order row would create a second
        # copy of live credentials in a table with broader read access.

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
