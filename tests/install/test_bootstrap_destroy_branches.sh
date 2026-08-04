#!/usr/bin/env bash
set -euo pipefail

# Mocked branch tests for `just bootstrap-destroy` recovery paths. No AWS, no
# terraform: fake `aws` and `terraform` binaries on PATH simulate the account.
# State lives in $MOCK_STATE so delete-table takes effect for later probes.
#
# Scenarios:
#   A) bucket missing + engine-owned lock table survives, no local state
#      -> table deleted directly, postconditions pass (exit 0)
#   B) bucket missing + engine-owned lock table that CANNOT be deleted
#      -> table postcondition must FAIL (nonzero) — surviving table is an error
#   C) bucket missing + table probe indeterminate (expired STS) -> abort (rc 2)
#   D) bucket missing + FOREIGN-owned table -> left in place, exit 0
#
# Run: tests/install/test_bootstrap_destroy_branches.sh

REPO="$(cd "$(dirname "$0")/../.." && pwd)"
MOCK_DIR="$(mktemp -d)"
export MOCK_STATE="$MOCK_DIR/state"
trap 'rm -rf "$MOCK_DIR"' EXIT

cat >"$MOCK_DIR/aws" <<'MOCK'
#!/usr/bin/env bash
# Behavior driven by env: MOCK_TABLE=present|absent|expired|foreign|undeletable
case "$*" in
*"sts get-caller-identity"*) echo "123456789012"; exit 0 ;;
*"ssm get-parameter"*) echo "An error occurred (ParameterNotFound) when calling the GetParameter operation" >&2; exit 254 ;;
*"s3api head-bucket"*) echo "An error occurred (404) when calling the HeadBucket operation: Not Found" >&2; exit 254 ;;
*"dynamodb describe-table"*)
  if [ -f "$MOCK_STATE/table-deleted" ] || [ "${MOCK_TABLE}" = absent ]; then
    echo "An error occurred (ResourceNotFoundException) when calling the DescribeTable operation" >&2; exit 254
  elif [ "${MOCK_TABLE}" = expired ]; then
    echo "An error occurred (ExpiredToken) when calling the DescribeTable operation" >&2; exit 254
  fi
  exit 0 ;;
*"dynamodb list-tags-of-resource"*)
  if [ "${MOCK_TABLE}" = foreign ]; then echo "someone-else"; else echo "engine-00-bootstrap"; fi
  exit 0 ;;
*"dynamodb delete-table"*)
  if [ "${MOCK_TABLE}" = undeletable ]; then exit 0; fi   # pretends to work, table survives
  mkdir -p "$MOCK_STATE"; touch "$MOCK_STATE/table-deleted"; exit 0 ;;
*"dynamodb wait table-not-exists"*) exit 0 ;;
*) exit 0 ;;
esac
MOCK
cat >"$MOCK_DIR/terraform" <<'MOCK'
#!/usr/bin/env bash
exit 0
MOCK
chmod +x "$MOCK_DIR/aws" "$MOCK_DIR/terraform"
export PATH="$MOCK_DIR:$PATH"

FAILURES=0
run_case() { # <desc> <want_rc> <MOCK_TABLE value>
  local desc="$1" want="$2" table="$3" got=0
  rm -rf "$MOCK_STATE"
  rm -f "$REPO/infra/00-bootstrap/terraform.tfstate"
  MOCK_TABLE="$table" just --justfile "$REPO/justfile" --working-directory "$REPO" bootstrap-destroy >/dev/null 2>&1 || got=$?
  if [ "$got" = "$want" ]; then
    echo "PASS ${desc} (rc=${got})"
  else
    echo "FAIL ${desc}: want rc=${want}, got rc=${got}"
    FAILURES=$((FAILURES + 1))
  fi
}

run_case "A: stranded engine-owned table deleted, postconditions pass" 0 present
run_case "B: surviving table after delete must fail postcondition" 1 undeletable
run_case "C: indeterminate table probe aborts" 2 expired
run_case "D: foreign-owned table left in place, success" 0 foreign

if [ "$FAILURES" -gt 0 ]; then
  echo "bootstrap-destroy branch tests: ${FAILURES} failure(s)"
  exit 1
fi
echo "bootstrap-destroy branch tests: all passed"
