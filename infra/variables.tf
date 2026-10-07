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
  description = "Monthly AWS spend ceiling (USD) for the account-wide budget. The single-VM workload stack runs ~$45–55/month (t4g.medium VM + EBS + public IPv4 ~$31, db.t4g.micro RDS + storage ~$15, KMS, Route 53, S3, flow logs and alarms a few dollars), so 75 leaves headroom while still flagging a runaway within days. Raise it deliberately when the stack grows."
  type        = number
  default     = 75

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

variable "migadu_verification_token" {
  description = "Migadu's ownership token for the platform domain: the value after `hosted-email-verify=` at admin.migadu.com → Domains → <domain> → DNS Configuration. Account-specific but not secret (it is published in DNS), so it lives in the operator's tfvars. Null until the domain has been added in Migadu; every other mail record is created anyway, and Migadu verifies the domain once the token is published (README.md § Email)."
  type        = string
  default     = null

  validation {
    condition     = var.migadu_verification_token == null || can(regex("^[A-Za-z0-9]+$", var.migadu_verification_token))
    error_message = "migadu_verification_token is the bare token, such as p8dxwnab, not the whole hosted-email-verify=... string."
  }
}

variable "dmarc_policy" {
  description = "What receivers do with mail that fails DMARC for the platform domain. Starts at `none` (monitor only) because both senders are new; raise it to `quarantine`, then `reject`, once the aggregate reports show SES and Migadu mail aligning."
  type        = string
  default     = "none"

  validation {
    condition     = contains(["none", "quarantine", "reject"], var.dmarc_policy)
    error_message = "dmarc_policy must be none, quarantine or reject."
  }
}

variable "dmarc_report_email" {
  description = "Mailbox that receives DMARC aggregate reports (rua) for the platform domain, such as ops@<domain> on Migadu. Unlike budget_alert_emails it is published in public DNS. It must be on the platform domain: a reporting address on another domain is ignored unless that domain publishes an external-reporting authorization. Null publishes the policy with no reporting address, which leaves `none` with nothing to monitor."
  type        = string
  default     = null

  validation {
    condition     = var.dmarc_report_email == null || can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.dmarc_report_email))
    error_message = "dmarc_report_email must be an email address."
  }

  validation {
    condition     = var.dmarc_report_email == null || can(regex("@${replace(lower(var.domain_name), ".", "\\.")}$", lower(var.dmarc_report_email)))
    error_message = "dmarc_report_email must be a mailbox on the platform domain; receivers drop reports addressed to another domain that has not authorized them."
  }
}

# ── Workload stack (network.tf, compute.tf, database.tf, monitoring.tf) ──────

variable "vpc_cidr" {
  description = "CIDR block of the workload VPC. /16 leaves room for the two public (x.x.0-1.0/24) and two private (x.x.10-11.0/24) subnets and anything added later."
  type        = string
  default     = "10.40.0.0/16"
}

variable "app_instance_type" {
  description = "EC2 instance type for the app VM. Must be arm64 (Graviton): the AMI is Amazon Linux 2023 arm64. t4g.medium (2 vCPU, 4 GB) — 2 GB is tight once PDF rendering and AI extraction run in-process beside the API, Redis and Caddy."
  type        = string
  default     = "t4g.medium"

  validation {
    condition     = can(regex("^(t4g|m7g|m8g|c7g|c8g|r7g|r8g|m6g|c6g|r6g)\\.", var.app_instance_type))
    error_message = "app_instance_type must be a Graviton (arm64) type such as t4g.medium; the AMI is arm64."
  }
}

variable "app_volume_size_gb" {
  description = "Root volume size (GB) for the app VM. Holds the OS, Docker images and build cache, Redis AOF and Caddy's certificates — not the database."
  type        = number
  default     = 30
}

variable "db_engine_version" {
  description = "Postgres major version for RDS. Major only: RDS picks the newest minor and auto-applies minor upgrades in the maintenance window. Changing it is a major-version upgrade, which needs allow_major_version_upgrade turned on for that apply."
  type        = string
  default     = "16"
}

variable "db_instance_class" {
  description = "RDS instance class. db.t4g.micro (2 vCPU burstable, 1 GB) is enough for a pilot; move up when the db-memory or db-cpu alarm fires."
  type        = string
  default     = "db.t4g.micro"
}

variable "db_allocated_storage_gb" {
  description = "Initial RDS storage (GB, gp3). 20 GB is the gp3 minimum."
  type        = number
  default     = 20
}

variable "db_max_allocated_storage_gb" {
  description = "Ceiling for RDS storage autoscaling (GB). Storage grows automatically up to this; the db-storage-low alarm warns before it is reached."
  type        = number
  default     = 100

  validation {
    condition     = var.db_max_allocated_storage_gb >= var.db_allocated_storage_gb
    error_message = "db_max_allocated_storage_gb must be at least db_allocated_storage_gb."
  }
}

variable "db_backup_retention_days" {
  description = "Days of RDS automated backups — the point-in-time-restore window. 7 covers a week's worth of 'when did this go wrong'; the nightly logical dumps in the backups bucket (deploy/backup.sh) keep 90 days beyond it."
  type        = number
  default     = 7

  validation {
    condition     = var.db_backup_retention_days >= 1 && var.db_backup_retention_days <= 35
    error_message = "db_backup_retention_days must be 1–35; 0 would disable point-in-time restore, which is the reason this database is on RDS."
  }
}

variable "db_multi_az" {
  description = "Run a standby replica in a second AZ with automatic failover. Off for the pilot (it doubles the database cost); turn it on when a customer needs an uptime commitment."
  type        = bool
  default     = false
}

variable "db_master_password" {
  description = "RDS master password. Ephemeral and write-only: it is passed to RDS but never stored in Terraform state or plans. Supply it from the private infra-secrets repo at apply time (README.md § Workload stack) — the same value goes into FEOH_DATABASE_URL in the sops env. URL-safe characters only (e.g. `openssl rand -hex 24`), since it is embedded in a connection URL."
  type        = string
  sensitive   = true
  ephemeral   = true
  nullable    = false

  validation {
    condition     = length(var.db_master_password) >= 24 && can(regex("^[A-Za-z0-9]+$", var.db_master_password))
    error_message = "db_master_password must be at least 24 alphanumeric characters (URL-safe; it is embedded in FEOH_DATABASE_URL)."
  }
}

variable "db_master_password_version" {
  description = "Bump this (1 → 2 → …) in the same apply that supplies a new db_master_password. A write-only attribute is invisible to Terraform's diff, so the version is how a password rotation is noticed and sent to RDS."
  type        = number
  default     = 1
}

variable "alert_emails" {
  description = "Addresses that receive infrastructure alarms (VM health, database CPU/memory/storage) through SNS. Each must click the confirmation link AWS sends once. Deliberately no default — this repo is public."
  type        = list(string)

  validation {
    condition     = length(var.alert_emails) > 0
    error_message = "Provide at least one address in alert_emails — an alarm with no subscriber alerts no one."
  }

  validation {
    condition     = alltrue([for e in var.alert_emails : can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", e))])
    error_message = "Every entry in alert_emails must be an email address."
  }
}
