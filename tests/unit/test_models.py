"""Unit tests for aws_exe_sys/common/models.py."""

import json
import base64
from dataclasses import dataclass

from aws_exe_sys.common.models import (
    DictMixin,
    BaseOrder,
    BaseJob,
    Order,
    SsmOrder,
    Job,
    SsmJob,
    OrderEvent,
    LockRecord,
    OrderRecord,
    format_order_num,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    FAILED,
    TIMED_OUT,
    JOB_ORDER_NAME,
    EXECUTION_TARGETS,
)


class TestStatusConstants:
    def test_status_values(self):
        assert QUEUED == "queued"
        assert RUNNING == "running"
        assert SUCCEEDED == "succeeded"
        assert FAILED == "failed"
        assert TIMED_OUT == "timed_out"

    def test_job_order_name(self):
        assert JOB_ORDER_NAME == "_job"

    def test_execution_targets(self):
        assert EXECUTION_TARGETS == frozenset({"lambda", "codebuild", "ssm"})


class TestOrder:
    def test_create_minimal(self):
        order = Order(cmds=["echo hello"], timeout=300)
        assert order.cmds == ["echo hello"]
        assert order.timeout == 300
        assert order.execution_target == "codebuild"
        assert order.must_succeed is True
        assert order.dependencies is None
        assert order.ssm_targets is None

    def test_create_full(self):
        order = Order(
            cmds=["cmd1", "cmd2"],
            timeout=600,
            order_name="deploy-vpc",
            git_repo="org/repo",
            git_folder="terraform/vpc",
            env_vars={"KEY": "VALUE"},
            ssm_paths=["/path/1"],
            secret_manager_paths=["secret/1"],
            execution_target="lambda",
            queue_id="vpc-plan",
            dependencies=["dep-1"],
            must_succeed=False,
            ssm_targets={"instance_ids": ["i-abc123"]},
        )
        assert order.order_name == "deploy-vpc"
        assert order.execution_target == "lambda"
        assert order.must_succeed is False
        assert order.ssm_targets == {"instance_ids": ["i-abc123"]}

    def test_to_dict(self):
        order = Order(cmds=["echo hi"], timeout=300)
        d = order.to_dict()
        assert "cmds" in d
        assert "timeout" in d
        # None fields should be excluded
        assert "order_name" not in d
        assert "env_vars" not in d

    def test_from_dict(self):
        data = {
            "cmds": ["echo test"],
            "timeout": 120,
            "order_name": "test-order",
            "execution_target": "lambda",
            "unknown_field": "ignored",
        }
        order = Order.from_dict(data)
        assert order.cmds == ["echo test"]
        assert order.timeout == 120
        assert order.order_name == "test-order"
        assert order.execution_target == "lambda"

    def test_commit_hash_field(self):
        order = Order(cmds=["echo"], timeout=300, commit_hash="abc123")
        assert order.commit_hash == "abc123"

    def test_commit_hash_in_to_dict(self):
        order = Order(cmds=["echo"], timeout=300, commit_hash="abc123")
        d = order.to_dict()
        assert d["commit_hash"] == "abc123"

    def test_commit_hash_excluded_when_none(self):
        order = Order(cmds=["echo"], timeout=300)
        d = order.to_dict()
        assert "commit_hash" not in d

    def test_to_dict_from_dict_roundtrip(self):
        order = Order(
            cmds=["cmd1"],
            timeout=300,
            order_name="test",
            env_vars={"A": "B"},
        )
        d = order.to_dict()
        restored = Order.from_dict(d)
        assert restored.cmds == order.cmds
        assert restored.timeout == order.timeout
        assert restored.order_name == order.order_name
        assert restored.env_vars == order.env_vars

    def test_execution_target_ssm(self):
        order = Order(cmds=["echo"], timeout=300, execution_target="ssm")
        assert order.execution_target == "ssm"

    def test_ssm_targets_field(self):
        targets = {"instance_ids": ["i-abc123", "i-def456"]}
        order = Order(cmds=["echo"], timeout=300, ssm_targets=targets)
        assert order.ssm_targets == targets

    def test_ssm_targets_with_tags(self):
        targets = {"tags": {"Environment": "prod", "Role": "web"}}
        order = Order(cmds=["echo"], timeout=300, ssm_targets=targets)
        assert order.ssm_targets["tags"]["Environment"] == "prod"

    def test_ssm_targets_in_to_dict(self):
        targets = {"instance_ids": ["i-abc123"]}
        order = Order(cmds=["echo"], timeout=300, ssm_targets=targets)
        d = order.to_dict()
        assert d["ssm_targets"] == targets

    def test_ssm_targets_excluded_when_none(self):
        order = Order(cmds=["echo"], timeout=300)
        d = order.to_dict()
        assert "ssm_targets" not in d

    def test_from_dict_with_ssm_targets(self):
        data = {
            "cmds": ["echo"],
            "timeout": 300,
            "execution_target": "ssm",
            "ssm_targets": {"instance_ids": ["i-abc123"]},
        }
        order = Order.from_dict(data)
        assert order.execution_target == "ssm"
        assert order.ssm_targets == {"instance_ids": ["i-abc123"]}


class TestJob:
    def _sample_job(self):
        return Job(
            git_repo="org/repo",
            git_token_location="aws:::ssm:/token",
            username="testuser",
            orders=[
                Order(cmds=["echo 1"], timeout=300, order_name="order-1"),
                Order(cmds=["echo 2"], timeout=600, order_name="order-2"),
            ],
            pr_number=42,
            flow_label="plan",
        )

    def test_create(self):
        job = self._sample_job()
        assert job.git_repo == "org/repo"
        assert len(job.orders) == 2
        assert job.pr_number == 42
        assert job.flow_label == "plan"
        assert job.presign_expiry == 7200
        assert job.job_timeout == 3600

    def test_to_dict(self):
        job = self._sample_job()
        d = job.to_dict()
        assert d["git_repo"] == "org/repo"
        assert len(d["orders"]) == 2
        assert "cmds" in d["orders"][0]

    def test_from_dict(self):
        data = {
            "git_repo": "org/repo",
            "git_token_location": "aws:::ssm:/token",
            "username": "user1",
            "pr_number": 10,
            "orders": [
                {"cmds": ["cmd1"], "timeout": 300},
            ],
        }
        job = Job.from_dict(data)
        assert job.git_repo == "org/repo"
        assert len(job.orders) == 1
        assert job.orders[0].cmds == ["cmd1"]

    def test_to_b64_from_b64_roundtrip(self):
        job = self._sample_job()
        b64_str = job.to_b64()
        # Verify it's valid base64
        decoded = json.loads(base64.b64decode(b64_str).decode())
        assert decoded["git_repo"] == "org/repo"
        # Verify roundtrip
        restored = Job.from_b64(b64_str)
        assert restored.git_repo == job.git_repo
        assert restored.username == job.username
        assert len(restored.orders) == len(job.orders)
        assert restored.orders[0].order_name == job.orders[0].order_name

    def test_commit_hash_field(self):
        job = Job(
            git_repo="org/repo",
            git_token_location="token",
            username="user",
            orders=[],
            commit_hash="abc123",
        )
        assert job.commit_hash == "abc123"

    def test_commit_hash_b64_roundtrip(self):
        job = Job(
            git_repo="org/repo",
            git_token_location="token",
            username="user",
            orders=[Order(cmds=["echo"], timeout=300)],
            commit_hash="abc123",
        )
        restored = Job.from_b64(job.to_b64())
        assert restored.commit_hash == "abc123"

    def test_commit_hash_excluded_when_none(self):
        job = Job(
            git_repo="org/repo",
            git_token_location="token",
            username="user",
            orders=[],
        )
        d = job.to_dict()
        assert "commit_hash" not in d

    def test_default_flow_label(self):
        job = Job(
            git_repo="org/repo",
            git_token_location="aws:::ssm:/token",
            username="user",
            orders=[],
        )
        assert job.flow_label == "exec"


class TestOrderEvent:
    def test_create(self):
        event = OrderEvent(
            trace_id="abc123",
            order_name="deploy-vpc",
            epoch=1708099200.0,
            event_type="dispatched",
            status=RUNNING,
            flow_id="user:abc123-exec",
            run_id="run-1",
        )
        assert event.trace_id == "abc123"
        assert event.order_name == "deploy-vpc"
        assert event.status == RUNNING

    def test_to_dict_excludes_none(self):
        event = OrderEvent(
            trace_id="abc",
            order_name="test",
            epoch=100.0,
            event_type="completed",
            status=SUCCEEDED,
        )
        d = event.to_dict()
        assert "log_location" not in d
        assert "execution_url" not in d

    def test_from_dict(self):
        data = {
            "trace_id": "abc",
            "order_name": "test",
            "epoch": 100.0,
            "event_type": "completed",
            "status": "succeeded",
            "extra_field": "ignored",
        }
        event = OrderEvent.from_dict(data)
        assert event.trace_id == "abc"
        assert event.status == "succeeded"


class TestLockRecord:
    def test_create(self):
        lock = LockRecord(
            run_id="run-1",
            orchestrator_id="orch-1",
            status="active",
            acquired_at=1708099200.0,
            ttl=3600,
            flow_id="user:abc-exec",
            trace_id="abc",
        )
        assert lock.run_id == "run-1"
        assert lock.status == "active"

    def test_to_dict(self):
        lock = LockRecord(
            run_id="run-1",
            orchestrator_id="orch-1",
            status="active",
            acquired_at=1000.0,
            ttl=3600,
        )
        d = lock.to_dict()
        assert d["run_id"] == "run-1"
        assert "flow_id" not in d

    def test_from_dict(self):
        data = {
            "run_id": "run-1",
            "orchestrator_id": "orch-1",
            "status": "active",
            "acquired_at": 1000.0,
            "ttl": 3600,
        }
        lock = LockRecord.from_dict(data)
        assert lock.run_id == "run-1"


class TestOrderRecord:
    def test_pk_format(self):
        record = OrderRecord(
            run_id="run-123",
            order_num="001",
            trace_id="abc",
            flow_id="user:abc-exec",
            order_name="deploy",
            cmds=["echo hi"],
        )
        assert record.pk == "run-123:001"

    def test_to_dict_includes_pk(self):
        record = OrderRecord(
            run_id="run-1",
            order_num="001",
            trace_id="abc",
            flow_id="user:abc-exec",
            order_name="deploy",
            cmds=["cmd1"],
        )
        d = record.to_dict()
        assert d["pk"] == "run-1:001"
        assert d["status"] == QUEUED

    def test_from_dict(self):
        data = {
            "run_id": "run-1",
            "order_num": "001",
            "trace_id": "abc",
            "flow_id": "user:abc-exec",
            "order_name": "deploy",
            "cmds": ["cmd1"],
            "status": RUNNING,
        }
        record = OrderRecord.from_dict(data)
        assert record.status == RUNNING

    def test_execution_target_default(self):
        record = OrderRecord(
            run_id="run-1",
            order_num="001",
            trace_id="abc",
            flow_id="user:abc-exec",
            order_name="deploy",
            cmds=["cmd1"],
        )
        assert record.execution_target == "codebuild"

    def test_execution_target_lambda(self):
        record = OrderRecord(
            run_id="run-1",
            order_num="001",
            trace_id="abc",
            flow_id="user:abc-exec",
            order_name="deploy",
            cmds=["cmd1"],
            execution_target="lambda",
        )
        assert record.execution_target == "lambda"

    def test_execution_target_ssm(self):
        record = OrderRecord(
            run_id="run-1",
            order_num="001",
            trace_id="abc",
            flow_id="user:abc-exec",
            order_name="deploy",
            cmds=["cmd1"],
            execution_target="ssm",
        )
        assert record.execution_target == "ssm"

    def test_ssm_targets_field(self):
        record = OrderRecord(
            run_id="run-1",
            order_num="001",
            trace_id="abc",
            flow_id="user:abc-exec",
            order_name="deploy",
            cmds=["cmd1"],
            ssm_targets={"instance_ids": ["i-abc123"]},
        )
        assert record.ssm_targets == {"instance_ids": ["i-abc123"]}

    def test_ssm_targets_in_to_dict(self):
        record = OrderRecord(
            run_id="run-1",
            order_num="001",
            trace_id="abc",
            flow_id="user:abc-exec",
            order_name="deploy",
            cmds=["cmd1"],
            ssm_targets={"instance_ids": ["i-abc123"]},
        )
        d = record.to_dict()
        assert d["ssm_targets"] == {"instance_ids": ["i-abc123"]}

    def test_ssm_targets_excluded_when_none(self):
        record = OrderRecord(
            run_id="run-1",
            order_num="001",
            trace_id="abc",
            flow_id="user:abc-exec",
            order_name="deploy",
            cmds=["cmd1"],
        )
        d = record.to_dict()
        assert "ssm_targets" not in d


class TestDictMixin:
    def test_to_dict_excludes_none(self):
        @dataclass
        class Sample(DictMixin):
            name: str
            value: int = None

        s = Sample(name="test")
        d = s.to_dict()
        assert d == {"name": "test"}

    def test_from_dict_ignores_unknown(self):
        @dataclass
        class Sample(DictMixin):
            name: str

        s = Sample.from_dict({"name": "test", "extra": "ignored"})
        assert s.name == "test"

    def test_roundtrip(self):
        @dataclass
        class Sample(DictMixin):
            name: str
            value: int = 42

        s = Sample(name="test", value=42)
        restored = Sample.from_dict(s.to_dict())
        assert restored.name == "test"
        assert restored.value == 42


class TestBaseOrder:
    def test_shared_fields_on_order(self):
        order = Order(cmds=["echo"], timeout=300, order_name="test")
        assert isinstance(order, BaseOrder)
        assert order.order_name == "test"
        assert order.must_succeed is True

    def test_shared_fields_on_ssm_order(self):
        order = SsmOrder(cmds=["echo"], timeout=300, ssm_targets={"instance_ids": ["i-1"]})
        assert isinstance(order, BaseOrder)
        assert order.must_succeed is True


class TestBaseJob:
    def test_shared_fields_on_job(self):
        job = Job(git_repo="org/repo", git_token_location="tok", username="u", orders=[])
        assert isinstance(job, BaseJob)
        assert job.presign_expiry == 7200

    def test_shared_fields_on_ssm_job(self):
        job = SsmJob(username="u", orders=[])
        assert isinstance(job, BaseJob)
        assert job.job_timeout == 3600

    def test_to_b64_from_b64_on_ssm_job(self):
        job = SsmJob(
            username="u",
            orders=[SsmOrder(cmds=["echo"], timeout=300, ssm_targets={"instance_ids": ["i-1"]})],
        )
        restored = SsmJob.from_b64(job.to_b64())
        assert restored.username == "u"
        assert len(restored.orders) == 1
        assert restored.orders[0].ssm_targets == {"instance_ids": ["i-1"]}


class TestFormatOrderNum:
    def test_zero_index(self):
        assert format_order_num(0) == "0001"

    def test_single_digit(self):
        assert format_order_num(8) == "0009"

    def test_large_index(self):
        assert format_order_num(9999) == "10000"
