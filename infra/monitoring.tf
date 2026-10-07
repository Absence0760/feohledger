# Alarms for the workload stack, emailed to var.alert_emails through one SNS
# topic. Each address must confirm its subscription once (AWS sends the link).
#
# What this covers: the VM's health checks (with automatic recovery and reboot)
# and its CPU credits, and the database's CPU, memory and storage. What it does
# not: whether the site answers from the outside — that is the external uptime
# monitor in docs/founder-runbooks/support-and-status.md, which also catches DNS
# and certificate failures no in-account metric can see.
#
# The topic is not encrypted with the app CMK: CloudWatch can only publish to a
# CMK-encrypted topic through an extra key-policy grant, and alarm notifications
# carry metric names and thresholds, never customer data.

#trivy:ignore:AVD-AWS-0095
resource "aws_sns_topic" "alerts" {
  name = "${var.project}-${var.environment}-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  for_each = toset(var.alert_emails)

  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = each.value
}

# ── App VM ───────────────────────────────────────────────────────────────────

# Host-level failure (AWS hardware or network): move the instance to healthy
# hardware, keeping its IDs, disk and Elastic IP.
resource "aws_cloudwatch_metric_alarm" "app_system_check" {
  alarm_name          = "${var.project}-${var.environment}-app-system-check"
  alarm_description   = "The app VM failed its AWS system status check; EC2 is recovering it onto new hardware."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_System"
  dimensions          = { InstanceId = aws_instance.app.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions = [
    "arn:aws:automate:${var.aws_region}:ec2:recover",
    aws_sns_topic.alerts.arn,
  ]
  ok_actions = [aws_sns_topic.alerts.arn]
}

# Guest-level failure (the OS hung, out of memory): reboot it.
resource "aws_cloudwatch_metric_alarm" "app_instance_check" {
  alarm_name          = "${var.project}-${var.environment}-app-instance-check"
  alarm_description   = "The app VM failed its instance status check; EC2 is rebooting it."
  namespace           = "AWS/EC2"
  metric_name         = "StatusCheckFailed_Instance"
  dimensions          = { InstanceId = aws_instance.app.id }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 3
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  alarm_actions = [
    "arn:aws:automate:${var.aws_region}:ec2:reboot",
    aws_sns_topic.alerts.arn,
  ]
  ok_actions = [aws_sns_topic.alerts.arn]
}

# t4g instances are burstable and run in unlimited credit mode by default:
# when the credit balance runs out the VM is not throttled, it bills the
# surplus CPU instead. A low balance under sustained load is therefore a cost
# signal — the point to move up an instance size rather than keep paying
# surplus credits.
resource "aws_cloudwatch_metric_alarm" "app_cpu_credits" {
  alarm_name          = "${var.project}-${var.environment}-app-cpu-credits-low"
  alarm_description   = "The app VM's CPU credit balance is low; sustained load is now billed as surplus credits. Consider a larger instance size."
  namespace           = "AWS/EC2"
  metric_name         = "CPUCreditBalance"
  dimensions          = { InstanceId = aws_instance.app.id }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 50
  comparison_operator = "LessThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# ── Database ─────────────────────────────────────────────────────────────────

resource "aws_cloudwatch_metric_alarm" "db_cpu" {
  alarm_name          = "${var.project}-${var.environment}-db-cpu-high"
  alarm_description   = "Database CPU has been above 80% for 15 minutes."
  namespace           = "AWS/RDS"
  metric_name         = "CPUUtilization"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Average"
  period              = 300
  evaluation_periods  = 3
  threshold           = 80
  comparison_operator = "GreaterThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# RDS db.t4g instances also run in unlimited credit mode: same signal as the
# VM's alarm above — a cost warning and a cue to resize.
resource "aws_cloudwatch_metric_alarm" "db_cpu_credits" {
  alarm_name          = "${var.project}-${var.environment}-db-cpu-credits-low"
  alarm_description   = "The database's CPU credit balance is low; sustained load is now billed as surplus credits. Consider a larger instance class."
  namespace           = "AWS/RDS"
  metric_name         = "CPUCreditBalance"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 20
  comparison_operator = "LessThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# 1 GB, not 2: storage autoscaling itself triggers at 10% free (2 GB of the
# initial 20), so a 2 GB alarm would fire on every routine scale-up. Below 1 GB
# means autoscaling is not keeping up or the max_allocated ceiling is reached.
resource "aws_cloudwatch_metric_alarm" "db_storage" {
  alarm_name          = "${var.project}-${var.environment}-db-storage-low"
  alarm_description   = "Database free storage is below 1 GB: autoscaling is not keeping up or db_max_allocated_storage_gb is reached. Past that, writes fail."
  namespace           = "AWS/RDS"
  metric_name         = "FreeStorageSpace"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1024 * 1024 * 1024
  comparison_operator = "LessThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

resource "aws_cloudwatch_metric_alarm" "db_memory" {
  alarm_name          = "${var.project}-${var.environment}-db-memory-low"
  alarm_description   = "Database freeable memory is below 50 MB; the instance class is too small."
  namespace           = "AWS/RDS"
  metric_name         = "FreeableMemory"
  dimensions          = { DBInstanceIdentifier = aws_db_instance.main.identifier }
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 3
  threshold           = 50 * 1024 * 1024
  comparison_operator = "LessThanThreshold"
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}
