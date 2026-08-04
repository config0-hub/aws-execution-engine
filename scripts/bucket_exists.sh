#!/usr/bin/env bash
set -euo pipefail

# Fail-loud bucket existence probe.
#   exit 0  -> bucket exists (and we can reach it)
#   exit 1  -> bucket genuinely does not exist (404)
#   exit 2  -> ANY other failure (expired STS, AccessDenied, network, 301) —
#              callers must abort, never treat this as "missing".
#
# Usage: bucket_exists.sh <bucket>

BUCKET="${1:?Usage: bucket_exists.sh <bucket>}"

err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT

if aws s3api head-bucket --bucket "$BUCKET" 2>"$err_file"; then
  exit 0
fi
if grep -Eq '404|Not Found|NoSuchBucket' "$err_file"; then
  exit 1
fi
echo "ERROR: head-bucket ${BUCKET} failed (NOT a missing bucket):" >&2
cat "$err_file" >&2
exit 2
