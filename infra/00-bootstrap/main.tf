terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }

  # Local state only — this is the bootstrap bucket itself
}

provider "aws" {
  region = var.aws_region
}

resource "aws_s3_bucket" "state" {
  bucket        = var.state_bucket_name
  force_destroy = false

  tags = {
    # Ownership marker read by the justfile's adoption logic. NOT atomic with
    # creation (S3 CreateBucket carries no tags; terraform tags in a follow-up
    # call) — the crash window is covered by the surviving LOCAL bootstrap
    # state, and an untagged bucket without that proof aborts the install.
    ManagedBy = "engine-00-bootstrap"
    Purpose   = "terraform-state"
  }
}

# Standalone installs need their own lock table; a combined install adopts the
# iac-ci bucket AND its lock table instead (this root is never applied then).
resource "aws_dynamodb_table" "locks" {
  name         = var.lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  tags = {
    ManagedBy = "engine-00-bootstrap"
    Purpose   = "terraform-state-lock"
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
