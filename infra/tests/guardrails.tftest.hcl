# Plan-only tests against mocked AWS providers — no credentials, no state, so CI
# runs them beside `terraform validate`. They pin what validate cannot see and a
# real plan would only reveal against the live account: the budget's alert
# wiring and its input guards, the certificate's tenant-wildcard coverage and
# validation-record wiring, the platform domain's registration settings and
# apex-only guard, and the access-log sink's delivery prerequisites.

mock_provider "aws" {
  # A mocked policy document renders a random string, which the KMS key and
  # bucket policies then reject as invalid JSON. Any well-formed document does.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }
}

mock_provider "aws" {
  alias = "us_east_1"
}

# A mocked certificate has no provider logic behind it, so its
# domain_validation_options would stay unknown at plan time. Supply the shape
# ACM really returns for an apex + wildcard pair: two entries, ONE shared CNAME.
override_resource {
  target          = aws_acm_certificate.platform
  override_during = plan
  values = {
    arn = "arn:aws:acm:us-east-1:000000000000:certificate/00000000-0000-0000-0000-000000000000"
    domain_validation_options = [
      {
        domain_name           = "feohledger.com"
        resource_record_name  = "_0123abcd.feohledger.com."
        resource_record_type  = "CNAME"
        resource_record_value = "_4567efab.acm-validations.aws."
      },
      {
        domain_name           = "*.feohledger.com"
        resource_record_name  = "_0123abcd.feohledger.com."
        resource_record_type  = "CNAME"
        resource_record_value = "_4567efab.acm-validations.aws."
      },
    ]
  }
}

# Give the source buckets known ARNs at plan time, so the access-log delivery
# policy's SourceArn condition can be compared exactly instead of staying
# unknown.
override_resource {
  target          = aws_s3_bucket.invoice_files
  override_during = plan
  values = {
    arn = "arn:aws:s3:::feohledger-invoices-test"
  }
}

override_resource {
  target          = aws_s3_bucket.audit_logs
  override_during = plan
  values = {
    arn = "arn:aws:s3:::feohledger-audit-logs-test"
  }
}

override_resource {
  target          = aws_s3_bucket.backups
  override_during = plan
  values = {
    arn = "arn:aws:s3:::feohledger-backups-test"
  }
}

variables {
  invoice_files_bucket_name = "feohledger-invoices-test"
  audit_logs_bucket_name    = "feohledger-audit-logs-test"
  access_logs_bucket_name   = "feohledger-access-logs-test"
  backups_bucket_name       = "feohledger-backups-test"
  budget_alert_emails       = ["ops@feohledger.test"]
}

run "budget_alerts_every_address_on_actual_and_forecast" {
  command = plan

  assert {
    condition     = aws_budgets_budget.monthly.limit_amount == "25" && aws_budgets_budget.monthly.time_unit == "MONTHLY"
    error_message = "The default budget must be 25 USD per month."
  }

  assert {
    condition     = length(aws_budgets_budget.monthly.notification) == 3
    error_message = "Expected three notifications (50 % actual, 100 % actual, 100 % forecast)."
  }

  assert {
    condition = length([
      for n in aws_budgets_budget.monthly.notification : n
      if n.notification_type == "FORECASTED" && n.threshold == 100
    ]) == 1
    error_message = "The 100 % FORECASTED notification is the early warning and must exist."
  }

  assert {
    condition = alltrue([
      for n in aws_budgets_budget.monthly.notification :
      toset(n.subscriber_email_addresses) == toset(var.budget_alert_emails)
    ])
    error_message = "Every notification must go to every address in budget_alert_emails."
  }
}

run "certificate_covers_tenant_subdomains" {
  command = plan

  assert {
    condition     = aws_acm_certificate.platform.domain_name == "feohledger.com"
    error_message = "The certificate must be issued for the platform domain."
  }

  assert {
    condition     = contains(aws_acm_certificate.platform.subject_alternative_names, "*.feohledger.com")
    error_message = "Tenants live on <slug>.<domain>; without the wildcard SAN every tenant host fails TLS."
  }

  assert {
    condition     = length(aws_route53_record.certificate_validation) == 2
    error_message = "Expected one validation record instance per domain_validation_options entry."
  }

  assert {
    condition = alltrue([
      for r in aws_route53_record.certificate_validation : r.allow_overwrite
    ])
    error_message = "The apex and wildcard share one CNAME; without allow_overwrite the second record fails to create."
  }
}

run "registered_domain_renews_stays_locked_and_private" {
  command = plan

  assert {
    condition     = aws_route53domains_registered_domain.platform.domain_name == var.domain_name
    error_message = "The registration Terraform adopts must be the platform domain itself."
  }

  assert {
    condition     = aws_route53domains_registered_domain.platform.auto_renew && aws_route53domains_registered_domain.platform.transfer_lock
    error_message = "A platform domain that can lapse or be transferred away takes every tenant URL with it."
  }

  assert {
    condition = alltrue([
      aws_route53domains_registered_domain.platform.admin_privacy,
      aws_route53domains_registered_domain.platform.registrant_privacy,
      aws_route53domains_registered_domain.platform.tech_privacy,
      aws_route53domains_registered_domain.platform.billing_privacy,
    ])
    error_message = "Registrant contact details must stay out of WHOIS."
  }
}

run "access_log_sink_accepts_s3_log_delivery" {
  command = plan

  assert {
    condition     = one(one(aws_s3_bucket_server_side_encryption_configuration.access_logs.rule).apply_server_side_encryption_by_default).sse_algorithm == "AES256"
    error_message = "AWS does not support SSE-KMS on a server-access-log destination; the sink must be SSE-S3 (decisions §166)."
  }

  assert {
    condition     = one(aws_s3_bucket_ownership_controls.access_logs.rule).object_ownership == "BucketOwnerEnforced"
    error_message = "Delivery is granted by the bucket policy, so ACLs stay disabled on the sink."
  }

  assert {
    condition = contains(flatten([
      for s in data.aws_iam_policy_document.access_logs_delivery.statement : [
        for p in s.principals : tolist(p.identifiers)
      ] if s.effect == "Allow" && contains(s.actions, "s3:PutObject")
    ]), "logging.s3.amazonaws.com")
    error_message = "Without an s3:PutObject grant to logging.s3.amazonaws.com, S3 delivers no log object at all."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.access_logs_delivery.statement :
      contains([for c in s.condition : c.variable], "aws:SourceAccount")
    ])
    error_message = "The delivery grant must be pinned to this account, or another account's bucket could log into this one."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.access_logs_delivery.statement :
      toset(flatten([
        for c in s.condition : tolist(c.values) if c.test == "ArnLike" && c.variable == "aws:SourceArn"
        ])) == toset([
        aws_s3_bucket.invoice_files.arn,
        aws_s3_bucket.audit_logs.arn,
        aws_s3_bucket.backups.arn,
      ])
    ])
    error_message = "The delivery grant must name exactly the three source buckets, or any bucket in the account could log into the sink."
  }
}

run "rejects_the_example_placeholder_address" {
  command = plan

  variables {
    budget_alert_emails = ["alerts@example.com"]
  }

  expect_failures = [var.budget_alert_emails]
}

run "rejects_an_empty_alert_list" {
  command = plan

  variables {
    budget_alert_emails = []
  }

  expect_failures = [var.budget_alert_emails]
}

run "rejects_a_non_address" {
  command = plan

  variables {
    budget_alert_emails = ["not-an-email"]
  }

  expect_failures = [var.budget_alert_emails]
}

run "rejects_a_zero_budget" {
  command = plan

  variables {
    monthly_budget_limit_usd = 0
  }

  expect_failures = [var.monthly_budget_limit_usd]
}

run "rejects_a_subdomain_as_the_platform_domain" {
  command = plan

  variables {
    domain_name = "feohledger.jaredhoward.com"
  }

  expect_failures = [var.domain_name]
}
