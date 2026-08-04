terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

data "aws_caller_identity" "current" {}
data "aws_partition" "current" {}
data "aws_region" "current" {}

locals {
  prefix     = var.project_prefix
  account_id = data.aws_caller_identity.current.account_id
  region     = data.aws_region.current.region
  # Account-suffixed (S3 names are global) AND engine-segmented: the iac-ci
  # foundation stack owns "<prefix>-done-<acct>", so the engine's buckets
  # carry an "engine" segment to avoid dual Terraform ownership of one bucket.
  internal_bucket_name   = "${local.prefix}-engine-internal-${local.account_id}"
  done_bucket_name       = "${local.prefix}-engine-done-${local.account_id}"
  default_lambda_memory  = var.lambda_memory
  default_lambda_timeout = var.lambda_timeout
  codebuild_compute      = var.codebuild_compute_type != "" ? var.codebuild_compute_type : "BUILD_GENERAL1_SMALL"
}
