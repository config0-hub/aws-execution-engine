resource "aws_iam_role" "codebuild_workflow" {
  name = "${local.prefix}-codebuild-workflow"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "states.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "codebuild_workflow" {
  name = "${local.prefix}-codebuild-workflow"
  role = aws_iam_role.codebuild_workflow.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "codebuild:StartBuild",
          "codebuild:StopBuild",
          "codebuild:BatchGetBuilds",
        ]
        Resource = aws_codebuild_project.worker.arn
      },
      {
        Effect = "Allow"
        Action = [
          "events:PutTargets",
          "events:PutRule",
          "events:DescribeRule",
        ]
        Resource = "arn:${data.aws_partition.current.partition}:events:${local.region}:${local.account_id}:rule/StepFunctionsGetEventForCodeBuildStartBuildRule"
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.finalizer.arn
      },
    ]
  })
}

resource "aws_sfn_state_machine" "codebuild" {
  name     = "${local.prefix}-codebuild"
  role_arn = aws_iam_role.codebuild_workflow.arn
  type     = "STANDARD"

  definition = jsonencode({
    Comment = "Run the execution-engine CodeBuild worker and guarantee a terminal S3 result"
    StartAt = "RunCodeBuild"
    States = {
      RunCodeBuild = {
        Type     = "Task"
        Resource = "arn:${data.aws_partition.current.partition}:states:::codebuild:startBuild.sync"
        # build_timeout (10 min = 600s) + 300s queue/provisioning + margin: a stuck
        # startBuild.sync times the state out into the Catch -> FinalizeResult
        # path instead of hanging the workflow forever.
        TimeoutSeconds = 1200
        Parameters = {
          ProjectName = aws_codebuild_project.worker.name
          EnvironmentVariablesOverride = [
            {
              Name      = "TRIGGER_ID"
              "Value.$" = "$.trigger_id"
              Type      = "PLAINTEXT"
            },
            {
              Name      = "S3_PACKAGE_URI"
              "Value.$" = "$.s3_package_uri"
              Type      = "PLAINTEXT"
            },
            {
              Name      = "SOPS_TYPE"
              "Value.$" = "$.sops_type"
              Type      = "PLAINTEXT"
            },
            {
              Name      = "SOPS_PATH"
              "Value.$" = "$.sops_path"
              Type      = "PLAINTEXT"
            },
            {
              Name      = "COMMANDS_B64"
              "Value.$" = "$.commands_b64"
              Type      = "PLAINTEXT"
            },
            {
              Name      = "DONE_ENDPOINT"
              "Value.$" = "$.done_endpoint"
              Type      = "PLAINTEXT"
            },
            {
              Name      = "EXECUTION_TARGET"
              "Value.$" = "$.execution_target"
              Type      = "PLAINTEXT"
            },
          ]
        }
        ResultSelector = {
          outcome = "succeeded"
        }
        ResultPath = "$.terminal_context"
        Catch = [
          {
            ErrorEquals = ["States.ALL"]
            ResultPath  = "$.terminal_context"
            Next        = "FinalizeResult"
          },
        ]
        Next = "FinalizeResult"
      }
      FinalizeResult = {
        Type     = "Task"
        Resource = "arn:${data.aws_partition.current.partition}:states:::lambda:invoke"
        Parameters = {
          FunctionName = aws_lambda_function.finalizer.arn
          Payload = {
            "trigger_id.$"       = "$.trigger_id"
            "done_endpoint.$"    = "$.done_endpoint"
            "terminal_context.$" = "$.terminal_context"
          }
        }
        Retry = [
          {
            ErrorEquals = [
              "Lambda.ServiceException",
              "Lambda.AWSLambdaException",
              "Lambda.SdkClientException",
              "Lambda.TooManyRequestsException",
            ]
            IntervalSeconds = 2
            MaxAttempts     = 3
            BackoffRate     = 2
          },
          {
            ErrorEquals     = ["States.TaskFailed"]
            IntervalSeconds = 2
            MaxAttempts     = 2
            BackoffRate     = 2
          },
        ]
        OutputPath = "$.Payload"
        End        = true
      }
    }
  })
}
