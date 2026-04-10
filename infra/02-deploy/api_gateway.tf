resource "aws_apigatewayv2_api" "api" {
  name          = "${local.prefix}-api"
  protocol_type = "HTTP"
}

resource "aws_apigatewayv2_stage" "default" {
  api_id      = aws_apigatewayv2_api.api.id
  name        = "$default"
  auto_deploy = true
}

resource "aws_apigatewayv2_integration" "init_job" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.init_job.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_init" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "POST /init"
  target             = "integrations/${aws_apigatewayv2_integration.init_job.id}"
  authorization_type = "AWS_IAM"
}

resource "aws_lambda_permission" "apigw_invoke" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.init_job.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}

# --- SSM config route ---

resource "aws_apigatewayv2_integration" "ssm_config" {
  api_id                 = aws_apigatewayv2_api.api.id
  integration_type       = "AWS_PROXY"
  integration_uri        = aws_lambda_function.ssm_config.invoke_arn
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "post_ssm" {
  api_id             = aws_apigatewayv2_api.api.id
  route_key          = "POST /ssm"
  target             = "integrations/${aws_apigatewayv2_integration.ssm_config.id}"
  authorization_type = "AWS_IAM"
}

resource "aws_lambda_permission" "apigw_ssm_config" {
  statement_id  = "AllowAPIGatewayInvokeSSM"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ssm_config.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.api.execution_arn}/*/*"
}
