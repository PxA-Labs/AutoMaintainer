# Monthly budget on this project with e-mail alerts. A budget only alerts;
# it never stops spending.

# Read at apply time, after the Resource Manager API is enabled.
data "google_project" "this" {
  project_id = var.project_id
  depends_on = [google_project_service.services]
}

resource "google_monitoring_notification_channel" "budget_email" {
  for_each     = toset(var.budget_notification_emails)
  display_name = "Budget alerts: ${each.value}"
  type         = "email"
  labels = {
    email_address = each.value
  }
  depends_on = [google_project_service.services]
}

resource "google_billing_budget" "project" {
  billing_account = var.billing_account_id
  display_name    = "${var.name_prefix} monthly budget"

  budget_filter {
    projects = ["projects/${data.google_project.this.number}"]
  }

  amount {
    specified_amount {
      currency_code = var.budget_currency
      units         = tostring(var.budget_amount)
    }
  }

  dynamic "threshold_rules" {
    for_each = var.budget_alert_thresholds
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  # Early warning: the month is on course to exceed the budget.
  threshold_rules {
    threshold_percent = 1.0
    spend_basis       = "FORECASTED_SPEND"
  }

  dynamic "all_updates_rule" {
    for_each = length(var.budget_notification_emails) > 0 ? [1] : []
    content {
      monitoring_notification_channels = [for c in google_monitoring_notification_channel.budget_email : c.id]
      disable_default_iam_recipients   = false
    }
  }

  depends_on = [google_project_service.services]
}
