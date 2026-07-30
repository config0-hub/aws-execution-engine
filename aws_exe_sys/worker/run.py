"""Worker execution logic — download, decrypt, run commands, write result.

Single entrypoint: ``run()`` always writes an ExecutionResult to the
done_endpoint, even on failure. This is the key invariant.
"""

import base64
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import time
import zipfile

from aws_exe_sys.common.result_writer import ExecutionResult, write_result
from aws_exe_sys.common.sops import SopsKeyExpired, handle_sops
from aws_exe_sys.common.subprocess_runner import run_commands

logger = logging.getLogger(__name__)

_SCRATCH_ROOT_NAME = "aws-exe-sys-worker"
_WORKDIR_PREFIX = "run-"


def _scratch_root() -> Path:
    """Return the root reserved for worker-created per-run directories."""
    return Path(tempfile.gettempdir()) / _SCRATCH_ROOT_NAME


def cleanup_stale_workdirs() -> None:
    """Remove stale worker-owned run directories from the scratch root."""
    scratch_root = _scratch_root()
    scratch_root.mkdir(parents=True, exist_ok=True)

    for path in scratch_root.iterdir():
        if path.name.startswith(_WORKDIR_PREFIX) and not path.is_symlink() and path.is_dir():
            shutil.rmtree(path)


def fetch_code_s3(s3_location: str) -> str:
    """Download and extract a zip from S3. Returns path to extracted directory."""
    import boto3

    scratch_root = _scratch_root()
    scratch_root.mkdir(parents=True, exist_ok=True)
    work_dir = tempfile.mkdtemp(prefix=_WORKDIR_PREFIX, dir=scratch_root)
    parts = s3_location.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""

    local_zip = os.path.join(work_dir, "code.zip")
    s3_client = boto3.client("s3")
    s3_client.download_file(bucket, key, local_zip)

    with zipfile.ZipFile(local_zip, "r") as zf:
        zf.extractall(work_dir)
    os.unlink(local_zip)
    return work_dir


def run(
    trigger_id: str,
    s3_package_uri: str,
    sops_type: str | None,
    sops_path: str | None,
    commands_b64: str,
    done_endpoint: str,
    execution_target: str,
) -> str:
    """Single worker entrypoint.

    Pipeline:
        1. Clean stale worker-owned scratch directories
        2. Download zip via fetch_code_s3
        3. handle_sops if sops_type provided
        4. Decode commands_b64
        5. run_commands with enriched env
        6. Build ExecutionResult
        7. ALWAYS write_result to done_endpoint (even on failure)

    Returns the final status string ("succeeded" or "failed").
    """
    started_at = time.monotonic()
    result: ExecutionResult | None = None

    try:
        # 1. Remove workspaces left by earlier warm-container invocations.
        cleanup_stale_workdirs()

        # 2. Download and extract code package
        work_dir = fetch_code_s3(s3_package_uri)

        # 3. Decrypt SOPS secrets if configured
        env_vars: dict[str, str] = {}
        if sops_type is not None:
            env_vars = handle_sops(work_dir, sops_type=sops_type, sops_path=sops_path)

        # 4. Decode commands
        commands: list[str] = json.loads(base64.b64decode(commands_b64))

        # 5. Build subprocess environment (no os.environ mutation)
        proc_env = os.environ.copy()
        proc_env.update({k: str(v) for k, v in env_vars.items()})

        # If SOPS env replaced AWS_ACCESS_KEY_ID but did NOT supply
        # AWS_SESSION_TOKEN, clear the Lambda's own session token from proc_env.
        # Keeping the Lambda's token while using a different key+secret would
        # make the AWS provider fail with InvalidClientTokenId (mismatched triple).
        if "AWS_ACCESS_KEY_ID" in env_vars and "AWS_SESSION_TOKEN" not in env_vars:
            proc_env.pop("AWS_SESSION_TOKEN", None)

        # 6. Execute commands
        steps = run_commands(commands, env=proc_env, work_dir=work_dir)

        # 7. Determine overall status
        if steps and all(s.status == "succeeded" for s in steps):
            status = "succeeded"
        else:
            status = "failed"

        result = ExecutionResult(
            trigger_id=trigger_id,
            status=status,
            steps=steps,
        )

    except SopsKeyExpired as exc:
        logger.error("SOPS key expired: %s", exc)
        result = ExecutionResult(
            trigger_id=trigger_id,
            status="failed",
            error=f"sops_key_expired: {exc}",
        )

    except Exception as exc:
        logger.exception("Worker run failed")
        result = ExecutionResult(
            trigger_id=trigger_id,
            status="failed",
            error=str(exc),
        )

    finally:
        # Key invariant: ALWAYS write result to done_endpoint
        if result is None:
            result = ExecutionResult(
                trigger_id=trigger_id,
                status="failed",
                error="unknown error — result was never set",
            )

        elapsed = time.monotonic() - started_at
        logger.info(
            "Worker finished: trigger_id=%s status=%s elapsed=%.2fs",
            trigger_id,
            result.status,
            elapsed,
        )

        # A missing marker is indistinguishable from a running job to callers.
        # Do not report success when the terminal result could not be persisted.
        write_result(done_endpoint, result)

    return result.status
