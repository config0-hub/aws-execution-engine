"""Contract tests for the CodeBuild Step Functions definition in Terraform."""

from pathlib import Path
import re

_INFRA = Path(__file__).resolve().parents[2] / "infra" / "02-deploy"
STATE_MACHINE_TERRAFORM = _INFRA / "step_functions.tf"


def _task_block(source: str, name: str) -> str:
    """Extract one state's block text, from its opening line to the next state."""
    match = re.search(rf"^      {name} = \{{\n(.*?)^      \}}\n", source, re.M | re.S)
    assert match, f"state {name} not found"
    return match.group(1)


def test_state_machine_uses_standard_sync_codebuild_and_finalizer():
    source = STATE_MACHINE_TERRAFORM.read_text()

    assert 'type     = "STANDARD"' in source
    assert "states:::codebuild:startBuild.sync" in source
    assert 'Next        = "FinalizeResult"' in source
    assert 'Resource = "arn:${data.aws_partition.current.partition}:states:::lambda:invoke"' in source
    assert "IfNoneMatch" not in source


def test_shared_override_list_has_exactly_eleven_plaintext_entries():
    source = STATE_MACHINE_TERRAFORM.read_text()

    expected_names = [
        "TRIGGER_ID",
        "S3_PACKAGE_URI",
        "SOPS_TYPE",
        "SOPS_PATH",
        "COMMANDS_B64",
        "DONE_ENDPOINT",
        "EXECUTION_TARGET",
        "TIMEOUT_SECONDS",
        "CALLBACK_URL",
        "CALLBACK_TOKEN",
        "EXECUTION_MODE",
    ]
    shared_block = source.split("codebuild_env_overrides = [")[1].split("\n  ]")[0]
    for name in expected_names:
        assert shared_block.count(f'Name      = "{name}"') == 1
    assert shared_block.count('Type      = "PLAINTEXT"') == 11


def test_both_tasks_carry_the_identical_shared_eleven_entry_list():
    source = STATE_MACHINE_TERRAFORM.read_text()

    default_task = _task_block(source, "RunCodeBuild")
    direct_task = _task_block(source, "RunCodeBuildDirect")

    # Both Task states reference the SAME shared local, so the 11-entry base
    # list is identical by construction; the direct Task only APPENDS its four
    # static entries via concat.
    assert "EnvironmentVariablesOverride = local.codebuild_env_overrides" in default_task
    assert "EnvironmentVariablesOverride = concat(local.codebuild_env_overrides, [" in direct_task


def test_choice_state_branches_on_execution_mode_before_either_task():
    source = STATE_MACHINE_TERRAFORM.read_text()

    # NormalizePayload -> RouteExecutionMode (Choice) -> Task: the Choice reads
    # $.execution_mode from the Pass state's merged output BEFORE either Task
    # fires.
    assert 'Next       = "RouteExecutionMode"' in source
    route = _task_block(source, "RouteExecutionMode")
    assert "Type = \"Choice\"" in route
    assert 'Variable     = "$.execution_mode"' in route
    assert 'StringEquals = "direct"' in route
    assert 'Next         = "RunCodeBuildDirect"' in route
    assert 'Default = "RunCodeBuild"' in route


def test_default_task_parameters_differ_from_baseline_only_by_execution_mode_entry():
    source = STATE_MACHINE_TERRAFORM.read_text()

    default_task = _task_block(source, "RunCodeBuild")
    params = default_task.split("Parameters = {")[1].split("\n        }")[0]

    # Field-by-field against the pre-Phase-1 baseline: exactly ProjectName,
    # TimeoutInMinutesOverride, and the env-override list (which gains the one
    # EXECUTION_MODE entry via the shared local, 10 -> 11). No direct-only
    # keys.
    assert "ProjectName = aws_codebuild_project.worker.name" in params
    assert '"TimeoutInMinutesOverride.$" = "$.build_timeout_minutes"' in params
    assert "EnvironmentVariablesOverride = local.codebuild_env_overrides" in params
    for forbidden in (
        "BuildspecOverride",
        "ImageOverride",
        "PrivilegedModeOverride",
        "ImagePullCredentialsTypeOverride",
        "ENGINE_ZIP_S3_BUCKET",
        "ENGINE_ZIP_S3_KEY",
        "SOPS_URL",
        "AGE_URL",
    ):
        assert forbidden not in default_task, f"{forbidden} leaked onto RunCodeBuild"
    # Nothing beyond the three expected Parameters keys.
    raw_keys = re.findall(r"^ {10}(\S+) *=", params, re.M)
    param_keys = {k.strip('"').removesuffix(".$") for k in raw_keys}
    assert param_keys == {"ProjectName", "TimeoutInMinutesOverride", "EnvironmentVariablesOverride"}

    # Non-Parameters fields unchanged from baseline.
    assert 'TimeoutSecondsPath = "$.sfn_timeout_seconds"' in default_task
    assert 'ErrorEquals = ["States.ALL"]' in default_task
    assert 'Next        = "FinalizeResult"' in default_task


def test_direct_task_carries_the_eight_direct_only_overrides():
    source = STATE_MACHINE_TERRAFORM.read_text()

    direct_task = _task_block(source, "RunCodeBuildDirect")
    assert "BuildspecOverride                = local.direct_mode_buildspec" in direct_task
    assert 'ImageOverride                    = "aws/codebuild/standard:7.0"' in direct_task
    assert "PrivilegedModeOverride           = true" in direct_task
    assert 'ImagePullCredentialsTypeOverride = "CODEBUILD"' in direct_task
    assert 'Name  = "ENGINE_ZIP_S3_BUCKET"' in direct_task
    assert "Value = var.engine_zip_s3_bucket" in direct_task
    assert 'Name  = "ENGINE_ZIP_S3_KEY"' in direct_task
    assert "Value = var.engine_zip_s3_key" in direct_task
    assert 'Name  = "SOPS_URL"' in direct_task
    assert "Value = local.direct_mode_sops_url" in direct_task
    assert 'Name  = "AGE_URL"' in direct_task
    assert "Value = local.direct_mode_age_url" in direct_task

    # None of the eight appear in codebuild_payload_defaults.
    defaults_block = source.split("codebuild_payload_defaults = {")[1].split("}")[0]
    for name in ("ENGINE_ZIP", "SOPS_URL", "AGE_URL", "BuildspecOverride", "ImageOverride"):
        assert name not in defaults_block


def test_direct_task_shares_catch_and_timeout_threading_with_default_task():
    source = STATE_MACHINE_TERRAFORM.read_text()

    for name in ("RunCodeBuild", "RunCodeBuildDirect"):
        block = _task_block(source, name)
        assert 'TimeoutSecondsPath = "$.sfn_timeout_seconds"' in block
        # fmt alignment differs between the two Parameters blocks - assert the
        # key/value pair, not the exact padding.
        assert re.search(r'"TimeoutInMinutesOverride\.\$"\s+= "\$\.build_timeout_minutes"', block)
        assert 'ErrorEquals = ["States.ALL"]' in block
        assert 'ResultPath  = "$.terminal_context"' in block
        assert 'Next        = "FinalizeResult"' in block
        assert "Next = \"FinalizeResult\"" in block


def test_state_machine_normalizes_missing_payload_keys_before_codebuild():
    source = STATE_MACHINE_TERRAFORM.read_text()

    # A missing input key makes a field-level JSONPath reference throw
    # States.Runtime, which Catch cannot intercept; the NormalizePayload Pass
    # state must merge defaults for all eleven payload keys before the Choice
    # or either task runs.
    assert 'StartAt = "NormalizePayload"' in source
    assert "States.JsonMerge(States.StringToJson(" in source
    assert "$$.Execution.Input, false)" in source

    expected_default_keys = {
        "trigger_id",
        "s3_package_uri",
        "sops_type",
        "sops_path",
        "commands_b64",
        "done_endpoint",
        "execution_target",
        "timeout_seconds",
        "callback_url",
        "callback_token",
        "execution_mode",
    }
    defaults_block = source.split("codebuild_payload_defaults = {")[1].split("}")[0]
    for key in expected_default_keys:
        assert f'{key}' in defaults_block, f"missing default for {key}"
        # Every referenced JSONPath has a matching normalized default.
        assert f'"$.{key}"' in source


def test_state_machine_timeouts_derive_from_timeout_seconds():
    source = STATE_MACHINE_TERRAFORM.read_text()

    # The state timeout and the per-build CodeBuild override both follow the
    # dispatcher-computed deadline inputs, not static constants.
    assert 'TimeoutSecondsPath = "$.sfn_timeout_seconds"' in source
    assert '"TimeoutInMinutesOverride.$" = "$.build_timeout_minutes"' in source
    assert "TimeoutSeconds = 1200" not in source
