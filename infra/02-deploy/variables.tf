variable "project_prefix" {
  description = "Resource name prefix (e.g. 'aws-exe-sys'). REQUIRED — no random generation."
  type        = string

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

variable "kms_key_arn" {
  description = "KMS key ARN for worker decryption of SOPS-encrypted credentials"
  type        = string
}

# --- Deploy artifact pointers (produced by scripts/build-zip.sh, uploaded by deploy pipeline) ---

variable "engine_zip_s3_bucket" {
  description = "S3 bucket holding the engine deploy artifacts (engine.zip and sops-age-layer.zip)."
  type        = string
}

variable "engine_zip_s3_key" {
  description = "S3 key for engine.zip (aws_exe_sys/ source + pip deps). Used by init_job + worker Lambdas AND by CodeBuild."
  type        = string
}

variable "sops_age_layer_s3_key" {
  description = "S3 key for sops-age-layer.zip. Attached to worker Lambda only."
  type        = string
}

variable "additional_package_bucket_arns" {
  description = "Additional S3 bucket ARNs from which callers may supply execution packages."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for arn in var.additional_package_bucket_arns : can(regex("^arn:(aws|aws-us-gov|aws-cn):s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", arn))
    ])
    error_message = "Every package bucket value must be an S3 bucket ARN without an object suffix."
  }
}

variable "s3_log_bucket_name" {
  description = "S3 bucket for CodeBuild build logs (written under codebuild/logs/). Empty disables S3 logs."
  type        = string
  default     = ""
}

variable "additional_result_bucket_arns" {
  description = "Additional S3 bucket ARNs to which workers may write terminal results."
  type        = list(string)
  default     = []

  validation {
    condition = alltrue([
      for arn in var.additional_result_bucket_arns : can(regex("^arn:(aws|aws-us-gov|aws-cn):s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", arn))
    ])
    error_message = "Every result bucket value must be an S3 bucket ARN without an object suffix."
  }
}
