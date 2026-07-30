"""SOPS encryption/decryption for packaged environment variables.

Engine-side only: fetch, decrypt, delete. Supports both age+SSM and KMS paths
via an explicit sops_type dispatcher.
"""

import contextlib
import json
import os
import subprocess

import boto3
from botocore.exceptions import ClientError


class SopsKeyExpired(Exception):
    """Raised when the SOPS age private key cannot be retrieved from SSM.

    SSM advanced-tier parameters store the SOPS key with an Expiration
    policy. Once the timestamp passes, SSM deletes the parameter, and
    `get_parameter` raises `ParameterNotFound`. This domain exception lets
    callers (in particular the worker) distinguish "the key is gone, bail
    out fast with a specific callback" from generic boto3 errors.
    """


def _run_cmd(cmd: list, env: dict | None = None) -> str:
    """Run a subprocess command and return stdout."""
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    if result.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n{result.stderr}")
    return result.stdout


def fetch_sops_key_ssm(ssm_path: str) -> str:
    """Fetch SOPS age private key from SSM Parameter Store.

    Returns the private key string.

    Raises:
        SopsKeyExpired: if the SSM parameter no longer exists (expired by
            the Expiration policy or manually deleted).
    """
    ssm = boto3.client("ssm")
    try:
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


def delete_sops_key_ssm(ssm_path: str) -> None:
    """Delete SOPS age private key from SSM (cleanup after job completion)."""
    ssm = boto3.client("ssm")
    with contextlib.suppress(ssm.exceptions.ParameterNotFound):
        ssm.delete_parameter(Name=ssm_path)  # Already expired or deleted if not found


def decrypt_env(
    encrypted_path: str,
    sops_key: str,
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
    )
    return json.loads(output)


def decrypt_with_kms(encrypted_path: str) -> dict[str, str]:
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
    )
    return json.loads(output)


def handle_sops(
    work_dir: str,
    sops_type: str | None = None,
    sops_path: str | None = None,
) -> dict[str, str]:
    """Top-level SOPS dispatcher.

    Args:
        work_dir: Working directory containing the encrypted secrets file.
        sops_type: One of "ssm" (age key via SSM), "kms" (direct KMS decrypt),
            or None (skip decryption).
        sops_path: SSM parameter path for the age key (required when
            sops_type="ssm").

    Returns:
        Dict of decrypted env vars, or empty dict if sops_type is None.
    """
    if sops_type is None:
        return {}

    encrypted_path = os.path.join(work_dir, "secrets.enc.json")

    if sops_type == "ssm":
        if not sops_path:
            raise ValueError("sops_path is required when sops_type is 'ssm'")
        age_key = fetch_sops_key_ssm(sops_path)
        decrypted = decrypt_env(encrypted_path, age_key)
        delete_sops_key_ssm(sops_path)
        return decrypted

    if sops_type == "kms":
        return decrypt_with_kms(encrypted_path)

    raise ValueError(f"Unknown sops_type: {sops_type!r}")
