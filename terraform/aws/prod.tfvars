lambda_name     = "ebird-mcp-prod"
stage_name      = "prod"
aws_region      = "us-west-2"
config_file     = "config.yaml"

# Lambda settings — overridden by config.yaml:aws.* if set there.
lambda_memory   = 512
lambda_timeout  = 30

# Viral-ready capacity, with a hard cost ceiling.
#
# Concurrent Lambdas. 50 × ~1.5s avg = ~30 rps theoretical sustained, which
# matches the API Gateway rate cap below. Default account limit is 1000
# unreserved concurrent executions, so this leaves plenty for other workloads.
lambda_reserved_concurrency = 50

# API Gateway throttling and daily quota.
#
# 50,000 req/day caps the worst-case AWS bill at ~$25/month even if pegged
# every day. Genuine viral traffic past that gets 429s — preferable to a
# surprise invoice. If the cap is hit consistently by real users, raise it
# deliberately and watch the CloudWatch dashboard.
#
# 20 rps sustained / 40 burst is enough for typical "trending" load without
# letting a single attacker burn through the daily quota in minutes.
api_quota_limit = 50000
api_rate_limit  = 20
api_burst_limit = 40

# WAF per-IP rate (rolling 5-minute window).
#
# 50 / 5min = 10 req/min per IP. Sized against the *upstream* eBird quota
# (1000 calls/day), NOT against AWS-side capacity. At 10 req/min one IP
# could in theory drain the entire daily eBird budget in ~100 minutes —
# already much friendlier than the previous 1000/5min cap, which let a
# single IP drain the budget in five minutes. Real conversational use
# (one MCP call per LLM turn) is well under one req/min, so this is not
# a user-visible limit for legitimate clients.
#
# If a single IP needs more headroom (e.g. a partner running batch jobs),
# raise this deliberately and coordinate with them, or have them
# self-host via scripts/deploy.sh.
waf_rate_limit_per_5min = 50

# Custom domain for the API Gateway. Setting this triggers:
#   - ACM cert issuance (DNS-validated, in us-west-2)
#   - aws_api_gateway_domain_name + base path mapping
#   - Two outputs you must wire up at DreamHost manually:
#       acm_validation_cname_name / _value  (validates the cert)
#       custom_domain_target                (routes ebird.codeforanchorage.org -> API GW)
# Final URL will be: https://ebird.codeforanchorage.org/mcp
custom_domain = "ebird.codeforanchorage.org"

# Optional: route CloudWatch alarms to an SNS topic so on-call gets paged.
# Create the topic once with:
#   aws sns create-topic --name ebird-mcp-alarms --region us-west-2
#   aws sns subscribe --topic-arn <arn> --protocol email \
#       --notification-endpoint you@example.com
# Then uncomment and set:
# alarm_sns_topic_arn = "arn:aws:sns:us-west-2:<ACCOUNT_ID>:ebird-mcp-alarms"
