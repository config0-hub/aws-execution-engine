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

  # S3 build logs land at s3://<bucket>/codebuild/logs/<build-id>.gz — the
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
    compute_type                = local.codebuild_compute
    image                       = "aws/codebuild/standard:7.0"
    type                        = "LINUX_CONTAINER"
    privileged_mode             = true
    image_pull_credentials_type = "CODEBUILD"

    environment_variable {
      name  = "AWS_EXE_SYS_INTERNAL_BUCKET"
      value = aws_s3_bucket.internal.id
    }

    environment_variable {
      name  = "AWS_EXE_SYS_DONE_BUCKET"
      value = aws_s3_bucket.done.id
    }

    environment_variable {
      name  = "ENGINE_ZIP_S3_BUCKET"
      value = var.engine_zip_s3_bucket
    }

    environment_variable {
      name  = "ENGINE_ZIP_S3_KEY"
      value = var.engine_zip_s3_key
    }

    environment_variable {
      name  = "SOPS_URL"
      value = "https://github.com/getsops/sops/releases/download/v3.9.4/sops-v3.9.4.linux.amd64"
    }

    environment_variable {
      name  = "AGE_URL"
      value = "https://dl.filippo.io/age/v1.2.1?for=linux/amd64"
    }
  }

  source {
    type      = "NO_SOURCE"
    buildspec = <<-BUILDSPEC
      version: 0.2
      phases:
        install:
          commands:
            - curl -fsSL "$SOPS_URL" -o /usr/local/bin/sops && chmod +x /usr/local/bin/sops
            - curl -fsSL "$AGE_URL" | tar xz --strip-components=1 -C /usr/local/bin age/age age/age-keygen
        build:
          commands:
            - aws s3 cp "s3://$ENGINE_ZIP_S3_BUCKET/$ENGINE_ZIP_S3_KEY" /tmp/engine.zip
            - mkdir -p /work && unzip -q /tmp/engine.zip -d /work
            - ENGINE_TASK_ROOT=/work bash /work/aws_exe_sys/worker/entrypoint.sh
    BUILDSPEC
  }
}
