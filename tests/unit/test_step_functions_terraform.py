"""Contract tests for the CodeBuild Step Functions definition in Terraform."""

import json
from pathlib import Path
import re

import pytest

_INFRA = Path(__file__).resolve().parents[2] / "infra" / "02-deploy"
STATE_MACHINE_TERRAFORM = _INFRA / "step_functions.tf"

# Sentinel values substituted for every Terraform reference the definition's
# jsonencode(...) expression uses, so the templated HCL renders to concrete
# JSON without credentials, providers, or a real plan.
_SENTINELS = {
    "data.aws_partition.current.partition": "aws",
    "aws_codebuild_project.worker.name": "sentinel-codebuild-project",
    "aws_lambda_function.finalizer.arn": "arn:aws:lambda:sentinel:finalizer",
    "var.engine_zip_s3_bucket": "sentinel-engine-zip-bucket",
    "var.engine_zip_s3_key": "sentinel-engine-zip-key",
    "local.direct_mode_buildspec": "sentinel-direct-mode-buildspec",
    "local.direct_mode_sops_url": "sentinel-direct-mode-sops-url",
    "local.direct_mode_age_url": "sentinel-direct-mode-age-url",
}


class _HclRenderError(AssertionError):
    """The step_functions.tf definition used syntax the renderer cannot evaluate."""


class _HclSubsetRenderer:
    """Evaluate the HCL-literal subset step_functions.tf uses into Python values.

    Supports objects, lists, quoted strings with ``${...}`` interpolation,
    numbers, booleans, ``concat(...)``, ``jsonencode(...)`` (inside string
    interpolation), and dotted references resolved via ``bindings``.
    """

    def __init__(self, source: str, bindings: dict[str, object]):
        self.source = source
        self.bindings = bindings

    def render_after(self, marker: str) -> object:
        """Parse the first value that follows ``marker`` in the source."""
        start = self.source.index(marker) + len(marker)
        value, _ = self._parse_value(start)
        return value

    def _resolve(self, ref: str) -> object:
        if ref not in self.bindings:
            raise _HclRenderError(f"no sentinel binding for reference {ref!r}")
        return self.bindings[ref]

    def _skip_trivia(self, i: int) -> int:
        source = self.source
        while i < len(source):
            if source[i] in " \t\r\n":
                i += 1
            elif source[i] == "#":
                i = source.index("\n", i)
            else:
                break
        return i

    def _parse_value(self, i: int) -> tuple[object, int]:
        source = self.source
        i = self._skip_trivia(i)
        char = source[i]
        if char == "{":
            return self._parse_object(i)
        if char == "[":
            return self._parse_list(i)
        if char == '"':
            return self._parse_string(i)
        match = re.match(r"-?\d+(\.\d+)?", source[i:])
        if match:
            text = match.group(0)
            number = float(text) if "." in text else int(text)
            return number, i + len(text)
        match = re.match(r"[A-Za-z_][A-Za-z0-9_.-]*", source[i:])
        if not match:
            raise _HclRenderError(f"unparseable value at offset {i}: {source[i : i + 40]!r}")
        ident = match.group(0)
        end = i + len(ident)
        if ident == "true":
            return True, end
        if ident == "false":
            return False, end
        if source[self._skip_trivia(end) : self._skip_trivia(end) + 1] == "(":
            return self._parse_call(ident, self._skip_trivia(end))
        return self._resolve(ident), end

    def _parse_call(self, name: str, i: int) -> tuple[object, int]:
        assert self.source[i] == "("
        args = []
        i += 1
        while True:
            i = self._skip_trivia(i)
            if self.source[i] == ")":
                i += 1
                break
            value, i = self._parse_value(i)
            args.append(value)
            i = self._skip_trivia(i)
            if self.source[i] == ",":
                i += 1
        if name == "concat":
            merged: list[object] = []
            for arg in args:
                if not isinstance(arg, list):
                    raise _HclRenderError("concat() argument is not a list")
                merged.extend(arg)
            return merged, i
        if name == "jsonencode" and len(args) == 1:
            return json.dumps(args[0]), i
        raise _HclRenderError(f"unsupported function call {name}()")

    def _parse_object(self, i: int) -> tuple[dict[str, object], int]:
        source = self.source
        assert source[i] == "{"
        result: dict[str, object] = {}
        i += 1
        while True:
            i = self._skip_trivia(i)
            if source[i] == "}":
                return result, i + 1
            if source[i] == '"':
                key, i = self._parse_string(i)
            else:
                match = re.match(r"[A-Za-z_][A-Za-z0-9_.-]*", source[i:])
                if not match:
                    raise _HclRenderError(f"bad object key at offset {i}")
                key = match.group(0)
                i += len(key)
            i = self._skip_trivia(i)
            if source[i] != "=":
                raise _HclRenderError(f"expected '=' after key {key!r}")
            value, i = self._parse_value(i + 1)
            result[str(key)] = value
            i = self._skip_trivia(i)
            if source[i] == ",":
                i += 1

    def _parse_list(self, i: int) -> tuple[list[object], int]:
        source = self.source
        assert source[i] == "["
        result: list[object] = []
        i += 1
        while True:
            i = self._skip_trivia(i)
            if source[i] == "]":
                return result, i + 1
            value, i = self._parse_value(i)
            result.append(value)
            i = self._skip_trivia(i)
            if source[i] == ",":
                i += 1

    def _parse_string(self, i: int) -> tuple[str, int]:
        source = self.source
        assert source[i] == '"'
        parts: list[str] = []
        i += 1
        while True:
            char = source[i]
            if char == '"':
                return "".join(parts), i + 1
            if char == "\\":
                parts.append(source[i + 1])
                i += 2
                continue
            if char == "$" and source[i : i + 3] == "$${":
                parts.append("${")
                i += 3
                continue
            if char == "$" and source[i : i + 2] == "${":
                rendered, expr_end = self._parse_value(i + 2)
                if not isinstance(rendered, str):
                    raise _HclRenderError("interpolation did not render to a string")
                parts.append(rendered)
                i = self._skip_trivia(expr_end)
                if source[i] != "}":
                    raise _HclRenderError("unterminated interpolation")
                i += 1
                continue
            parts.append(char)
            i += 1


def _render_state_machine_definition() -> dict:
    """Render the jsonencode(...) definition to the concrete ASL JSON object."""
    source = STATE_MACHINE_TERRAFORM.read_text()

    locals_renderer = _HclSubsetRenderer(source, dict(_SENTINELS))
    payload_defaults = locals_renderer.render_after("codebuild_payload_defaults = ")
    env_overrides = locals_renderer.render_after("codebuild_env_overrides = ")

    bindings: dict[str, object] = dict(_SENTINELS)
    bindings["local.codebuild_payload_defaults"] = payload_defaults
    bindings["local.codebuild_env_overrides"] = env_overrides

    definition = _HclSubsetRenderer(source, bindings).render_after("definition = jsonencode(")
    assert isinstance(definition, dict)
    return definition


@pytest.fixture(scope="module")
def rendered_asl() -> dict:
    return _render_state_machine_definition()


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


# ---------------------------------------------------------------------------
# Render-level assertions: the tests below evaluate the templated definition
# to the concrete ASL JSON (Terraform references replaced by sentinels) and
# assert against the rendered structure, not the HCL source text.
# ---------------------------------------------------------------------------

# The pre-direct-mode (pre-v5.1) shared env-override list: the ten payload
# fields every build received before the EXECUTION_MODE discriminator landed.
_PRE_DIRECT_MODE_ENV_NAMES = [
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
]

_PRE_DIRECT_MODE_ENV_PATHS = {
    "TRIGGER_ID": "$.trigger_id",
    "S3_PACKAGE_URI": "$.s3_package_uri",
    "SOPS_TYPE": "$.sops_type",
    "SOPS_PATH": "$.sops_path",
    "COMMANDS_B64": "$.commands_b64",
    "DONE_ENDPOINT": "$.done_endpoint",
    "EXECUTION_TARGET": "$.execution_target",
    "TIMEOUT_SECONDS": "$.timeout_seconds",
    "CALLBACK_URL": "$.callback_url",
    "CALLBACK_TOKEN": "$.callback_token",
}


def test_rendered_definition_is_valid_json(rendered_asl):
    # The rendered definition must survive a strict JSON round-trip: every
    # Terraform expression evaluated away, nothing but plain JSON types left.
    round_tripped = json.loads(json.dumps(rendered_asl))
    assert round_tripped == rendered_asl
    assert round_tripped["StartAt"] == "NormalizePayload"


def test_rendered_flow_is_normalize_then_choice_then_both_tasks(rendered_asl):
    states = rendered_asl["States"]

    normalize = states["NormalizePayload"]
    assert normalize["Type"] == "Pass"
    assert normalize["Next"] == "RouteExecutionMode"

    choice = states["RouteExecutionMode"]
    assert choice["Type"] == "Choice"
    assert choice["Choices"] == [
        {
            "Variable": "$.execution_mode",
            "StringEquals": "direct",
            "Next": "RunCodeBuildDirect",
        }
    ]
    assert choice["Default"] == "RunCodeBuild"

    for name in ("RunCodeBuild", "RunCodeBuildDirect"):
        task = states[name]
        assert task["Type"] == "Task"
        assert task["Resource"].endswith(":states:::codebuild:startBuild.sync")
        assert task["Next"] == "FinalizeResult"


def test_rendered_tasks_share_the_identical_eleven_entry_base_list(rendered_asl):
    states = rendered_asl["States"]
    default_env = states["RunCodeBuild"]["Parameters"]["EnvironmentVariablesOverride"]
    direct_env = states["RunCodeBuildDirect"]["Parameters"]["EnvironmentVariablesOverride"]

    expected_names = _PRE_DIRECT_MODE_ENV_NAMES + ["EXECUTION_MODE"]
    assert [entry["Name"] for entry in default_env] == expected_names
    # The direct task's list starts with the structurally identical base list.
    assert direct_env[: len(default_env)] == default_env

    expected_paths = dict(_PRE_DIRECT_MODE_ENV_PATHS, EXECUTION_MODE="$.execution_mode")
    for entry in default_env:
        assert entry["Value.$"] == expected_paths[entry["Name"]]
        assert entry["Type"] == "PLAINTEXT"
        assert set(entry) == {"Name", "Value.$", "Type"}


def test_rendered_default_task_matches_baseline_plus_execution_mode_only(rendered_asl):
    task = rendered_asl["States"]["RunCodeBuild"]
    params = task["Parameters"]

    # Exactly the pre-direct-mode Parameters shape: the same three keys, no
    # direct-only override keys.
    assert set(params) == {
        "ProjectName",
        "TimeoutInMinutesOverride.$",
        "EnvironmentVariablesOverride",
    }
    assert params["ProjectName"] == _SENTINELS["aws_codebuild_project.worker.name"]
    assert params["TimeoutInMinutesOverride.$"] == "$.build_timeout_minutes"

    # The env list is the pre-direct-mode ten entries plus exactly the one
    # EXECUTION_MODE entry - no other drift.
    env = params["EnvironmentVariablesOverride"]
    baseline = [
        {"Name": name, "Value.$": _PRE_DIRECT_MODE_ENV_PATHS[name], "Type": "PLAINTEXT"}
        for name in _PRE_DIRECT_MODE_ENV_NAMES
    ]
    assert env == baseline + [
        {"Name": "EXECUTION_MODE", "Value.$": "$.execution_mode", "Type": "PLAINTEXT"}
    ]

    # Non-Parameters plumbing unchanged from baseline.
    assert task["TimeoutSecondsPath"] == "$.sfn_timeout_seconds"
    assert task["Catch"] == [
        {
            "ErrorEquals": ["States.ALL"],
            "ResultPath": "$.terminal_context",
            "Next": "FinalizeResult",
        }
    ]


def test_rendered_direct_task_carries_exactly_the_direct_only_additions(rendered_asl):
    states = rendered_asl["States"]
    default_params = states["RunCodeBuild"]["Parameters"]
    direct_params = states["RunCodeBuildDirect"]["Parameters"]

    # Exactly the four direct-only override Parameters beyond the shared shape.
    assert set(direct_params) - set(default_params) == {
        "BuildspecOverride",
        "ImageOverride",
        "PrivilegedModeOverride",
        "ImagePullCredentialsTypeOverride",
    }
    assert direct_params["BuildspecOverride"] == _SENTINELS["local.direct_mode_buildspec"]
    assert direct_params["ImageOverride"] == "aws/codebuild/standard:7.0"
    assert direct_params["PrivilegedModeOverride"] is True
    assert direct_params["ImagePullCredentialsTypeOverride"] == "CODEBUILD"
    assert direct_params["ProjectName"] == default_params["ProjectName"]
    assert direct_params["TimeoutInMinutesOverride.$"] == "$.build_timeout_minutes"

    # Env list: exactly the shared eleven followed by exactly the four
    # direct-only static env vars, in order, with plain Value (not Value.$).
    env = direct_params["EnvironmentVariablesOverride"]
    base_len = len(default_params["EnvironmentVariablesOverride"])
    extras = env[base_len:]
    assert extras == [
        {
            "Name": "ENGINE_ZIP_S3_BUCKET",
            "Value": _SENTINELS["var.engine_zip_s3_bucket"],
            "Type": "PLAINTEXT",
        },
        {
            "Name": "ENGINE_ZIP_S3_KEY",
            "Value": _SENTINELS["var.engine_zip_s3_key"],
            "Type": "PLAINTEXT",
        },
        {
            "Name": "SOPS_URL",
            "Value": _SENTINELS["local.direct_mode_sops_url"],
            "Type": "PLAINTEXT",
        },
        {
            "Name": "AGE_URL",
            "Value": _SENTINELS["local.direct_mode_age_url"],
            "Type": "PLAINTEXT",
        },
    ]


def test_rendered_normalize_pass_merges_defaults_for_all_eleven_keys(rendered_asl):
    normalize = rendered_asl["States"]["NormalizePayload"]
    merged_expr = normalize["Parameters"]["merged.$"]
    assert merged_expr.startswith("States.JsonMerge(States.StringToJson('")
    assert merged_expr.endswith("'), $$.Execution.Input, false)")

    defaults = json.loads(merged_expr.split("StringToJson('", 1)[1].split("')", 1)[0])
    assert defaults == {
        "trigger_id": "",
        "s3_package_uri": "",
        "sops_type": "",
        "sops_path": "",
        "commands_b64": "",
        "done_endpoint": "",
        "execution_target": "",
        "timeout_seconds": "",
        "callback_url": "",
        "callback_token": "",
        "execution_mode": "",
    }
    assert normalize["OutputPath"] == "$.merged"
