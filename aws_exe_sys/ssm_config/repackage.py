"""Repackage SSM orders — package code, fetch credentials, no SOPS."""

import json
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
from aws_exe_sys.common import s3 as s3_ops
from aws_exe_sys.common.models import SsmJob, SsmOrder, format_order_num


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
    """Repackage all SSM orders via the code source registry.

    Dispatches each order through the registry exactly like the Lambda
    / CodeBuild variant. SSM orders frequently come in commands-only
    form (no git, no S3) — the registry handles that via
    :class:`CommandsOnlyCodeSource`.
    """
    results: List[Dict] = []
    sources = new_sources()
    try:
        for i, order in enumerate(job.orders):
            kind = detect_kind(order, job, sources)
            code_dir = sources[kind].fetch(order, job)
            results.append(_process_ssm_order(
                job=job,
                order=order,
                order_index=i,
                code_dir=code_dir,
                run_id=run_id,
                trace_id=trace_id,
                flow_id=flow_id,
                internal_bucket=internal_bucket,
            ))
    finally:
        for source in sources.values():
            source.cleanup()

    return results
