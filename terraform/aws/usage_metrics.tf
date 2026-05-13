# Usage tracking: pinned CloudWatch Logs Insights queries + a dashboard.
#
# No new data is captured — everything here reads the existing access logs
# (aws_cloudwatch_log_group.api_gateway_access) and Lambda logs
# (aws_cloudwatch_log_group.lambda_logs). The session ID is minted on every
# `initialize` in server/http_handler.py; the access log format is defined in
# terraform/aws/access_logs.tf.
#
# Caveats baked into the queries below:
#   - "sessions" is per-connection (Mcp-Session-Id), not per-conversation.
#     Clients reconnect routinely; expect overcounting.
#   - "unique clients" uses (sourceIp, userAgent) as a best-effort proxy.
#     Real user counts would require auth (see Tier 4 in the design notes).

locals {
  lambda_log_group     = aws_cloudwatch_log_group.lambda_logs.name
  apigw_log_group      = aws_cloudwatch_log_group.api_gateway_access.name
  query_name_prefix    = "${local.lambda_name}/usage"
}

# ── Saved Logs Insights queries ────────────────────────────────────────────

resource "aws_cloudwatch_query_definition" "sessions_per_day" {
  name            = "${local.query_name_prefix}/sessions-per-day"
  log_group_names = [local.lambda_log_group]

  query_string = <<-EOT
    fields @timestamp, mcp_session_id, jsonrpc_method
    | filter jsonrpc_method = "initialize" and ispresent(mcp_session_id)
    | stats count_distinct(mcp_session_id) as sessions by bin(1d)
    | sort @timestamp asc
  EOT
}

resource "aws_cloudwatch_query_definition" "unique_clients_per_day" {
  name            = "${local.query_name_prefix}/unique-clients-per-day"
  log_group_names = [local.apigw_log_group]

  query_string = <<-EOT
    fields @timestamp, sourceIp, userAgent, concat(sourceIp, "|", userAgent) as client_key
    | filter ispresent(sourceIp)
    | stats count_distinct(client_key) as unique_clients,
            count_distinct(sourceIp) as unique_ips
            by bin(1d)
    | sort @timestamp asc
  EOT
}

resource "aws_cloudwatch_query_definition" "mcp_client_breakdown" {
  name            = "${local.query_name_prefix}/mcp-client-breakdown"
  log_group_names = [local.lambda_log_group]

  query_string = <<-EOT
    fields @timestamp,
           jsonrpc_params.clientInfo.name as client,
           jsonrpc_params.clientInfo.version as version
    | filter jsonrpc_method = "initialize"
    | stats count(*) as initializes by client, version
    | sort initializes desc
  EOT
}

resource "aws_cloudwatch_query_definition" "tool_popularity" {
  name            = "${local.query_name_prefix}/tool-popularity"
  log_group_names = [local.lambda_log_group]

  query_string = <<-EOT
    fields @timestamp, jsonrpc_params.name as tool
    | filter jsonrpc_method = "tools/call" and ispresent(tool)
    | stats count(*) as calls by tool
    | sort calls desc
  EOT
}

resource "aws_cloudwatch_query_definition" "top_source_ips" {
  name            = "${local.query_name_prefix}/top-source-ips"
  log_group_names = [local.apigw_log_group]

  query_string = <<-EOT
    fields @timestamp, sourceIp, userAgent
    | stats count(*) as requests,
            count_distinct(userAgent) as distinct_uas
            by sourceIp
    | sort requests desc
    | limit 50
  EOT
}

# ── Dashboard ──────────────────────────────────────────────────────────────
#
# Logs-Insights widgets re-run the query each time the dashboard loads, so
# there is no incremental cost beyond the (free) Insights scan against the
# 30-day log retention. Default view is the last 24h; widen via the time
# picker on the dashboard itself.

resource "aws_cloudwatch_dashboard" "usage" {
  dashboard_name = "${local.lambda_name}-usage"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "log"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Sessions per day (distinct Mcp-Session-Id on initialize)"
          region = var.aws_region
          view   = "timeSeries"
          stacked = false
          query = join("\n", [
            "SOURCE '${local.lambda_log_group}'",
            "| fields @timestamp, mcp_session_id, jsonrpc_method",
            "| filter jsonrpc_method = \"initialize\" and ispresent(mcp_session_id)",
            "| stats count_distinct(mcp_session_id) as sessions by bin(1d)",
            "| sort @timestamp asc",
          ])
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Unique clients per day (sourceIp+userAgent and sourceIp)"
          region = var.aws_region
          view   = "timeSeries"
          stacked = false
          query = join("\n", [
            "SOURCE '${local.apigw_log_group}'",
            "| fields @timestamp, sourceIp, userAgent, concat(sourceIp, \"|\", userAgent) as client_key",
            "| filter ispresent(sourceIp)",
            "| stats count_distinct(client_key) as unique_clients, count_distinct(sourceIp) as unique_ips by bin(1d)",
            "| sort @timestamp asc",
          ])
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "MCP client family (clientInfo.name on initialize)"
          region = var.aws_region
          view   = "bar"
          query = join("\n", [
            "SOURCE '${local.lambda_log_group}'",
            "| fields jsonrpc_params.clientInfo.name as client",
            "| filter jsonrpc_method = \"initialize\"",
            "| stats count(*) as initializes by client",
            "| sort initializes desc",
          ])
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Tool popularity (tools/call by tool name)"
          region = var.aws_region
          view   = "bar"
          query = join("\n", [
            "SOURCE '${local.lambda_log_group}'",
            "| fields jsonrpc_params.name as tool",
            "| filter jsonrpc_method = \"tools/call\" and ispresent(tool)",
            "| stats count(*) as calls by tool",
            "| sort calls desc",
          ])
        }
      },
      {
        type   = "log"
        x      = 0
        y      = 12
        width  = 24
        height = 6
        properties = {
          title  = "Top source IPs (last selected window) — watch for abuse"
          region = var.aws_region
          view   = "table"
          query = join("\n", [
            "SOURCE '${local.apigw_log_group}'",
            "| fields sourceIp, userAgent",
            "| stats count(*) as requests, count_distinct(userAgent) as distinct_uas by sourceIp",
            "| sort requests desc",
            "| limit 50",
          ])
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 18
        width  = 12
        height = 6
        properties = {
          title  = "API Gateway request count (built-in CloudWatch metric)"
          region = var.aws_region
          view   = "timeSeries"
          stacked = false
          metrics = [
            ["AWS/ApiGateway", "Count", "ApiName", aws_api_gateway_rest_api.mcp_api.name, "Stage", aws_api_gateway_stage.prod.stage_name, { stat = "Sum", period = 3600 }]
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 18
        width  = 12
        height = 6
        properties = {
          title  = "API Gateway 4XX / 5XX rate"
          region = var.aws_region
          view   = "timeSeries"
          stacked = false
          metrics = [
            ["AWS/ApiGateway", "4XXError", "ApiName", aws_api_gateway_rest_api.mcp_api.name, "Stage", aws_api_gateway_stage.prod.stage_name, { stat = "Sum", period = 3600, label = "4XX" }],
            [".", "5XXError", ".", ".", ".", ".", { stat = "Sum", period = 3600, label = "5XX" }],
          ]
        }
      },
    ]
  })
}
