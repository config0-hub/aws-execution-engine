"""Lambda entrypoint for worker."""

import logging
from typing import Any, Dict

from aws_exe_sys.worker.run import run

logger = logging.getLogger(__name__)


def handler(event: Dict[str, Any], context: Any = None) -> dict:
    """Lambda handler. Receives s3_location and internal_bucket."""
    s3_location = event.get("s3_location", "")
    internal_bucket = event.get("internal_bucket", "")
    sops_key_ssm_path = event.get("sops_key_ssm_path", "")
    # Plaintext fallback callback URL — used on SopsKeyExpired so the
    # worker can still finalize the order without decrypting the bundle.
    callback_url = event.get("callback_url", "")
    # Run identity — threaded through to send_callback's DynamoDB
    # fallback when the presigned S3 PUT is unreachable.
    run_id = event.get("run_id", "")
    order_num = event.get("order_num", "")

    if not s3_location:
        logger.error("Missing s3_location in event")
        return {"status": "failed", "error": "Missing s3_location"}

    try:
        status = run(
            s3_location,
            internal_bucket,
            sops_key_ssm_path=sops_key_ssm_path,
            callback_url=callback_url,
            run_id=run_id,
            order_num=order_num,
        )
        return {"status": status}
    except Exception as e:
        logger.exception("Worker failed")
        return {"status": "failed", "error": str(e)}
