"""Shared code source operations — git clone, S3 fetch, credential retrieval, zip."""

import os
import shutil
import subprocess
import tempfile
import zipfile
from typing import Dict, List, Optional, Tuple

import boto3


def fetch_ssm_values(paths: List[str], region: Optional[str] = None) -> Dict[str, str]:
    """Fetch values from AWS SSM Parameter Store."""
    if not paths:
        return {}
    client = boto3.client("ssm", region_name=region)
    result = {}
    for path in paths:
        resp = client.get_parameter(Name=path, WithDecryption=True)
        # Use the last segment of the path as the env var name
        key = path.rsplit("/", 1)[-1].upper().replace("-", "_")
        result[key] = resp["Parameter"]["Value"]
    return result


def fetch_secret_values(paths: List[str], region: Optional[str] = None) -> Dict[str, str]:
    """Fetch values from AWS Secrets Manager."""
    if not paths:
        return {}
    client = boto3.client("secretsmanager", region_name=region)
    result = {}
    for path in paths:
        resp = client.get_secret_value(SecretId=path)
        key = path.rsplit("/", 1)[-1].upper().replace("-", "_")
        result[key] = resp["SecretString"]
    return result


def _strip_location_prefix(location: str) -> str:
    """Strip provider prefix from credential location.

    Supports formats like 'aws:::ssm:/path/to/param' -> '/path/to/param'.
    Plain SSM paths are returned as-is.
    """
    if location.startswith("aws:::ssm:"):
        return location[len("aws:::ssm:"):]
    return location


def resolve_git_credentials(
    token_location: str = "",
    ssh_key_location: Optional[str] = None,
    region: Optional[str] = None,
) -> Tuple[str, Optional[str]]:
    """Resolve git credential locations (SSM paths) to usable values.

    Args:
        token_location: SSM path containing git token
        ssh_key_location: SSM path containing SSH private key

    Returns (token, ssh_key_path) where ssh_key_path is a temp file if resolved.
    """
    token = ""
    ssh_key_path = None

    if token_location:
        vals = fetch_ssm_values([_strip_location_prefix(token_location)], region=region)
        if vals:
            token = list(vals.values())[0]

    if ssh_key_location:
        vals = fetch_ssm_values([_strip_location_prefix(ssh_key_location)], region=region)
        if vals:
            key_content = list(vals.values())[0]
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
) -> str:
    """Clone a git repo. HTTPS+token is primary; SSH is fallback.

    Args:
        repo: "org/repo" format
        token: GitHub token for HTTPS auth
        commit_hash: Optional specific commit to checkout
        ssh_key_path: Optional local path to SSH private key (fallback)
    """
    work_dir = tempfile.mkdtemp(prefix="aws-exe-sys-git-")
    depth = "2" if commit_hash else "1"

    # Primary: HTTPS with token
    if token:
        clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
        try:
            subprocess.run(
                ["git", "clone", "--depth", depth, clone_url, work_dir],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError:
            if ssh_key_path:
                # Fallback: SSH
                shutil.rmtree(work_dir, ignore_errors=True)
                work_dir = tempfile.mkdtemp(prefix="aws-exe-sys-git-")
                _clone_via_ssh(repo, ssh_key_path, work_dir, depth)
            else:
                raise
    elif ssh_key_path:
        # SSH only (no token available)
        _clone_via_ssh(repo, ssh_key_path, work_dir, depth)
    else:
        # Public repo -- unauthenticated HTTPS
        clone_url = f"https://github.com/{repo}.git"
        subprocess.run(
            ["git", "clone", "--depth", depth, clone_url, work_dir],
            check=True, capture_output=True, text=True,
        )

    if commit_hash:
        try:
            subprocess.run(
                ["git", "checkout", commit_hash],
                check=True, capture_output=True, text=True, cwd=work_dir,
            )
        except subprocess.CalledProcessError:
            # Commit not in shallow clone — fetch it directly
            subprocess.run(
                ["git", "fetch", "--depth", "1", "origin", commit_hash],
                check=True, capture_output=True, text=True, cwd=work_dir,
            )
            subprocess.run(
                ["git", "checkout", commit_hash],
                check=True, capture_output=True, text=True, cwd=work_dir,
            )

    return work_dir


def _clone_via_ssh(repo: str, ssh_key_path: str, work_dir: str, depth: str) -> None:
    """Clone via SSH with a specific key file."""
    ssh_url = f"git@github.com:{repo}.git"
    env = {
        **os.environ,
        "GIT_SSH_COMMAND": f"ssh -i {ssh_key_path} -o StrictHostKeyChecking=no",
    }
    subprocess.run(
        ["git", "clone", "--depth", depth, ssh_url, work_dir],
        check=True, capture_output=True, text=True, env=env,
    )


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


def group_git_orders(
    orders,
    job,
) -> Tuple[Dict[Tuple[str, Optional[str]], list], List[int]]:
    """Group git-sourced orders by (repo, commit_hash).

    Returns:
        git_groups: dict mapping (repo, commit_hash) -> list of (order_index, order)
        s3_indices: list of order indices that use S3 source
    """
    git_groups: Dict[Tuple[str, Optional[str]], list] = {}
    s3_indices: List[int] = []

    for i, order in enumerate(orders):
        if getattr(order, "s3_location", None):
            s3_indices.append(i)
        else:
            repo = getattr(order, "git_repo", None) or getattr(job, "git_repo", None) or ""
            commit = getattr(order, "commit_hash", None) or getattr(job, "commit_hash", None)
            key = (repo, commit)
            git_groups.setdefault(key, []).append((i, order))

    return git_groups, s3_indices


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
