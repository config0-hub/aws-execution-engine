#!/usr/bin/env bash
set -euo pipefail

# Fail-loud DynamoDB table existence probe.
#   exit 0 -> table exists
#   exit 1 -> table genuinely does not exist (ResourceNotFoundException)
#   exit 2 -> ANY other failure (expired STS, AccessDenied, throttling) —
#             callers must abort, never treat this as "missing".
#
# Usage: table_exists.sh <table>

TABLE="${1:?Usage: table_exists.sh <table>}"

err_file="$(mktemp)"
trap 'rm -f "$err_file"' EXIT

if aws dynamodb describe-table --table-name "$TABLE" >/dev/null 2>"$err_file"; then
  exit 0
fi
if grep -q 'ResourceNotFoundException' "$err_file"; then
  exit 1
fi
echo "ERROR: describe-table ${TABLE} failed (NOT a missing table):" >&2
cat "$err_file" >&2
exit 2
