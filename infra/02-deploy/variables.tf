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

variable "engine_code_source" {
  description = <<-EOT
    Engine code loading strategy (bootstrap seam).

    - kind = "inline"  — Lambdas load from the baked image (current behavior, zero diff).
                         value must be "".
    - kind = "ssm_url" — Lambdas load via aws_exe_sys/bootstrap_handler.py at cold start.
                         value is the SSM Parameter Store path holding a
                         base64-encoded JSON dict {"url": "...", "sha256": "..."}.

    The default is "inline" — leaving this unset keeps the live deployment unchanged.
  EOT
  type = object({
    kind  = string
    value = string
  })
  default = {
    kind  = "inline"
    value = ""
  }
  validation {
    condition     = contains(["inline", "ssm_url"], var.engine_code_source.kind)
    error_message = "engine_code_source.kind must be \"inline\" or \"ssm_url\"."
  }
  validation {
    condition     = var.engine_code_source.kind == "inline" || length(var.engine_code_source.value) > 0
    error_message = "engine_code_source.value must be a non-empty SSM path when kind=\"ssm_url\"."
  }
  validation {
    condition     = var.engine_code_source.kind != "inline" || length(var.engine_code_source.value) == 0
    error_message = "engine_code_source.value must be empty when kind=\"inline\"."
  }
}
