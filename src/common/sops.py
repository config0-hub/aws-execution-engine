"""SOPS encryption/decryption for cross-account credential management."""

import json
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import boto3


def _run_cmd(cmd: list, env: Optional[dict] = None) -> str:
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


def _generate_age_key() -> Tuple[str, str, str]:
    """Generate a temporary age key pair.

    Returns (public_key, private_key_content, secret_key_file_path).
    """
    key_file = tempfile.mktemp(suffix=".key")
    _run_cmd(["age-keygen", "-o", key_file])
    with open(key_file, "r") as f:
        content = f.read()
    public_key = None
    for line in content.splitlines():
        if line.startswith("# public key:"):
            public_key = line.split(":", 1)[1].strip()
            break
    if not public_key:
        raise RuntimeError("Failed to extract public key from age-keygen output")
    return public_key, content, key_file


def store_sops_key_ssm(
    run_id: str,
    order_num: str,
    private_key: str,
    ttl_hours: int = 2,
) -> str:
    """Store SOPS age private key in SSM Parameter Store with auto-expiration.

    Uses advanced tier to support parameter policies (expiration).
    Returns the SSM parameter path.
    """
    ssm = boto3.client("ssm")
    prefix = os.environ.get("AWS_EXE_SYS_SSM_PREFIX", "exe-sys")
    path = f"/{prefix}/sops-keys/{run_id}/{order_num}"

    expiration = (
        datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
    ).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    ssm.put_parameter(
        Name=path,
        Value=private_key,
        Type="SecureString",
        Tier="Advanced",
        Policies=json.dumps([
            {
                "Type": "Expiration",
                "Version": "1.0",
                "Attributes": {
                    "Timestamp": expiration,
                },
            }
        ]),
        Overwrite=True,
    )
    return path


def fetch_sops_key_ssm(ssm_path: str) -> str:
    """Fetch SOPS age private key from SSM Parameter Store.

    Returns the private key string.
    """
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(Name=ssm_path, WithDecryption=True)
    return resp["Parameter"]["Value"]


def delete_sops_key_ssm(ssm_path: str) -> None:
    """Delete SOPS age private key from SSM (cleanup after job completion)."""
    ssm = boto3.client("ssm")
    try:
        ssm.delete_parameter(Name=ssm_path)
    except ssm.exceptions.ParameterNotFound:
        pass  # Already expired or deleted


def encrypt_env(
    env_vars: Dict[str, str],
    sops_key: Optional[str] = None,
) -> Tuple[str, str]:
    """Encrypt a dict of env vars with SOPS.

    If no sops_key provided, generates a temporary age key.
    Returns (path_to_encrypted_file, key_used).
    """
    # Write plaintext env vars as JSON
    plaintext_file = tempfile.mktemp(suffix=".json")
    with open(plaintext_file, "w") as f:
        json.dump(env_vars, f)

    encrypted_file = tempfile.mktemp(suffix=".enc.json")

    if sops_key is None:
        public_key, _private_key_content, key_file = _generate_age_key()
        sops_key = public_key
        env_extra = {"SOPS_AGE_KEY_FILE": key_file}
    else:
        env_extra = {}

    _run_cmd(
        [
            "sops",
            "--encrypt",
            "--age", sops_key,
            "--input-type", "json",
            "--output-type", "json",
            "--output", encrypted_file,
            plaintext_file,
        ],
        env=env_extra,
    )

    # Clean up plaintext
    os.unlink(plaintext_file)

    return encrypted_file, sops_key


def decrypt_env(
    encrypted_path: str,
    sops_key: str,
) -> Dict[str, str]:
    """Decrypt a SOPS file and return dict of env vars."""
    env_extra = {}
    # If it looks like an age key file path, set SOPS_AGE_KEY_FILE
    if os.path.isfile(sops_key):
        env_extra["SOPS_AGE_KEY_FILE"] = sops_key
    else:
        env_extra["SOPS_AGE_KEY"] = sops_key

    output = _run_cmd(
        [
            "sops",
            "--decrypt",
            "--input-type", "json",
            "--output-type", "json",
            encrypted_path,
        ],
        env=env_extra,
    )
    return json.loads(output)


def repackage_order(
    code_dir: str,
    env_vars: Dict[str, str],
    sops_key: Optional[str] = None,
) -> str:
    """Repackage an order directory with SOPS-encrypted env vars.

    Takes a flat dict of all env vars to encrypt (caller is responsible
    for assembling credentials, callback URLs, introspection fields, etc.).
    Encrypts with SOPS and writes metadata files.

    Returns path to the repackaged directory.
    """
    merged = dict(env_vars)

    # Encrypt with SOPS
    encrypted_file, key_used = encrypt_env(merged, sops_key)

    # Copy encrypted file to code_dir
    dest_encrypted = os.path.join(code_dir, "secrets.enc.json")
    os.rename(encrypted_file, dest_encrypted)

    # Write env_vars.env — plaintext var names only (no values)
    env_file = os.path.join(code_dir, "env_vars.env")
    with open(env_file, "w") as f:
        for key in sorted(merged.keys()):
            f.write(f"{key}\n")

    return code_dir
