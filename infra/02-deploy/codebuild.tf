resource "aws_codebuild_project" "worker" {
  name         = "${local.prefix}-worker"
  service_role = aws_iam_role.codebuild.arn

  artifacts {
    type = "NO_ARTIFACTS"
  }

  environment {
    compute_type                = local.codebuild_compute
    image                       = local.image_uri
    type                        = "LINUX_CONTAINER"
    privileged_mode             = false
    image_pull_credentials_type = "SERVICE_ROLE"

    environment_variable {
      name  = "AWS_EXE_SYS_ORDERS_TABLE"
      value = aws_dynamodb_table.orders.name
    }

    environment_variable {
      name  = "AWS_EXE_SYS_ORDER_EVENTS_TABLE"
      value = aws_dynamodb_table.order_events.name
    }

    environment_variable {
      name  = "AWS_EXE_SYS_LOCKS_TABLE"
      value = aws_dynamodb_table.orchestrator_locks.name
    }

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
    buildspec = "version: 0.2\nphases:\n  build:\n    commands:\n      - /entrypoint.sh\n"
  }
}
