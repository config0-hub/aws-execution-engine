#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 4 ]; then
  echo "Usage: $0 <ecr_repo_url> <image_tag> <region> <project_prefix>" >&2
  exit 1
fi

ECR_REPO_URL="$1"
IMAGE_TAG="$2"
REGION="$3"
PROJECT_PREFIX="$4"

cat > terraform.tfvars <<EOF
image_tag      = "${IMAGE_TAG}"
ecr_repo       = "${ECR_REPO_URL}"
aws_region     = "${REGION}"
project_prefix = "${PROJECT_PREFIX}"
EOF

echo "Generated terraform.tfvars (image_tag=${IMAGE_TAG}, region=${REGION}, project_prefix=${PROJECT_PREFIX})"
