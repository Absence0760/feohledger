# RDS for PostgreSQL 16 — the system of record for the control plane and every
# tenant database (`feohledger` + one `feoh_<slug>` per organization, all on
# this one instance). docs/decisions.md §254 records why the pilot runs a
# managed database beside an otherwise single-VM stack: point-in-time restore to
# within ~5 minutes instead of a nightly dump's 24-hour loss window.
#
# Reachable only from the app VM's security group, in private subnets with no
# route to the internet. TLS is mandatory (`rds.force_ssl`); the app connects
# with PGSSLMODE from the deploy env (deploy/README.md).
#
# The master password is a write-only attribute fed from an ephemeral variable,
# so it is never stored in Terraform state or plan files. The operator supplies
# it from the private infra-secrets repo at apply time (README.md § Workload
# stack); the same value goes into FEOH_DATABASE_URL in the sops env.

# RDS creates this group itself on first export — unencrypted by a CMK and kept
# forever. Declaring it first gives connection logs (client addresses, user
# names) the same 90-day retention as the VPC flow logs. Same encryption call
# as the flow-log group (network.tf).
#trivy:ignore:AVD-AWS-0017
resource "aws_cloudwatch_log_group" "rds_postgresql" {
  name              = "/aws/rds/instance/${var.project}-${var.environment}/postgresql"
  retention_in_days = 90
}

resource "aws_db_subnet_group" "main" {
  name       = "${var.project}-${var.environment}"
  subnet_ids = aws_subnet.private[*].id

  tags = {
    Name = "${var.project}-${var.environment}"
  }
}

resource "aws_security_group" "db" {
  name        = "${var.project}-${var.environment}-db"
  description = "Postgres, from the app VM only."
  vpc_id      = aws_vpc.main.id

  tags = {
    Name = "${var.project}-${var.environment}-db"
  }
}

resource "aws_vpc_security_group_ingress_rule" "db_from_app" {
  security_group_id            = aws_security_group.db.id
  description                  = "Postgres from the app VM"
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.app.id
}

resource "aws_db_parameter_group" "main" {
  name        = "${var.project}-${var.environment}-pg16"
  family      = "postgres16"
  description = "Postgres 16 for ${var.project} ${var.environment}: TLS-only, connection logging."

  # Refuse any connection that is not TLS.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  # Connection and disconnection records for incident review (who connected,
  # from where, for how long). Statements are not logged: they would carry
  # invoice data and banking fields into CloudWatch.
  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }
}

# Performance Insights is not free past its 7-day tier and adds nothing at pilot
# load; turn it on when there is a query to tune.
#trivy:ignore:AVD-AWS-0133
resource "aws_db_instance" "main" {
  identifier     = "${var.project}-${var.environment}"
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  allocated_storage     = var.db_allocated_storage_gb
  max_allocated_storage = var.db_max_allocated_storage_gb # storage autoscaling ceiling
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.app.arn

  # The app's control-plane database; tenant databases are created beside it
  # by tenant provisioning (CREATE DATABASE as the master user).
  db_name  = "feohledger"
  username = "postgres"

  password_wo         = var.db_master_password
  password_wo_version = var.db_master_password_version

  # IAM database authentication (Trivy AWS-0176). Additive: it lets a role
  # granted `rds-db:connect` sign in with a 15-minute token instead of the
  # master password, and password login keeps working alongside it. The app
  # still connects with the password in FEOH_DATABASE_URL; moving it onto
  # tokens is tracked in docs/followups.md. Toggling it is an in-place modify.
  iam_database_authentication_enabled = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  # Point-in-time restore window. RDS keeps transaction logs every 5 minutes,
  # so any second inside this window is restorable.
  backup_retention_period = var.db_backup_retention_days
  backup_window           = "07:00-07:30" # UTC; ~03:00 US Eastern
  maintenance_window      = "sun:08:00-sun:08:30"
  copy_tags_to_snapshot   = true

  # Minor versions carry security fixes and apply in the maintenance window;
  # a major upgrade is a deliberate change to db_engine_version.
  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false

  # A database holding customers' financial records must not be deletable by a
  # stray `terraform destroy`, and must leave a snapshot if it ever is.
  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${var.project}-${var.environment}-final"

  # Postgres logs to CloudWatch for connection review (see the parameter group).
  enabled_cloudwatch_logs_exports = ["postgresql"]

  performance_insights_enabled = false

  tags = {
    Name = "${var.project}-${var.environment}"
  }

  depends_on = [aws_cloudwatch_log_group.rds_postgresql]
}
