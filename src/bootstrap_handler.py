"""Bootstrap handler — downloads proprietary engine code at cold start.

This handler is the entry point for all engine Lambda functions. It:
1. Downloads engine code tarball via presigned URL
2. Extracts to /tmp/engine/
3. Extends sys.path and PATH
4. Delegates to the actual handler specified by ENGINE_HANDLER env var

Code source priority (presigned URL, plain HTTPS — no AWS credentials needed):
1. engine_code_url in event payload (presigned URL passed directly by dispatcher)
2. ENGINE_CODE_URL env var (presigned URL, for testing/overrides)
3. ENGINE_CODE_SSM_PATH env var → read presigned URL from SSM Parameter Store

On warm invocations, the code is already in /tmp/ — no re-download.
"""

import base64
import logging
import os
import sys
import tarfile
import urllib.request

logger = logging.getLogger(__name__)

CODE_DIR = "/tmp/engine"
_loaded = False


def _get_presigned_url_from_ssm(ssm_path: str) -> str:
    """Read base64-encoded presigned URL from SSM Parameter Store."""
    import boto3
    ssm = boto3.client("ssm")
    resp = ssm.get_parameter(Name=ssm_path, WithDecryption=True)
    url_b64 = resp["Parameter"]["Value"]
    return base64.b64decode(url_b64).decode("utf-8")


def _download_from_url(url: str, dest: str) -> None:
    """Download code tarball from a presigned URL (plain HTTPS)."""
    logger.info("Downloading engine code from presigned URL...")
    urllib.request.urlretrieve(url, dest)  # noqa: S310 — presigned S3 URL


def _bootstrap(event: dict | None) -> None:
    global _loaded
    if _loaded and os.path.exists(CODE_DIR):
        return

    tarball = "/tmp/engine-code.tar.gz"

    # Priority 1: Presigned URL from event payload (dispatcher passes it directly)
    code_url = None
    if isinstance(event, dict):
        code_url = event.get("engine_code_url")

    # Priority 2: Presigned URL from env var (testing/overrides)
    if not code_url:
        code_url = os.environ.get("ENGINE_CODE_URL")

    # Priority 3: Read presigned URL from SSM Parameter Store
    if not code_url:
        ssm_path = os.environ.get("ENGINE_CODE_SSM_PATH")
        if ssm_path:
            code_url = _get_presigned_url_from_ssm(ssm_path)

    if not code_url:
        raise RuntimeError(
            "No engine_code_url: pass engine_code_url in event, "
            "set ENGINE_CODE_URL env var, or set ENGINE_CODE_SSM_PATH"
        )
    _download_from_url(code_url, tarball)

    # Extract
    os.makedirs(CODE_DIR, exist_ok=True)
    with tarfile.open(tarball, "r:gz") as tar:
        tar.extractall(CODE_DIR, filter="data")  # noqa: S202 — trusted tarball from our own S3

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

    handler_module = os.environ.get("ENGINE_HANDLER", "src.init_job.handler")
    handler_func = os.environ.get("ENGINE_HANDLER_FUNC", "handler")

    module = __import__(handler_module, fromlist=[handler_func])
    actual_handler = getattr(module, handler_func)
    return actual_handler(event, context)
