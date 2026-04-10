"""Upload repackaged orders to S3."""

from typing import Dict, List

from src.common import s3 as s3_ops


def upload_orders(
    repackaged_orders: List[Dict],
    run_id: str,
    bucket: str,
) -> None:
    """Upload each repackaged order's exec.zip to S3.

    Expected path: tmp/exec/<run_id>/<order_num>/exec.zip
    """
    for order_info in repackaged_orders:
        if order_info.get("zip_path") is None:
            continue
        s3_ops.upload_exec_zip(
            bucket=bucket,
            run_id=run_id,
            order_num=order_info["order_num"],
            file_path=order_info["zip_path"],
        )
