"""Repackage SSM orders — package code, fetch credentials, no SOPS."""

import json
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
from src.common import s3 as s3_ops
from src.common.models import SsmJob, SsmOrder, format_order_num


def _process_ssm_order(
    job: SsmJob,
    order: SsmOrder,
    order_index: int,
    code_dir: str,
    run_id: str,
    trace_id: str,
    flow_id: str,
    internal_bucket: str,
) -> Dict:
    """Process a single SSM order: fetch credentials, build env dict, zip code."""
    order_num = format_order_num(order_index)
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

    # Build merged env dict (no SOPS encryption)
    bundler = OrderBundler(
        run_id=run_id,
        order_id=order_name,
        order_num=order_num,
        trace_id=trace_id,
        flow_id=flow_id,
        env_vars=order.env_vars or {},
        ssm_values=ssm_values,
        secret_values=secret_values,
        callback_url=callback_url,
    )
    env_dict = bundler.build_env()

    # Write cmds.json and env_vars.json into code dir for the SSM document
    with open(os.path.join(code_dir, "cmds.json"), "w") as f:
        json.dump(order.cmds, f)
    with open(os.path.join(code_dir, "env_vars.json"), "w") as f:
        json.dump(env_dict, f)

    # Zip
    zip_path = os.path.join(tempfile.gettempdir(), f"{run_id}_{order_num}_exec.zip")
    zip_directory(code_dir, zip_path)

    return {
        "order_num": order_num,
        "order_name": order_name,
        "zip_path": zip_path,
        "callback_url": callback_url,
        "code_dir": code_dir,
        "env_dict": env_dict,
    }


def repackage_ssm_orders(
    job: SsmJob,
    run_id: str,
    trace_id: str,
    flow_id: str,
    internal_bucket: str,
) -> List[Dict]:
    """Repackage all SSM orders — package code, fetch credentials, no SOPS.

    Returns list of dicts with keys:
        order_num, order_name, zip_path, callback_url, code_dir, env_dict
    """
    results: List[Optional[Dict]] = [None] * len(job.orders)
    shared_clone_dirs: List[str] = []

    try:
        # Phase 1: Group git orders and clone once per unique (repo, commit_hash)
        git_groups, s3_indices = group_git_orders(job.orders, job)

        # Resolve git credentials once for all clones
        token, ssh_key_path = resolve_git_credentials(
            token_location=job.git_token_location or "",
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
                results[i] = _process_ssm_order(
                    job=job,
                    order=order,
                    order_index=i,
                    code_dir=code_dir,
                    run_id=run_id,
                    trace_id=trace_id,
                    flow_id=flow_id,
                    internal_bucket=internal_bucket,
                )

        # Phase 2: Process S3-sourced orders
        for i in s3_indices:
            order = job.orders[i]
            code_dir = fetch_code_s3(order.s3_location)
            results[i] = _process_ssm_order(
                job=job,
                order=order,
                order_index=i,
                code_dir=code_dir,
                run_id=run_id,
                trace_id=trace_id,
                flow_id=flow_id,
                internal_bucket=internal_bucket,
            )

        # Phase 3: SSM orders with no code source (commands-only)
        for i, order in enumerate(job.orders):
            if results[i] is not None:
                continue
            code_dir = tempfile.mkdtemp(prefix="aws-exe-sys-ssm-")
            results[i] = _process_ssm_order(
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
        for clone_dir in shared_clone_dirs:
            shutil.rmtree(clone_dir, ignore_errors=True)

    return results
