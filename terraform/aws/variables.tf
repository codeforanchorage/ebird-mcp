variable "lambda_name" {
  description = "Lambda function name. If empty, derived from config.yaml."
  type        = string
  default     = ""
}

variable "aws_region" {
  description = "AWS region for deployment."
  type        = string
  default     = "us-west-2"
}

variable "config_file" {
  description = "Path to config.yaml (relative to terraform/aws/)."
  type        = string
  default     = "config.yaml"
}

variable "lambda_memory" {
  description = "Lambda memory in MB. Overridden by config.yaml:aws.lambda_memory if set."
  type        = number
  default     = 512
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds. Overridden by config.yaml:aws.lambda_timeout if set."
  type        = number
  default     = 30
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrency cap. -1 = unreserved. Set to a small int (e.g. 10) to bound cost."
  type        = number
  default     = 10
}

variable "api_quota_limit" {
  description = "API Gateway daily request quota."
  type        = number
  default     = 1000
}

variable "api_rate_limit" {
  description = "API Gateway sustained rate limit (requests/second)."
  type        = number
  default     = 5
}

variable "api_burst_limit" {
  description = "API Gateway burst limit."
  type        = number
  default     = 10
}

variable "stage_name" {
  description = "API Gateway stage name (e.g. prod, staging)."
  type        = string
  default     = "staging"
}

variable "custom_domain" {
  description = "Custom domain (FQDN) to bind to the API Gateway, e.g. 'ebird.codeforanchorage.org'. Leave empty to skip."
  type        = string
  default     = ""
}

variable "waf_rate_limit_per_5min" {
  description = "WAF per-IP rate limit over a 5-minute window. 0 disables WAF entirely."
  type        = number
  default     = 1000
}

variable "alarm_sns_topic_arn" {
  description = "Optional SNS topic ARN for CloudWatch alarm notifications."
  type        = string
  default     = ""
}

variable "use_shared_waf" {
  description = <<-EOT
    Associate this MCP's API Gateway stage with the FLEET-WIDE WAFv2 web ACL
    (created by the mcp-stats repo) instead of standing up a dedicated one here.

    The shared ACL must already exist and have published its ARN to SSM before
    this is turned on. Flipping this to true DESTROYS this repo's own web ACL,
    so the shared ACL must already carry an equivalent rule for this MCP.
  EOT
  type        = bool
  default     = false
}

variable "shared_waf_ssm_parameter" {
  description = <<-EOT
    SSM Parameter Store path holding the shared fleet web ACL's ARN. Must match
    `fleet_waf_ssm_parameter` in the mcp-stats repo.
  EOT
  type        = string
  default     = "/mcp-fleet/waf/web_acl_arn"
}
