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

  # 900 is the AWS upper bound: the last layer, never reached. The payload's
  # timeout_seconds (T, at most 800 for the lambda target) is the clock that
  # ends the work; the worker kills the run and writes the result at T.
  timeout     = local.default_lambda_timeout > 0 ? local.default_lambda_timeout : 900
  memory_size = local.default_lambda_memory > 0 ? local.default_lambda_memory : 2048

  # Commands may unpack large caller-provided packages and their dependencies.
  ephemeral_storage {
    size = 2048
  }

  environment {
    variables = local.lambda_env
  }
}

# --- Async invoke policy: AWS never re-invokes a dead engine Lambda. ---
#
# init_job invokes the worker with InvocationType=Event; the Lambda service
# would retry a failed async invoke twice by default, re-running work the
# order already fired. The run-cycle contract's timeout rule sets retries to
# 0 for every engine Lambda. A Lambda that dies anyway (crash, out of memory,
# function timeout) is reported to the failures queue so people can see the
# death; the order itself is failed by its caller when no result marker
# arrives. Nothing consumes the queue on the order's behalf. An async invoke
# the Lambda service cannot start within 60 seconds is reported there too,
# not started later against an order its caller has already failed.
#
# The finalizer carries the same config. Today its only invoker is the
# CodeBuild state machine's FinalizeResult task (step_functions.tf,
# states:::lambda:invoke, a RequestResponse call that Step Functions itself
# retries), so the Lambda service's async retry never applies to it; the
# config closes the door should anything ever invoke it with Event.

resource "aws_sqs_queue" "lambda_failures" {
  name = "${local.prefix}-lambda-failures"
}

resource "aws_lambda_function_event_invoke_config" "init_job" {
  function_name                = aws_lambda_function.init_job.function_name
  maximum_event_age_in_seconds = 60
  maximum_retry_attempts       = 0

  destination_config {
    on_failure {
      destination = aws_sqs_queue.lambda_failures.arn
    }
  }
}

resource "aws_lambda_function_event_invoke_config" "worker" {
  function_name                = aws_lambda_function.worker.function_name
  maximum_event_age_in_seconds = 60
  maximum_retry_attempts       = 0

  destination_config {
    on_failure {
      destination = aws_sqs_queue.lambda_failures.arn
    }
  }
}

resource "aws_lambda_function_event_invoke_config" "finalizer" {
  function_name                = aws_lambda_function.finalizer.function_name
  maximum_event_age_in_seconds = 60
  maximum_retry_attempts       = 0

  destination_config {
    on_failure {
      destination = aws_sqs_queue.lambda_failures.arn
    }
  }
}
