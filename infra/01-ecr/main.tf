terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }

  # Local state only — mirrors infra/00-bootstrap's convention for this small
  # standalone root.
}

provider "aws" {
  region = var.aws_region
}

resource "aws_ecr_repository" "this" {
  name = "aws-execution-engine"

  # Allow destroy even when the repo holds images (a plain destroy of a
  # non-empty repo fails with RepositoryNotEmptyException). Required by the
  # one-time `task ecr:recover-destroy` recovery flow.
  force_delete = true
}
