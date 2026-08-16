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
}
