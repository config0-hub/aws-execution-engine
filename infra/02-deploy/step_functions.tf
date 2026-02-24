resource "aws_sfn_state_machine" "watchdog" {
  name     = "${local.prefix}-watchdog"
  role_arn = aws_iam_role.step_functions.arn

  definition = jsonencode({
    Comment = "Watchdog: polls for order completion, writes timed_out if exceeded"
    StartAt = "CheckResult"
    States = {
      CheckResult = {
        Type       = "Task"
        Resource   = aws_lambda_function.watchdog_check.arn
        ResultPath = "$.taskResult"
        Next       = "IsDone"
      }
      IsDone = {
        Type = "Choice"
        Choices = [
          {
            Variable      = "$.taskResult.done"
            BooleanEquals = true
            Next          = "Succeed"
          }
        ]
        Default = "WaitStep"
      }
      WaitStep = {
        Type    = "Wait"
        Seconds = 60
        Next    = "CheckResult"
      }
      Succeed = {
        Type = "Succeed"
      }
    }
  })
}

resource "aws_iam_role" "step_functions" {
  name = "${local.prefix}-sfn-role"

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

resource "aws_iam_role_policy" "step_functions" {
  name = "${local.prefix}-sfn-policy"
  role = aws_iam_role.step_functions.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "lambda:InvokeFunction"
        Resource = aws_lambda_function.watchdog_check.arn
      }
    ]
  })
}
