variable "aws_region" {
  description = "Primary AWS region for all resources defined in this module."
  type        = string
  default     = "us-east-1"
}

variable "project" {
  description = "Project slug used for resource naming + default_tags."
  type        = string
  default     = "feohledger"
}

variable "environment" {
  description = "Deployment environment (prod, staging, etc.). Becomes part of resource names and tags so multiple envs can coexist in the same account."
  type        = string
  default     = "prod"
}

variable "invoice_files_bucket_name" {
  description = "S3 bucket that stores uploaded invoice PDFs / images. Must be globally unique; typically '<project>-invoices-<env>-<random>'."
  type        = string
}

variable "audit_logs_bucket_name" {
  description = "S3 bucket that receives shipped tenant audit-log rows. Retention runs under Object Lock Compliance mode for 7 years — this bucket name cannot be reused after retention starts."
  type        = string
}

variable "invoice_retention_days" {
  description = "Default S3 Object Lock retention (days) applied to new invoice-file objects. Governance mode — a suitably privileged IAM principal can still override for a legitimate business reason. 365d is the shortest period that covers a full tax cycle."
  type        = number
  default     = 365
}

variable "audit_retention_days" {
  description = "Default S3 Object Lock retention (days) for shipped audit-log objects. Compliance mode — not even the root account can shorten it during the lock period. 7 years (2555 days) matches the SOX / SOC 2 long-tail evidence window."
  type        = number
  default     = 2555
}

variable "access_logs_bucket_name" {
  description = "S3 bucket that aggregates server-access logs from the invoice + audit-log buckets. Required by AWS-0089 / SOC 2 CC7.2 — every data-bearing bucket must have access logging enabled."
  type        = string
}

variable "access_logs_retention_days" {
  description = "Lifecycle expiration (days) for objects in the access-logs bucket. 365d covers a full audit cycle without the storage cost of indefinite retention; access logs are signal-of-access, not the audit trail itself (that's the Object Lock bucket)."
  type        = number
  default     = 365
}

variable "backups_bucket_name" {
  description = "S3 bucket that receives the nightly pg_dump stream from deploy/backup.sh (control plane + every tenant DB). Must be globally unique; typically '<project>-backups-<env>-<random>'. No Object Lock — the lifecycle expiration is the retention policy."
  type        = string
}

variable "backup_retention_days" {
  description = "Lifecycle expiration (days) for nightly database dumps in the backups bucket. A full dump of every DB lands every night, so this window is the entire storage bill — 90d is a generous restore horizon at pilot volume."
  type        = number
  default     = 90
}

variable "domain_name" {
  description = "The platform's registered apex domain. acm.tf issues the certificate for it plus a one-level wildcard (tenants live on <slug>.<domain>), and domain.tf manages its registration settings. It must be registered through Route 53 in this account, which is what creates the public hosted zone of the same name that both files look up (README.md § Platform domain + certificate)."
  type        = string
  default     = "feohledger.com"

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]*[a-z0-9])?\\.[a-z]{2,}$", var.domain_name))
    error_message = "domain_name must be a registered apex such as feohledger.com, not a subdomain: domain.tf manages the domain's registration, and only the apex has one."
  }
}

variable "monthly_budget_limit_usd" {
  description = "Monthly AWS spend ceiling (USD) for the account-wide budget. Pre-launch the account runs two KMS keys, a hosted zone and near-empty S3 buckets — a few dollars a month — so 25 leaves headroom for early experiments while still flagging a runaway within days. Raise it deliberately when the ECS/RDS stack lands."
  type        = number
  default     = 25

  validation {
    condition     = var.monthly_budget_limit_usd > 0
    error_message = "monthly_budget_limit_usd must be positive — a zero budget alerts on the first cent and trains everyone to ignore it."
  }
}

variable "budget_alert_emails" {
  description = "Addresses that receive every budget notification. Deliberately no default: this repo is public, so the real address lives only in the operator's tfvars (canonical copy in the private infra-secrets repo)."
  type        = list(string)

  validation {
    condition     = length(var.budget_alert_emails) > 0
    error_message = "Provide at least one address in budget_alert_emails — a budget with no subscribers alerts no one."
  }

  validation {
    condition     = alltrue([for e in var.budget_alert_emails : can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", e))])
    error_message = "Every entry in budget_alert_emails must be an email address."
  }

  validation {
    condition     = alltrue([for e in var.budget_alert_emails : !can(regex("(?i)@example\\.(com|org|net)$", e))])
    error_message = "budget_alert_emails still holds the example placeholder — replace it with a real address."
  }
}
