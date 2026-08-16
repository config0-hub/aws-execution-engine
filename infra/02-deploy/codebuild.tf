# The engine's own image, mirrored into the tenant ECR repo by
# infra/01-ecr + scripts/mirror-image.sh. It bakes the engine code at
# /opt/engine (ENGINE_TASK_ROOT) with tofu/sops/age on PATH - docker/Dockerfile.
data "aws_ecr_repository" "engine" {
  name = var.engine_ecr_repository_name
}

# Existence gate, not a pin: CodeBuild still runs `:latest` (see `image`
# below). Without this, `tofu plan/apply` plans green against an empty repo
# and every build then fails at PROVISIONING. This forces a fail-loud error
# here instead, when no image has been mirrored yet (infra/01-ecr +
# scripts/mirror-image.sh).
data "aws_ecr_image" "engine_latest" {
  repository_name = data.aws_ecr_repository.engine.name
  image_tag       = "latest"
}

resource "aws_codebuild_project" "worker" {
  name         = "${local.prefix}-worker"
  service_role = aws_iam_role.codebuild.arn

  # The REAL per-build bound is the TimeoutInMinutesOverride the Step
  # Functions RunCodeBuild task passes (derived from the payload's
  # timeout_seconds plus a small margin - step_functions.tf). The static
  # build_timeout here is only a generous ceiling that per-build overrides
  # stay under. queued_timeout covers the QUEUED phase only (minimum 5).
  # HONEST GAP: AWS does not explicitly document which clock the PROVISIONING
  # phase counts against; the Step Functions state timeout
  # (sfn_timeout_seconds = timeout_seconds + queued bound + margin) stays the
  # wall-clock backstop that bounds the whole startBuild.sync task,
  # provisioning included.
  build_timeout  = 480
  queued_timeout = 5

  artifacts {
    type = "NO_ARTIFACTS"
  }

  # S3 build logs land at s3://<bucket>/codebuild/logs/<build-id>.gz - the
  # path the config0_publisher log reader expects. CloudWatch logs stay on.
  dynamic "logs_config" {
    for_each = var.s3_log_bucket_name != "" ? [1] : []
    content {
      s3_logs {
        status   = "ENABLED"
        location = "${var.s3_log_bucket_name}/codebuild/logs"
      }
    }
  }

  environment {
    compute_type = local.codebuild_compute
    image        = "${data.aws_ecr_repository.engine.repository_url}:latest"
    type         = "LINUX_CONTAINER"
    # privileged_mode off: nothing in the engine execution path runs docker
    # (docker appears in this repo only for local image build/test/mirror).
    privileged_mode             = false
    image_pull_credentials_type = "SERVICE_ROLE"

    environment_variable {
      name  = "AWS_EXE_SYS_INTERNAL_BUCKET"
      value = aws_s3_bucket.internal.id
    }

    environment_variable {
      name  = "AWS_EXE_SYS_DONE_BUCKET"
      value = aws_s3_bucket.done.id
    }
  }

  source {
    type      = "NO_SOURCE"
    buildspec = <<-BUILDSPEC
      version: 0.2
      phases:
        build:
          commands:
            - ENGINE_TASK_ROOT=/opt/engine bash /opt/engine/aws_exe_sys/worker/entrypoint.sh
    BUILDSPEC
  }

  # Forces data.aws_ecr_image.engine_latest into the plan/apply graph (an
  # unreferenced data source is otherwise dead weight) so a missing :latest
  # image fails loud here, at plan time, instead of the every-build
  # PROVISIONING failure this replaces.
  lifecycle {
    precondition {
      condition     = data.aws_ecr_image.engine_latest.id != ""
      error_message = "No :latest image in the ${var.engine_ecr_repository_name} ECR repo - run scripts/mirror-image.sh (or task ecr:mirror) before applying infra/02-deploy."
    }
  }
}
