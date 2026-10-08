# Network for the single-VM workload stack (docs/minimal-deployment.md).
#
# One VPC, two availability zones:
#
#   - public subnets  — the app VM (it needs a public IP for Caddy's TLS and to
#                       reach the model APIs, Stripe and S3).
#   - private subnets — RDS only. No route to the internet at all: RDS needs
#                       none, and the only thing that may reach it is the VM's
#                       security group (database.tf).
#
# There is deliberately no NAT gateway. It would cost ~$33/month to give the
# private subnets outbound internet that nothing in them uses. RDS requires a
# subnet group spanning two AZs even for a single-AZ instance, which is the only
# reason there are two of each.
#
# Its own VPC rather than the account's default one: the default VPC's subnets
# are all public and auto-assign public IPs, which is the wrong shape for a
# database, and a stack that depends on resources Terraform did not create is
# not reproducible in a fresh account.

data "aws_availability_zones" "available" {
  state = "available"
}

locals {
  azs = slice(data.aws_availability_zones.available.names, 0, 2)
}

resource "aws_vpc" "main" {
  cidr_block           = var.vpc_cidr
  enable_dns_support   = true
  enable_dns_hostnames = true # RDS endpoints are DNS names

  tags = {
    Name = "${var.project}-${var.environment}"
  }
}

resource "aws_internet_gateway" "main" {
  vpc_id = aws_vpc.main.id

  tags = {
    Name = "${var.project}-${var.environment}"
  }
}

resource "aws_subnet" "public" {
  count = 2

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index)

  # The VM gets an Elastic IP (compute.tf), so nothing launched here needs an
  # auto-assigned address.
  map_public_ip_on_launch = false

  tags = {
    Name = "${var.project}-${var.environment}-public-${local.azs[count.index]}"
  }
}

resource "aws_subnet" "private" {
  count = 2

  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 10)

  tags = {
    Name = "${var.project}-${var.environment}-private-${local.azs[count.index]}"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.main.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.main.id
  }

  tags = {
    Name = "${var.project}-${var.environment}-public"
  }
}

resource "aws_route_table_association" "public" {
  count = 2

  subnet_id      = aws_subnet.public[count.index].id
  route_table_id = aws_route_table.public.id
}

# The private subnets keep the VPC's main route table: local routes only.

# The default security group of a new VPC allows all traffic between its
# members. Nothing uses it, so strip every rule rather than leave an
# allow-all group lying around for something to be attached to by accident.
resource "aws_default_security_group" "main" {
  vpc_id = aws_vpc.main.id
}

# Flow logs for rejected traffic only — the signal that matters for incident
# review (someone probing the VM or the database), at a fraction of the cost of
# logging every accepted packet.
#
# Encrypted with CloudWatch Logs' service-owned key rather than the app CMK:
# using the CMK would need a key-policy grant to the logs service principal, and
# rejected-connection metadata (addresses and ports) is not the customer data
# the CMK exists to protect.
#trivy:ignore:AVD-AWS-0017
resource "aws_cloudwatch_log_group" "vpc_flow_logs" {
  name              = "/${var.project}/${var.environment}/vpc-flow-logs"
  retention_in_days = 90
}

data "aws_iam_policy_document" "flow_logs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["vpc-flow-logs.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }
  }
}

resource "aws_iam_role" "flow_logs" {
  name               = "${var.project}-${var.environment}-vpc-flow-logs"
  assume_role_policy = data.aws_iam_policy_document.flow_logs_assume.json
}

data "aws_iam_policy_document" "flow_logs_write" {
  statement {
    actions = [
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["${aws_cloudwatch_log_group.vpc_flow_logs.arn}:*"]
  }
}

resource "aws_iam_role_policy" "flow_logs" {
  name   = "write-flow-logs"
  role   = aws_iam_role.flow_logs.id
  policy = data.aws_iam_policy_document.flow_logs_write.json
}

resource "aws_flow_log" "main" {
  vpc_id          = aws_vpc.main.id
  traffic_type    = "REJECT"
  log_destination = aws_cloudwatch_log_group.vpc_flow_logs.arn
  iam_role_arn    = aws_iam_role.flow_logs.arn
}
