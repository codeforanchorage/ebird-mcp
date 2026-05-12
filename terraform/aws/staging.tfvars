lambda_name     = "ebird-mcp-staging"
stage_name      = "staging"
aws_region      = "us-west-2"
config_file     = "config.yaml"

lambda_memory   = 512
lambda_timeout  = 30
lambda_reserved_concurrency = 5

api_quota_limit = 500
api_rate_limit  = 5
api_burst_limit = 10

# WAF disabled on staging — prod is the public-facing target, staging is
# unpublicized dev infra. Setting this to 0 destroys the WAFv2 ACL and its
# association, saving ~$5/month. Bump back to 1000 if staging ever takes
# real user traffic or needs production-realistic security tests.
waf_rate_limit_per_5min = 0

