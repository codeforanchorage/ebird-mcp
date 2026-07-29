# API Gateway access logs to CloudWatch.
#
# aws_api_gateway_account is a REGION-level singleton: AWS stores one
# CloudWatch role ARN per region, and applying this overwrites whatever is
# there. If another stack in the same account/region already manages it,
# either `terraform import aws_api_gateway_account.this api-gateway-account`
# or comment these resources out.

resource "aws_iam_role" "api_gateway_cloudwatch" {
  name = "${local.lambda_name}-apigw-cloudwatch"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "apigateway.amazonaws.com"
        }
      }
    ]
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_iam_role_policy_attachment" "api_gateway_cloudwatch" {
  role       = aws_iam_role.api_gateway_cloudwatch.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonAPIGatewayPushToCloudWatchLogs"
}

# aws_api_gateway_account is intentionally NOT declared here.
#
# It is an ACCOUNT+REGION-LEVEL SINGLETON -- AWS stores exactly one CloudWatch
# role ARN for all of API Gateway in the region. It is now owned solely by the
# mcp-stats repo (terraform/aws/apigw_account.tf), which points it at the
# fleet-owned mcp-fleet-apigw-cloudwatch role.
#
# Declaring it per-MCP meant the whole fleet's API Gateway access logging hung
# off ONE MCP's IAM role, so deleting that role would have broken logging for
# every MCP -- including the log groups the mcp-stats dashboard reads.
#
# The aws_iam_role.api_gateway_cloudwatch above is no longer referenced by this
# stack. It is left in place deliberately: removing it would destroy an IAM role
# as a side effect of this refactor, and keeping it makes reverting trivial.
resource "aws_cloudwatch_log_group" "api_gateway_access" {
  name              = "/aws/apigateway/${local.lambda_name}-access"
  retention_in_days = 30

  # Discovered by the mcp-observability project via the Resource Groups
  # Tagging API. Keep this tag on every MCP fork's log groups.
  tags = {
    Project = "mcp-server"
  }

  lifecycle {
    create_before_destroy = true
  }
}
