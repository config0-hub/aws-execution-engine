output "api_gateway_url" {
  description = "API Gateway endpoint URL"
  value       = aws_apigatewayv2_api.api.api_endpoint
}

output "api_gateway_arn" {
  description = "API Gateway execution ARN"
  value       = aws_apigatewayv2_api.api.execution_arn
}

output "api_gateway_id" {
  description = "API Gateway ID"
  value       = aws_apigatewayv2_api.api.id
}

output "lambda_function_names" {
  description = "Map of Lambda function names"
  value = {
    init_job        = aws_lambda_function.init_job.function_name
    orchestrator    = aws_lambda_function.orchestrator.function_name
    watchdog_check  = aws_lambda_function.watchdog_check.function_name
    worker          = aws_lambda_function.worker.function_name
    ssm_config      = aws_lambda_function.ssm_config.function_name
  }
}

output "lambda_function_arns" {
  description = "Map of Lambda function ARNs"
  value = {
    init_job        = aws_lambda_function.init_job.arn
    orchestrator    = aws_lambda_function.orchestrator.arn
    watchdog_check  = aws_lambda_function.watchdog_check.arn
    worker          = aws_lambda_function.worker.arn
    ssm_config      = aws_lambda_function.ssm_config.arn
  }
}

output "orders_table_name" {
  description = "Orders DynamoDB table name"
  value       = aws_dynamodb_table.orders.name
}

output "orders_table_arn" {
  description = "Orders DynamoDB table ARN"
  value       = aws_dynamodb_table.orders.arn
}

output "order_events_table_name" {
  description = "Order events DynamoDB table name"
  value       = aws_dynamodb_table.order_events.name
}

output "order_events_table_arn" {
  description = "Order events DynamoDB table ARN"
  value       = aws_dynamodb_table.order_events.arn
}

output "dynamodb_table_names" {
  description = "Map of DynamoDB table names"
  value = {
    orders             = aws_dynamodb_table.orders.name
    order_events       = aws_dynamodb_table.order_events.name
    orchestrator_locks = aws_dynamodb_table.orchestrator_locks.name
  }
}

output "done_bucket_name" {
  description = "Done S3 bucket name"
  value       = aws_s3_bucket.done.bucket
}

output "done_bucket_arn" {
  description = "Done S3 bucket ARN"
  value       = aws_s3_bucket.done.arn
}

output "s3_bucket_names" {
  description = "Map of S3 bucket names"
  value = {
    internal = aws_s3_bucket.internal.id
    done     = aws_s3_bucket.done.id
  }
}

output "init_job_function_name" {
  description = "init_job Lambda function name"
  value       = aws_lambda_function.init_job.function_name
}

output "init_job_function_arn" {
  description = "init_job Lambda function ARN"
  value       = aws_lambda_function.init_job.arn
}

output "step_function_arn" {
  description = "Watchdog Step Function ARN"
  value       = aws_sfn_state_machine.watchdog.arn
}

output "codebuild_project_name" {
  description = "CodeBuild project name"
  value       = aws_codebuild_project.worker.name
}

output "ssm_document_name" {
  description = "SSM Document name for command execution"
  value       = aws_ssm_document.run_commands.name
}
