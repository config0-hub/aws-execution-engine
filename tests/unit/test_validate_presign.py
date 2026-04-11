"""Tests for presign_expiry validation in aws_exe_sys/init_job/validate.py.

A short presign_expiry paired with a long order.timeout means the callback
presigned URL expires before the worker can call back, producing a silent
failure. Reject these at submission.
"""

from aws_exe_sys.common.models import Job, Order
from aws_exe_sys.init_job.validate import validate_orders


def _job(orders, presign_expiry=7200):
    return Job(
        orders=orders,
        git_repo="org/repo",
        git_token_location="aws:::ssm:/token",
        username="testuser",
        presign_expiry=presign_expiry,
    )


def _order(timeout):
    return Order(
        cmds=["echo hi"],
        timeout=timeout,
        order_name=f"order-{timeout}",
    )


def test_presign_too_short():
    """presign_expiry=600, order.timeout=1800 — fails (600 < 1800 + 300)."""
    job = _job([_order(1800)], presign_expiry=600)
    errors = validate_orders(job)
    assert errors
    assert "presign_expiry" in errors[0]
    assert "1800" in errors[0]
    assert "600" in errors[0]


def test_presign_adequate():
    """presign_expiry=7200, order.timeout=300 — passes with room to spare."""
    job = _job([_order(300)], presign_expiry=7200)
    errors = validate_orders(job)
    assert errors == []


def test_presign_exact_max_plus_buffer():
    """presign_expiry equals max_timeout + 300s buffer — passes exactly at boundary."""
    job = _job([_order(1800)], presign_expiry=1800 + 300)
    errors = validate_orders(job)
    assert errors == []


def test_presign_one_short_of_buffer_fails():
    """presign_expiry one second below the max_timeout + 300s buffer — fails."""
    job = _job([_order(1800)], presign_expiry=1800 + 299)
    errors = validate_orders(job)
    assert errors
    assert "presign_expiry" in errors[0]


def test_presign_uses_max_of_multiple_orders():
    """When multiple orders exist, the longest timeout drives the requirement."""
    job = _job(
        [_order(300), _order(5000), _order(200)],
        presign_expiry=2000,  # fails because 5000 + 300 > 2000
    )
    errors = validate_orders(job)
    assert errors
    assert "5000" in errors[0]
