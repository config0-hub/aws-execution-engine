# --- Shared Lambda assume-role policy ---

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# --- CloudWatch Logs policy (attached to all Lambda roles) ---

data "aws_iam_policy_document" "lambda_logs" {
  statement {
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = ["arn:aws:logs:${local.region}:${local.account_id}:*"]
  }
}

# ============================================================
# init_job
# ============================================================

resource "aws_iam_role" "init_job" {
  name               = "${local.prefix}-init-job"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "init_job" {
  name = "${local.prefix}-init-job"
  role = aws_iam_role.init_job.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query"]
        Resource = [
          aws_dynamodb_table.orders.arn,
          "${aws_dynamodb_table.orders.arn}/index/*",
          aws_dynamodb_table.order_events.arn,
          "${aws_dynamodb_table.order_events.arn}/index/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.internal.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:PutParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/exe-sys/sops-keys/*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "init_job_logs" {
  name   = "logs"
  role   = aws_iam_role.init_job.id
  policy = data.aws_iam_policy_document.lambda_logs.json
}

# ============================================================
# orchestrator
# ============================================================

resource "aws_iam_role" "orchestrator" {
  name               = "${local.prefix}-orchestrator"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "orchestrator" {
  name = "${local.prefix}-orchestrator"
  role = aws_iam_role.orchestrator.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
        ]
        Resource = [
          aws_dynamodb_table.orders.arn,
          "${aws_dynamodb_table.orders.arn}/index/*",
          aws_dynamodb_table.order_events.arn,
          "${aws_dynamodb_table.order_events.arn}/index/*",
          aws_dynamodb_table.orchestrator_locks.arn,
        ]
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject", "s3:PutObject"]
        Resource = [
          "${aws_s3_bucket.internal.arn}/*",
          "${aws_s3_bucket.done.arn}/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.worker.arn
      },
      {
        Effect   = "Allow"
        Action   = ["codebuild:StartBuild"]
        Resource = aws_codebuild_project.worker.arn
      },
      {
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = aws_sfn_state_machine.watchdog.arn
      },
      {
        Effect = "Allow"
        Action = ["ssm:SendCommand"]
        Resource = [
          aws_ssm_document.run_commands.arn,
          "arn:aws:ec2:${local.region}:${local.account_id}:instance/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:DeleteParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/exe-sys/sops-keys/*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "orchestrator_logs" {
  name   = "logs"
  role   = aws_iam_role.orchestrator.id
  policy = data.aws_iam_policy_document.lambda_logs.json
}

# ============================================================
# watchdog_check
# ============================================================

resource "aws_iam_role" "watchdog_check" {
  name               = "${local.prefix}-watchdog-check"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "watchdog_check" {
  name = "${local.prefix}-watchdog-check"
  role = aws_iam_role.watchdog_check.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:PutObject"]
        Resource = "${aws_s3_bucket.internal.arn}/tmp/callbacks/runs/*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "watchdog_check_logs" {
  name   = "logs"
  role   = aws_iam_role.watchdog_check.id
  policy = data.aws_iam_policy_document.lambda_logs.json
}

# ============================================================
# worker
# ============================================================

resource "aws_iam_role" "worker" {
  name               = "${local.prefix}-worker"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "worker" {
  name = "${local.prefix}-worker"
  role = aws_iam_role.worker.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.internal.arn}/tmp/exec/*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.order_events.arn
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/exe-sys/sops-keys/*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "worker_logs" {
  name   = "logs"
  role   = aws_iam_role.worker.id
  policy = data.aws_iam_policy_document.lambda_logs.json
}

# ============================================================
# CodeBuild service role
# ============================================================

resource "aws_iam_role" "codebuild" {
  name = "${local.prefix}-codebuild"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "codebuild.amazonaws.com"
        }
      }
    ]
  })
}

resource "aws_iam_role_policy" "codebuild" {
  name = "${local.prefix}-codebuild"
  role = aws_iam_role.codebuild.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "ecr:GetAuthorizationToken",
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "${aws_s3_bucket.internal.arn}/tmp/exec/*"
      },
      {
        Effect   = "Allow"
        Action   = ["dynamodb:PutItem"]
        Resource = aws_dynamodb_table.order_events.arn
      },
      {
        Effect = "Allow"
        Action = [
          "logs:CreateLogGroup",
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = "arn:aws:logs:${local.region}:${local.account_id}:*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/exe-sys/sops-keys/*"
      },
    ]
  })
}

# ============================================================
# ssm_config
# ============================================================

resource "aws_iam_role" "ssm_config" {
  name               = "${local.prefix}-ssm-config"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "ssm_config" {
  name = "${local.prefix}-ssm-config"
  role = aws_iam_role.ssm_config.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["dynamodb:PutItem", "dynamodb:GetItem", "dynamodb:Query"]
        Resource = [
          aws_dynamodb_table.orders.arn,
          aws_dynamodb_table.order_events.arn,
          "${aws_dynamodb_table.order_events.arn}/index/*",
        ]
      },
      {
        Effect   = "Allow"
        Action   = ["s3:PutObject", "s3:GetObject"]
        Resource = "${aws_s3_bucket.internal.arn}/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/*"
      },
      {
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = "arn:aws:secretsmanager:${local.region}:${local.account_id}:secret:*"
      },
    ]
  })
}

resource "aws_iam_role_policy" "ssm_config_logs" {
  name   = "logs"
  role   = aws_iam_role.ssm_config.id
  policy = data.aws_iam_policy_document.lambda_logs.json
}
