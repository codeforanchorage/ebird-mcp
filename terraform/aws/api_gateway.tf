# API Gateway REST API for the MCP /mcp endpoint.

resource "aws_api_gateway_rest_api" "mcp_api" {
  name        = "${local.lambda_name}-api"
  description = "API Gateway for eBird MCP server"

  endpoint_configuration {
    types = ["REGIONAL"]
  }
}

resource "aws_api_gateway_resource" "mcp" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  parent_id   = aws_api_gateway_rest_api.mcp_api.root_resource_id
  path_part   = "mcp"
}

# POST is the main JSON-RPC method.
resource "aws_api_gateway_method" "mcp_post" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp.id
  http_method      = "POST"
  authorization    = "NONE"
  api_key_required = false
}

# GET — MCP Streamable HTTP allows this for SSE; we return 405 (spec-compliant).
resource "aws_api_gateway_method" "mcp_get" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp.id
  http_method      = "GET"
  authorization    = "NONE"
  api_key_required = false
}

# DELETE — MCP spec session termination; we return 405.
resource "aws_api_gateway_method" "mcp_delete" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp.id
  http_method      = "DELETE"
  authorization    = "NONE"
  api_key_required = false
}

# OPTIONS — CORS preflight, served by the Lambda so the allowlist applies.
resource "aws_api_gateway_method" "mcp_options" {
  rest_api_id      = aws_api_gateway_rest_api.mcp_api.id
  resource_id      = aws_api_gateway_resource.mcp.id
  http_method      = "OPTIONS"
  authorization    = "NONE"
  api_key_required = false
}

resource "aws_api_gateway_integration" "mcp_post_integration" {
  rest_api_id             = aws_api_gateway_rest_api.mcp_api.id
  resource_id             = aws_api_gateway_resource.mcp.id
  http_method             = aws_api_gateway_method.mcp_post.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

resource "aws_api_gateway_integration" "mcp_get_integration" {
  rest_api_id             = aws_api_gateway_rest_api.mcp_api.id
  resource_id             = aws_api_gateway_resource.mcp.id
  http_method             = aws_api_gateway_method.mcp_get.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

resource "aws_api_gateway_integration" "mcp_delete_integration" {
  rest_api_id             = aws_api_gateway_rest_api.mcp_api.id
  resource_id             = aws_api_gateway_resource.mcp.id
  http_method             = aws_api_gateway_method.mcp_delete.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

# OPTIONS goes to the Lambda so the in-app Origin allowlist enforces preflight.
resource "aws_api_gateway_integration" "mcp_options_integration" {
  rest_api_id             = aws_api_gateway_rest_api.mcp_api.id
  resource_id             = aws_api_gateway_resource.mcp.id
  http_method             = aws_api_gateway_method.mcp_options.http_method
  integration_http_method = "POST"
  type                    = "AWS_PROXY"
  uri                     = aws_lambda_function.mcp_server.invoke_arn
}

# Method response — documents which CORS headers may appear. AWS_PROXY passes
# Lambda response headers through directly, so this does not enforce values.
resource "aws_api_gateway_method_response" "mcp_post_response_200" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  resource_id = aws_api_gateway_resource.mcp.id
  http_method = aws_api_gateway_method.mcp_post.http_method
  status_code = "200"

  response_parameters = {
    "method.response.header.Access-Control-Allow-Origin"  = true
    "method.response.header.Access-Control-Allow-Headers" = true
    "method.response.header.Access-Control-Allow-Methods" = true
  }
}

resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.mcp_server.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_api_gateway_rest_api.mcp_api.execution_arn}/*/*"
}

resource "aws_api_gateway_deployment" "mcp_deployment" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id

  triggers = {
    redeployment = sha1(jsonencode([
      aws_api_gateway_resource.mcp.id,
      aws_api_gateway_method.mcp_post.id,
      aws_api_gateway_method.mcp_get.id,
      aws_api_gateway_method.mcp_delete.id,
      aws_api_gateway_method.mcp_options.id,
      aws_api_gateway_integration.mcp_post_integration.id,
      aws_api_gateway_integration.mcp_get_integration.id,
      aws_api_gateway_integration.mcp_delete_integration.id,
      aws_api_gateway_integration.mcp_options_integration.id,
      aws_lambda_function.mcp_server.arn,
    ]))
  }

  lifecycle {
    create_before_destroy = true
  }

  depends_on = [
    aws_api_gateway_method.mcp_post,
    aws_api_gateway_method.mcp_get,
    aws_api_gateway_method.mcp_delete,
    aws_api_gateway_method.mcp_options,
    aws_api_gateway_integration.mcp_post_integration,
    aws_api_gateway_integration.mcp_get_integration,
    aws_api_gateway_integration.mcp_delete_integration,
    aws_api_gateway_integration.mcp_options_integration,
    aws_api_gateway_method_response.mcp_post_response_200,
  ]
}

resource "aws_api_gateway_stage" "prod" {
  deployment_id = aws_api_gateway_deployment.mcp_deployment.id
  rest_api_id   = aws_api_gateway_rest_api.mcp_api.id
  stage_name    = var.stage_name

  xray_tracing_enabled = true

  access_log_settings {
    destination_arn = aws_cloudwatch_log_group.api_gateway_access.arn
    format = jsonencode({
      requestTime        = "$context.requestTime"
      requestId          = "$context.requestId"
      httpMethod         = "$context.httpMethod"
      resourcePath       = "$context.resourcePath"
      status             = "$context.status"
      sourceIp           = "$context.identity.sourceIp"
      userAgent          = "$context.identity.userAgent"
      integrationLatency = "$context.integrationLatency"
      responseLength     = "$context.responseLength"
    })
  }

  depends_on = [aws_api_gateway_account.this]

  lifecycle {
    create_before_destroy = true
  }
}

# Stage-wide throttling for all methods.
resource "aws_api_gateway_method_settings" "mcp_post" {
  rest_api_id = aws_api_gateway_rest_api.mcp_api.id
  stage_name  = aws_api_gateway_stage.prod.stage_name
  method_path = "*/*"

  settings {
    throttling_burst_limit = var.api_burst_limit
    throttling_rate_limit  = var.api_rate_limit
  }
}

resource "aws_api_gateway_usage_plan" "mcp_usage_plan" {
  name = "${local.lambda_name}-usage-plan"

  api_stages {
    api_id = aws_api_gateway_rest_api.mcp_api.id
    stage  = aws_api_gateway_stage.prod.stage_name
  }

  quota_settings {
    limit  = var.api_quota_limit
    period = "DAY"
  }

  throttle_settings {
    burst_limit = var.api_burst_limit
    rate_limit  = var.api_rate_limit
  }
}
