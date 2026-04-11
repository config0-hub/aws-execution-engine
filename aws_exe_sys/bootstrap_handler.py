"""Bootstrap handler — downloads proprietary engine code at cold start.

This handler is the entry point for engine Lambda functions when
`var.engine_code_source.kind != "inline"`. It:
1. Resolves a (url, sha256) pair from event payload, env vars, or SSM
2. Downloads the engine code tarball and verifies its SHA256 integrity
3. Extracts to /tmp/engine/, extends sys.path and PATH
4. Delegates to the actual handler specified by ENGINE_HANDLER env var

Code source priority (all three produce an integrity-verified dict):
1. `event["engine_code"]` — dict {"url": str, "sha256": str}
2. Env vars ENGINE_CODE_URL + ENGINE_CODE_SHA256 (both required together)
3. ENGINE_CODE_SSM_PATH env var → SSM parameter holding base64-encoded JSON
   payload {"url": "...", "sha256": "..."}

Greenfield: no legacy shapes accepted. Plain-string URLs, missing sha256,
or a base64'd URL in SSM all raise BootstrapIntegrityError at cold start.

On warm invocations the code is already in /tmp/ — no re-download.
"""

import base64
import binascii
import hashlib
import json
import logging
import os
import sys
import tarfile
import urllib.request

logger = logging.getLogger(__name__)

CODE_DIR = "/tmp/engine"
_loaded = False


class BootstrapIntegrityError(Exception):
    """Raised when engine code cannot be loaded safely (shape, SHA, or source)."""


def _parse_ssm_payload(ssm_path: str, raw_value: str) -> tuple[str, str]:
    """Decode an SSM parameter value into (url, sha256).

    Expected shape: base64-encoded JSON `{"url": "...", "sha256": "..."}`.
    """
    try:
        decoded = base64.b64decode(raw_value, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError) as e:
        raise BootstrapIntegrityError(
            f"SSM value at {ssm_path} is not valid base64: {e}"
        ) from e
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError as e:
        raise BootstrapIntegrityError(
            f"SSM value at {ssm_path} must be base64-encoded JSON dict "
            f"(got non-JSON after decode): {e}"
        ) from e
    if not isinstance(payload, dict):
        raise BootstrapIntegrityError(
            f"SSM value at {ssm_path} must be a base64-encoded JSON dict, "
            f"got {type(payload).__name__}"
        )
    url = payload.get("url")
    sha = payload.get("sha256")
    if not isinstance(url, str) or not url:
        raise BootstrapIntegrityError(
            f"SSM value at {ssm_path} missing required field 'url'"
        )
    if not isinstance(sha, str) or not sha:
        raise BootstrapIntegrityError(
            f"SSM value at {ssm_path} missing required field 'sha256'"
        )
    return url, sha


def _get_code_source_from_ssm(ssm_path: str) -> tuple[str, str]:
    """Read and parse the engine code source from SSM Parameter Store."""
    import boto3  # local import — keeps cold-start lean when unused
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(Name=ssm_path, WithDecryption=True)
    raw_value = resp["Parameter"]["Value"]
    return _parse_ssm_payload(ssm_path, raw_value)


def _resolve_code_source(event: dict | None) -> tuple[str, str]:
    """Return (url, sha256). Raises BootstrapIntegrityError on malformed input."""
    # Priority 1: event payload
    if isinstance(event, dict):
        if "engine_code_url" in event:
            raise BootstrapIntegrityError(
                "event.engine_code_url (plain string) is not supported. "
                "Pass event.engine_code = {'url': ..., 'sha256': ...}"
            )
        code = event.get("engine_code")
        if code is not None:
            if not isinstance(code, dict):
                raise BootstrapIntegrityError(
                    "event.engine_code must be a dict with 'url' and 'sha256'"
                )
            url = code.get("url")
            sha = code.get("sha256")
            if not isinstance(url, str) or not url:
                raise BootstrapIntegrityError(
                    "event.engine_code missing required field 'url'"
                )
            if not isinstance(sha, str) or not sha:
                raise BootstrapIntegrityError(
                    "event.engine_code missing required field 'sha256'"
                )
            return url, sha

    # Priority 2: env vars (both required together)
    env_url = os.environ.get("ENGINE_CODE_URL")
    env_sha = os.environ.get("ENGINE_CODE_SHA256")
    if env_url or env_sha:
        if not env_url:
            raise BootstrapIntegrityError(
                "ENGINE_CODE_SHA256 is set but ENGINE_CODE_URL is missing"
            )
        if not env_sha:
            raise BootstrapIntegrityError(
                "ENGINE_CODE_URL is set but ENGINE_CODE_SHA256 is missing"
            )
        return env_url, env_sha

    # Priority 3: SSM parameter path
    ssm_path = os.environ.get("ENGINE_CODE_SSM_PATH")
    if ssm_path:
        return _get_code_source_from_ssm(ssm_path)

    raise RuntimeError(
        "No engine code source: set event.engine_code, "
        "ENGINE_CODE_URL + ENGINE_CODE_SHA256 env vars, "
        "or ENGINE_CODE_SSM_PATH"
    )


def _download_and_verify(url: str, expected_sha256: str, dest: str) -> None:
    """Download tarball, verify SHA256, raise on mismatch (before extraction)."""
    logger.info("Downloading engine code tarball...")
    urllib.request.urlretrieve(url, dest)  # noqa: S310 — presigned URL, integrity verified below

    hasher = hashlib.sha256()
    with open(dest, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            hasher.update(chunk)
    actual = hasher.hexdigest()
    if actual != expected_sha256:
        # Log to stderr for CloudWatch visibility even if logging isn't configured
        sys.stderr.write(
            f"bootstrap_handler: SHA256 mismatch — "
            f"expected {expected_sha256}, got {actual}\n"
        )
        raise BootstrapIntegrityError(
            f"engine code sha256 mismatch: expected {expected_sha256}, got {actual}"
        )


def _bootstrap(event: dict | None) -> None:
    global _loaded
    if _loaded and os.path.exists(CODE_DIR):
        return

    tarball = "/tmp/engine-code.tar.gz"
    url, expected_sha256 = _resolve_code_source(event)
    _download_and_verify(url, expected_sha256, tarball)

    # Extract (integrity verified above)
    os.makedirs(CODE_DIR, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tar:
        tar.extractall(CODE_DIR, filter="data")  # noqa: S202 — sha256-verified tarball

    # Extend Python path
    sys.path.insert(0, CODE_DIR)

    # Extend PATH for Go/system binaries
    bin_dir = os.path.join(CODE_DIR, "bin")
    if os.path.isdir(bin_dir):
        os.environ["PATH"] = f"{bin_dir}:{os.environ.get('PATH', '')}"

    logger.info("Engine code loaded at %s", CODE_DIR)
    _loaded = True


def handler(event: dict, context: object) -> dict:
    """Lambda entry point — bootstrap engine code then delegate."""
    _bootstrap(event)

    handler_module = os.environ.get("ENGINE_HANDLER", "aws_exe_sys.init_job.handler")
    handler_func = os.environ.get("ENGINE_HANDLER_FUNC", "handler")

    module = __import__(handler_module, fromlist=[handler_func])
    actual_handler = getattr(module, handler_func)
    return actual_handler(event, context)
