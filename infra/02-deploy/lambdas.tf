locals {
  lambda_env = {
    AWS_EXE_SYS_INTERNAL_BUCKET = aws_s3_bucket.internal.id
    AWS_EXE_SYS_DONE_BUCKET     = aws_s3_bucket.done.id
  }
}

# --- init_job: pure orchestration. No Layers needed. ---

resource "aws_lambda_function" "init_job" {
  function_name = "${local.prefix}-init-job"
  role          = aws_iam_role.init_job.arn
  package_type  = "Image"
  image_uri     = var.engine_image_uri
  architectures = ["x86_64"]

  image_config {
    entry_point = ["/usr/local/bin/python3", "-m", "awslambdaric"]
    command     = ["aws_exe_sys.init_job.handler.handler"]
  }

  timeout     = local.default_lambda_timeout > 0 ? local.default_lambda_timeout : 300
  memory_size = local.default_lambda_memory > 0 ? local.default_lambda_memory : 512

  environment {
    variables = merge(
      local.lambda_env,
      {
        AWS_EXE_SYS_WORKER_LAMBDA               = "${local.prefix}-worker"
        AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN = aws_sfn_state_machine.codebuild.arn
      },
    )
  }
}

resource "aws_lambda_function_url" "init_job" {
  function_name      = aws_lambda_function.init_job.function_name
  authorization_type = "AWS_IAM"
}

# --- finalizer: atomically creates only a missing failed CodeBuild result. ---

resource "aws_lambda_function" "finalizer" {
  function_name = "${local.prefix}-finalizer"
  role          = aws_iam_role.finalizer.arn
  package_type  = "Image"
  image_uri     = var.engine_image_uri
  architectures = ["x86_64"]

  image_config {
    entry_point = ["/usr/local/bin/python3", "-m", "awslambdaric"]
    command     = ["aws_exe_sys.finalizer.handler.handler"]
  }

  timeout     = 30
  memory_size = 128
}

# --- worker: executes payload commands and decrypts optional SOPS payloads. ---

resource "aws_lambda_function" "worker" {
  function_name = "${local.prefix}-worker"
  role          = aws_iam_role.worker.arn
  package_type  = "Image"
  image_uri     = var.engine_image_uri
  architectures = ["x86_64"]

  image_config {
    entry_point = ["/usr/local/bin/python3", "-m", "awslambdaric"]
    command     = ["aws_exe_sys.worker.handler.handler"]
  }

  timeout     = local.default_lambda_timeout > 0 ? local.default_lambda_timeout : 600
  memory_size = local.default_lambda_memory > 0 ? local.default_lambda_memory : 2048

  # Commands may unpack large caller-provided packages and their dependencies.
  ephemeral_storage {
    size = 2048
  }

  environment {
    variables = local.lambda_env
  }
}
