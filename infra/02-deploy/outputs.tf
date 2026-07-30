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
    init_job = aws_lambda_function.init_job.function_name
    worker   = aws_lambda_function.worker.function_name
  }
}

output "lambda_function_arns" {
  description = "Map of Lambda function ARNs"
  value = {
    init_job = aws_lambda_function.init_job.arn
    worker   = aws_lambda_function.worker.arn
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

output "init_job_url" {
  description = "init_job Lambda function URL"
  value       = aws_lambda_function_url.init_job.function_url
}

output "worker_function_name" {
  description = "worker Lambda function name"
  value       = aws_lambda_function.worker.function_name
}

output "worker_function_arn" {
  description = "worker Lambda function ARN"
  value       = aws_lambda_function.worker.arn
}

output "s3_bucket_names" {
  description = "Map of S3 bucket names"
  value = {
    internal = aws_s3_bucket.internal.id
    done     = aws_s3_bucket.done.id
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

output "codebuild_project_name" {
  description = "CodeBuild project name"
  value       = aws_codebuild_project.worker.name
}

