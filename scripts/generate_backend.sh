#!/usr/bin/env bash
set -euo pipefail

if [ $# -ne 3 ]; then
  echo "Usage: $0 <bucket_name> <stage_key> <region>" >&2
  exit 1
fi

BUCKET_NAME="$1"
STAGE_KEY="$2"
REGION="$3"

cat > backend.tf <<EOF
terraform {
  backend "s3" {
    bucket = "${BUCKET_NAME}"
    key    = "${STAGE_KEY}/terraform.tfstate"
    region = "${REGION}"
  }
}
EOF

echo "Generated backend.tf (bucket=${BUCKET_NAME}, key=${STAGE_KEY}/terraform.tfstate, region=${REGION})"
