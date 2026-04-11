"""Repackage orders with credentials and encrypted env vars."""

import os
import tempfile
from typing import Dict, List

from aws_exe_sys.common.bundler import OrderBundler
from aws_exe_sys.common.code_source import (
    fetch_ssm_values,
    fetch_secret_values,
    zip_directory,
)
from aws_exe_sys.common.code_sources import detect_kind, new_sources
from aws_exe_sys.common.models import Job, Order, format_order_num
from aws_exe_sys.common import s3 as s3_ops
from aws_exe_sys.common.sops import _generate_age_key, store_sops_key_ssm


def _process_order(
    job: Job,
    order: Order,
    order_index: int,
    code_dir: str,
    run_id: str,
    trace_id: str,
    flow_id: str,
    internal_bucket: str,
    sops_ttl_hours: int,
) -> Dict:
    """Process a single order: fetch credentials, bundle, zip."""
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

    # Generate SOPS keypair if not provided, store private key in SSM
    sops_key = order.sops_key
    sops_key_ssm_path = None
    if not sops_key:
        public_key, private_key_content, _key_file = _generate_age_key()
        sops_key = public_key
        sops_key_ssm_path = store_sops_key_ssm(
            run_id,
            order_num,
            private_key_content,
            ttl_hours=sops_ttl_hours,
        )

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
    """Repackage all orders via the code source registry.

    For each order the registry decides which source kind (git, s3, or
    commands-only) applies and delegates fetching to that source. Git
    clones are transparently shared across orders with the same
    ``(repo, commit_hash)`` — the cache lives on the ``GitCodeSource``
    instance returned by ``new_sources`` and is wiped by ``cleanup()``
    in the ``finally`` block.
    """
    # SOPS key TTL — computed once for the whole job so every order's
    # SSM parameter outlives the longest-running sibling. A 30-second
    # order can still be blocked by a 4-hour dependency, and its SOPS
    # key must still be fetchable when it finally runs. The +1 hour is
    # a safety margin on top of the ceiling.
    max_order_timeout = max((o.timeout for o in job.orders), default=0)
    sops_ttl_hours = max(job.job_timeout, max_order_timeout) // 3600 + 1

    results: List[Dict] = []
    sources = new_sources()
    try:
        for i, order in enumerate(job.orders):
            kind = detect_kind(order, job, sources)
            code_dir = sources[kind].fetch(order, job)
            results.append(_process_order(
                job=job,
                order=order,
                order_index=i,
                code_dir=code_dir,
                run_id=run_id,
                trace_id=trace_id,
                flow_id=flow_id,
                internal_bucket=internal_bucket,
                sops_ttl_hours=sops_ttl_hours,
            ))
    finally:
        for source in sources.values():
            source.cleanup()

    return results
