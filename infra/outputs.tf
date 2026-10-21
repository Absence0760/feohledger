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

output "app_instance_id" {
  description = "Instance ID of the app VM — the target for `aws ssm start-session` (README.md § Workload stack)."
  value       = aws_instance.app.id
}

output "app_public_ip" {
  description = "Elastic IP of the app VM. api. points here always; the apex and *. do until frontend_on_cloudfront moves them to CloudFront."
  value       = aws_eip.app.public_ip
}

output "db_address" {
  description = "RDS endpoint hostname — the host part of FEOH_DATABASE_URL in the sops env: postgresql+asyncpg://feoh_app@<db_address>:5432/feohledger with FEOH_DATABASE_IAM_AUTH true (no password — docs/decisions.md §304)."
  value       = aws_db_instance.main.address
}

output "alerts_topic_arn" {
  description = "SNS topic every infrastructure alarm notifies."
  value       = aws_sns_topic.alerts.arn
}

output "vm_deploy_document_name" {
  description = "SSM command document the release workflow runs on the app VM (deploy.tf). .github/workflows/vm-deploy.yml names it directly; the two must agree."
  value       = aws_ssm_document.deploy.name
}

output "frontend_bucket" {
  description = "S3 bucket holding the published SPA build — the FRONTEND_BUCKET secret on the production environment (.github/workflows/vm-deploy.yml)."
  value       = aws_s3_bucket.frontend.bucket
}

output "frontend_distribution_id" {
  description = "CloudFront distribution serving the SPA — the CLOUDFRONT_DISTRIBUTION_ID secret on the production environment."
  value       = aws_cloudfront_distribution.frontend.id
}

output "frontend_distribution_domain" {
  description = "The distribution's own *.cloudfront.net hostname, for testing a publish before frontend_on_cloudfront moves DNS."
  value       = aws_cloudfront_distribution.frontend.domain_name
}
