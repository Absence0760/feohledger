output "app_kms_key_arn" {
  description = "ARN of the customer-managed KMS key used for at-rest encryption (RDS / S3 / SQS)."
  value       = aws_kms_key.app.arn
}

output "app_kms_key_alias" {
  description = "Alias name pointing at the app KMS key — safe to reference from IAM policies that want to survive key rotation."
  value       = aws_kms_alias.app.name
}

output "invoice_files_bucket" {
  description = "Name of the S3 bucket that stores uploaded invoice files."
  value       = aws_s3_bucket.invoice_files.bucket
}

output "audit_logs_bucket" {
  description = "Name of the S3 bucket that receives shipped audit-log objects. Subject to COMPLIANCE-mode Object Lock — deletion of this bucket is not possible until all objects have aged past their retention date."
  value       = aws_s3_bucket.audit_logs.bucket
}

output "backups_bucket" {
  description = "Name of the S3 bucket that receives the nightly database dumps (deploy/backup.sh) — set it as BACKUP_S3_BUCKET in the deploy env."
  value       = aws_s3_bucket.backups.bucket
}

output "platform_certificate_arn" {
  description = "ARN of the validated us-east-1 ACM certificate for the platform domain and its wildcard — for the CloudFront distribution (or an ALB listener) when the workload stack lands. Taken from the validation resource, so it only resolves once the certificate is ISSUED."
  value       = aws_acm_certificate_validation.platform.certificate_arn
}

output "platform_zone_id" {
  description = "Route 53 hosted zone ID of the platform domain, for the alias records the workload stack will add."
  value       = data.aws_route53_zone.platform.zone_id
}

output "ses_identity_arn" {
  description = "ARN of the SES domain identity the app sends as. Scope the VM instance profile's ses:SendEmail to it (docs/minimal-deployment.md § 1)."
  value       = aws_sesv2_email_identity.platform.arn
}

output "ses_mail_from_domain" {
  description = "The envelope-sender (MAIL FROM) subdomain SES addresses bounces to — its MX and SPF live there, apart from the apex records Migadu owns."
  value       = aws_sesv2_email_identity_mail_from_attributes.platform.mail_from_domain
}
