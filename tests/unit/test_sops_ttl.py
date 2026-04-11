"""P3-3: SOPS key TTL coordination at repackage_orders.

Every order in a job shares one SOPS bundle lifecycle — the longest
running sibling (or the job-level timeout, whichever is larger) floors
the TTL for ALL orders. ``store_sops_key_ssm`` must be called with
``ttl_hours = max(job.job_timeout, max_order_timeout) // 3600 + 1``.

The +1 is a safety margin so that even a job whose max timeout sits
exactly on an hour boundary gets one extra hour of SSM parameter
lifetime beyond the ceiling.
"""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from aws_exe_sys.common.models import Job, Order
from aws_exe_sys.init_job.repackage import _process_order, repackage_orders


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


def _patched_repackage(job):
    """Run repackage_orders with every external dependency mocked out,
    returning the list of ttl_hours kwargs passed to store_sops_key_ssm.
    """
    observed_ttls = []

    def _fake_store(run_id, order_num, private_key, ttl_hours):
        observed_ttls.append(ttl_hours)
        return f"/aws-exe-sys/sops-keys/{run_id}/{order_num}"

    with tempfile.TemporaryDirectory() as clone_dir:
        with open(os.path.join(clone_dir, "main.tf"), "w") as f:
            f.write("resource {}")

        with patch(
            "aws_exe_sys.init_job.repackage.store_sops_key_ssm",
            side_effect=_fake_store,
        ), patch(
            "aws_exe_sys.init_job.repackage._generate_age_key",
            return_value=("age1pub", "AGE-SECRET", "/tmp/k"),
        ), patch(
            "aws_exe_sys.common.code_sources.git.resolve_git_credentials",
            return_value=("tok", None),
        ), patch(
            "aws_exe_sys.common.code_sources.git.clone_repo",
            return_value=clone_dir,
        ), patch(
            "aws_exe_sys.init_job.repackage.fetch_ssm_values",
            return_value={},
        ), patch(
            "aws_exe_sys.init_job.repackage.fetch_secret_values",
            return_value={},
        ), patch(
            "aws_exe_sys.init_job.repackage.s3_ops.generate_callback_presigned_url",
            return_value="https://presigned.url",
        ), patch(
            "aws_exe_sys.init_job.repackage.OrderBundler",
            return_value=MagicMock(),
        ):
            repackage_orders(
                job=job,
                run_id="run-1",
                trace_id="abc123",
                flow_id="user:abc123-exec",
                internal_bucket="test-bucket",
            )

    return observed_ttls


class TestSopsTtlCoordination:

    def test_ttl_scales_with_longest_order(self):
        """job_timeout=3600, max order timeout=14400 -> TTL=5 hours.

        Formula: max(3600, 14400) // 3600 + 1 = 4 + 1 = 5.
        """
        job = _make_job(
            job_timeout=3600,
            orders=[_make_order(timeout=14400)],
        )
        ttls = _patched_repackage(job)
        assert ttls == [5]

    def test_ttl_uses_job_timeout_when_larger(self):
        """job_timeout=10800 dominates a small order timeout.

        Formula: max(10800, 300) // 3600 + 1 = 3 + 1 = 4 hours.
        """
        job = _make_job(
            job_timeout=10800,
            orders=[_make_order(timeout=300)],
        )
        ttls = _patched_repackage(job)
        assert ttls == [4]

    def test_ttl_floor_is_one_hour_above_max(self):
        """Sub-1h timeouts still receive the +1 safety margin.

        job_timeout=1800, order timeout=600 -> max(1800,600)=1800
        -> 1800 // 3600 = 0, + 1 = 1 hour TTL floor.
        """
        job = _make_job(
            job_timeout=1800,
            orders=[_make_order(timeout=600)],
        )
        ttls = _patched_repackage(job)
        assert ttls == [1]

    def test_ttl_passed_to_store_sops_key_ssm(self):
        """Direct unit on _process_order: the computed ttl_hours param
        is threaded verbatim to store_sops_key_ssm as a kwarg.
        """
        job = _make_job(
            job_timeout=3600,
            orders=[_make_order(timeout=7200)],
        )
        order = job.orders[0]

        with tempfile.TemporaryDirectory() as clone_dir, patch(
            "aws_exe_sys.init_job.repackage.store_sops_key_ssm",
            return_value="/aws-exe-sys/sops-keys/run-1/0001",
        ) as mock_store, patch(
            "aws_exe_sys.init_job.repackage._generate_age_key",
            return_value=("age1pub", "AGE-SECRET", "/tmp/k"),
        ), patch(
            "aws_exe_sys.init_job.repackage.fetch_ssm_values", return_value={},
        ), patch(
            "aws_exe_sys.init_job.repackage.fetch_secret_values", return_value={},
        ), patch(
            "aws_exe_sys.init_job.repackage.s3_ops.generate_callback_presigned_url",
            return_value="https://cb.url",
        ), patch(
            "aws_exe_sys.init_job.repackage.OrderBundler",
            return_value=MagicMock(),
        ):
            _process_order(
                job=job,
                order=order,
                order_index=0,
                code_dir=clone_dir,
                run_id="run-1",
                trace_id="t1",
                flow_id="f1",
                internal_bucket="test-bucket",
                sops_ttl_hours=3,
            )

        mock_store.assert_called_once()
        assert mock_store.call_args.kwargs.get("ttl_hours") == 3

    def test_ttl_uses_max_across_multiple_orders(self):
        """All orders in a job with timeouts [300, 7200, 600] must see
        the same TTL computed from 7200 — max(3600, 7200)//3600 + 1 = 3.
        """
        job = _make_job(
            job_timeout=3600,
            orders=[
                _make_order(timeout=300),
                _make_order(timeout=7200),
                _make_order(timeout=600),
            ],
        )
        ttls = _patched_repackage(job)
        assert len(ttls) == 3
        assert ttls == [3, 3, 3]
