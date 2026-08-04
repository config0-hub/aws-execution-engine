#!/usr/bin/env bash
set -euo pipefail

# Fail-loud bucket ownership probe: prints the bucket's ManagedBy tag value,
# or "untagged" when the bucket verifiably has no tag set. ANY other failure
# (expired STS, AccessDenied, network) aborts — an unreadable tag must never
# be interpreted as "not ours".
#
# Usage: bucket_owner.sh <bucket>

BUCKET="${1:?Usage: bucket_owner.sh <bucket>}"

err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT

if owner="$(aws s3api get-bucket-tagging --bucket "$BUCKET" \
  --query "TagSet[?Key=='ManagedBy'].Value" --output text 2>"$err_file")"; then
  if [ -z "$owner" ] || [ "$owner" = "None" ]; then
    echo "untagged"
  else
    echo "$owner"
  fi
  exit 0
fi
if grep -q 'NoSuchTagSet' "$err_file"; then
  echo "untagged"
  exit 0
fi
echo "ERROR: get-bucket-tagging ${BUCKET} failed (NOT an untagged bucket):" >&2
cat "$err_file" >&2
exit 2
