"""Worker execution logic — download, decrypt, run commands, callback."""

import glob
import json
import logging
import os
import subprocess
import tempfile
from typing import Optional

from aws_exe_sys.common import dynamodb, sops
from aws_exe_sys.common.code_source import fetch_code_s3
from aws_exe_sys.common.sops import SopsKeyExpired
from aws_exe_sys.worker.callback import send_callback

logger = logging.getLogger(__name__)


def _decrypt_and_load_env(work_dir: str, sops_key_ssm_path: str = "") -> dict:
    """Find SOPS encrypted file, decrypt, and load env vars."""
    encrypted_path = os.path.join(work_dir, "secrets.enc.json")
    if not os.path.exists(encrypted_path):
        return {}

    # Primary: fetch SOPS key from SSM (passed from handler)
    if sops_key_ssm_path:
        from aws_exe_sys.common.sops import fetch_sops_key_ssm
        private_key_content = fetch_sops_key_ssm(sops_key_ssm_path)
        # Write to temp file for SOPS CLI
        key_fd = tempfile.NamedTemporaryFile(
            suffix=".key", prefix="aws-exe-sys-sops-", mode="w", delete=False
        )
        key_fd.write(private_key_content)
        key_fd.close()
        sops_key = key_fd.name
    else:
        # Fallback: check env vars
        sops_key = os.environ.get("SOPS_AGE_KEY", "")
        if not sops_key:
            sops_key_file = os.environ.get("SOPS_AGE_KEY_FILE", "")
            if sops_key_file:
                sops_key = sops_key_file

    if not sops_key:
        logger.warning("No SOPS key found, skipping decryption")
        return {}

    env_vars = sops.decrypt_env(encrypted_path, sops_key)
    return env_vars


def _setup_events_dir(trace_id: str) -> str:
    """Create the shared events directory for subprocess event reporting.

    Subprocesses write JSON event files here. After command execution,
    the main process reads them and transfers to DynamoDB.
    """
    events_dir = f"/tmp/share/{trace_id}/events"
    os.makedirs(events_dir, exist_ok=True)
    return events_dir


def _collect_and_write_events(
    events_dir: str,
    trace_id: str,
    order_name: str,
    flow_id: str = "",
    run_id: str = "",
) -> int:
    """Read JSON event files from shared dir and write to DynamoDB order_events.

    Returns the number of events successfully written.
    """
    if not os.path.isdir(events_dir):
        return 0

    json_files = sorted(glob.glob(os.path.join(events_dir, "*.json")))
    if not json_files:
        return 0

    count = 0
    for filepath in json_files:
        filename = os.path.basename(filepath)
        stem = os.path.splitext(filename)[0]

        try:
            with open(filepath) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Skipping malformed event file %s: %s", filename, e)
            continue

        if not isinstance(data, dict):
            logger.warning("Skipping event file %s: not a JSON object", filename)
            continue

        event_type = data.pop("event_type", stem)
        status = data.pop("status", "info")

        meta_fields = {}
        if flow_id:
            meta_fields["flow_id"] = flow_id
        if run_id:
            meta_fields["run_id"] = run_id

        try:
            dynamodb.put_event(
                trace_id=trace_id,
                order_name=order_name,
                event_type=event_type,
                status=status,
                data=data if data else None,
                extra_fields=meta_fields if meta_fields else None,
            )
            count += 1
        except Exception as e:
            logger.warning("Failed to write event %s to DynamoDB: %s", filename, e)

    logger.info("Collected %d event(s) from %s", count, events_dir)
    return count


def _execute_commands(cmds: list, work_dir: str, timeout: int = 0, env: Optional[dict] = None) -> tuple:
    """Execute commands sequentially, capturing output.

    Returns (status, combined_log).
    """
    proc_env = env if env is not None else os.environ.copy()
    combined_log = []
    status = "succeeded"

    for cmd in cmds:
        logger.info("Executing: %s", cmd)
        combined_log.append(f"$ {cmd}")

        try:
            proc = subprocess.Popen(
                cmd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=work_dir,
                env=proc_env,
            )

            if timeout > 0:
                stdout, _ = proc.communicate(timeout=timeout)
            else:
                stdout, _ = proc.communicate()

            output = stdout.decode("utf-8", errors="replace") if stdout else ""
            combined_log.append(output)

            if proc.returncode != 0:
                combined_log.append(f"Exit code: {proc.returncode}")
                status = "failed"
                break

        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            combined_log.append(f"Command timed out after {timeout}s")
            status = "timed_out"
            break
        except Exception as e:
            combined_log.append(f"Error: {e}")
            status = "failed"
            break

    return status, "\n".join(combined_log)


def run(
    s3_location: str,
    internal_bucket: str = "",
    sops_key_ssm_path: str = "",
    callback_url: str = "",
    run_id: str = "",
    order_num: str = "",
) -> str:
    """Main worker execution flow.

    1. Download and extract exec.zip
    2. Decrypt SOPS -> load env vars
    3. Execute commands
    4. Send callback

    Args:
        callback_url: Plaintext fallback callback URL. Supplied by the
            invoker (Lambda handler event or CodeBuild env). Kept outside
            the SOPS bundle so it remains reachable if the bundle cannot
            be decrypted (e.g. expired SOPS key). The in-bundle CALLBACK_URL
            still wins on the happy path.
        run_id: Run identifier, threaded through to ``send_callback`` for
            the DynamoDB fallback. Required because on the
            ``SopsKeyExpired`` path the decrypted ``env_vars`` are empty,
            so identity cannot come from the bundle.
        order_num: Order number, threaded alongside ``run_id`` for the
            same reason.

    Returns final status.
    """
    # 1. Download and extract
    work_dir = fetch_code_s3(s3_location)

    # 2. Decrypt and load env vars
    try:
        env_vars = _decrypt_and_load_env(work_dir, sops_key_ssm_path=sops_key_ssm_path)
    except SopsKeyExpired as exc:
        # Permanent failure: we cannot decrypt the bundle and the key is
        # gone. Finalize the order via the plaintext fallback callback URL
        # so the orchestrator doesn't wait for the watchdog.
        logger.error("SOPS key expired, finalizing order: %s", exc)
        if callback_url:
            send_callback(
                callback_url,
                "failed",
                f"sops_key_expired: {exc}",
                run_id=run_id,
                order_num=order_num,
            )
        else:
            logger.error(
                "No fallback callback_url available; orchestrator will "
                "wait for the watchdog to finalize."
            )
        return "failed"

    # 3. Set up shared events directory for subprocess event reporting
    trace_id = env_vars.get("TRACE_ID", "")
    order_name = env_vars.get("ORDER_ID", "")
    flow_id = env_vars.get("FLOW_ID", "")
    # Prefer the explicit run()/handler parameter; fall back to the
    # in-bundle RUN_ID for call paths that don't supply it yet.
    if not run_id:
        run_id = env_vars.get("RUN_ID", "")
    events_dir = ""
    if trace_id:
        events_dir = _setup_events_dir(trace_id)

    # 4. Read commands from order config (if present) or env
    cmds_str = env_vars.get("CMDS", "")
    if cmds_str:
        try:
            cmds = json.loads(cmds_str)
        except json.JSONDecodeError:
            cmds = [cmds_str]
    else:
        # Look for cmds.json in work dir
        cmds_file = os.path.join(work_dir, "cmds.json")
        if os.path.exists(cmds_file):
            with open(cmds_file) as f:
                cmds = json.load(f)
        else:
            cmds = []

    if not cmds:
        logger.error("No commands found to execute")
        callback_url = env_vars.get("CALLBACK_URL", "")
        if callback_url:
            send_callback(
                callback_url,
                "failed",
                "No commands found",
                run_id=run_id,
                order_num=order_num,
            )
        return "failed"

    # 5. Build subprocess environment (no os.environ mutation)
    proc_env = os.environ.copy()
    proc_env.update({k: str(v) for k, v in env_vars.items()})
    if events_dir:
        proc_env["AWS_EXE_SYS_EVENTS_DIR"] = events_dir

    # 6. Execute
    timeout = int(env_vars.get("TIMEOUT", os.environ.get("TIMEOUT", "0")))
    status, log_output = _execute_commands(cmds, work_dir, timeout=timeout, env=proc_env)

    # 7. Collect subprocess events and write to DynamoDB
    if events_dir and trace_id and order_name:
        _collect_and_write_events(events_dir, trace_id, order_name, flow_id, run_id)

    # 8. Callback
    callback_url = env_vars.get("CALLBACK_URL", "")
    if callback_url:
        send_callback(
            callback_url,
            status,
            log_output,
            run_id=run_id,
            order_num=order_num,
        )
    else:
        logger.warning("No CALLBACK_URL found, skipping callback")

    return status


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    s3_loc = os.environ.get("S3_LOCATION", "")
    bucket = os.environ.get("INTERNAL_BUCKET", "")
    if not s3_loc:
        logger.error("Missing S3_LOCATION env var")
        exit(1)
    result = run(s3_loc, bucket)
    exit(0 if result == "succeeded" else 1)
