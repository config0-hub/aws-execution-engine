terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }

  # backend.tf is generated at deploy time by scripts/generate_backend.sh
}

provider "aws" {
  region = var.aws_region
}

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  prefix               = var.project_prefix
  account_id           = data.aws_caller_identity.current.account_id
  region               = data.aws_region.current.name
  image_uri            = "${var.ecr_repo}:${var.image_tag}"
  internal_bucket_name = "${local.prefix}-internal"
  done_bucket_name     = "${local.prefix}-done"
  default_lambda_memory  = var.lambda_memory
  default_lambda_timeout = var.lambda_timeout
  codebuild_compute      = var.codebuild_compute_type != "" ? var.codebuild_compute_type : "BUILD_GENERAL1_SMALL"
}
