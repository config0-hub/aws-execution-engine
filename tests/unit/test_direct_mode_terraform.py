"""Contract tests for direct mode (execution_mode = "direct") in Terraform.

Direct mode (wire contract v5.1) restores the pre-c013a7b delivery path as an
opt-in SFN branch: a static dispatcher-owned buildspec on
aws/codebuild/standard:7.0 privileged, pulling engine.zip from S3. These tests
pin what must NOT change (the default engine-image project resource) and what
the direct branch must carry (the buildspec locals, the restored IAM read).
"""

from pathlib import Path
import re

_INFRA = Path(__file__).resolve().parents[2] / "infra" / "02-deploy"
CODEBUILD_TERRAFORM = _INFRA / "codebuild.tf"
IAM_TERRAFORM = _INFRA / "iam.tf"


def _project_resource_block(source: str) -> str:
    """The aws_codebuild_project.worker resource body (up to its closing brace)."""
    match = re.search(
        r'^resource "aws_codebuild_project" "worker" \{\n(.*?)^\}\n', source, re.M | re.S
    )
    assert match, "aws_codebuild_project.worker not found"
    return match.group(1)


class TestCodebuildProjectResourceUnchanged:
    """The e4bdf9f engine-image baseline stays byte-relevant: no direct-mode
    leakage into the shared project resource (ADV3-002)."""

    def test_environment_variable_blocks_unchanged_from_baseline(self):
        project = _project_resource_block(CODEBUILD_TERRAFORM.read_text())
        env_var_names = re.findall(r'environment_variable \{\n\s*name  = "([A-Z0-9_]+)"', project)
        assert env_var_names == ["AWS_EXE_SYS_INTERNAL_BUCKET", "AWS_EXE_SYS_DONE_BUCKET"]
        for forbidden in ("ENGINE_ZIP_S3_BUCKET", "ENGINE_ZIP_S3_KEY", "SOPS_URL", "AGE_URL"):
            assert forbidden not in project, f"{forbidden} leaked into the project resource"

    def test_project_keeps_engine_image_nonprivileged_service_role(self):
        project = _project_resource_block(CODEBUILD_TERRAFORM.read_text())
        assert 'image        = "${data.aws_ecr_repository.engine.repository_url}:latest"' in project
        assert "privileged_mode             = false" in project
        assert 'image_pull_credentials_type = "SERVICE_ROLE"' in project
        # The project's own buildspec stays the one-line engine-image runner,
        # never the direct-mode buildspec.
        assert "ENGINE_TASK_ROOT=/opt/engine bash /opt/engine/aws_exe_sys/worker/entrypoint.sh" in project
        assert "direct_mode_buildspec" not in project


class TestDirectModeBuildspec:
    def test_buildspec_local_reinstates_pre_c013a7b_phases(self):
        source = CODEBUILD_TERRAFORM.read_text()
        buildspec = source.split("direct_mode_buildspec = <<-BUILDSPEC")[1].split("BUILDSPEC")[0]

        # install phase: pinned sops + age into /usr/local/bin.
        assert '- curl -fsSL "$SOPS_URL" -o /usr/local/bin/sops && chmod +x /usr/local/bin/sops' in buildspec
        assert '- curl -fsSL "$AGE_URL" | tar xz --strip-components=1 -C /usr/local/bin age/age age/age-keygen' in buildspec
        # build phase: engine.zip pull + ENGINE_TASK_ROOT entrypoint invocation.
        assert '- aws s3 cp "s3://$ENGINE_ZIP_S3_BUCKET/$ENGINE_ZIP_S3_KEY" /tmp/engine.zip' in buildspec
        assert "- mkdir -p /work && unzip -q /tmp/engine.zip -d /work" in buildspec
        assert "- ENGINE_TASK_ROOT=/work bash /work/aws_exe_sys/worker/entrypoint.sh" in buildspec
        # The buildspec only ever pulls the ENGINE artifact - the workload zip
        # (s3_package_uri) is fetched by the worker itself via fetch_code_s3.
        assert "s3_package_uri" not in buildspec
        assert "S3_PACKAGE_URI" not in buildspec

    def test_pinned_tool_urls(self):
        source = CODEBUILD_TERRAFORM.read_text()
        assert (
            'direct_mode_sops_url = "https://github.com/getsops/sops/releases/download/v3.9.4/sops-v3.9.4.linux.amd64"'
            in source
        )
        assert 'direct_mode_age_url  = "https://dl.filippo.io/age/v1.2.1?for=linux/amd64"' in source


class TestCodebuildRoleIam:
    def _codebuild_policy(self) -> str:
        source = IAM_TERRAFORM.read_text()
        return source.split('resource "aws_iam_role_policy" "codebuild" {')[1]

    def test_restored_engine_zip_s3_getobject_statement_present(self):
        policy = self._codebuild_policy()
        assert 'Resource = "arn:aws:s3:::${var.engine_zip_s3_bucket}/*"' in policy

    def test_ecr_pull_statements_not_dropped(self):
        policy = self._codebuild_policy()
        assert '"ecr:GetAuthorizationToken"' in policy
        assert '"ecr:BatchGetImage"' in policy
        assert '"ecr:GetDownloadUrlForLayer"' in policy
        assert '"ecr:BatchCheckLayerAvailability"' in policy
        assert "Resource = data.aws_ecr_repository.engine.arn" in policy
