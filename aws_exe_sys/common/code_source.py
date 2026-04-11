"""Shared code source operations — git clone, S3 fetch, credential retrieval, zip.

Credential fetching is a thin facade over ``aws_exe_sys.common.credentials``. The
two legacy helpers — ``fetch_ssm_values`` and ``fetch_secret_values`` — keep
list-of-paths signatures for back-compat with existing call sites, but both
dispatch through the registry so third-party schemes can override them.

Git cloning dispatches through ``aws_exe_sys.common.vcs`` to the registered
provider for ``job.git_provider`` (default ``"github"``). ``github.com``
is no longer hardcoded anywhere in this module.
"""

import os
import shutil
import tempfile
import zipfile
from typing import Dict, List, Optional, Tuple

import boto3

from aws_exe_sys.common.credentials import (
    AwsSecretsManagerProvider,
    AwsSsmProvider,
    fetch_location,
    get_provider as _get_credential_provider,
)
from aws_exe_sys.common.vcs import get_provider as _get_vcs_provider


def fetch_ssm_values(paths: List[str], region: Optional[str] = None) -> Dict[str, str]:
    """Fetch values from AWS SSM Parameter Store via the credential registry.

    Each SSM parameter value is a base64-encoded JSON dict of env var
    key/value pairs. All decoded dicts are merged into a single result.

    Raises:
        ValueError: if the parameter value is not valid base64, or does
            not decode to a JSON dict. The error names the SSM path so
            operators can tell which parameter is misconfigured.
    """
    if not paths:
        return {}
    provider = _get_credential_provider(AwsSsmProvider.scheme)
    result: Dict[str, str] = {}
    for path in paths:
        result.update(provider.fetch(path, region=region))
    return result


def fetch_secret_values(paths: List[str], region: Optional[str] = None) -> Dict[str, str]:
    """Fetch values from AWS Secrets Manager via the credential registry.

    A secret's ``SecretString`` is interpreted in two ways:

    1. If it parses as a JSON dict, each key becomes an env var. This
       matches the common Secrets Manager convention of packing many
       fields (e.g. RDS credentials) into one secret.
    2. Otherwise (plain string, JSON list, scalar) the whole value is
       assigned to a single env var named after the path's last segment.
    """
    if not paths:
        return {}
    provider = _get_credential_provider(AwsSecretsManagerProvider.scheme)
    result: Dict[str, str] = {}
    for path in paths:
        result.update(provider.fetch(path, region=region))
    return result


def resolve_git_credentials(
    token_location: str = "",
    ssh_key_location: Optional[str] = None,
    region: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Resolve git credential locations into usable values.

    Both ``token_location`` and ``ssh_key_location`` are credential
    locations — ``vendor:::scheme:path`` URIs, explicit ``scheme:path``,
    or bare paths defaulting to AWS SSM. They are dispatched through the
    credential registry, so third-party providers work out of the box.

    Args:
        token_location: Credential location containing a git token. The
            fetched value must be a dict with at least one entry; the
            first value (insertion order) is used as the token.
        ssh_key_location: Credential location containing an SSH private
            key. The fetched value is written to a 0600 temp file.
        region: Optional AWS region (ignored by non-AWS providers).

    Returns:
        ``(token, ssh_key_path)`` where ``ssh_key_path`` is a local temp
        file path if an SSH key was resolved, else ``None``.
    """
    token = ""
    ssh_key_path = None

    if token_location:
        vals = fetch_location(token_location, region=region)
        if vals:
            token = next(iter(vals.values()))

    if ssh_key_location:
        vals = fetch_location(ssh_key_location, region=region)
        if vals:
            key_content = next(iter(vals.values()))
            ssh_key_path = tempfile.mktemp(suffix=".key", prefix="aws-exe-sys-ssh-")
            with open(ssh_key_path, "w") as f:
                f.write(key_content)
            os.chmod(ssh_key_path, 0o600)

    return token, ssh_key_path


def clone_repo(
    repo: str,
    token: str = "",
    commit_hash: Optional[str] = None,
    ssh_key_path: Optional[str] = None,
    provider: str = "github",
) -> str:
    """Clone a git repo via a registered VCS provider.

    HTTPS+token is primary; SSH is fallback. The host is owned by the
    provider — this function is provider-agnostic.

    Args:
        repo: Provider-scoped identifier (e.g. ``"org/repo"`` for GitHub).
        token: HTTPS auth token. Provider decides how to embed it.
        commit_hash: Optional commit to check out after clone.
        ssh_key_path: Optional local path to an SSH private key used as
            a fallback (or primary, when ``token`` is empty).
        provider: Short name of a registered VCS provider. Defaults to
            ``"github"``.

    Returns:
        The local work directory containing the cloned repo.
    """
    work_dir = tempfile.mkdtemp(prefix="aws-exe-sys-git-")
    vcs = _get_vcs_provider(provider)
    vcs.clone(
        repo=repo,
        dest=work_dir,
        token=token,
        commit_hash=commit_hash,
        ssh_key_path=ssh_key_path,
    )
    return work_dir


def extract_folder(clone_dir: str, folder: Optional[str] = None) -> str:
    """Copy a folder (or the entire repo) from a shared clone into an isolated temp dir.

    Each order needs its own copy because OrderBundler writes files in-place.
    Excludes .git directory to save space.
    """
    source = os.path.join(clone_dir, folder) if folder else clone_dir
    if not os.path.isdir(source):
        raise FileNotFoundError(
            f"Folder '{folder}' not found in cloned repo at {clone_dir}"
        )
    isolated_dir = tempfile.mkdtemp(prefix="aws-exe-sys-order-")
    shutil.copytree(source, isolated_dir, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(".git"))
    return isolated_dir


def fetch_code_s3(s3_location: str) -> str:
    """Download and extract a zip from S3. Returns path to extracted directory."""
    work_dir = tempfile.mkdtemp(prefix="aws-exe-sys-s3-")
    # Parse s3://bucket/key
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


def zip_directory(code_dir: str, output_path: str) -> str:
    """Zip a directory into output_path."""
    with zipfile.ZipFile(output_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for root, dirs, files in os.walk(code_dir):
            for f in files:
                full_path = os.path.join(root, f)
                arcname = os.path.relpath(full_path, code_dir)
                zf.write(full_path, arcname)
    return output_path
