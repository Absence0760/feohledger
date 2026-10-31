# Plan-only tests against mocked AWS providers — no credentials, no state, so CI
# runs them beside `terraform validate`. They pin what validate cannot see and a
# real plan would only reveal against the live account: the budget's alert
# wiring and its input guards, the certificate's tenant-wildcard coverage and
# validation-record wiring, the platform domain's registration settings and
# apex-only guard, the access-log sink's delivery prerequisites, and the platform
# domain's mail records (SES identity, DKIM, MAIL FROM, Migadu, DMARC) with their
# input guards, and the release-deploy grant (deploy.tf): one document, one
# instance, one sha-shaped parameter; and the SPA stack (frontend.tf): a bucket
# only this distribution reads, the platform certificate on the apex + wildcard,
# a publish grant scoped to that bucket and distribution, a missing-asset alarm
# on the 404 rate with the monitoring subscription that publishes that metric,
# and DNS that stays on
# the VM until frontend_on_cloudfront moves it — with api. on the VM regardless;
# the VM's database grant: rds-db:connect on the app role alone; and the
# external uptime checks (uptime.tf): which hosts are checked and how,
# that each alarm pages and emails, that the phone subscription exists only
# when a number is set, and the certificate-expiry backstop.

mock_provider "aws" {
  # A mocked policy document renders a random string, which the KMS key and
  # bucket policies then reject as invalid JSON. Any well-formed document does.
  mock_data "aws_iam_policy_document" {
    defaults = {
      json = "{\"Version\":\"2012-10-17\",\"Statement\":[]}"
    }
  }

  # The VPC takes its two AZs from this list; a mocked empty list would make
  # the slice in network.tf fail before any assertion runs.
  mock_data "aws_availability_zones" {
    defaults = {
      names = ["us-east-1a", "us-east-1b", "us-east-1c"]
    }
  }

  mock_data "aws_kms_alias" {
    defaults = {
      target_key_arn = "arn:aws:kms:us-east-1:000000000000:key/00000000-0000-0000-0000-00000000sops"
    }
  }

  mock_data "aws_ssm_parameter" {
    defaults = {
      value = "ami-0123456789abcdef0"
    }
  }

  # The RDS IAM-auth grant's ARN is built from these; mocked values would be
  # random strings the assertion could not name.
  mock_data "aws_region" {
    defaults = {
      region = "us-east-1"
    }
  }

  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "000000000000"
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

# SES only publishes an identity's DKIM tokens once the identity exists, so a
# mocked one would leave the three DKIM record names unknown at plan time.
# Supply the shape Easy DKIM returns: three tokens.
override_resource {
  target          = aws_sesv2_email_identity.platform
  override_during = plan
  values = {
    arn = "arn:aws:ses:us-east-1:000000000000:identity/feohledger.com"
    dkim_signing_attributes = {
      next_signing_key_length = "RSA_2048_BIT"
      tokens                  = ["tokenone", "tokentwo", "tokenthree"]
    }
  }
}

# The deploy role's SendCommand grant is compared against these ARNs exactly,
# so give them known values at plan time.
override_resource {
  target          = aws_instance.app
  override_during = plan
  values = {
    arn = "arn:aws:ec2:us-east-1:000000000000:instance/i-0123456789abcdef0"
  }
}

override_resource {
  target          = aws_ssm_document.deploy
  override_during = plan
  values = {
    arn = "arn:aws:ssm:us-east-1:000000000000:document/feohledger-vm-deploy"
  }
}

# The SPA stack (frontend.tf): the bucket and distribution ARNs are compared
# exactly in the bucket and deploy-role policies, and the distribution's own
# hostname is what the DNS alias records must point at.
override_resource {
  target          = aws_s3_bucket.access_logs
  override_during = plan
  values = {
    arn = "arn:aws:s3:::feohledger-access-logs-test"
  }
}

override_resource {
  target          = aws_s3_bucket.frontend
  override_during = plan
  values = {
    arn                         = "arn:aws:s3:::feohledger-frontend-test"
    bucket_regional_domain_name = "feohledger-frontend-test.s3.us-east-1.amazonaws.com"
  }
}

override_resource {
  target          = aws_cloudfront_distribution.frontend
  override_during = plan
  values = {
    arn            = "arn:aws:cloudfront::000000000000:distribution/E0123456789ABC"
    id             = "E0123456789ABC"
    domain_name    = "d0123456789abc.cloudfront.net"
    hosted_zone_id = "Z2FDTNDATAQYW2"
  }
}

# The RDS resource id (db-XXXX) is what an rds-db:connect ARN names; a mock
# leaves it unknown at plan time.
override_resource {
  target          = aws_db_instance.main
  override_during = plan
  values = {
    resource_id = "db-TESTRESOURCEID0000000000"
  }
}

override_resource {
  target          = aws_eip.app
  override_during = plan
  values = {
    public_ip = "192.0.2.10"
  }
}

# The uptime alarms name both topics and their health check; give those known
# values so the assertions compare them exactly instead of staying unknown.
override_resource {
  target          = aws_sns_topic.alerts
  override_during = plan
  values = {
    arn = "arn:aws:sns:us-east-1:000000000000:feohledger-prod-alerts"
  }
}

override_resource {
  target          = aws_sns_topic.pager
  override_during = plan
  values = {
    arn = "arn:aws:sns:us-east-1:000000000000:feohledger-prod-pager"
  }
}

override_resource {
  target          = aws_route53_health_check.uptime
  override_during = plan
  values = {
    id = "00000000-0000-0000-0000-0000000uptime"
  }
}

variables {
  invoice_files_bucket_name = "feohledger-invoices-test"
  audit_logs_bucket_name    = "feohledger-audit-logs-test"
  access_logs_bucket_name   = "feohledger-access-logs-test"
  backups_bucket_name       = "feohledger-backups-test"
  budget_alert_emails       = ["ops@feohledger.test"]
  alert_emails              = ["ops@feohledger.test"]
  db_master_password        = "testonlynotarealdbpassword"
}

run "budget_alerts_every_address_on_actual_and_forecast" {
  command = plan

  assert {
    condition     = aws_budgets_budget.monthly.limit_amount == "75" && aws_budgets_budget.monthly.time_unit == "MONTHLY"
    error_message = "The default budget must be 75 USD per month (the single-VM + RDS stack runs ~$45–55)."
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
        aws_s3_bucket.frontend.arn,
      ])
      if contains(flatten([for p in s.principals : tolist(p.identifiers)]), "logging.s3.amazonaws.com")
    ])
    error_message = "The S3 delivery grant must name exactly the four source buckets, or any bucket in the account could log into the sink."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.access_logs_delivery.statement :
      toset(s.resources) == toset(["arn:aws:s3:::feohledger-access-logs-test/cloudfront/*"])
      if contains(flatten([for p in s.principals : tolist(p.identifiers)]), "delivery.logs.amazonaws.com") && contains(s.actions, "s3:PutObject")
    ])
    error_message = "CloudFront log delivery may write under cloudfront/ only."
  }

  assert {
    condition = anytrue([
      for s in data.aws_iam_policy_document.access_logs_delivery.statement :
      toset(s.actions) == toset(["s3:GetBucketAcl", "s3:ListBucket"]) && toset(s.resources) == toset(["arn:aws:s3:::feohledger-access-logs-test"])
      if contains(flatten([for p in s.principals : tolist(p.identifiers)]), "delivery.logs.amazonaws.com")
    ])
    error_message = "CloudWatch log delivery reads the sink's ACL before writing; without s3:GetBucketAcl on the bucket the delivery is refused."
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

run "ses_sends_as_the_platform_domain_with_every_dkim_key_published" {
  command = plan

  assert {
    condition     = aws_sesv2_email_identity.platform.email_identity == "feohledger.com"
    error_message = "The SES identity must be the platform domain, so the app can send as any address on it."
  }

  assert {
    condition     = length(aws_route53_record.ses_dkim) == 3
    error_message = "Easy DKIM issues three tokens; a missing CNAME leaves the identity unverified."
  }

  assert {
    condition = alltrue([
      for i, token in ["tokenone", "tokentwo", "tokenthree"] :
      aws_route53_record.ses_dkim[i].name == "${token}._domainkey.feohledger.com" &&
      aws_route53_record.ses_dkim[i].type == "CNAME" &&
      aws_route53_record.ses_dkim[i].records == toset(["${token}.dkim.amazonses.com"])
    ])
    error_message = "Each SES DKIM token must be published as <token>._domainkey pointing at <token>.dkim.amazonses.com."
  }
}

run "ses_bounces_go_to_the_send_subdomain_not_the_apex" {
  command = plan

  assert {
    condition     = aws_sesv2_email_identity_mail_from_attributes.platform.mail_from_domain == "send.feohledger.com"
    error_message = "SES's envelope sender must be send.<domain>, so its MX and SPF stay off the apex records Migadu owns."
  }

  assert {
    condition     = aws_sesv2_email_identity_mail_from_attributes.platform.behavior_on_mx_failure == "USE_DEFAULT_VALUE"
    error_message = "If the send. MX goes missing, SES must fall back to its own envelope rather than reject app mail."
  }

  assert {
    condition     = aws_route53_record.ses_mail_from_mx.name == "send.feohledger.com" && aws_route53_record.ses_mail_from_mx.records == toset(["10 feedback-smtp.us-east-1.amazonses.com"])
    error_message = "The MAIL FROM subdomain needs the bounce MX of the region the identity lives in."
  }

  assert {
    condition     = aws_route53_record.ses_mail_from_spf.name == "send.feohledger.com" && aws_route53_record.ses_mail_from_spf.records == toset(["v=spf1 include:amazonses.com ~all"])
    error_message = "The MAIL FROM subdomain's SPF must authorize SES."
  }
}

run "migadu_receives_mail_for_the_apex" {
  command = plan

  assert {
    condition     = aws_route53_record.migadu_mx.name == "feohledger.com" && aws_route53_record.migadu_mx.records == toset(["10 aspmx1.migadu.com", "20 aspmx2.migadu.com"])
    error_message = "The apex MX must point at Migadu's two inbound hosts."
  }

  assert {
    condition = alltrue([
      for key in ["key1", "key2", "key3"] :
      aws_route53_record.migadu_dkim[key].name == "${key}._domainkey.feohledger.com" &&
      aws_route53_record.migadu_dkim[key].records == toset(["${key}.feohledger.com._domainkey.migadu.com."])
    ])
    error_message = "Migadu signs with three rotating DKIM keys; each selector must CNAME to its Migadu-hosted key."
  }
}

run "apex_spf_authorizes_migadu_alone_until_a_token_is_set" {
  command = plan

  assert {
    condition     = aws_route53_record.apex_txt.records == toset(["v=spf1 include:spf.migadu.com -all"])
    error_message = "SES mail's envelope is on send., so the apex SPF must authorize Migadu alone and fail hard; with no token there is nothing else to publish."
  }
}

run "apex_txt_carries_the_migadu_token_beside_spf" {
  command = plan

  variables {
    migadu_verification_token = "p8dxwnab"
  }

  assert {
    condition     = aws_route53_record.apex_txt.records == toset(["v=spf1 include:spf.migadu.com -all", "hosted-email-verify=p8dxwnab"])
    error_message = "Route 53 keys a record set by name and type, so SPF and Migadu's ownership token must share the one apex TXT record."
  }
}

run "dmarc_starts_at_monitor_in_one_record" {
  command = plan

  assert {
    condition     = aws_route53_record.dmarc.name == "_dmarc.feohledger.com" && aws_route53_record.dmarc.records == toset(["v=DMARC1; p=none;"])
    error_message = "One _dmarc record governs both senders, and it starts at p=none."
  }
}

run "dmarc_reports_go_to_a_mailbox_on_the_domain" {
  command = plan

  variables {
    dmarc_policy       = "quarantine"
    dmarc_report_email = "ops@feohledger.com"
  }

  assert {
    condition     = aws_route53_record.dmarc.records == toset(["v=DMARC1; p=quarantine; rua=mailto:ops@feohledger.com;"])
    error_message = "The DMARC record must carry the chosen policy and the aggregate-report address."
  }
}

run "rejects_an_unknown_dmarc_policy" {
  command = plan

  variables {
    dmarc_policy = "monitor"
  }

  expect_failures = [var.dmarc_policy]
}

run "rejects_the_whole_migadu_verify_string" {
  command = plan

  variables {
    migadu_verification_token = "hosted-email-verify=p8dxwnab"
  }

  expect_failures = [var.migadu_verification_token]
}

run "rejects_a_dmarc_address_off_the_platform_domain" {
  command = plan

  variables {
    dmarc_report_email = "dmarc@reports.example.org"
  }

  expect_failures = [var.dmarc_report_email]
}

# ── Workload stack (network.tf, compute.tf, database.tf, monitoring.tf) ──────

run "database_is_private_encrypted_tls_only_and_hard_to_delete" {
  command = plan

  assert {
    condition     = aws_db_instance.main.publicly_accessible == false
    error_message = "RDS must not be publicly accessible; only the app VM reaches it."
  }

  assert {
    condition     = aws_db_instance.main.storage_encrypted == true
    error_message = "RDS storage must be encrypted with the app key."
  }

  assert {
    condition     = aws_db_instance.main.deletion_protection == true && aws_db_instance.main.skip_final_snapshot == false
    error_message = "RDS must have deletion protection on and take a final snapshot if ever deleted."
  }

  assert {
    condition     = aws_db_instance.main.backup_retention_period >= 1
    error_message = "Automated backups (point-in-time restore) are the reason the database is on RDS; retention must be at least a day."
  }

  assert {
    condition     = anytrue([for p in aws_db_parameter_group.main.parameter : p.name == "rds.force_ssl" && p.value == "1"])
    error_message = "The parameter group must force TLS on every connection."
  }

  assert {
    condition     = anytrue([for p in aws_db_parameter_group.main.parameter : p.name == "rds.force_ssl" && p.apply_method == "pending-reboot"])
    error_message = "rds.force_ssl must declare apply_method = \"pending-reboot\", as AWS reports it, or every plan shows a no-op in-place update."
  }

  assert {
    condition     = aws_db_instance.main.engine == "postgres" && aws_db_instance.main.engine_version == "16"
    error_message = "The database must be Postgres 16 — the major version dev, CI and the compose stack run."
  }
}

run "database_accepts_connections_from_the_app_vm_only" {
  command = plan

  assert {
    condition     = aws_vpc_security_group_ingress_rule.db_from_app.from_port == 5432 && aws_vpc_security_group_ingress_rule.db_from_app.to_port == 5432
    error_message = "The database security group must open Postgres only."
  }

  assert {
    condition     = aws_vpc_security_group_ingress_rule.db_from_app.cidr_ipv4 == null
    error_message = "The database must be reachable from the app VM's security group, never from a CIDR range."
  }
}

run "app_vm_signs_in_to_the_database_as_the_app_role_only" {
  command = plan

  assert {
    condition = toset(flatten([
      for s in data.aws_iam_policy_document.app_permissions.statement : tolist(s.resources)
      if contains(s.actions, "rds-db:connect")
    ])) == toset(["arn:aws:rds-db:us-east-1:000000000000:dbuser:db-TESTRESOURCEID0000000000/feoh_app"])
    error_message = "rds-db:connect must name exactly the feoh_app user on this instance — never dbuser:*, another instance, or the master user (docs/decisions.md §310)."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.app_permissions.statement :
      !contains(s.actions, "rds-db:connect") || length(s.actions) == 1
    ])
    error_message = "The database-connect grant is its own statement, so its resource scope cannot widen another action's."
  }
}

run "app_vm_has_no_ssh_and_only_web_ports" {
  command = plan

  assert {
    condition = alltrue([
      for r in [
        aws_vpc_security_group_ingress_rule.app_http,
        aws_vpc_security_group_ingress_rule.app_https,
        aws_vpc_security_group_ingress_rule.app_http3,
      ] : contains([80, 443], r.from_port) && r.from_port == r.to_port
    ])
    error_message = "The app VM may expose only 80 and 443; access is Session Manager, not SSH."
  }
}

run "app_vm_requires_imdsv2_with_container_hop_and_encrypted_disk" {
  command = plan

  assert {
    condition     = aws_instance.app.metadata_options[0].http_tokens == "required"
    error_message = "The app VM must require IMDSv2."
  }

  assert {
    condition     = aws_instance.app.metadata_options[0].http_put_response_hop_limit == 2
    error_message = "IMDS hop limit must be 2, or the containers cannot reach the instance-profile credentials."
  }

  assert {
    condition     = aws_instance.app.root_block_device[0].encrypted == true
    error_message = "The app VM's disk must be encrypted."
  }

  assert {
    condition     = aws_instance.app.disable_api_termination == true
    error_message = "The app VM must have termination protection on."
  }
}

run "apex_api_and_every_tenant_resolve_to_the_app_vm" {
  command = plan

  assert {
    condition     = aws_route53_record.apex.name == "feohledger.com" && aws_route53_record.api.name == "api.feohledger.com" && aws_route53_record.tenants.name == "*.feohledger.com"
    error_message = "The apex, api. and the tenant wildcard must each have an A record."
  }
}

run "rejects_a_weak_database_password" {
  command = plan

  variables {
    db_master_password = "short"
  }

  expect_failures = [var.db_master_password]
}

run "rejects_a_non_graviton_instance_type" {
  command = plan

  variables {
    app_instance_type = "t3.medium"
  }

  expect_failures = [var.app_instance_type]
}

run "alarms_need_a_subscriber" {
  command = plan

  variables {
    alert_emails = []
  }

  expect_failures = [var.alert_emails]
}

# ── Release deploys (deploy.tf) ──────────────────────────────────────────────

run "deploy_role_runs_only_the_deploy_document_on_only_the_app_vm" {
  command = plan

  assert {
    condition = toset(flatten([
      for s in data.aws_iam_policy_document.deploy_vm.statement : tolist(s.resources)
      if contains(s.actions, "ssm:SendCommand")
    ])) == toset([aws_instance.app.arn, aws_ssm_document.deploy.arn])
    error_message = "ssm:SendCommand must name exactly the app VM and the deploy document — any other document (AWS-RunShellScript) is arbitrary root shell on the VM."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.deploy_vm.statement :
      !contains(tolist(s.resources), "*") || toset(s.actions) == toset(["ssm:GetCommandInvocation"])
    ])
    error_message = "Only the read-only ssm:GetCommandInvocation, which has no resource type, may be granted on \"*\"."
  }

  assert {
    condition     = aws_iam_role_policy.deploy_vm.role == "feohledger-deploy"
    error_message = "The grant attaches to the baseline-owned deploy role by name; the role itself is not defined here."
  }
}

run "deploy_document_takes_only_a_commit_sha" {
  command = plan

  assert {
    condition     = keys(jsondecode(aws_ssm_document.deploy.content).parameters) == ["Ref"]
    error_message = "The deploy document must take exactly one parameter, the commit to deploy — a free-form parameter would make the deploy role a remote shell."
  }

  assert {
    condition     = jsondecode(aws_ssm_document.deploy.content).parameters.Ref.allowedPattern == "^[0-9a-f]{40}$"
    error_message = "Ref must be refused by SSM unless it is a full 40-character sha, before any shell sees it."
  }

  assert {
    condition = anytrue([
      for line in one(jsondecode(aws_ssm_document.deploy.content).mainSteps).inputs.runCommand :
      startswith(line, "sudo -iu ec2-user /home/ec2-user/feohledger/deploy/deploy.sh --ref '{{ Ref }}' ")
    ])
    error_message = "The document must run deploy.sh as ec2-user (the checkout owner, in the docker group) with the release's ref."
  }

  assert {
    condition     = element(one(jsondecode(aws_ssm_document.deploy.content).mainSteps).inputs.runCommand, -1) == "exit $rc"
    error_message = "The document must exit with deploy.sh's status, or a failed deploy reports success."
  }
}

# ── SPA on S3 + CloudFront (frontend.tf) ─────────────────────────────────────

run "frontend_bucket_is_never_public_and_only_this_distribution_reads_it" {
  command = plan

  assert {
    condition = alltrue([
      aws_s3_bucket_public_access_block.frontend.block_public_acls,
      aws_s3_bucket_public_access_block.frontend.block_public_policy,
      aws_s3_bucket_public_access_block.frontend.ignore_public_acls,
      aws_s3_bucket_public_access_block.frontend.restrict_public_buckets,
    ])
    error_message = "The SPA bucket must block every form of public access; CloudFront reads it through OAC."
  }

  assert {
    condition = alltrue([
      for s in data.aws_iam_policy_document.frontend_bucket.statement :
      flatten([for p in s.principals : tolist(p.identifiers)]) == ["cloudfront.amazonaws.com"]
      && flatten([for c in s.condition : tolist(c.values) if c.variable == "AWS:SourceArn"]) == [aws_cloudfront_distribution.frontend.arn]
      if s.effect == null || s.effect == "Allow"
    ])
    error_message = "Every Allow on the SPA bucket must go to the CloudFront service principal, pinned to this one distribution."
  }

  assert {
    condition = contains(flatten([
      for s in data.aws_iam_policy_document.frontend_bucket.statement : tolist(s.actions)
      if s.effect == null || s.effect == "Allow"
    ]), "s3:ListBucket")
    error_message = "Without s3:ListBucket, S3 answers 403 for a missing key and a missing asset stops being a 404."
  }

  assert {
    condition     = one(aws_s3_bucket_versioning.frontend.versioning_configuration).status == "Enabled"
    error_message = "The SPA bucket must be versioned — restoring prior object versions is the frontend rollback."
  }

  assert {
    condition     = aws_cloudfront_origin_access_control.frontend.signing_behavior == "always" && aws_cloudfront_origin_access_control.frontend.origin_access_control_origin_type == "s3"
    error_message = "CloudFront must sign every origin request (OAC), or the private bucket refuses it."
  }
}

run "distribution_serves_the_apex_and_every_tenant_on_the_platform_certificate" {
  command = plan

  assert {
    condition     = toset(aws_cloudfront_distribution.frontend.aliases) == toset(["feohledger.com", "*.feohledger.com"])
    error_message = "The distribution must answer the apex and the tenant wildcard — and only those (api. stays on the VM)."
  }

  assert {
    condition     = one(aws_cloudfront_distribution.frontend.viewer_certificate).ssl_support_method == "sni-only" && one(aws_cloudfront_distribution.frontend.viewer_certificate).minimum_protocol_version == "TLSv1.2_2021"
    error_message = "SNI with TLS 1.2+ only."
  }

  assert {
    condition     = one(aws_cloudfront_distribution.frontend.default_cache_behavior).viewer_protocol_policy == "redirect-to-https"
    error_message = "Plain HTTP must redirect, as Caddy did."
  }

  assert {
    condition = anytrue([
      for f in one(aws_cloudfront_distribution.frontend.default_cache_behavior).function_association :
      f.event_type == "viewer-request"
    ])
    error_message = "The SPA router must run on viewer-request, or every deep link 404s."
  }

  assert {
    condition     = aws_cloudfront_function.spa_router.runtime == "cloudfront-js-2.0" && aws_cloudfront_function.spa_router.publish
    error_message = "The router must be published on the 2.0 runtime; an unpublished function never reaches the LIVE stage."
  }

  assert {
    condition     = aws_cloudfront_cache_policy.frontend.min_ttl == 0
    error_message = "A non-zero minimum TTL overrides index.html's no-cache, and a release would serve the previous build."
  }

  assert {
    condition = alltrue([
      one(one(aws_cloudfront_response_headers_policy.frontend.security_headers_config).strict_transport_security).access_control_max_age_sec == 31536000,
      one(one(aws_cloudfront_response_headers_policy.frontend.security_headers_config).strict_transport_security).include_subdomains,
      one(one(aws_cloudfront_response_headers_policy.frontend.security_headers_config).frame_options).frame_option == "DENY",
      one(one(aws_cloudfront_response_headers_policy.frontend.security_headers_config).referrer_policy).referrer_policy == "strict-origin-when-cross-origin",
    ])
    error_message = "The SPA's security headers must match what Caddy served (deploy/Caddyfile)."
  }
}

run "missing_asset_alarm_watches_404s_and_has_its_metric" {
  command = plan

  assert {
    condition     = aws_cloudwatch_metric_alarm.frontend_404.metric_name == "404ErrorRate" && aws_cloudwatch_metric_alarm.frontend_404.dimensions["DistributionId"] == aws_cloudfront_distribution.frontend.id
    error_message = "The missing-asset alarm must watch this distribution's 404 rate: the 4xx rate also counts the 403s CloudFront gives disallowed-method requests (scanner POSTs), which page for nothing."
  }

  assert {
    condition     = aws_cloudfront_monitoring_subscription.frontend.distribution_id == aws_cloudfront_distribution.frontend.id && one(one(aws_cloudfront_monitoring_subscription.frontend.monitoring_subscription).realtime_metrics_subscription_config).realtime_metrics_subscription_status == "Enabled"
    error_message = "404ErrorRate is an additional metric: without an enabled monitoring subscription it has no data and the alarm can never fire."
  }
}

run "publish_grant_covers_only_this_bucket_and_distribution" {
  command = plan

  assert {
    condition = toset(flatten([
      for s in data.aws_iam_policy_document.deploy_frontend.statement : tolist(s.resources)
      ])) == toset([
      "arn:aws:s3:::feohledger-frontend-test",
      "arn:aws:s3:::feohledger-frontend-test/*",
      aws_cloudfront_distribution.frontend.arn,
    ])
    error_message = "The deploy role's frontend grant must name only the SPA bucket and this distribution."
  }

  assert {
    condition = !contains(flatten([
      for s in data.aws_iam_policy_document.deploy_frontend.statement : tolist(s.actions)
    ]), "s3:GetObject")
    error_message = "The publish never reads objects back; GetObject stays ungranted."
  }

  assert {
    condition     = aws_iam_role_policy.deploy_frontend.role == "feohledger-deploy"
    error_message = "The grant attaches to the baseline-owned deploy role by name."
  }
}

run "spa_dns_stays_on_the_vm_until_the_cutover_variable" {
  command = plan

  assert {
    condition     = aws_route53_record.apex.records == toset(["192.0.2.10"]) && length(aws_route53_record.apex.alias) == 0
    error_message = "By default the apex must stay on the VM's Elastic IP."
  }

  assert {
    condition     = aws_route53_record.tenants.records == toset(["192.0.2.10"]) && length(aws_route53_record.tenants.alias) == 0
    error_message = "By default the tenant wildcard must stay on the VM's Elastic IP."
  }

  assert {
    condition     = length(aws_route53_record.spa_ipv6) == 0
    error_message = "No AAAA record before the cutover — the VM has no IPv6 address."
  }
}

run "cutover_moves_the_apex_and_tenants_to_cloudfront_and_leaves_api_on_the_vm" {
  command = plan

  variables {
    frontend_on_cloudfront = true
  }

  assert {
    condition = alltrue([
      for r in [aws_route53_record.apex, aws_route53_record.tenants] :
      one(r.alias).name == "d0123456789abc.cloudfront.net" && one(r.alias).zone_id == "Z2FDTNDATAQYW2" && r.records == null
    ])
    error_message = "After the cutover the apex and tenant A records must alias the distribution, with no static IP left beside it."
  }

  assert {
    condition     = toset([for r in aws_route53_record.spa_ipv6 : r.name]) == toset(["feohledger.com", "*.feohledger.com"]) && alltrue([for r in aws_route53_record.spa_ipv6 : r.type == "AAAA"])
    error_message = "CloudFront serves IPv6, so the apex and wildcard each get an AAAA alias."
  }

  assert {
    condition     = aws_route53_record.api.records == toset(["192.0.2.10"])
    error_message = "api.<domain> must stay on the VM through the cutover."
  }
}

# ── External uptime checks + paging (uptime.tf, monitoring.tf) ───────────────

run "uptime_checks_the_apex_and_the_api_over_https_from_three_regions" {
  command = plan

  assert {
    condition     = toset(keys(aws_route53_health_check.uptime)) == toset(["apex", "api"])
    error_message = "Without a tenant slug, exactly the apex and the API are checked."
  }

  assert {
    condition = alltrue([
      for c in aws_route53_health_check.uptime :
      c.port == 443 && c.enable_sni && c.request_interval == 30 && c.failure_threshold == 3 && length(c.regions) == 3
    ])
    error_message = "Every check is HTTPS on 443 with SNI (CloudFront needs it), a 30 s interval, 3 failures, and three regions."
  }

  assert {
    condition     = aws_route53_health_check.uptime["apex"].fqdn == "feohledger.com" && aws_route53_health_check.uptime["apex"].type == "HTTPS" && aws_route53_health_check.uptime["apex"].resource_path == "/"
    error_message = "The apex check requests https://<domain>/."
  }

  assert {
    condition     = aws_route53_health_check.uptime["api"].fqdn == "api.feohledger.com" && aws_route53_health_check.uptime["api"].resource_path == "/api/health"
    error_message = "The API check requests the public liveness probe on api.<domain>."
  }

  assert {
    condition     = aws_route53_health_check.uptime["api"].type == "HTTPS_STR_MATCH" && aws_route53_health_check.uptime["api"].search_string == "\"status\":\"ok\""
    error_message = "The API check must match the liveness body, or a 200 from the SPA router (index.html) would pass for it."
  }
}

run "uptime_checks_a_tenant_host_when_a_slug_is_set" {
  command = plan

  variables {
    uptime_tenant_slug = "pilot-test"
  }

  assert {
    condition     = aws_route53_health_check.uptime["tenant"].fqdn == "pilot-test.feohledger.com" && aws_route53_health_check.uptime["tenant"].type == "HTTPS"
    error_message = "The tenant check requests https://<slug>.<domain>/."
  }

  assert {
    condition     = toset(keys(aws_cloudwatch_metric_alarm.uptime)) == toset(["apex", "api", "tenant"])
    error_message = "Every health check gets its own alarm."
  }
}

run "uptime_alarms_page_and_email_on_failure_and_recovery" {
  command = plan

  assert {
    condition = alltrue([
      for a in aws_cloudwatch_metric_alarm.uptime :
      a.namespace == "AWS/Route53" && a.metric_name == "HealthCheckStatus" && a.dimensions["HealthCheckId"] == "00000000-0000-0000-0000-0000000uptime"
      && a.comparison_operator == "LessThanThreshold" && a.threshold == 1
    ])
    error_message = "Each uptime alarm watches its health check's HealthCheckStatus and fires below 1."
  }

  assert {
    condition = alltrue([
      for a in aws_cloudwatch_metric_alarm.uptime :
      toset(a.alarm_actions) == toset(["arn:aws:sns:us-east-1:000000000000:feohledger-prod-pager", "arn:aws:sns:us-east-1:000000000000:feohledger-prod-alerts"])
      && toset(a.ok_actions) == toset(a.alarm_actions)
    ])
    error_message = "An outage and its recovery both go to the pager (phone) and the alerts (email) topics."
  }

  assert {
    condition     = alltrue([for a in aws_cloudwatch_metric_alarm.uptime : a.treat_missing_data == "breaching"])
    error_message = "An uptime alarm that cannot see its metric must not stay quiet."
  }
}

run "only_customer_facing_alarms_page" {
  command = plan

  assert {
    condition     = contains(aws_cloudwatch_metric_alarm.db_storage.alarm_actions, "arn:aws:sns:us-east-1:000000000000:feohledger-prod-pager")
    error_message = "Database storage running out pages: past it writes fail, and no uptime check notices first."
  }

  assert {
    condition = alltrue([
      for a in [
        aws_cloudwatch_metric_alarm.db_cpu,
        aws_cloudwatch_metric_alarm.db_memory,
        aws_cloudwatch_metric_alarm.db_cpu_credits,
        aws_cloudwatch_metric_alarm.app_cpu_credits,
        aws_cloudwatch_metric_alarm.frontend_5xx,
        aws_cloudwatch_metric_alarm.frontend_404,
        aws_cloudwatch_metric_alarm.platform_certificate_expiry,
      ] : !contains(a.alarm_actions, "arn:aws:sns:us-east-1:000000000000:feohledger-prod-pager")
    ])
    error_message = "Sizing, cost and early-warning alarms stay email-only, so the phone only rings when customers are affected."
  }
}

run "no_phone_number_means_no_sms_subscription" {
  command = plan

  assert {
    condition     = length(aws_sns_topic_subscription.pager_sms) == 0 && length(aws_sns_sms_preferences.pager) == 0 && length(aws_cloudwatch_metric_alarm.pager_delivery_failed) == 0 && length(aws_cloudwatch_metric_alarm.pager_sms_spend) == 0
    error_message = "With alert_phone_number unset, nothing may be subscribed by SMS, the account's SMS settings stay untouched, and there are no SMS alarms to watch."
  }
}

run "a_phone_number_subscribes_the_pager_topic_by_sms" {
  command = plan

  variables {
    alert_phone_number = "+12025550123"
  }

  assert {
    condition     = length(aws_sns_topic_subscription.pager_sms) == 1 && aws_sns_topic_subscription.pager_sms[0].protocol == "sms" && aws_sns_topic_subscription.pager_sms[0].topic_arn == "arn:aws:sns:us-east-1:000000000000:feohledger-prod-pager"
    error_message = "The number subscribes to the pager topic over SMS."
  }

  assert {
    condition     = nonsensitive(aws_sns_topic_subscription.pager_sms[0].endpoint) == "+12025550123"
    error_message = "The subscription texts the configured number."
  }

  assert {
    condition     = aws_sns_sms_preferences.pager[0].default_sms_type == "Transactional"
    error_message = "Pages go out as Transactional SMS."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.pager_delivery_failed[0].metric_name == "NumberOfNotificationsFailed" && aws_cloudwatch_metric_alarm.pager_delivery_failed[0].dimensions["TopicName"] == "feohledger-prod-pager" && toset(aws_cloudwatch_metric_alarm.pager_delivery_failed[0].alarm_actions) == toset(["arn:aws:sns:us-east-1:000000000000:feohledger-prod-alerts"])
    error_message = "A text SNS could not deliver must be reported by email, never through the pager it failed on."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.pager_sms_spend[0].metric_name == "SMSMonthToDateSpentUSD" && aws_cloudwatch_metric_alarm.pager_sms_spend[0].threshold == 0.8 && toset(aws_cloudwatch_metric_alarm.pager_sms_spend[0].alarm_actions) == toset(["arn:aws:sns:us-east-1:000000000000:feohledger-prod-alerts"])
    error_message = "Nearing the USD 1 default SMS spend limit, after which SNS stops texting, must be reported by email."
  }
}

run "rejects_a_phone_number_not_in_e164_form" {
  command = plan

  variables {
    alert_phone_number = "(202) 555-0123"
  }

  expect_failures = [var.alert_phone_number]
}

run "rejects_a_hostname_as_the_uptime_tenant_slug" {
  command = plan

  variables {
    uptime_tenant_slug = "pilot.feohledger.com"
  }

  expect_failures = [var.uptime_tenant_slug]
}

run "certificate_expiry_backstops_acm_renewal" {
  command = plan

  assert {
    condition     = aws_cloudwatch_metric_alarm.platform_certificate_expiry.namespace == "AWS/CertificateManager" && aws_cloudwatch_metric_alarm.platform_certificate_expiry.metric_name == "DaysToExpiry" && aws_cloudwatch_metric_alarm.platform_certificate_expiry.threshold == 30
    error_message = "The certificate alarm fires once DaysToExpiry drops under 30."
  }

  assert {
    condition     = aws_cloudwatch_metric_alarm.platform_certificate_expiry.dimensions["CertificateArn"] == "arn:aws:acm:us-east-1:000000000000:certificate/00000000-0000-0000-0000-000000000000"
    error_message = "It must watch the platform certificate."
  }

  assert {
    condition     = toset(aws_cloudwatch_metric_alarm.platform_certificate_expiry.alarm_actions) == toset(["arn:aws:sns:us-east-1:000000000000:feohledger-prod-alerts"])
    error_message = "A month's warning emails; it does not page."
  }
}

# The alarms on the default provider (db_storage) publish to the pager topic on
# the us-east-1 alias; that only works while the two are the same region.
run "pager_refuses_a_region_its_default_provider_alarms_cannot_reach" {
  command = plan

  variables {
    aws_region = "eu-west-1"
  }

  expect_failures = [aws_sns_topic.pager, aws_s3_bucket.frontend]
}
