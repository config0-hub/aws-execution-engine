resource "aws_codebuild_project" "worker" {
  name         = "${local.prefix}-worker"
  service_role = aws_iam_role.codebuild.arn

  # Two clocks bound the build against the platform's 900s watch from FIRE:
  #   queued_timeout (5 min = 300s) + build_timeout (10 min = 600s) = 900s.
  # Verified AWS semantics (CodeBuild API reference, Project type):
  #   - queuedTimeoutInMinutes: "The number of minutes a build is allowed to
  #     be queued before it times out." Covers the QUEUED phase only.
  #     Minimum allowed value is 5, so 300s is the tightest enforceable bound.
  #   - timeoutInMinutes: "How long, in minutes ... for AWS CodeBuild to wait
  #     before timing out any related build that did not get marked as
  #     completed." This is the post-queue build clock.
  # HONEST GAP: AWS does not explicitly document which clock the PROVISIONING
  # phase counts against. If provisioning falls outside both clocks, the
  # 300 + 600 arithmetic is not a proof; the Step Functions TimeoutSeconds
  # (step_functions.tf) stays the wall-clock backstop that bounds the whole
  # startBuild.sync task, provisioning included.
  build_timeout  = 10
  queued_timeout = 5

  artifacts {
    type = "NO_ARTIFACTS"
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
