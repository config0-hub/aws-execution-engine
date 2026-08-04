#!/usr/bin/env bash
set -euo pipefail

# Generates a backend.tf file in the specified directory for S3 remote state.
#
# Usage: generate_backend.sh <bucket_name> <state_key> <region> <target_dir> [lock_table]
#
# Example:
#   ./scripts/generate_backend.sh iac-ci-state-099623381343 deploy us-east-1 infra/deploy/ iac-ci-tf-locks

BUCKET_NAME="${1:?Usage: generate_backend.sh <bucket_name> <state_key> <region> <target_dir> [lock_table]}"
STATE_KEY="${2:?Usage: generate_backend.sh <bucket_name> <state_key> <region> <target_dir> [lock_table]}"
REGION="${3:?Usage: generate_backend.sh <bucket_name> <state_key> <region> <target_dir> [lock_table]}"
TARGET_DIR="${4:?Usage: generate_backend.sh <bucket_name> <state_key> <region> <target_dir> [lock_table]}"
LOCK_TABLE="${5:-}"

LOCK_LINE=""
if [ -n "$LOCK_TABLE" ]; then
  LOCK_LINE="    dynamodb_table = \"${LOCK_TABLE}\""
fi

cat >"${TARGET_DIR}/backend.tf" <<EOF
terraform {
  backend "s3" {
    bucket = "${BUCKET_NAME}"
    key    = "${STATE_KEY}/terraform.tfstate"
    region = "${REGION}"
${LOCK_LINE}
  }
}
EOF

echo "Generated ${TARGET_DIR}/backend.tf (bucket=${BUCKET_NAME}, key=${STATE_KEY}/terraform.tfstate)"
