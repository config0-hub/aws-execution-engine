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
        Action = ["s3:GetObject"]
        Resource = concat(
          ["${aws_s3_bucket.internal.arn}/*"],
          [for arn in var.additional_package_bucket_arns : "${arn}/*"],
        )
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/exe-sys/sops-keys/*"
      },
      {
        Effect   = "Allow"
        Action   = ["lambda:InvokeFunction"]
        Resource = aws_lambda_function.worker.arn
      },
      {
        Effect   = "Allow"
        Action   = ["states:StartExecution"]
        Resource = aws_sfn_state_machine.codebuild.arn
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
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = concat(
          ["${aws_s3_bucket.internal.arn}/*"],
          [for arn in var.additional_package_bucket_arns : "${arn}/*"],
        )
      },
      {
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = concat(
          ["${aws_s3_bucket.done.arn}/*"],
          [for arn in var.additional_result_bucket_arns : "${arn}/*"],
        )
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/exe-sys/sops-keys/*"
      },
      {
        Effect   = "Allow"
        Action   = ["ssm:DeleteParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/exe-sys/sops-keys/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.kms_key_arn
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
# finalizer
# ============================================================

resource "aws_iam_role" "finalizer" {
  name               = "${local.prefix}-finalizer"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy" "finalizer" {
  name = "${local.prefix}-finalizer"
  role = aws_iam_role.finalizer.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = concat(
          ["${aws_s3_bucket.done.arn}/*"],
          [for arn in var.additional_result_bucket_arns : "${arn}/*"],
        )
      },
    ]
  })
}

resource "aws_iam_role_policy" "finalizer_logs" {
  name   = "logs"
  role   = aws_iam_role.finalizer.id
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
        Effect   = "Allow"
        Action   = ["s3:GetObject"]
        Resource = "arn:aws:s3:::${var.engine_zip_s3_bucket}/*"
      },
      {
        Effect = "Allow"
        Action = ["s3:GetObject"]
        Resource = concat(
          ["${aws_s3_bucket.internal.arn}/*"],
          [for arn in var.additional_package_bucket_arns : "${arn}/*"],
        )
      },
      {
        Effect = "Allow"
        Action = ["s3:PutObject"]
        Resource = concat(
          ["${aws_s3_bucket.done.arn}/*"],
          [for arn in var.additional_result_bucket_arns : "${arn}/*"],
          # S3 build logs (codebuild.tf logs_config) — grant only the exact
          # codebuild/logs/ prefix when a log bucket is configured.
          var.s3_log_bucket_name != "" ? ["arn:aws:s3:::${var.s3_log_bucket_name}/codebuild/logs/*"] : [],
        )
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
      {
        Effect   = "Allow"
        Action   = ["ssm:DeleteParameter"]
        Resource = "arn:aws:ssm:${local.region}:${local.account_id}:parameter/exe-sys/sops-keys/*"
      },
      {
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.kms_key_arn
      },
    ]
  })
}
