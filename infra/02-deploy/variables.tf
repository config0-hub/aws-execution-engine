variable "project_prefix" {
  description = "Resource name prefix (e.g. 'aws-exe-sys'). REQUIRED — no random generation."
  type        = string
  # No default — must be explicitly provided

  validation {
    condition     = length(var.project_prefix) > 0
    error_message = "project_prefix is required. The installer does not generate random names."
  }
}

variable "lambda_memory" {
  description = "Default Lambda memory in MB (0 = use per-function defaults)"
  type        = number
  default     = 0
}

variable "lambda_timeout" {
  description = "Default Lambda timeout in seconds (0 = use per-function defaults)"
  type        = number
  default     = 0
}

variable "codebuild_compute_type" {
  description = "CodeBuild compute type (empty = BUILD_GENERAL1_SMALL)"
  type        = string
  default     = ""
}

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
