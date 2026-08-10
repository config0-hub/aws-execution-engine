resource "aws_codebuild_project" "worker" {
  name         = "${local.prefix}-worker"
  service_role = aws_iam_role.codebuild.arn

  # Hard runtime bound: the platform watch gives 900s from FIRE = 600s build
  # + 300s queue/provisioning, and CodeBuild's own clock starts AFTER
  # provisioning - a 15-minute clock could outlive the watch and race a
  # requeue. 10 minutes (600s) matches the authored caps exactly, so even a
  # provisioning-delayed build dies inside the watch window.
  build_timeout = 10

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
