# WAFv2 web ACL for the /mcp endpoint.
#
# The endpoint has no auth, so WAF is the primary defense against abuse:
# per-IP rate limiting and AWS-managed signature rules for known bad inputs.
#
# Set waf_rate_limit_per_5min = 0 to skip creating the WAF entirely.

locals {
  # Create this MCP's OWN web ACL only when it is not delegating to the
  # fleet-wide one (mcp-stats/terraform/aws/shared_waf.tf). WAF bills ~$5/mo per
  # web ACL + $1/mo per rule regardless of traffic, so a dedicated ACL per MCP
  # is ~$8/mo of fixed cost; the shared ACL carries this MCP's rate limit as its
  # own Host-scoped rule instead.
  #
  # NOTE: once use_shared_waf is true, waf_rate_limit_per_5min is no longer read
  # here — the effective limit lives in mcp-stats' `fleet_waf_members`. Change it
  # there, not in this repo's tfvars.
  waf_enabled = var.waf_rate_limit_per_5min > 0 && !var.use_shared_waf

  # The stage gets associated under either path.
  waf_associated = local.waf_enabled || var.use_shared_waf
}

# The shared ACL lives in a different Terraform state (mcp-stats), so its ARN
# comes via SSM rather than a cross-state remote read.
data "aws_ssm_parameter" "shared_waf_arn" {
  count = var.use_shared_waf ? 1 : 0

  name = var.shared_waf_ssm_parameter
}

resource "aws_wafv2_web_acl" "mcp_api" {
  count = local.waf_enabled ? 1 : 0

  name  = "${local.lambda_name}-waf"
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "RateLimitPerIP"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = var.waf_rate_limit_per_5min
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.lambda_name}-RateLimitPerIP"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedRulesKnownBadInputsRuleSet"
    priority = 2

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesKnownBadInputsRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.lambda_name}-KnownBadInputs"
      sampled_requests_enabled   = true
    }
  }

  rule {
    name     = "AWSManagedRulesCommonRuleSet"
    priority = 3

    override_action {
      none {}
    }

    statement {
      managed_rule_group_statement {
        name        = "AWSManagedRulesCommonRuleSet"
        vendor_name = "AWS"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "${local.lambda_name}-CommonRuleSet"
      sampled_requests_enabled   = true
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = "${local.lambda_name}-waf"
    sampled_requests_enabled   = true
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_wafv2_web_acl_association" "mcp_api" {
  count = local.waf_associated ? 1 : 0

  resource_arn = aws_api_gateway_stage.prod.arn

  # Splat + one() instead of indexing [0]: exactly one of these lists is
  # non-empty and one([]) is null, so the inactive branch cannot blow up with
  # an index-out-of-range while the ternary is being evaluated.
  web_acl_arn = var.use_shared_waf ? one(data.aws_ssm_parameter.shared_waf_arn[*].value) : one(aws_wafv2_web_acl.mcp_api[*].arn)
}
