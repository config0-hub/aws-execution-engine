variable "state_bucket_name" {
  description = "Name of the S3 bucket for Terraform state"
  type        = string
}

variable "lock_table_name" {
  description = "Name of the DynamoDB lock table for Terraform state locking"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}
