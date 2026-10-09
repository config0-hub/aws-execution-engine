"""SOPS encryption/decryption for packaged environment variables.

Engine-side only: fetch, decrypt, delete. Supports both age+SSM and KMS paths
via an explicit sops_type dispatcher.
"""

import contextlib
import json
import os
import subprocess
import time

import boto3
from botocore.exceptions import ClientError

from aws_exe_sys.common.subprocess_runner import (
    ExecutionTimedOut,
    boto_config_until,
    run_until_deadline,
    until_deadline,
)


class SopsKeyExpired(Exception):
    """Raised when the SOPS age private key cannot be retrieved from SSM.

    SSM advanced-tier parameters store the SOPS key with an Expiration
    policy. Once the timestamp passes, SSM deletes the parameter, and
    `get_parameter` raises `ParameterNotFound`. This domain exception lets
    callers (in particular the worker) distinguish "the key is gone, bail
    out fast with a specific callback" from generic boto3 errors.
    """


def _run_cmd(cmd: list, env: dict | None = None, *, deadline: float) -> str:
    """Run a subprocess command under the execution deadline and return stdout.

    The command shares the worker's one whole-execution deadline: it is not
    started once the deadline has passed, and it is killed (with its process
    group) when the deadline passes while it runs.
    """
    if time.monotonic() >= deadline:
        raise ExecutionTimedOut(f"deadline passed before {cmd[0]} started", [])
    returncode, stdout, stderr, timed_out = run_until_deadline(
        cmd,
        deadline=deadline,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={**os.environ, **(env or {})},
    )
    if timed_out:
        raise ExecutionTimedOut(f"deadline passed while {cmd[0]} was running; killed", [])
    if returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{stderr}")
    return stdout


def fetch_sops_key_ssm(ssm_path: str, *, deadline: float) -> str:
    """Fetch SOPS age private key from SSM Parameter Store, no later than ``deadline``.

    Returns the private key string.

    Raises:
        SopsKeyExpired: if the SSM parameter no longer exists (expired by
            the Expiration policy or manually deleted).
        ExecutionTimedOut: the deadline passed while the key was fetched.
    """
    ssm = boto3.client("ssm", config=boto_config_until(deadline))
    try:
        with until_deadline("fetching the SOPS key from SSM"):
            resp = ssm.get_parameter(Name=ssm_path, WithDecryption=True)
    except ssm.exceptions.ParameterNotFound as exc:
        raise SopsKeyExpired(f"SOPS key at SSM path {ssm_path!r} is missing or expired") from exc
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code in ("ParameterNotFound", "AccessDeniedException"):
            raise SopsKeyExpired(
                f"SOPS key at SSM path {ssm_path!r} is missing or expired (AWS error code: {error_code})"
            ) from exc
        raise
    return resp["Parameter"]["Value"]


def delete_sops_key_ssm(ssm_path: str, *, deadline: float) -> None:
    """Delete SOPS age private key from SSM (cleanup after decryption), no later than ``deadline``."""
    ssm = boto3.client("ssm", config=boto_config_until(deadline))
    with (
        contextlib.suppress(ssm.exceptions.ParameterNotFound),  # Already expired or deleted if not found
        until_deadline("deleting the SOPS key from SSM"),
    ):
        ssm.delete_parameter(Name=ssm_path)


def decrypt_env(
    encrypted_path: str,
    sops_key: str,
    *,
    deadline: float,
) -> dict[str, str]:
    """Decrypt a SOPS file using an age key and return dict of env vars."""
    env_extra = {}
    if os.path.isfile(sops_key):
        env_extra["SOPS_AGE_KEY_FILE"] = sops_key
    else:
        env_extra["SOPS_AGE_KEY"] = sops_key

    output = _run_cmd(
        [
            "sops",
            "--decrypt",
            "--input-type",
            "json",
            "--output-type",
            "json",
            encrypted_path,
        ],
        env=env_extra,
        deadline=deadline,
    )
    return json.loads(output)


def decrypt_with_kms(encrypted_path: str, *, deadline: float) -> dict[str, str]:
    """Decrypt a SOPS file using KMS (ARN embedded in the SOPS file metadata).

    Calls ``sops --decrypt`` directly — no key parameter needed because the
    KMS ARN is stored inside the encrypted file's SOPS metadata.

    Returns a dict of decrypted env vars.
    """
    output = _run_cmd(
        [
            "sops",
            "--decrypt",
            "--input-type",
            "json",
            "--output-type",
            "json",
            encrypted_path,
        ],
        deadline=deadline,
    )
    return json.loads(output)


def handle_sops(
    work_dir: str,
    sops_type: str | None = None,
    sops_path: str | None = None,
    *,
    deadline: float,
) -> dict[str, str]:
    """Top-level SOPS dispatcher.

    Args:
        work_dir: Working directory containing the encrypted secrets file.
        sops_type: One of "ssm" (age key via SSM), "kms" (direct KMS decrypt),
            or None (skip decryption).
        sops_path: SSM parameter path for the age key (required when
            sops_type="ssm").
        deadline: ``time.monotonic()`` value of the worker's whole-execution
            deadline; the SSM key fetch and delete end at it and
            ``sops --decrypt`` is killed when it passes.

    Returns:
        Dict of decrypted env vars, or empty dict if sops_type is None.
    """
    if sops_type is None:
        return {}

    encrypted_path = os.path.join(work_dir, "secrets.enc.json")

    if sops_type == "ssm":
        if not sops_path:
            raise ValueError("sops_path is required when sops_type is 'ssm'")
        age_key = fetch_sops_key_ssm(sops_path, deadline=deadline)
        decrypted = decrypt_env(encrypted_path, age_key, deadline=deadline)
        delete_sops_key_ssm(sops_path, deadline=deadline)
        return decrypted

    if sops_type == "kms":
        return decrypt_with_kms(encrypted_path, deadline=deadline)

    raise ValueError(f"Unknown sops_type: {sops_type!r}")
