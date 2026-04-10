resource "aws_lambda_permission" "s3_invoke_orchestrator" {
  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.orchestrator.function_name
  principal     = "s3.amazonaws.com"
  source_arn    = aws_s3_bucket.internal.arn
}

resource "aws_s3_bucket_notification" "orchestrator_trigger" {
  bucket = aws_s3_bucket.internal.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.orchestrator.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "tmp/callbacks/runs/"
    filter_suffix       = "result.json"
  }

  depends_on = [aws_lambda_permission.s3_invoke_orchestrator]
}
