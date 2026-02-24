variable "image_tag" {
  description = "Docker image tag (typically git SHA)"
  type        = string
}

variable "ecr_repo" {
  description = "ECR repository URL"
  type        = string
}

variable "aws_region" {
  description = "AWS region"
  type        = string
}

variable "jwt_secret_ssm_path" {
  description = "SSM path for JWT shared secret (cross-account credential transport)"
  type        = string
  default     = ""
}
