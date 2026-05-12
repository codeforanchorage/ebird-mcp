output "lambda_function_name" {
  description = "Name of the Lambda function"
  value       = aws_lambda_function.mcp_server.function_name
}

output "lambda_function_arn" {
  description = "ARN of the Lambda function"
  value       = aws_lambda_function.mcp_server.arn
}

output "cloudwatch_log_group" {
  description = "CloudWatch Log Group name for Lambda logs"
  value       = aws_cloudwatch_log_group.lambda_logs.name
}

output "api_gateway_access_log_group" {
  description = "CloudWatch Log Group for API Gateway access logs"
  value       = aws_cloudwatch_log_group.api_gateway_access.name
}

output "api_gateway_url" {
  description = "Public MCP endpoint URL. Paste this into Claude Connectors -> Add custom connector."
  value       = "${aws_api_gateway_stage.prod.invoke_url}/mcp"
}

# ── Custom-domain outputs (only populated when var.custom_domain is set) ──
#
# After the FIRST `terraform apply`, ACM creates a validation CNAME that you
# must add at your DNS provider (DreamHost). Apply will block on the
# aws_acm_certificate_validation resource until ACM sees the record.

output "acm_validation_cname_name" {
  description = "DNS record HOST to add at DreamHost (CNAME) — required for ACM to validate the certificate. Strip the trailing zone if DreamHost wants a relative host."
  value       = var.custom_domain != "" ? tolist(aws_acm_certificate.mcp_cert[0].domain_validation_options)[0].resource_record_name : null
}

output "acm_validation_cname_value" {
  description = "DNS record VALUE to add at DreamHost for the ACM validation CNAME."
  value       = var.custom_domain != "" ? tolist(aws_acm_certificate.mcp_cert[0].domain_validation_options)[0].resource_record_value : null
}

output "custom_domain_target" {
  description = "The API Gateway regional domain — add a second CNAME at DreamHost pointing your custom domain at this value."
  value       = var.custom_domain != "" ? aws_api_gateway_domain_name.custom[0].regional_domain_name : null
}

output "custom_domain_url" {
  description = "Public custom-domain MCP URL once DNS is wired up. This is what users paste into Claude Connectors."
  value       = var.custom_domain != "" ? "https://${var.custom_domain}/mcp" : null
}
