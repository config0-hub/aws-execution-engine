"""Worker execution logic — download, decrypt, run commands, callback."""

import glob
import json
import logging
import os
import signal
import subprocess
import tempfile
import zipfile
from typing import Optional

import boto3

from src.common import dynamodb, sops
from src.worker.callback import send_callback

logger = logging.getLogger(__name__)


def _download_and_extract(s3_location: str) -> str:
    """Download exec.zip from S3 and extract to temp directory."""
    work_dir = tempfile.mkdtemp(prefix="aws-exe-sys-worker-")

    # Parse s3://bucket/key
    parts = s3_location.replace("s3://", "").split("/", 1)
    bucket = parts[0]
    key = parts[1] if len(parts) > 1 else ""

    local_zip = os.path.join(work_dir, "exec.zip")
    s3_client = boto3.client("s3")
    s3_client.download_file(bucket, key, local_zip)

    with zipfile.ZipFile(local_zip, "r") as zf:
        zf.extractall(work_dir)
    os.unlink(local_zip)

    return work_dir


def _decrypt_and_load_env(work_dir: str) -> dict:
    """Find SOPS encrypted file, decrypt, and load env vars."""
    encrypted_path = os.path.join(work_dir, "secrets.enc.json")
    if not os.path.exists(encrypted_path):
        return {}

    # Primary: fetch SOPS key from SSM (set by init_job)
    sops_key_ssm_path = os.environ.get("SOPS_KEY_SSM_PATH", "")
    if sops_key_ssm_path:
        from src.common.sops import fetch_sops_key_ssm
        private_key_content = fetch_sops_key_ssm(sops_key_ssm_path)
        # Write to temp file for SOPS CLI
        key_file = tempfile.mktemp(suffix=".key")
        with open(key_file, "w") as f:
            f.write(private_key_content)
        sops_key = key_file
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

    # Load into os.environ
    for k, v in env_vars.items():
        os.environ[k] = str(v)

    return env_vars


def _setup_events_dir(trace_id: str) -> str:
    """Create the shared events directory and expose it via env var.

    Subprocesses write JSON event files here. After command execution,
    the main process reads them and transfers to DynamoDB.
    """
    events_dir = f"/tmp/share/{trace_id}/events"
    os.makedirs(events_dir, exist_ok=True)
    os.environ["AWS_EXE_SYS_EVENTS_DIR"] = events_dir
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


def _execute_commands(cmds: list, work_dir: str, timeout: int = 0) -> tuple:
    """Execute commands sequentially, capturing output.

    Returns (status, combined_log).
    """
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
                env=os.environ.copy(),
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


def run(s3_location: str, internal_bucket: str = "") -> str:
    """Main worker execution flow.

    1. Download and extract exec.zip
    2. Decrypt SOPS -> load env vars
    3. Execute commands
    4. Send callback

    Returns final status.
    """
    # 1. Download and extract
    work_dir = _download_and_extract(s3_location)

    # 2. Decrypt and load env vars
    env_vars = _decrypt_and_load_env(work_dir)

    # 3. Set up shared events directory for subprocess event reporting
    trace_id = env_vars.get("TRACE_ID", "")
    order_name = env_vars.get("ORDER_ID", "")
    flow_id = env_vars.get("FLOW_ID", "")
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
            send_callback(callback_url, "failed", "No commands found")
        return "failed"

    # 5. Execute
    timeout = int(env_vars.get("TIMEOUT", os.environ.get("TIMEOUT", "0")))
    status, log_output = _execute_commands(cmds, work_dir, timeout=timeout)

    # 6. Collect subprocess events and write to DynamoDB
    if events_dir and trace_id and order_name:
        _collect_and_write_events(events_dir, trace_id, order_name, flow_id, run_id)

    # 7. Callback
    callback_url = env_vars.get("CALLBACK_URL", "")
    if callback_url:
        send_callback(callback_url, status, log_output)
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
