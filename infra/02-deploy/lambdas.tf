locals {
  lambda_env = {
    AWS_EXE_SYS_ORDERS_TABLE       = aws_dynamodb_table.orders.name
    AWS_EXE_SYS_ORDER_EVENTS_TABLE = aws_dynamodb_table.order_events.name
    AWS_EXE_SYS_LOCKS_TABLE        = aws_dynamodb_table.orchestrator_locks.name
    AWS_EXE_SYS_INTERNAL_BUCKET    = aws_s3_bucket.internal.id
    AWS_EXE_SYS_DONE_BUCKET        = aws_s3_bucket.done.id
    AWS_EXE_SYS_SSM_PREFIX         = "exe-sys"
  }
}

# --- init_job ---

resource "aws_lambda_function" "init_job" {
  function_name = "${local.prefix}-init-job"
  role          = aws_iam_role.init_job.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  timeout       = local.default_lambda_timeout > 0 ? local.default_lambda_timeout : 300
  memory_size   = local.default_lambda_memory > 0 ? local.default_lambda_memory : 512

  image_config {
    command = ["src.init_job.handler.handler"]
  }

  environment {
    variables = merge(local.lambda_env, {
      JWT_SECRET_SSM_PATH = var.jwt_secret_ssm_path
    })
  }
}

resource "aws_lambda_function_url" "init_job" {
  function_name      = aws_lambda_function.init_job.function_name
  authorization_type = "AWS_IAM"
}

# --- orchestrator ---

resource "aws_lambda_function" "orchestrator" {
  function_name = "${local.prefix}-orchestrator"
  role          = aws_iam_role.orchestrator.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  timeout       = local.default_lambda_timeout > 0 ? local.default_lambda_timeout : 600
  memory_size   = local.default_lambda_memory > 0 ? local.default_lambda_memory : 512

  image_config {
    command = ["src.orchestrator.handler.handler"]
  }

  environment {
    variables = merge(local.lambda_env, {
      AWS_EXE_SYS_WORKER_LAMBDA     = "${local.prefix}-worker"
      AWS_EXE_SYS_CODEBUILD_PROJECT = aws_codebuild_project.worker.name
      AWS_EXE_SYS_WATCHDOG_SFN      = aws_sfn_state_machine.watchdog.arn
      AWS_EXE_SYS_SSM_DOCUMENT      = aws_ssm_document.run_commands.name
    })
  }
}

# --- watchdog_check ---

resource "aws_lambda_function" "watchdog_check" {
  function_name = "${local.prefix}-watchdog-check"
  role          = aws_iam_role.watchdog_check.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  timeout       = local.default_lambda_timeout > 0 ? local.default_lambda_timeout : 60
  memory_size   = local.default_lambda_memory > 0 ? local.default_lambda_memory : 256

  image_config {
    command = ["src.watchdog_check.handler.handler"]
  }

  environment {
    variables = local.lambda_env
  }
}

# --- worker ---

resource "aws_lambda_function" "worker" {
  function_name = "${local.prefix}-worker"
  role          = aws_iam_role.worker.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  timeout       = local.default_lambda_timeout > 0 ? local.default_lambda_timeout : 600
  memory_size   = local.default_lambda_memory > 0 ? local.default_lambda_memory : 1024

  image_config {
    command = ["src.worker.handler.handler"]
  }

  environment {
    variables = local.lambda_env
  }
}

# --- ssm_config ---

resource "aws_lambda_function" "ssm_config" {
  function_name = "${local.prefix}-ssm-config"
  role          = aws_iam_role.ssm_config.arn
  package_type  = "Image"
  image_uri     = local.image_uri
  timeout       = local.default_lambda_timeout > 0 ? local.default_lambda_timeout : 300
  memory_size   = local.default_lambda_memory > 0 ? local.default_lambda_memory : 512

  image_config {
    command = ["src.ssm_config.handler.handler"]
  }

  environment {
    variables = local.lambda_env
  }
}
