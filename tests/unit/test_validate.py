"""Unit tests for src/init_job/validate.py."""

import pytest

from src.common.models import Job, Order
from src.init_job.validate import validate_orders


def _make_job(orders=None, **kwargs):
    defaults = {
        "git_repo": "org/repo",
        "git_token_location": "aws:::ssm:/token",
        "username": "testuser",
    }
    defaults.update(kwargs)
    return Job(orders=orders or [], **defaults)


def _make_order(**kwargs):
    defaults = {
        "cmds": ["echo hello"],
        "timeout": 300,
    }
    defaults.update(kwargs)
    return Order(**defaults)


class TestValidateOrders:
    def test_valid_orders_pass(self):
        job = _make_job(orders=[_make_order(), _make_order(order_name="deploy-rds")])
        errors = validate_orders(job)
        assert errors == []

    def test_no_orders_fails(self):
        job = _make_job(orders=[])
        errors = validate_orders(job)
        assert len(errors) == 1
        assert "no orders" in errors[0].lower()

    def test_missing_cmds_fails(self):
        job = _make_job(orders=[_make_order(cmds=[])])
        errors = validate_orders(job)
        assert len(errors) == 1
        assert "cmds" in errors[0].lower()

    def test_missing_timeout_fails(self):
        job = _make_job(orders=[_make_order(timeout=0)])
        errors = validate_orders(job)
        assert len(errors) == 1
        assert "timeout" in errors[0].lower()

    def test_missing_code_source_fails(self):
        job = _make_job(
            git_repo="",
            git_token_location="",
            orders=[_make_order()],
        )
        errors = validate_orders(job)
        assert len(errors) == 1
        assert "code source" in errors[0].lower()

    def test_s3_location_satisfies_code_source(self):
        job = _make_job(
            git_repo="",
            git_token_location="",
            orders=[_make_order(s3_location="s3://bucket/code.zip")],
        )
        errors = validate_orders(job)
        assert errors == []

    def test_fail_fast_returns_first_error(self):
        job = _make_job(orders=[
            _make_order(cmds=[]),  # invalid
            _make_order(timeout=0),  # also invalid
        ])
        errors = validate_orders(job)
        # Only returns first error (fail-fast)
        assert len(errors) == 1
        assert "cmds" in errors[0].lower()

    def test_order_name_in_error_message(self):
        job = _make_job(orders=[_make_order(order_name="deploy-vpc", cmds=[])])
        errors = validate_orders(job)
        assert "deploy-vpc" in errors[0]

    def test_invalid_execution_target_fails(self):
        job = _make_job(orders=[_make_order(execution_target="kubernetes")])
        errors = validate_orders(job)
        assert len(errors) == 1
        assert "execution_target" in errors[0].lower()
        assert "kubernetes" in errors[0]

    def test_valid_execution_target_lambda(self):
        job = _make_job(orders=[_make_order(execution_target="lambda")])
        errors = validate_orders(job)
        assert errors == []

    def test_valid_execution_target_codebuild(self):
        job = _make_job(orders=[_make_order(execution_target="codebuild")])
        errors = validate_orders(job)
        assert errors == []

    def test_valid_execution_target_ssm(self):
        job = _make_job(orders=[_make_order(execution_target="ssm")])
        errors = validate_orders(job)
        assert errors == []
