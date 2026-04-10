"""Lambda entrypoint for init_job — Part 1: receive job, repackage, and insert orders.

Supports three invocation sources:
  - Direct Lambda invoke: {"job_parameters_b64": "..."}
  - SNS trigger: {"Records": [{"Sns": {"Message": "{...}"}}]}
  - API Gateway: {"httpMethod": "POST", "body": "{...}"}
"""

import logging
import os
import uuid
from typing import Any, Dict

from src.common.models import Job
from src.common.trace import generate_trace_id
from src.common.flow import generate_flow_id
from src.common import s3 as s3_ops
from src.common.lambda_handler import lambda_handler
from src.init_job.validate import validate_orders
from src.init_job.repackage import repackage_orders
from src.init_job.upload import upload_orders
from src.init_job.insert import insert_orders

logger = logging.getLogger(__name__)


def process_job_and_insert_orders(
    job_parameters_b64: str,
    credentials_token: str = "",
    trace_id: str = "",
    run_id: str = "",
    done_endpt: str = "",
) -> dict:
    """Main processing function. Orchestrates the full init_job flow."""
    internal_bucket = os.environ.get("AWS_EXE_SYS_INTERNAL_BUCKET", "")
    done_bucket = os.environ.get("AWS_EXE_SYS_DONE_BUCKET", "")

    # Decode job parameters
    job = Job.from_b64(job_parameters_b64)

    # --- JWT credential injection ---
    if credentials_token:
        jwt_secret_ssm_path = os.environ.get("JWT_SECRET_SSM_PATH", "")
        if not jwt_secret_ssm_path:
            return {"status": "error", "error": "JWT_SECRET_SSM_PATH not configured"}

        import boto3
        from src.common.jwt_creds import verify_credentials_token

        ssm = boto3.client("ssm")
        resp = ssm.get_parameter(Name=jwt_secret_ssm_path, WithDecryption=True)
        jwt_secret = resp["Parameter"]["Value"]

        claims = verify_credentials_token(credentials_token, jwt_secret)

        injected_creds = {
            k: claims[k]
            for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")
            if k in claims
        }

        for order in job.orders:
            if order.env_vars is None:
                order.env_vars = {}
            order.env_vars.update(injected_creds)

        logger.info("Injected credentials from JWT", extra={
            "target_account_id": claims.get("target_account_id"),
            "assumed_role_arn": claims.get("assumed_role_arn"),
        })
    # --- End JWT credential injection ---

    # Generate IDs
    if not trace_id:
        trace_id = generate_trace_id()
    if not run_id:
        run_id = str(uuid.uuid4())

    flow_id = generate_flow_id(job.username, trace_id, job.flow_label)

    if not done_endpt:
        done_endpt = f"s3://{done_bucket}/{run_id}/done"

    # Step 1: Validate
    errors = validate_orders(job)
    if errors:
        return {
            "status": "error",
            "errors": errors,
            "run_id": run_id,
            "trace_id": trace_id,
        }

    # Step 2: Repackage
    repackaged = repackage_orders(
        job=job,
        run_id=run_id,
        trace_id=trace_id,
        flow_id=flow_id,
        internal_bucket=internal_bucket,
    )

    # Step 3: Upload
    upload_orders(repackaged, run_id, internal_bucket)

    # Step 4: Insert into DynamoDB
    insert_orders(
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
        process_job_and_insert_orders,
        event,
        extra_fields=["credentials_token", "trace_id", "run_id", "done_endpt"],
        logger_name="init_job",
    )
