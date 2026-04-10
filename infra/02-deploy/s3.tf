resource "aws_s3_bucket" "internal" {
  bucket        = local.internal_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_lifecycle_configuration" "internal" {
  bucket = aws_s3_bucket.internal.id

  rule {
    id     = "expire-after-1-day"
    status = "Enabled"

    expiration {
      days = 1
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "internal" {
  bucket = aws_s3_bucket.internal.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "internal" {
  bucket = aws_s3_bucket.internal.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket" "done" {
  bucket        = local.done_bucket_name
  force_destroy = true
}

resource "aws_s3_bucket_lifecycle_configuration" "done" {
  bucket = aws_s3_bucket.done.id

  rule {
    id     = "expire-after-1-day"
    status = "Enabled"

    expiration {
      days = 1
    }
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "done" {
  bucket = aws_s3_bucket.done.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "done" {
  bucket = aws_s3_bucket.done.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
