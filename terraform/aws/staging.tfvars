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

waf_rate_limit_per_5min = 1000
