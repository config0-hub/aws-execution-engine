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

locals {
  # Every SimplePayload field the RunCodeBuild / RunCodeBuildDirect task
  # states (and the RouteExecutionMode Choice) reference by JSONPath. A
  # missing key in the execution input makes that reference throw
  # States.Runtime, which is uncatchable (a Catch on States.ALL does NOT catch
  # it), so FinalizeResult would never run and no done-marker would be written.
  # NormalizePayload merges these empty-string defaults under the execution
  # input so every referenced key always exists.
  codebuild_payload_defaults = {
    trigger_id       = ""
    s3_package_uri   = ""
    sops_type        = ""
    sops_path        = ""
    commands_b64     = ""
    done_endpoint    = ""
    execution_target = ""
    timeout_seconds  = ""
    callback_url     = ""
    callback_token   = ""
    execution_mode   = ""
  }

  # The one shared 11-entry EnvironmentVariablesOverride list, built from the
  # codebuild_payload_defaults keys above. BOTH Task states (RunCodeBuild and
  # RunCodeBuildDirect) carry exactly this list - the four direct-only env
  # vars (ENGINE_ZIP_S3_BUCKET / ENGINE_ZIP_S3_KEY / SOPS_URL / AGE_URL) are
  # appended ONLY on RunCodeBuildDirect's own Parameters, never here.
  codebuild_env_overrides = [
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
    {
      Name      = "TIMEOUT_SECONDS"
      "Value.$" = "$.timeout_seconds"
      Type      = "PLAINTEXT"
    },
    {
      Name      = "CALLBACK_URL"
      "Value.$" = "$.callback_url"
      Type      = "PLAINTEXT"
    },
    {
      Name      = "CALLBACK_TOKEN"
      "Value.$" = "$.callback_token"
      Type      = "PLAINTEXT"
    },
    {
      Name      = "EXECUTION_MODE"
      "Value.$" = "$.execution_mode"
      Type      = "PLAINTEXT"
    },
  ]
}

resource "aws_sfn_state_machine" "codebuild" {
  name     = "${local.prefix}-codebuild"
  role_arn = aws_iam_role.codebuild_workflow.arn
  type     = "STANDARD"

  definition = jsonencode({
    Comment = "Run the execution-engine CodeBuild worker and guarantee a terminal S3 result"
    StartAt = "NormalizePayload"
    States = {
      NormalizePayload = {
        Type = "Pass"
        # Defaults-normalization: shallow-merge empty-string defaults for all
        # eleven payload keys UNDER the execution input (input wins), so every
        # JSONPath the Choice and task states reference always resolves and
        # uncatchable States.Runtime templating failures cannot occur. The
        # defaults do NOT mask real requirements: an empty trigger_id /
        # done_endpoint etc. makes the worker fail loudly inside the container
        # and write its failed marker - the correct fail-loud path.
        Parameters = {
          "merged.$" = "States.JsonMerge(States.StringToJson('${jsonencode(local.codebuild_payload_defaults)}'), $$.Execution.Input, false)"
        }
        OutputPath = "$.merged"
        Next       = "RouteExecutionMode"
      }
      RouteExecutionMode = {
        # Dispatch-only discriminator (wire contract v5.1): execution_mode is
        # read HERE, once, before either Task fires - never inside the
        # container. "direct" selects the pre-c013a7b delivery path
        # (standard:7.0 privileged + static buildspec pulling engine.zip from
        # S3); anything else (the normalized "" default) takes today's
        # engine-image path unchanged.
        Type = "Choice"
        Choices = [
          {
            Variable     = "$.execution_mode"
            StringEquals = "direct"
            Next         = "RunCodeBuildDirect"
          },
        ]
        Default = "RunCodeBuild"
      }
      RunCodeBuild = {
        Type     = "Task"
        Resource = "arn:${data.aws_partition.current.partition}:states:::codebuild:startBuild.sync"
        # Deadline-driven wall-clock backstop over the whole startBuild.sync
        # task: the dispatcher computes sfn_timeout_seconds =
        # timeout_seconds + queued bound (300s) + provisioning margin (300s)
        # and passes it in the execution input - the PROVISIONING phase is not
        # explicitly assigned to either CodeBuild clock, so the margin covers
        # it. A stuck build times the state out into the
        # Catch -> FinalizeResult path instead of hanging the workflow forever.
        TimeoutSecondsPath = "$.sfn_timeout_seconds"
        Parameters = {
          ProjectName = aws_codebuild_project.worker.name
          # Per-build override derived from timeout_seconds + margin (ceil to
          # minutes) by the dispatcher; the project's static build_timeout is
          # only a generous ceiling the override stays under.
          "TimeoutInMinutesOverride.$" = "$.build_timeout_minutes"
          EnvironmentVariablesOverride = local.codebuild_env_overrides
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
      RunCodeBuildDirect = {
        # Direct mode (execution_mode = "direct"): START-time identical to
        # RunCodeBuild - same project, same shared 11-entry env-override list,
        # same Catch -> FinalizeResult, same sfn_timeout_seconds /
        # build_timeout_minutes threading - PLUS the eight direct-only
        # StartBuild Parameters below, which exist ONLY on this Task: the
        # static dispatcher-owned buildspec (codebuild.tf locals), the managed
        # standard:7.0 image, privileged mode (docker-in-docker), CODEBUILD
        # pull credentials (public AWS-managed image; the project's
        # SERVICE_ROLE default is for the private tenant ECR pull), and the
        # four env vars the buildspec reads to fetch/run engine.zip.
        Type               = "Task"
        Resource           = "arn:${data.aws_partition.current.partition}:states:::codebuild:startBuild.sync"
        TimeoutSecondsPath = "$.sfn_timeout_seconds"
        Parameters = {
          ProjectName                      = aws_codebuild_project.worker.name
          "TimeoutInMinutesOverride.$"     = "$.build_timeout_minutes"
          BuildspecOverride                = local.direct_mode_buildspec
          ImageOverride                    = "aws/codebuild/standard:7.0"
          PrivilegedModeOverride           = true
          ImagePullCredentialsTypeOverride = "CODEBUILD"
          EnvironmentVariablesOverride = concat(local.codebuild_env_overrides, [
            {
              Name  = "ENGINE_ZIP_S3_BUCKET"
              Value = var.engine_zip_s3_bucket
              Type  = "PLAINTEXT"
            },
            {
              Name  = "ENGINE_ZIP_S3_KEY"
              Value = var.engine_zip_s3_key
              Type  = "PLAINTEXT"
            },
            {
              Name  = "SOPS_URL"
              Value = local.direct_mode_sops_url
              Type  = "PLAINTEXT"
            },
            {
              Name  = "AGE_URL"
              Value = local.direct_mode_age_url
              Type  = "PLAINTEXT"
            },
          ])
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
