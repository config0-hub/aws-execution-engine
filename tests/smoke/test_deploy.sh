#!/usr/bin/env bash
set -euo pipefail

PASSED=0
FAILED=0

pass() { echo "PASS: $1"; ((PASSED++)); }
fail() { echo "FAIL: $1"; ((FAILED++)); }

# Validate required env vars
for var in PREFIX AWS_REGION ORDERS_TABLE ORDER_EVENTS_TABLE LOCKS_TABLE; do
  if [ -z "${!var:-}" ]; then
    echo "ERROR: $var is not set" >&2
    exit 1
  fi
done

# Derive deterministic bucket and resource names from PREFIX
INTERNAL_BUCKET="${PREFIX}-internal"
DONE_BUCKET="${PREFIX}-done"

# 1. Lambda functions exist (deterministic names from prefix)
for SUFFIX in init-job orchestrator watchdog-check worker ssm-config; do
  FUNC="${PREFIX}-${SUFFIX}"
  if aws lambda get-function --function-name "$FUNC" --region "$AWS_REGION" >/dev/null 2>&1; then
    pass "Lambda $FUNC exists"
  else
    fail "Lambda $FUNC not found"
  fi
done

# 2. DynamoDB tables exist
for TABLE_VAR in ORDERS_TABLE ORDER_EVENTS_TABLE LOCKS_TABLE; do
  TABLE="${!TABLE_VAR}"
  if aws dynamodb describe-table --table-name "$TABLE" --region "$AWS_REGION" >/dev/null 2>&1; then
    pass "DynamoDB table $TABLE exists"
  else
    fail "DynamoDB table $TABLE not found"
  fi
done

# 3. S3 buckets exist (deterministic names)
for BUCKET in "$INTERNAL_BUCKET" "$DONE_BUCKET"; do
  if aws s3api head-bucket --bucket "$BUCKET" --region "$AWS_REGION" 2>/dev/null; then
    pass "S3 bucket $BUCKET exists"
  else
    fail "S3 bucket $BUCKET not found"
  fi
done

# 4. Step Function exists
if aws stepfunctions list-state-machines --region "$AWS_REGION" 2>/dev/null | grep -q "${PREFIX}-watchdog"; then
  pass "Step Function ${PREFIX}-watchdog exists"
else
  fail "Step Function ${PREFIX}-watchdog not found"
fi

# 5. S3 notification configured on internal bucket
NOTIF=$(aws s3api get-bucket-notification-configuration --bucket "$INTERNAL_BUCKET" --region "$AWS_REGION" 2>/dev/null || echo "{}")
if echo "$NOTIF" | grep -q "LambdaFunctionConfigurations"; then
  pass "S3 notification configured on $INTERNAL_BUCKET"
else
  fail "S3 notification not configured on $INTERNAL_BUCKET"
fi

# 6. CodeBuild project exists
if aws codebuild batch-get-projects --names "${PREFIX}-worker" --region "$AWS_REGION" 2>/dev/null | grep -q "${PREFIX}-worker"; then
  pass "CodeBuild project ${PREFIX}-worker exists"
else
  fail "CodeBuild project ${PREFIX}-worker not found"
fi

echo ""
echo "Results: $PASSED passed, $FAILED failed"

if [ "$FAILED" -gt 0 ]; then
  exit 1
fi
