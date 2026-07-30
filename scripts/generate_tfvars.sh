#!/usr/bin/env bash
# generate_tfvars.sh — write terraform.tfvars for infra/02-deploy from env vars.
# All required env vars fail loud if unset.
set -euo pipefail

: "${ENGINE_ZIP_S3_BUCKET:?required}"
: "${ENGINE_ZIP_S3_KEY:?required}"
: "${SOPS_AGE_LAYER_S3_KEY:?required}"
: "${KMS_KEY_ARN:?required}"
: "${AWS_REGION:?required}"
: "${PROJECT_PREFIX:?required}"

ADDITIONAL_PACKAGE_BUCKET_ARNS_JSON="${ADDITIONAL_PACKAGE_BUCKET_ARNS_JSON:-[]}"
ADDITIONAL_RESULT_BUCKET_ARNS_JSON="${ADDITIONAL_RESULT_BUCKET_ARNS_JSON:-[]}"

normalize_bucket_arns() {
	python3 - "$1" <<'PY'
import json
import re
import sys

value = json.loads(sys.argv[1])
pattern = re.compile(r"^arn:(aws|aws-us-gov|aws-cn):s3:::[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
if not isinstance(value, list) or not all(isinstance(arn, str) and pattern.fullmatch(arn) for arn in value):
    raise SystemExit("bucket ARN input must be a JSON array of S3 bucket ARNs")
print(json.dumps(value, separators=(",", ":")))
PY
}

PACKAGE_BUCKET_ARNS="$(normalize_bucket_arns "$ADDITIONAL_PACKAGE_BUCKET_ARNS_JSON")"
RESULT_BUCKET_ARNS="$(normalize_bucket_arns "$ADDITIONAL_RESULT_BUCKET_ARNS_JSON")"

cat >terraform.tfvars <<EOF
project_prefix                 = "${PROJECT_PREFIX}"
kms_key_arn                    = "${KMS_KEY_ARN}"
engine_zip_s3_bucket           = "${ENGINE_ZIP_S3_BUCKET}"
engine_zip_s3_key              = "${ENGINE_ZIP_S3_KEY}"
sops_age_layer_s3_key          = "${SOPS_AGE_LAYER_S3_KEY}"
additional_package_bucket_arns = ${PACKAGE_BUCKET_ARNS}
additional_result_bucket_arns  = ${RESULT_BUCKET_ARNS}
EOF

echo "Generated terraform.tfvars (project_prefix=${PROJECT_PREFIX}, region=${AWS_REGION})"
