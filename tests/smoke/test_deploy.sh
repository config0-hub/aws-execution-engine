#!/usr/bin/env bash
set -euo pipefail

PASSED=0
FAILED=0

pass() {
	echo "PASS: $1"
	((PASSED++))
}
fail() {
	echo "FAIL: $1"
	((FAILED++))
}

for var in PREFIX AWS_REGION; do
	if [ -z "${!var:-}" ]; then
		echo "ERROR: $var is not set" >&2
		exit 1
	fi
done

INTERNAL_BUCKET="${PREFIX}-internal"
DONE_BUCKET="${PREFIX}-done"

for SUFFIX in init-job worker finalizer; do
	FUNC="${PREFIX}-${SUFFIX}"
	if aws lambda get-function --function-name "$FUNC" --region "$AWS_REGION" >/dev/null 2>&1; then
		pass "Lambda $FUNC exists"
	else
		fail "Lambda $FUNC not found"
	fi
done

for BUCKET in "$INTERNAL_BUCKET" "$DONE_BUCKET"; do
	if aws s3api head-bucket --bucket "$BUCKET" --region "$AWS_REGION" 2>/dev/null; then
		pass "S3 bucket $BUCKET exists"
	else
		fail "S3 bucket $BUCKET not found"
	fi
done

if aws codebuild batch-get-projects --names "${PREFIX}-worker" --region "$AWS_REGION" 2>/dev/null | grep -q "${PREFIX}-worker"; then
	pass "CodeBuild project ${PREFIX}-worker exists"
else
	fail "CodeBuild project ${PREFIX}-worker not found"
fi

if aws stepfunctions describe-state-machine --state-machine-arn "arn:aws:states:${AWS_REGION}:$(aws sts get-caller-identity --query Account --output text):stateMachine:${PREFIX}-codebuild" --region "$AWS_REGION" >/dev/null 2>&1; then
	pass "Step Functions state machine ${PREFIX}-codebuild exists"
else
	fail "Step Functions state machine ${PREFIX}-codebuild not found"
fi

echo ""
echo "Results: $PASSED passed, $FAILED failed"

if [ "$FAILED" -gt 0 ]; then
	exit 1
fi
