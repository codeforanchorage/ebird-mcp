# Custom domain for the API Gateway, with an ACM cert validated by DNS.
#
# Set `custom_domain = "ebird.codeforanchorage.org"` in prod.tfvars to enable.
# Leave it empty (default) to skip — every resource here is `count`-gated.
#
# Two-step DNS dance you (the operator) must do at DreamHost:
#
#   1. After the first `terraform apply`, ACM emits a validation CNAME.
#      Add it at DreamHost using outputs:
#        - acm_validation_cname_name  (e.g. "_abc123.ebird.codeforanchorage.org.")
#        - acm_validation_cname_value (e.g. "_xyz789.acm-validations.aws.")
#      Wait 5–15 minutes for ACM to detect it.
#
#   2. Once ACM issues the cert and the `aws_api_gateway_domain_name`
#      resource finishes creating, add the traffic CNAME at DreamHost:
#        - host:   ebird   (NOT the full FQDN — DreamHost prepends the zone)
#        - value:  <custom_domain_target output>
#               (looks like d-abc123.execute-api.us-west-2.amazonaws.com)
#
# The base path mapping with `base_path = ""` makes
# https://ebird.codeforanchorage.org/mcp resolve to /prod/mcp on the API.

locals {
  custom_domain_enabled = var.custom_domain != ""
}

# ACM certificate. For REGIONAL API Gateway endpoints, the cert must live in
# the same region as the API Gateway itself (us-west-2 here).
resource "aws_acm_certificate" "mcp_cert" {
  count = local.custom_domain_enabled ? 1 : 0

  domain_name       = var.custom_domain
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

# API Gateway custom domain (REGIONAL). Depends on the cert being issued,
# which AWS handles via `aws_acm_certificate_validation` below.
resource "aws_api_gateway_domain_name" "custom" {
  count = local.custom_domain_enabled ? 1 : 0

  domain_name              = var.custom_domain
  regional_certificate_arn = aws_acm_certificate_validation.mcp_cert[0].certificate_arn

  endpoint_configuration {
    types = ["REGIONAL"]
  }

  security_policy = "TLS_1_2"
}

# Base path mapping. base_path = "" means the API Gateway resource path is
# preserved end-to-end, so https://<domain>/mcp -> stage prod -> resource /mcp.
resource "aws_api_gateway_base_path_mapping" "custom" {
  count = local.custom_domain_enabled ? 1 : 0

  api_id      = aws_api_gateway_rest_api.mcp_api.id
  stage_name  = aws_api_gateway_stage.prod.stage_name
  domain_name = aws_api_gateway_domain_name.custom[0].domain_name
  base_path   = ""
}

# Waits until the validation CNAME you added at DreamHost is detected by ACM
# and the cert is issued. No DNS records are created here — ACM expects the
# operator to add them manually.
resource "aws_acm_certificate_validation" "mcp_cert" {
  count = local.custom_domain_enabled ? 1 : 0

  certificate_arn = aws_acm_certificate.mcp_cert[0].arn

  timeouts {
    create = "30m"
  }
}
