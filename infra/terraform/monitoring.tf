# Metrics, alerts and a dashboard in Cloud Monitoring.
#
# The backend writes its OpenTelemetry metrics to Cloud Monitoring when
# OTEL_CLOUD_MONITORING_ENABLED is true (backend/observability.py). Set it
# for the API and the worker through var.app_env, e.g.
#   app_env = { OTEL_CLOUD_MONITORING_ENABLED = "true", ... }
# Each metric becomes the metric type workload.googleapis.com/<name> on the
# generic_task resource (job = service name, task_id = one process).
#
# monitoring.googleapis.com is enabled in main.tf (local.services), and the
# worker already holds roles/monitoring.metricWriter (iam.tf); neither is
# declared again here, since two resources owning one binding would remove
# it when either is destroyed.
#
# Cloud Monitoring refuses an alert policy on a workload metric type that
# has never been written, so the four application alerts are created only
# when monitoring_app_alerts_enabled is true: apply once with the switch on
# in app_env, wait for the first runs (README.md, "Monitoring"), then turn
# them on. backend/tests/test_gcp_monitoring_config.py checks every filter
# against the metric catalogue in observability.py.

# --- Variables -----------------------------------------------------------------

variable "monitoring_alert_email" {
  description = "E-mail address that receives the monitoring alerts. Required on purpose: pass null for no notification channel (the alerts still open incidents in the console)."
  type        = string
  nullable    = true
}

variable "monitoring_app_alerts_enabled" {
  description = "Create the alerts on the backend's own metrics (run failure rate, p95 run duration, estimated LLM cost, worker heartbeat). Turn on only after OTEL_CLOUD_MONITORING_ENABLED has written each metric once; Cloud Monitoring refuses a policy on a metric type it has not seen. The Cloud Run 5xx alert does not depend on it."
  type        = bool
  default     = false
}

variable "alert_run_failure_rate" {
  description = "Alert when more than this fraction of the agent runs finished in the last hour failed."
  type        = number
  default     = 0.5

  validation {
    condition     = var.alert_run_failure_rate > 0 && var.alert_run_failure_rate <= 1
    error_message = "alert_run_failure_rate must be in (0, 1]."
  }
}

variable "alert_run_duration_p95_seconds" {
  description = "Alert when the 95th percentile duration of the runs finished in the last hour is above this. Runs are stopped at 30 minutes (1800 s)."
  type        = number
  default     = 1500
}

variable "alert_llm_cost_per_day_usd" {
  description = "Alert when the ESTIMATED LLM cost of the last 24 hours exceeds this many USD. The estimate uses the backend's price table (LLM_PRICES_JSON), not the provider's bill."
  type        = number
  default     = 10
}

variable "alert_worker_heartbeat_absent_seconds" {
  description = "Alert when no Celery worker process has reported a heartbeat for this long (at least 120, a multiple of 60)."
  type        = number
  default     = 600

  validation {
    condition     = var.alert_worker_heartbeat_absent_seconds >= 120 && var.alert_worker_heartbeat_absent_seconds % 60 == 0
    error_message = "alert_worker_heartbeat_absent_seconds must be a multiple of 60, at least 120."
  }
}

variable "alert_api_5xx_ratio" {
  description = "Alert when more than this fraction of the API's requests answered 5xx over 5 minutes, for 5 minutes."
  type        = number
  default     = 0.05

  validation {
    condition     = var.alert_api_5xx_ratio > 0 && var.alert_api_5xx_ratio <= 1
    error_message = "alert_api_5xx_ratio must be in (0, 1]."
  }
}

# --- IAM ------------------------------------------------------------------------

# The API writes its metrics with its own service account.
resource "google_project_iam_member" "api_metric_writer" {
  project = var.project_id
  role    = "roles/monitoring.metricWriter"
  member  = google_service_account.api.member
}

# --- Notification channel -------------------------------------------------------

resource "google_monitoring_notification_channel" "alert_email" {
  count        = var.monitoring_alert_email == null ? 0 : 1
  display_name = "${var.name_prefix} alerts"
  type         = "email"
  labels = {
    email_address = var.monitoring_alert_email
  }
  depends_on = [google_project_service.services]
}

# --- Alert policies -------------------------------------------------------------

resource "google_monitoring_alert_policy" "run_failure_rate" {
  count        = var.monitoring_app_alerts_enabled ? 1 : 0
  display_name = "${var.name_prefix}: agent run failure rate"
  combiner     = "OR"

  conditions {
    display_name = "failed or timed out / all finished runs, last hour"
    condition_threshold {
      # A run stopped by the 30-minute limit has outcome "timeout" (tasks.py).
      filter             = "metric.type=\"workload.googleapis.com/agent_runs_total\" AND resource.type=\"generic_task\" AND metric.label.outcome=monitoring.regex.full_match(\"failed|timeout\")"
      denominator_filter = "metric.type=\"workload.googleapis.com/agent_runs_total\" AND resource.type=\"generic_task\""
      comparison         = "COMPARISON_GT"
      threshold_value    = var.alert_run_failure_rate
      duration           = "0s"
      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
      denominator_aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "More than ${var.alert_run_failure_rate * 100} % of the agent runs that finished in the last hour failed or timed out. With few runs one failure can cross the threshold. Check the failed runs in the dashboard and the worker logs (Logs Explorer, VM instance ${var.name_prefix}-worker)."
  }

  notification_channels = google_monitoring_notification_channel.alert_email[*].id
  depends_on            = [google_project_service.services]
}

resource "google_monitoring_alert_policy" "run_duration_p95" {
  count        = var.monitoring_app_alerts_enabled ? 1 : 0
  display_name = "${var.name_prefix}: p95 agent run duration"
  combiner     = "OR"

  conditions {
    display_name = "p95 run duration, last hour"
    condition_threshold {
      filter          = "metric.type=\"workload.googleapis.com/agent_run_duration_seconds\" AND resource.type=\"generic_task\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.alert_run_duration_p95_seconds
      duration        = "0s"
      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_PERCENTILE_95"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "The 95th percentile duration of the agent runs that finished in the last hour is above ${var.alert_run_duration_p95_seconds} s. Runs are stopped at 30 minutes; a slow LLM provider or rate limiting is the usual cause."
  }

  notification_channels = google_monitoring_notification_channel.alert_email[*].id
  depends_on            = [google_project_service.services]
}

resource "google_monitoring_alert_policy" "llm_cost_per_day" {
  count        = var.monitoring_app_alerts_enabled ? 1 : 0
  display_name = "${var.name_prefix}: estimated LLM cost per day"
  combiner     = "OR"

  conditions {
    display_name = "estimated LLM cost, last 24 hours"
    condition_threshold {
      filter          = "metric.type=\"workload.googleapis.com/llm_estimated_cost_usd_total\" AND resource.type=\"generic_task\""
      comparison      = "COMPARISON_GT"
      threshold_value = var.alert_llm_cost_per_day_usd
      duration        = "0s"
      # 24 hours is within the 25-hour limit of alerting alignment periods.
      aggregations {
        alignment_period     = "86400s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "The ESTIMATED LLM cost of the last 24 hours is above ${var.alert_llm_cost_per_day_usd} USD. It is an estimate from the backend's price table (LLM_PRICES_JSON), not the provider's bill; models missing from the table are not counted."
  }

  notification_channels = google_monitoring_notification_channel.alert_email[*].id
  depends_on            = [google_project_service.services]
}

resource "google_monitoring_alert_policy" "worker_heartbeat_absent" {
  count        = var.monitoring_app_alerts_enabled ? 1 : 0
  display_name = "${var.name_prefix}: Celery worker heartbeat absent"
  combiner     = "OR"

  conditions {
    display_name = "no worker process reports a heartbeat"
    condition_absent {
      filter   = "metric.type=\"workload.googleapis.com/celery_worker_heartbeat\" AND resource.type=\"generic_task\""
      duration = "${var.alert_worker_heartbeat_absent_seconds}s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "No Celery worker process has written a heartbeat for ${var.alert_worker_heartbeat_absent_seconds} s: no agent run is being consumed and POST /start answers 503. Check the VM ${var.name_prefix}-worker and its automaintainer-worker unit."
  }

  notification_channels = google_monitoring_notification_channel.alert_email[*].id
  depends_on            = [google_project_service.services]
}

resource "google_monitoring_alert_policy" "api_5xx_ratio" {
  display_name = "${var.name_prefix}: API 5xx ratio"
  combiner     = "OR"

  conditions {
    display_name = "5xx / all API requests, 5 minutes"
    condition_threshold {
      filter             = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${google_cloud_run_v2_service.api.name}\" AND metric.label.response_code_class=\"5xx\""
      denominator_filter = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${google_cloud_run_v2_service.api.name}\""
      comparison         = "COMPARISON_GT"
      threshold_value    = var.alert_api_5xx_ratio
      duration           = "300s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
      denominator_aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
      trigger {
        count = 1
      }
    }
  }

  documentation {
    mime_type = "text/markdown"
    content   = "More than ${var.alert_api_5xx_ratio * 100} % of the requests to ${google_cloud_run_v2_service.api.name} answered 5xx for 5 minutes. At low traffic a few errors are enough. Check the Cloud Run logs and /healthz/supabase."
  }

  notification_channels = google_monitoring_notification_channel.alert_email[*].id
  depends_on            = [google_project_service.services]
}

# --- Dashboard ------------------------------------------------------------------

locals {
  # Dashboard charts may refer to metric types that have not been written yet.
  monitoring_charts = [
    {
      title    = "Agent runs finished, by outcome"
      filter   = "metric.type=\"workload.googleapis.com/agent_runs_total\" AND resource.type=\"generic_task\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = ["metric.label.\"outcome\""]
      plot     = "STACKED_BAR"
    },
    {
      title    = "Agent run duration p95 (s), by outcome"
      filter   = "metric.type=\"workload.googleapis.com/agent_run_duration_seconds\" AND resource.type=\"generic_task\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_PERCENTILE_95"
      group_by = ["metric.label.\"outcome\""]
      plot     = "LINE"
    },
    {
      title    = "LLM tokens, by phase and kind"
      filter   = "metric.type=\"workload.googleapis.com/llm_phase_tokens_total\" AND resource.type=\"generic_task\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = ["metric.label.\"phase\"", "metric.label.\"kind\""]
      plot     = "STACKED_BAR"
    },
    {
      title    = "Estimated LLM cost (USD, an estimate), by model"
      filter   = "metric.type=\"workload.googleapis.com/llm_estimated_cost_usd_total\" AND resource.type=\"generic_task\""
      aligner  = "ALIGN_DELTA"
      reducer  = "REDUCE_SUM"
      group_by = ["metric.label.\"model\""]
      plot     = "STACKED_BAR"
    },
    {
      title    = "Live Celery worker processes"
      filter   = "metric.type=\"workload.googleapis.com/celery_worker_heartbeat\" AND resource.type=\"generic_task\""
      aligner  = "ALIGN_MAX"
      reducer  = "REDUCE_SUM"
      group_by = []
      plot     = "LINE"
    },
    {
      title    = "API requests, by response class"
      filter   = "metric.type=\"run.googleapis.com/request_count\" AND resource.type=\"cloud_run_revision\" AND resource.label.service_name=\"${google_cloud_run_v2_service.api.name}\""
      aligner  = "ALIGN_RATE"
      reducer  = "REDUCE_SUM"
      group_by = ["metric.label.\"response_code_class\""]
      plot     = "STACKED_AREA"
    },
  ]
}

resource "google_monitoring_dashboard" "automaintainer" {
  dashboard_json = jsonencode({
    displayName = "${var.name_prefix} operations"
    mosaicLayout = {
      columns = 12
      tiles = [
        for i, chart in local.monitoring_charts : {
          xPos   = (i % 2) * 6
          yPos   = floor(i / 2) * 4
          width  = 6
          height = 4
          widget = {
            title = chart.title
            xyChart = {
              dataSets = [{
                plotType = chart.plot
                timeSeriesQuery = {
                  timeSeriesFilter = {
                    filter = chart.filter
                    aggregation = {
                      alignmentPeriod    = "300s"
                      perSeriesAligner   = chart.aligner
                      crossSeriesReducer = chart.reducer
                      groupByFields      = chart.group_by
                    }
                  }
                }
              }]
            }
          }
        }
      ]
    }
  })

  depends_on = [google_project_service.services]
}

# --- Outputs --------------------------------------------------------------------

output "monitoring_dashboard_url" {
  description = "The operations dashboard in the Cloud console."
  value       = "https://console.cloud.google.com/monitoring/dashboards/builder/${element(split("/", google_monitoring_dashboard.automaintainer.id), 3)}?project=${var.project_id}"
}

output "monitoring_alert_policies" {
  description = "Alert policy resource names (the application alerts appear once monitoring_app_alerts_enabled is true)."
  value = concat(
    google_monitoring_alert_policy.run_failure_rate[*].name,
    google_monitoring_alert_policy.run_duration_p95[*].name,
    google_monitoring_alert_policy.llm_cost_per_day[*].name,
    google_monitoring_alert_policy.worker_heartbeat_absent[*].name,
    [google_monitoring_alert_policy.api_5xx_ratio.name],
  )
}
