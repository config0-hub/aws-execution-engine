"""Repackage orders with credentials and encrypted env vars."""

import os
import shutil
import tempfile
from typing import Dict, List, Optional

from src.common.bundler import OrderBundler
from src.common.code_source import (
    fetch_ssm_values,
    fetch_secret_values,
    clone_repo,
    extract_folder,
    group_git_orders,
    fetch_code_s3,
    zip_directory,
    resolve_git_credentials,
)
from src.common.models import Job, Order
from src.common import s3 as s3_ops
from src.common.sops import _generate_age_key, store_sops_key_ssm


def _process_order(
    job: Job,
    order: Order,
    order_index: int,
    code_dir: str,
    run_id: str,
    trace_id: str,
    flow_id: str,
    internal_bucket: str,
) -> Dict:
    """Process a single order: fetch credentials, bundle, zip."""
    order_num = str(order_index + 1).zfill(4)
    order_name = order.order_name or f"order-{order_num}"

    # Fetch credentials
    ssm_values = fetch_ssm_values(order.ssm_paths or [])
    secret_values = fetch_secret_values(order.secret_manager_paths or [])

    # Generate presigned callback URL
    callback_url = s3_ops.generate_callback_presigned_url(
        bucket=internal_bucket,
        run_id=run_id,
        order_num=order_num,
        expiry=job.presign_expiry,
    )

    # Generate SOPS keypair if not provided, store private key in SSM
    sops_key = order.sops_key
    sops_key_ssm_path = None
    if not sops_key:
        public_key, private_key_content, _key_file = _generate_age_key()
        sops_key = public_key
        sops_key_ssm_path = store_sops_key_ssm(run_id, order_num, private_key_content)

    # Build and encrypt with OrderBundler
    bundler = OrderBundler(
        run_id=run_id,
        order_id=order_name,
        order_num=order_num,
        trace_id=trace_id,
        flow_id=flow_id,
        cmds=order.cmds,
        env_vars=order.env_vars or {},
        ssm_values=ssm_values,
        secret_values=secret_values,
        callback_url=callback_url,
    )
    bundler.repackage(code_dir, sops_key=sops_key)

    # Re-zip
    zip_path = os.path.join(tempfile.gettempdir(), f"{run_id}_{order_num}_exec.zip")
    zip_directory(code_dir, zip_path)

    return {
        "order_num": order_num,
        "order_name": order_name,
        "zip_path": zip_path,
        "callback_url": callback_url,
        "code_dir": code_dir,
        "sops_key_ssm_path": sops_key_ssm_path,
    }


def repackage_orders(
    job: Job,
    run_id: str,
    trace_id: str,
    flow_id: str,
    internal_bucket: str,
) -> List[Dict]:
    """Repackage all orders with credentials and SOPS encryption.

    Groups git-sourced orders by (repo, commit_hash) to avoid redundant clones.

    Returns list of dicts with keys:
        order_num, order_name, zip_path, callback_url, code_dir
    """
    results: List[Optional[Dict]] = [None] * len(job.orders)
    shared_clone_dirs: List[str] = []

    try:
        # Phase 1: Group git orders and clone once per unique (repo, commit_hash)
        git_groups, s3_indices = group_git_orders(job.orders, job)

        # Resolve git credentials once for all clones
        token, ssh_key_path = resolve_git_credentials(
            token_location=job.git_token_location,
            ssh_key_location=job.git_ssh_key_location,
        )

        for (repo, commit_hash), order_entries in git_groups.items():
            clone_dir = clone_repo(
                repo=repo,
                token=token,
                commit_hash=commit_hash,
                ssh_key_path=ssh_key_path,
            )
            shared_clone_dirs.append(clone_dir)

            for i, order in order_entries:
                code_dir = extract_folder(clone_dir, order.git_folder)
                results[i] = _process_order(
                    job=job,
                    order=order,
                    order_index=i,
                    code_dir=code_dir,
                    run_id=run_id,
                    trace_id=trace_id,
                    flow_id=flow_id,
                    internal_bucket=internal_bucket,
                )

        # Phase 2: Process S3-sourced orders (unchanged)
        for i in s3_indices:
            order = job.orders[i]
            code_dir = fetch_code_s3(order.s3_location)
            results[i] = _process_order(
                job=job,
                order=order,
                order_index=i,
                code_dir=code_dir,
                run_id=run_id,
                trace_id=trace_id,
                flow_id=flow_id,
                internal_bucket=internal_bucket,
            )
    finally:
        # Clean up shared clone directories
        for clone_dir in shared_clone_dirs:
            shutil.rmtree(clone_dir, ignore_errors=True)

    return results
