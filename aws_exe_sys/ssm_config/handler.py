"""Lambda entrypoint for ssm_config — SSM config provider.

Separate construction point for SSM orders. Packages code, fetches credentials
(no SOPS), uploads to S3, inserts into shared DynamoDB orders table, and
triggers the orchestrator.

Supports three invocation sources:
  - Direct Lambda invoke: {"job_parameters_b64": "..."}
  - SNS trigger: {"Records": [{"Sns": {"Message": "{...}"}}]}
  - API Gateway: {"httpMethod": "POST", "body": "{...}"}
"""

import logging
import os
import uuid
from typing import Any, Dict

from aws_exe_sys.common.trace import generate_trace_id
from aws_exe_sys.common.flow import generate_flow_id
from aws_exe_sys.common import s3 as s3_ops
from aws_exe_sys.common.models import SsmJob
from aws_exe_sys.common.lambda_handler import lambda_handler
from aws_exe_sys.ssm_config.validate import validate_ssm_orders
from aws_exe_sys.ssm_config.repackage import repackage_ssm_orders
from aws_exe_sys.ssm_config.insert import insert_ssm_orders
from aws_exe_sys.init_job.upload import upload_orders

logger = logging.getLogger(__name__)


def process_ssm_job(
    job_parameters_b64: str,
    trace_id: str = "",
    run_id: str = "",
    done_endpt: str = "",
) -> dict:
    """Main processing function for SSM config provider."""
    internal_bucket = os.environ.get("AWS_EXE_SYS_INTERNAL_BUCKET", "")
    done_bucket = os.environ.get("AWS_EXE_SYS_DONE_BUCKET", "")

    job = SsmJob.from_b64(job_parameters_b64)

    if not trace_id:
        trace_id = generate_trace_id()
    if not run_id:
        run_id = str(uuid.uuid4())

    flow_id = generate_flow_id(job.username, trace_id, job.flow_label)

    if not done_endpt:
        done_endpt = f"s3://{done_bucket}/{run_id}/done"

    # Step 1: Validate
    errors = validate_ssm_orders(job)
    if errors:
        return {
            "status": "error",
            "errors": errors,
            "run_id": run_id,
            "trace_id": trace_id,
        }

    # Step 2: Repackage (no SOPS)
    repackaged = repackage_ssm_orders(
        job=job,
        run_id=run_id,
        trace_id=trace_id,
        flow_id=flow_id,
        internal_bucket=internal_bucket,
    )

    # Step 3: Upload
    upload_orders(repackaged, run_id, internal_bucket)

    # Step 4: Insert into DynamoDB
    insert_ssm_orders(
        job=job,
        run_id=run_id,
        flow_id=flow_id,
        trace_id=trace_id,
        repackaged_orders=repackaged,
        internal_bucket=internal_bucket,
    )

    # Step 5: Write init trigger to kick off orchestrator
    s3_ops.write_init_trigger(
        bucket=internal_bucket,
        run_id=run_id,
    )

    return {
        "status": "ok",
        "run_id": run_id,
        "trace_id": trace_id,
        "flow_id": flow_id,
        "done_endpt": done_endpt,
    }


def handler(event: Dict[str, Any], context: Any = None) -> dict:
    """Lambda entrypoint. Supports direct invoke, SNS, and API Gateway."""
    return lambda_handler(
        process_ssm_job,
        event,
        extra_fields=["trace_id", "run_id", "done_endpt"],
        logger_name="ssm_config",
    )
