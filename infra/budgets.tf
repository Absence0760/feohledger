# AWS Budgets — account-wide monthly spend ceiling.
#
# Account-wide is the right scope, not a shortcut: the FeohLedger account holds
# nothing but this project, and not every chargeable line (KMS requests, Route 53
# queries, data transfer) carries the project tag a tag-filtered budget would
# need. Three notifications, all to var.budget_alert_emails:
#
#   - 50 % ACTUAL      — halfway through the month's allowance
#   - 100 % ACTUAL     — the budget is blown for this month
#   - 100 % FORECASTED — the current burn rate projects past the limit
#
# The forecast is the one that catches a runaway early; the ACTUAL thresholds
# fire only after spend lands on the bill, up to ~24 h later. Each address is
# emailed directly — no SNS topic, so there is no subscription to confirm and no
# topic encryption to manage.
#
# Prerequisite: IAM access to billing data. The account bootstrap creates member
# accounts with `iam_user_access_to_billing = "ALLOW"`, and the Budgets API
# answers for this account; if an apply is ever refused with "ask the payer
# account to enable budgets", turn on "IAM user and role access to billing
# information" in the account's root settings (a root-only step) rather than
# removing the budget.
#
# Applied by an operator with AdministratorAccess. The GitHub deploy role has no
# budgets:* permission and should not get one.

resource "aws_budgets_budget" "monthly" {
  name         = "${var.project}-${var.environment}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 50
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.budget_alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "ACTUAL"
    subscriber_email_addresses = var.budget_alert_emails
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = var.budget_alert_emails
  }
}
