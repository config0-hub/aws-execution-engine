"""Unit tests for the execution target registry (P2-4).

The architectural point of this registry is that a third-party
execution backend (ECS, Fargate, on-prem agents, …) can be registered
at runtime and ``aws_exe_sys/orchestrator/dispatch.py`` will route to it with
no code changes in the call site. These tests prove that end-to-end.
"""

import json
from typing import Any, List
from unittest.mock import MagicMock, patch

import pytest

from aws_exe_sys.orchestrator.targets import (
    CodeBuildTarget,
    ExecutionTarget,
    LambdaTarget,
    SsmTarget,
    TARGETS,
    UnknownTargetError,
    get_target,
    list_targets,
    register_target,
)
from aws_exe_sys.orchestrator.targets import registry as target_registry


@pytest.fixture
def clean_registry():
    """Snapshot and restore the execution target registry across tests."""
    saved = dict(target_registry._TARGETS)
    try:
        yield
    finally:
        target_registry._TARGETS.clear()
        target_registry._TARGETS.update(saved)


@pytest.fixture
def aws_env(monkeypatch):
    """Populate env vars the built-in targets look up at dispatch time."""
    monkeypatch.setenv("AWS_EXE_SYS_WORKER_LAMBDA", "test-worker-lambda")
    monkeypatch.setenv("AWS_EXE_SYS_CODEBUILD_PROJECT", "test-codebuild-project")
    monkeypatch.setenv("AWS_EXE_SYS_SSM_DOCUMENT", "test-ssm-document")


# ---------------------------------------------------------------------------
# Built-in targets are registered at import time
# ---------------------------------------------------------------------------


class TestBuiltinRegistration:
    def test_all_three_names_registered(self):
        names = list_targets()
        assert "lambda" in names
        assert "codebuild" in names
        assert "ssm" in names

    def test_protocol_runtime_check(self):
        """Built-in targets satisfy the ExecutionTarget Protocol structurally."""
        assert isinstance(LambdaTarget(), ExecutionTarget)
        assert isinstance(CodeBuildTarget(), ExecutionTarget)
        assert isinstance(SsmTarget(), ExecutionTarget)

    def test_statuses_derives_from_registry(self):
        """aws_exe_sys.common.statuses.EXECUTION_TARGETS is sourced from the registry."""
        from aws_exe_sys.common.statuses import EXECUTION_TARGETS

        assert EXECUTION_TARGETS == frozenset({"lambda", "codebuild", "ssm"})


# ---------------------------------------------------------------------------
# LambdaTarget — async invoke with the worker payload
# ---------------------------------------------------------------------------


class TestLambdaTarget:
    @patch("aws_exe_sys.orchestrator.targets.lambda_target.boto3.client")
    def test_lambda_target(self, mock_client_factory, aws_env):
        """Asserts the Lambda invoke is async with the right payload."""
        mock_client = MagicMock()
        mock_client.invoke.return_value = {
            "ResponseMetadata": {"RequestId": "req-abc"},
        }
        mock_client_factory.return_value = mock_client

        target = LambdaTarget()
        order = {
            "order_num": "0001",
            "s3_location": "s3://bucket/exec.zip",
            "callback_url": "https://cb.example/put",
            "sops_key_ssm_path": "/sops/run-1/0001",
        }

        execution_id = target.dispatch(order, "run-1", "internal-bucket")

        assert execution_id == "req-abc"
        mock_client_factory.assert_called_once_with("lambda")
        mock_client.invoke.assert_called_once()
        call_kwargs = mock_client.invoke.call_args.kwargs
        assert call_kwargs["FunctionName"] == "test-worker-lambda"
        assert call_kwargs["InvocationType"] == "Event"
        payload = json.loads(call_kwargs["Payload"].decode())
        assert payload["s3_location"] == "s3://bucket/exec.zip"
        assert payload["internal_bucket"] == "internal-bucket"
        assert payload["callback_url"] == "https://cb.example/put"
        assert payload["sops_key_ssm_path"] == "/sops/run-1/0001"


# ---------------------------------------------------------------------------
# CodeBuildTarget — start_build with env var overrides
# ---------------------------------------------------------------------------


class TestCodeBuildTarget:
    @patch("aws_exe_sys.orchestrator.targets.codebuild.boto3.client")
    def test_codebuild_target(self, mock_client_factory, aws_env):
        """Asserts start_build is called with overridden env vars."""
        mock_client = MagicMock()
        mock_client.start_build.return_value = {
            "build": {"id": "test-codebuild:build-xyz"},
        }
        mock_client_factory.return_value = mock_client

        target = CodeBuildTarget()
        order = {
            "order_num": "0001",
            "s3_location": "s3://bucket/exec.zip",
            "sops_key_ssm_path": "/sops/run-1/0001",
        }

        execution_id = target.dispatch(order, "run-1", "internal-bucket")

        assert execution_id == "test-codebuild:build-xyz"
        mock_client_factory.assert_called_once_with("codebuild")
        call_kwargs = mock_client.start_build.call_args.kwargs
        assert call_kwargs["projectName"] == "test-codebuild-project"
        env = {e["name"]: e["value"] for e in call_kwargs["environmentVariablesOverride"]}
        assert env["S3_LOCATION"] == "s3://bucket/exec.zip"
        assert env["INTERNAL_BUCKET"] == "internal-bucket"
        assert env["SOPS_KEY_SSM_PATH"] == "/sops/run-1/0001"

    @patch("aws_exe_sys.orchestrator.targets.codebuild.boto3.client")
    def test_codebuild_without_sops_key_omits_override(self, mock_client_factory, aws_env):
        mock_client = MagicMock()
        mock_client.start_build.return_value = {"build": {"id": "b-1"}}
        mock_client_factory.return_value = mock_client

        CodeBuildTarget().dispatch(
            {"s3_location": "s3://b/k"}, "run-1", "int-bucket",
        )

        env = {
            e["name"]: e["value"]
            for e in mock_client.start_build.call_args.kwargs["environmentVariablesOverride"]
        }
        assert "SOPS_KEY_SSM_PATH" not in env


# ---------------------------------------------------------------------------
# SsmTarget — send_command with tags or instance_ids
# ---------------------------------------------------------------------------


class TestSsmTarget:
    @patch("aws_exe_sys.orchestrator.targets.ssm.boto3.client")
    def test_ssm_target(self, mock_client_factory, aws_env):
        """Asserts send_command uses the right document and targeting."""
        mock_client = MagicMock()
        mock_client.send_command.return_value = {
            "Command": {"CommandId": "cmd-xyz"},
        }
        mock_client_factory.return_value = mock_client

        target = SsmTarget()
        order = {
            "order_num": "0001",
            "cmds": ["echo hi", "uname -a"],
            "timeout": 600,
            "callback_url": "https://cb.example/put",
            "ssm_targets": {"instance_ids": ["i-abc123"]},
        }

        execution_id = target.dispatch(order, "run-1", "internal-bucket")

        assert execution_id == "cmd-xyz"
        mock_client_factory.assert_called_once_with("ssm")
        call_kwargs = mock_client.send_command.call_args.kwargs
        assert call_kwargs["DocumentName"] == "test-ssm-document"
        assert call_kwargs["TimeoutSeconds"] == 600
        assert call_kwargs["InstanceIds"] == ["i-abc123"]
        assert json.loads(call_kwargs["Parameters"]["Commands"][0]) == [
            "echo hi", "uname -a",
        ]

    @patch("aws_exe_sys.orchestrator.targets.ssm.boto3.client")
    def test_ssm_target_with_tags(self, mock_client_factory, aws_env):
        """Tag-based targeting maps to SSM Targets[] structure."""
        mock_client = MagicMock()
        mock_client.send_command.return_value = {"Command": {"CommandId": "cmd-tag"}}
        mock_client_factory.return_value = mock_client

        order = {
            "cmds": ["echo"],
            "timeout": 300,
            "ssm_targets": {"tags": {"Role": "web", "Env": "prod"}},
        }
        SsmTarget().dispatch(order, "run-1", "internal-bucket")

        tgts = mock_client.send_command.call_args.kwargs["Targets"]
        tag_keys = {t["Key"] for t in tgts}
        assert tag_keys == {"tag:Role", "tag:Env"}


# ---------------------------------------------------------------------------
# Third-party target registration — the architectural point
# ---------------------------------------------------------------------------


class _FakeEcsFargateTarget:
    """Minimal stub proving third-party targets plug into the registry."""

    name = "ecs_fargate"

    def __init__(self) -> None:
        self.dispatched: List[Any] = []

    def dispatch(self, order: Any, run_id: str, internal_bucket: str) -> str:
        self.dispatched.append({
            "order": order, "run_id": run_id, "internal_bucket": internal_bucket,
        })
        return f"ecs-task-{run_id}"


class _BadTargetNoName:
    """Missing the required name class attribute."""

    def dispatch(self, order, run_id, internal_bucket):
        return ""


class TestRegisterThirdPartyTarget:
    def test_register_ecs_fargate_stub(self, clean_registry):
        """Register an ECS/Fargate stub and verify dispatch routes to it."""
        fake = _FakeEcsFargateTarget()
        register_target(fake)

        assert "ecs_fargate" in list_targets()
        assert get_target("ecs_fargate") is fake

        # Verify end-to-end: when dispatch.py sees execution_target="ecs_fargate",
        # TARGETS[execution_target].dispatch gets called with our stub.
        order = {
            "order_num": "0099",
            "execution_target": "ecs_fargate",
            "cmds": ["run task"],
        }
        execution_id = TARGETS["ecs_fargate"].dispatch(
            order, "run-42", "internal-bucket",
        )

        assert execution_id == "ecs-task-run-42"
        assert len(fake.dispatched) == 1
        assert fake.dispatched[0]["run_id"] == "run-42"

    def test_register_target_without_name_raises(self, clean_registry):
        with pytest.raises(ValueError, match="name"):
            register_target(_BadTargetNoName())

    def test_register_target_with_explicit_name(self, clean_registry):
        """Explicit name= overrides / supplies the registry key."""
        register_target(_BadTargetNoName(), name="explicit")
        assert "explicit" in list_targets()

    def test_unknown_target_raises(self, clean_registry):
        """Looking up an unregistered target raises UnknownTargetError."""
        with pytest.raises(UnknownTargetError, match="unknown execution_target"):
            get_target("no_such_target")


# ---------------------------------------------------------------------------
# dispatch.py routes through the registry
# ---------------------------------------------------------------------------


class TestDispatchRoutesThroughRegistry:
    """Prove that dispatch.py hits TARGETS, not hardcoded if/elif."""

    def test_dispatch_raises_on_unknown_target(self):
        """An order with an unregistered execution_target must surface
        UnknownTargetError from the registry rather than silently falling
        back to codebuild."""
        from aws_exe_sys.orchestrator.dispatch import _dispatch_single
        from aws_exe_sys.common import dynamodb

        with patch.object(dynamodb, "reserve_order_for_dispatch", return_value=True):
            order = {
                "order_num": "0001",
                "order_name": "weird",
                "execution_target": "no_such_target",
                "s3_location": "s3://b/k",
                "timeout": 300,
            }
            with pytest.raises(UnknownTargetError):
                _dispatch_single(
                    order, "run-1", "flow-1", "trace-1",
                    "internal-bucket",
                    dynamodb_resource=MagicMock(),
                )
