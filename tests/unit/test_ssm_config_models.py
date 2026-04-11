"""Unit tests for aws_exe_sys/ssm_config/models.py."""

import json
import base64

from aws_exe_sys.common.models import SsmOrder, SsmJob


class TestSsmOrder:
    def test_create_minimal(self):
        order = SsmOrder(
            cmds=["echo hello"],
            timeout=300,
            ssm_targets={"instance_ids": ["i-abc123"]},
        )
        assert order.cmds == ["echo hello"]
        assert order.timeout == 300
        assert order.ssm_targets == {"instance_ids": ["i-abc123"]}
        assert order.must_succeed is True
        assert order.dependencies is None
        assert order.order_name is None
        assert order.env_vars is None

    def test_create_full(self):
        order = SsmOrder(
            cmds=["cmd1", "cmd2"],
            timeout=600,
            ssm_targets={"tags": [{"Key": "env", "Values": ["prod"]}]},
            order_name="deploy-ssm",
            git_repo="org/repo",
            git_folder="scripts/",
            commit_hash="abc123",
            s3_location="s3://bucket/code.zip",
            env_vars={"KEY": "VALUE"},
            ssm_paths=["/path/1"],
            secret_manager_paths=["secret/1"],
            ssm_document_name="AWS-RunShellScript",
            queue_id="ssm-queue",
            dependencies=["dep-1"],
            must_succeed=False,
            callback_url="https://callback.example.com",
        )
        assert order.order_name == "deploy-ssm"
        assert order.must_succeed is False
        assert order.ssm_document_name == "AWS-RunShellScript"
        assert order.dependencies == ["dep-1"]
        assert order.callback_url == "https://callback.example.com"

    def test_to_dict(self):
        order = SsmOrder(
            cmds=["echo hi"],
            timeout=300,
            ssm_targets={"instance_ids": ["i-abc"]},
        )
        d = order.to_dict()
        assert "cmds" in d
        assert "timeout" in d
        assert "ssm_targets" in d
        # None fields should be excluded
        assert "order_name" not in d
        assert "env_vars" not in d
        assert "git_repo" not in d

    def test_to_dict_includes_set_fields(self):
        order = SsmOrder(
            cmds=["echo"],
            timeout=300,
            ssm_targets={"instance_ids": ["i-1"]},
            order_name="my-order",
            env_vars={"A": "B"},
        )
        d = order.to_dict()
        assert d["order_name"] == "my-order"
        assert d["env_vars"] == {"A": "B"}

    def test_from_dict(self):
        data = {
            "cmds": ["echo test"],
            "timeout": 120,
            "ssm_targets": {"tags": [{"Key": "Name", "Values": ["web"]}]},
            "order_name": "test-order",
            "unknown_field": "ignored",
        }
        order = SsmOrder.from_dict(data)
        assert order.cmds == ["echo test"]
        assert order.timeout == 120
        assert order.order_name == "test-order"
        assert order.ssm_targets == {"tags": [{"Key": "Name", "Values": ["web"]}]}

    def test_from_dict_ignores_unknown_fields(self):
        data = {
            "cmds": ["echo"],
            "timeout": 60,
            "ssm_targets": {"instance_ids": ["i-1"]},
            "totally_unknown": "value",
            "another_extra": 42,
        }
        order = SsmOrder.from_dict(data)
        assert order.cmds == ["echo"]
        assert not hasattr(order, "totally_unknown")

    def test_ssm_targets_with_instance_ids(self):
        order = SsmOrder(
            cmds=["echo"],
            timeout=300,
            ssm_targets={"instance_ids": ["i-abc123", "i-def456"]},
        )
        assert order.ssm_targets["instance_ids"] == ["i-abc123", "i-def456"]

    def test_ssm_targets_with_tags(self):
        targets = {"tags": [{"Key": "env", "Values": ["staging", "prod"]}]}
        order = SsmOrder(cmds=["echo"], timeout=300, ssm_targets=targets)
        assert order.ssm_targets["tags"][0]["Key"] == "env"

    def test_to_dict_from_dict_roundtrip(self):
        order = SsmOrder(
            cmds=["cmd1", "cmd2"],
            timeout=300,
            ssm_targets={"instance_ids": ["i-abc"]},
            order_name="test",
            env_vars={"A": "B"},
        )
        d = order.to_dict()
        restored = SsmOrder.from_dict(d)
        assert restored.cmds == order.cmds
        assert restored.timeout == order.timeout
        assert restored.order_name == order.order_name
        assert restored.env_vars == order.env_vars
        assert restored.ssm_targets == order.ssm_targets


class TestSsmJob:
    def _sample_job(self):
        return SsmJob(
            username="testuser",
            orders=[
                SsmOrder(
                    cmds=["echo 1"],
                    timeout=300,
                    ssm_targets={"instance_ids": ["i-abc"]},
                    order_name="order-1",
                ),
                SsmOrder(
                    cmds=["echo 2"],
                    timeout=600,
                    ssm_targets={"tags": [{"Key": "env", "Values": ["prod"]}]},
                    order_name="order-2",
                ),
            ],
            git_repo="org/repo",
            flow_label="ssm-deploy",
        )

    def test_create(self):
        job = self._sample_job()
        assert job.username == "testuser"
        assert len(job.orders) == 2
        assert job.git_repo == "org/repo"
        assert job.flow_label == "ssm-deploy"
        assert job.presign_expiry == 7200
        assert job.job_timeout == 3600

    def test_to_dict(self):
        job = self._sample_job()
        d = job.to_dict()
        assert d["username"] == "testuser"
        assert len(d["orders"]) == 2
        assert "cmds" in d["orders"][0]
        assert "ssm_targets" in d["orders"][0]

    def test_to_dict_excludes_none(self):
        job = SsmJob(
            username="user",
            orders=[],
        )
        d = job.to_dict()
        assert "git_token_location" not in d
        assert "git_ssh_key_location" not in d
        assert "commit_hash" not in d

    def test_from_dict(self):
        data = {
            "username": "user1",
            "orders": [
                {
                    "cmds": ["cmd1"],
                    "timeout": 300,
                    "ssm_targets": {"instance_ids": ["i-1"]},
                },
            ],
            "git_repo": "org/repo",
        }
        job = SsmJob.from_dict(data)
        assert job.username == "user1"
        assert job.git_repo == "org/repo"
        assert len(job.orders) == 1
        assert job.orders[0].cmds == ["cmd1"]
        assert job.orders[0].ssm_targets == {"instance_ids": ["i-1"]}

    def test_from_dict_ignores_unknown_fields(self):
        data = {
            "username": "user1",
            "orders": [],
            "unknown_top_level": "ignored",
        }
        job = SsmJob.from_dict(data)
        assert job.username == "user1"

    def test_to_b64_from_b64_roundtrip(self):
        job = self._sample_job()
        b64_str = job.to_b64()
        # Verify it's valid base64
        decoded = json.loads(base64.b64decode(b64_str).decode())
        assert decoded["username"] == "testuser"
        # Verify roundtrip
        restored = SsmJob.from_b64(b64_str)
        assert restored.username == job.username
        assert restored.git_repo == job.git_repo
        assert len(restored.orders) == len(job.orders)
        assert restored.orders[0].order_name == job.orders[0].order_name
        assert restored.orders[0].ssm_targets == job.orders[0].ssm_targets
        assert restored.orders[1].ssm_targets == job.orders[1].ssm_targets

    def test_default_flow_label(self):
        job = SsmJob(
            username="user",
            orders=[],
        )
        assert job.flow_label == "ssm"

    def test_default_presign_expiry(self):
        job = SsmJob(username="user", orders=[])
        assert job.presign_expiry == 7200

    def test_default_job_timeout(self):
        job = SsmJob(username="user", orders=[])
        assert job.job_timeout == 3600
