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
  description = "Platform domain the ACM certificate covers (the apex plus a one-level wildcard for tenant subdomains). Must be a public Route 53 zone in this account — today the feohledger.jaredhoward.com child zone the estate account bootstrap delegated here. Moving to a product apex means hosting that zone in this account and changing this value."
  type        = string
  default     = "feohledger.jaredhoward.com"
}
