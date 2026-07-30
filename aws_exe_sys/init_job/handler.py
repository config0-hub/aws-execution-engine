"""Lambda entrypoint for init_job — thin validation + dispatch layer.

Receives a SimplePayload (7 fields), validates it, checks that the
referenced AWS resources exist, and dispatches to the appropriate
execution target (Lambda or CodeBuild).
"""

import logging
from typing import Any

from aws_exe_sys.common.lambda_handler import apigw_response, normalize_event
from aws_exe_sys.common.payload import PayloadValidationError, SimplePayload
from aws_exe_sys.init_job.dispatcher import dispatch
from aws_exe_sys.init_job.validate import validate_payload_resources

logger = logging.getLogger("init_job")


def handler(event: dict[str, Any], context: Any = None) -> dict:
    """Lambda entrypoint. Supports direct invoke, SNS, and API Gateway."""
    is_apigw = "httpMethod" in event or ("requestContext" in event and "http" in event.get("requestContext", {}))

    try:
        payload_dict = normalize_event(event)

        if "_apigw_error" in payload_dict:
            return apigw_response(405, {"status": "error", "error": payload_dict["_apigw_error"]})

        payload = SimplePayload.from_dict(payload_dict)
        payload.validate()

        resource_errors = validate_payload_resources(payload)
        if resource_errors:
            result = {"status": "error", "errors": resource_errors}
            return apigw_response(400, result) if is_apigw else result

        dispatch(payload)

        result = {"status": "ok", "trigger_id": payload.trigger_id}
        return apigw_response(200, result) if is_apigw else result

    except PayloadValidationError as exc:
        result = {"status": "error", "error": str(exc)}
        return apigw_response(400, result) if is_apigw else result
    except Exception as exc:
        logger.exception("init_job failed")
        result = {"status": "error", "error": str(exc)}
        return apigw_response(500, result) if is_apigw else result
