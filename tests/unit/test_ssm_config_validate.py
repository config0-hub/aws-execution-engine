"""Unit tests for aws_exe_sys/ssm_config/validate.py."""

import pytest

from aws_exe_sys.common.models import SsmJob, SsmOrder
from aws_exe_sys.ssm_config.validate import validate_ssm_orders


def _make_job(orders=None, **kwargs):
    defaults = {
        "username": "testuser",
    }
    defaults.update(kwargs)
    return SsmJob(orders=orders or [], **defaults)


def _make_order(**kwargs):
    defaults = {
        "cmds": ["echo hello"],
        "timeout": 300,
        "ssm_targets": {"instance_ids": ["i-abc123"]},
    }
    defaults.update(kwargs)
    return SsmOrder(**defaults)


class TestValidateSsmOrders:
    def test_valid_with_instance_ids_passes(self):
        job = _make_job(orders=[
            _make_order(ssm_targets={"instance_ids": ["i-abc123"]}),
        ])
        errors = validate_ssm_orders(job)
        assert errors == []

    def test_valid_with_tags_passes(self):
        job = _make_job(orders=[
            _make_order(ssm_targets={"tags": [{"Key": "env", "Values": ["prod"]}]}),
        ])
        errors = validate_ssm_orders(job)
        assert errors == []

    def test_no_orders_fails(self):
        job = _make_job(orders=[])
        errors = validate_ssm_orders(job)
        assert len(errors) == 1
        assert "no orders" in errors[0].lower()

    def test_missing_cmds_fails(self):
        job = _make_job(orders=[_make_order(cmds=[])])
        errors = validate_ssm_orders(job)
        assert len(errors) == 1
        assert "cmds" in errors[0].lower()

    def test_missing_timeout_fails(self):
        job = _make_job(orders=[_make_order(timeout=0)])
        errors = validate_ssm_orders(job)
        assert len(errors) == 1
        assert "timeout" in errors[0].lower()

    def test_negative_timeout_fails(self):
        job = _make_job(orders=[_make_order(timeout=-1)])
        errors = validate_ssm_orders(job)
        assert len(errors) == 1
        assert "timeout" in errors[0].lower()

    def test_missing_ssm_targets_fails(self):
        job = _make_job(orders=[_make_order(ssm_targets={})])
        errors = validate_ssm_orders(job)
        assert len(errors) == 1
        assert "ssm_targets" in errors[0].lower()

    def test_empty_ssm_targets_no_ids_or_tags_fails(self):
        job = _make_job(orders=[
            _make_order(ssm_targets={"other_key": "value"}),
        ])
        errors = validate_ssm_orders(job)
        assert len(errors) == 1
        assert "instance_ids" in errors[0] or "tags" in errors[0]

    def test_ssm_targets_empty_instance_ids_fails(self):
        job = _make_job(orders=[
            _make_order(ssm_targets={"instance_ids": []}),
        ])
        errors = validate_ssm_orders(job)
        assert len(errors) == 1
        assert "instance_ids" in errors[0] or "tags" in errors[0]

    def test_no_code_source_is_ok(self):
        """SSM orders don't require git_repo or s3_location."""
        job = _make_job(
            git_repo=None,
            orders=[_make_order()],
        )
        errors = validate_ssm_orders(job)
        assert errors == []

    def test_fail_fast_returns_first_error(self):
        job = _make_job(orders=[
            _make_order(cmds=[]),       # invalid: missing cmds
            _make_order(timeout=0),     # also invalid: bad timeout
        ])
        errors = validate_ssm_orders(job)
        # Only returns first error (fail-fast)
        assert len(errors) == 1
        assert "cmds" in errors[0].lower()

    def test_order_name_in_error_message(self):
        job = _make_job(orders=[
            _make_order(order_name="deploy-ssm", cmds=[]),
        ])
        errors = validate_ssm_orders(job)
        assert "deploy-ssm" in errors[0]

    def test_multiple_valid_orders_pass(self):
        job = _make_job(orders=[
            _make_order(order_name="order-1"),
            _make_order(
                order_name="order-2",
                ssm_targets={"tags": [{"Key": "Name", "Values": ["web"]}]},
            ),
        ])
        errors = validate_ssm_orders(job)
        assert errors == []
