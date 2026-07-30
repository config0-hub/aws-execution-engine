"""Lambda + CodeBuild entrypoint for the simplified worker."""

import logging
import os
from typing import Any

from aws_exe_sys.common.lambda_handler import normalize_event
from aws_exe_sys.common.payload import PayloadValidationError, SimplePayload
from aws_exe_sys.common.result_writer import ExecutionResult, write_result
from aws_exe_sys.worker.run import run

logger = logging.getLogger(__name__)

_PAYLOAD_FIELDS = [
    "trigger_id",
    "s3_package_uri",
    "sops_type",
    "sops_path",
    "commands_b64",
    "done_endpoint",
    "execution_target",
]


def _write_prerun_failure(payload_dict: dict[str, Any], error_msg: str) -> None:
    """Write a ``failed`` ExecutionResult for a failure that happened BEFORE run().

    ``run()`` owns the always-write-a-result invariant once it is entered, but a
    parse/validate failure never reaches it — and the WATCH side treats an absent
    done-marker as "still running", so a skipped write parks the order forever.
    This closes that hole: it writes the same canonical ExecutionResult to the
    raw payload's ``done_endpoint`` so finalize can still see a terminal result.
    """
    done_endpoint = payload_dict.get("done_endpoint", "")
    if not done_endpoint:
        raise ValueError("worker pre-run failure has no done_endpoint; terminal result cannot be written")

    result = ExecutionResult(
        trigger_id=payload_dict.get("trigger_id", ""),
        status="failed",
        error=error_msg,
    )
    write_result(done_endpoint, result)


def handler(event: dict[str, Any], context: Any = None) -> dict:
    """Lambda handler — extract 7-field payload and call run().

    Parse, validate, and run are wrapped so a pre-run failure STILL writes a
    ``failed`` ExecutionResult to ``done_endpoint`` via the handler-level
    ``finally`` (the always-write-a-result invariant). ``run()`` writes its own
    result once entered, so the finally only writes when run() was never reached.
    """
    payload_dict: dict[str, Any] = {}
    reached_run = False
    error_msg = ""

    try:
        payload_dict = normalize_event(event)
        payload = SimplePayload.from_dict(payload_dict)
        payload.validate()
        reached_run = True
        status = run(
            trigger_id=payload.trigger_id,
            s3_package_uri=payload.s3_package_uri,
            sops_type=payload.sops_type,
            sops_path=payload.sops_path,
            commands_b64=payload.commands_b64,
            done_endpoint=payload.done_endpoint,
            execution_target=payload.execution_target,
        )
        return {"status": status}
    except (PayloadValidationError, ValueError, KeyError, TypeError) as exc:
        error_msg = f"pre-run failure: {exc}"
        logger.exception("worker pre-run failure")
        return {"status": "failed", "error": error_msg}
    finally:
        if not reached_run:
            _write_prerun_failure(payload_dict, error_msg or "pre-run failure: unknown error")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    payload = SimplePayload(
        trigger_id=os.environ.get("TRIGGER_ID", ""),
        s3_package_uri=os.environ.get("S3_PACKAGE_URI", ""),
        sops_type=os.environ.get("SOPS_TYPE") or None,
        sops_path=os.environ.get("SOPS_PATH") or None,
        commands_b64=os.environ.get("COMMANDS_B64", ""),
        done_endpoint=os.environ.get("DONE_ENDPOINT", ""),
        execution_target=os.environ.get("EXECUTION_TARGET", ""),
    )
    payload.validate()
    status = run(
        trigger_id=payload.trigger_id,
        s3_package_uri=payload.s3_package_uri,
        sops_type=payload.sops_type,
        sops_path=payload.sops_path,
        commands_b64=payload.commands_b64,
        done_endpoint=payload.done_endpoint,
        execution_target=payload.execution_target,
    )
    exit(0 if status == "succeeded" else 1)
