# Root Terraform entrypoint.
#
# The AWS stack is still being built out — see docs/production-deployment.md
# for the planned shape (ECS, ALB, CloudFront, RDS). What lives here today is
# the security substrate needed as a SOC 2 engineering prerequisite:
#
#   - kms.tf  : customer-managed KMS key for at-rest encryption, auto-rotated
#   - s3.tf   : buckets for invoice files and audit-log shipping, with
#               versioning + Object Lock
#
# The `terraform` block pins versions and keeps state in the S3 bucket the
# estate account bootstrap created in the FeohLedger AWS account.

terraform {
  required_version = ">= 1.11"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.44"
    }
  }

  # State lives in `feohledger-tfstate-<account-id>`, created by the estate
  # account bootstrap (templates/infra/bootstrap/2-baseline) with versioning and
  # SSE. Locking is S3-native — `use_lockfile` (Terraform 1.11+) takes a
  # conditional-write lock object beside the state, so there is no DynamoDB
  # table. The key already matches the per-env layout this stack is meant to
  # split into (`infra/envs/<env>/`), so that split is a file move rather than a
  # state migration.
  #
  # `bucket` is the one partial-config value: its name embeds the AWS account
  # ID, which stays out of this public repo. Pass it with
  # `terraform init -backend-config=backend.config` (gitignored; shape in
  # backend.config.example) — the same pattern the estate's other bootstrapped
  # projects use.
  #
  # Terraform 1.15+ `validate` checks the backend's required arguments, so
  # credential-free validation (CI, local) still needs a gitignored
  # `backend_override.tf` pointing at the `local` backend — see
  # README.md § Local usage / .github/workflows/terraform.yml.
  backend "s3" {
    key          = "envs/prod/terraform.tfstate"
    region       = "us-east-1"
    use_lockfile = true
    encrypt      = true
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      project     = var.project
      managed_by  = "terraform"
      environment = var.environment
    }
  }
}

# CloudFront only accepts ACM certificates issued in us-east-1, so the platform
# certificate (acm.tf) is pinned there regardless of var.aws_region.
provider "aws" {
  alias  = "us_east_1"
  region = "us-east-1"

  default_tags {
    tags = {
      project     = var.project
      managed_by  = "terraform"
      environment = var.environment
    }
  }
}
