terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.0"
    }
  }
}

provider "aws" {
  region = var.aws_region
}

# Read config.yaml at plan time and bake the entire config into a Lambda env var.
locals {
  config = yamldecode(file(var.config_file))

  lambda_name = var.lambda_name != "" ? var.lambda_name : (
    try(local.config.aws.lambda_name, "") != "" ? local.config.aws.lambda_name : (
      try(local.config.server_name, "") != "" ? lower(replace(local.config.server_name, " ", "-")) : "ebird-mcp"
    )
  )

  lambda_memory  = try(local.config.aws.lambda_memory, null) != null ? local.config.aws.lambda_memory : var.lambda_memory
  lambda_timeout = try(local.config.aws.lambda_timeout, null) != null ? local.config.aws.lambda_timeout : var.lambda_timeout

  config_json = jsonencode(local.config)
}

# IAM role for Lambda
resource "aws_iam_role" "lambda_role" {
  name = "${local.lambda_name}-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_iam_role_policy_attachment" "lambda_basic" {
  role       = aws_iam_role.lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Lambda deployment package (built by scripts/deploy.sh).
locals {
  lambda_zip_path = "${path.module}/lambda-deployment.zip"
  lambda_zip_hash = filebase64sha256(local.lambda_zip_path)
}

resource "aws_lambda_function" "mcp_server" {
  filename         = local.lambda_zip_path
  function_name    = local.lambda_name
  role             = aws_iam_role.lambda_role.arn
  handler          = "server.adapters.aws_lambda.lambda_handler"
  source_code_hash = local.lambda_zip_hash
  runtime          = "python3.11"
  memory_size      = local.lambda_memory
  timeout          = local.lambda_timeout

  # Cap concurrency to bound cost and blast radius from traffic spikes.
  reserved_concurrent_executions = var.lambda_reserved_concurrency

  environment {
    variables = {
      EBIRD_MCP_CONFIG = local.config_json
    }
  }

  depends_on = [
    aws_iam_role_policy_attachment.lambda_basic,
  ]

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_cloudwatch_log_group" "lambda_logs" {
  name              = "/aws/lambda/${local.lambda_name}"
  retention_in_days = 14

  # Discovered by the mcp-observability project via the Resource Groups
  # Tagging API. Keep this tag on every MCP fork's log groups.
  tags = {
    Project = "mcp-server"
  }

  lifecycle {
    create_before_destroy = true
  }
}
