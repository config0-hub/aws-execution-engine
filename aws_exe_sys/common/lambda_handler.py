"""Generic Lambda handler infrastructure shared by init_job and ssm_config."""

import json
import logging
from typing import Any, Callable, Dict, List


def normalize_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Extract the job payload from any supported invocation source.

    Returns a flat dict with at minimum 'job_parameters_b64'.
    """
    # SNS: unwrap first record's Message
    if "Records" in event:
        records = event["Records"]
        if records and "Sns" in records[0]:
            message = records[0]["Sns"].get("Message", "{}")
            if isinstance(message, str):
                return json.loads(message)
            return message

    # API Gateway format 2.0: requestContext.http
    if "requestContext" in event and "http" in event.get("requestContext", {}):
        method = event["requestContext"]["http"].get("method", "")
        if method != "POST":
            return {"_apigw_error": f"Method {method} not allowed"}
        body = event.get("body", "")
        if isinstance(body, str):
            return json.loads(body) if body else {}
        return body if isinstance(body, dict) else {}

    # API Gateway format 1.0: httpMethod
    if "httpMethod" in event:
        if event["httpMethod"] != "POST":
            return {"_apigw_error": f"Method {event['httpMethod']} not allowed"}
        body = event.get("body", "")
        if isinstance(body, str):
            return json.loads(body) if body else {}
        return body if isinstance(body, dict) else {}

    # Direct invoke: event is the payload
    return event


def apigw_response(status_code: int, body: dict) -> dict:
    """Wrap result in API Gateway proxy response format."""
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def lambda_handler(
    process_fn: Callable,
    event: Dict[str, Any],
    extra_fields: List[str],
    logger_name: str = "lambda_handler",
) -> dict:
    """Generic Lambda entrypoint wrapper.

    Args:
        process_fn: The processing function to call with job_parameters_b64 and extra kwargs.
        event: The raw Lambda event.
        extra_fields: Additional field names to extract from the payload and pass to process_fn.
        logger_name: Logger name for this handler.

    Returns:
        Response dict (direct) or API Gateway proxy response.
    """
    log = logging.getLogger(logger_name)
    is_apigw = "httpMethod" in event or (
        "requestContext" in event and "http" in event.get("requestContext", {})
    )

    try:
        payload = normalize_event(event)

        # API Gateway method rejection
        if "_apigw_error" in payload:
            return apigw_response(405, {"status": "error", "error": payload["_apigw_error"]})

        job_parameters_b64 = payload.get("job_parameters_b64", "")

        if not job_parameters_b64:
            result = {"status": "error", "error": "Missing job_parameters_b64"}
            return apigw_response(400, result) if is_apigw else result

        kwargs = {k: payload.get(k, "") for k in extra_fields}
        result = process_fn(job_parameters_b64, **kwargs)

        if is_apigw:
            code = 200 if result.get("status") == "ok" else 400
            return apigw_response(code, result)
        return result

    except Exception as e:
        log.exception(f"{logger_name} failed")
        result = {"status": "error", "error": str(e)}
        return apigw_response(500, result) if is_apigw else result
