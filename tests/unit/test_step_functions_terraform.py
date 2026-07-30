"""Contract tests for the CodeBuild Step Functions definition in Terraform."""

from pathlib import Path

STATE_MACHINE_TERRAFORM = Path(__file__).resolve().parents[2] / "infra" / "02-deploy" / "step_functions.tf"


def test_state_machine_uses_standard_sync_codebuild_and_finalizer():
    source = STATE_MACHINE_TERRAFORM.read_text()

    assert 'type     = "STANDARD"' in source
    assert "states:::codebuild:startBuild.sync" in source
    assert 'Next        = "FinalizeResult"' in source
    assert 'Resource = "arn:${data.aws_partition.current.partition}:states:::lambda:invoke"' in source
    assert "IfNoneMatch" not in source


def test_state_machine_passes_exactly_seven_plaintext_environment_overrides():
    source = STATE_MACHINE_TERRAFORM.read_text()

    expected_names = {
        "TRIGGER_ID",
        "S3_PACKAGE_URI",
        "SOPS_TYPE",
        "SOPS_PATH",
        "COMMANDS_B64",
        "DONE_ENDPOINT",
        "EXECUTION_TARGET",
    }
    for name in expected_names:
        assert source.count(f'Name      = "{name}"') == 1
    assert source.count('Type      = "PLAINTEXT"') == 7
