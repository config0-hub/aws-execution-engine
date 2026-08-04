#!/usr/bin/env bash
set -euo pipefail

# Install-time configuration in SSM Parameter Store (SecureString).
# All config lives under /iac-ci/install/<project>/<key>.
#
# Usage:
#   ssm_config.sh set <key> <value>         # non-secret values
#   ssm_config.sh set-stdin <key>           # secret values (read from stdin)
#   ssm_config.sh get <key>                 # fails if unset
#   ssm_config.sh get-or <key> <default>    # default ONLY on ParameterNotFound
#   ssm_config.sh delete-all                # removes the whole namespace
#
# Project namespace defaults to "engine"; override with SSM_CONFIG_PROJECT.
#
# Fail-loud contract: only a genuine ParameterNotFound falls back to the
# default. Expired credentials, AccessDenied, throttling, and network errors
# always abort — silently defaulting on those could flip ownership or naming
# decisions mid-install.

PROJECT="${SSM_CONFIG_PROJECT:-engine}"
PREFIX="/iac-ci/install/${PROJECT}"

usage() {
  grep '^#   ' "$0" | sed 's/^#   //' >&2
  exit 1
}

put_param() { # <key> <value>
  aws ssm put-parameter --name "${PREFIX}/$1" --value "$2" \
    --type SecureString --overwrite >/dev/null
  echo "set ${PREFIX}/$1"
}

cmd="${1:-}"
case "$cmd" in
set)
  key="${2:?set requires <key> <value>}"
  value="${3:?set requires <key> <value>}"
  put_param "$key" "$value"
  ;;
set-stdin)
  key="${2:?set-stdin requires <key>}"
  value="$(cat)"
  [ -n "$value" ] || {
    echo "ERROR: empty value on stdin" >&2
    exit 1
  }
  put_param "$key" "$value"
  ;;
get)
  key="${2:?get requires <key>}"
  aws ssm get-parameter --name "${PREFIX}/${key}" --with-decryption \
    --query 'Parameter.Value' --output text
  ;;
get-or)
  key="${2:?get-or requires <key> <default>}"
  default="${3?get-or requires <key> <default>}"
  err_file="$(mktemp)"
  trap 'rm -f "$err_file"' EXIT
  if value="$(aws ssm get-parameter --name "${PREFIX}/${key}" --with-decryption \
    --query 'Parameter.Value' --output text 2>"$err_file")"; then
    echo "$value"
  elif grep -q 'ParameterNotFound' "$err_file"; then
    echo "$default"
  else
    echo "ERROR: SSM get-parameter ${PREFIX}/${key} failed (NOT a missing parameter):" >&2
    cat "$err_file" >&2
    exit 1
  fi
  ;;
delete-all)
  names="$(aws ssm get-parameters-by-path --path "$PREFIX" --recursive \
    --query 'Parameters[].Name' --output text)"
  if [ -z "$names" ] || [ "$names" = "None" ]; then
    echo "no parameters under ${PREFIX}"
    exit 0
  fi
  for name in $names; do
    aws ssm delete-parameter --name "$name"
    echo "deleted $name"
  done
  ;;
*)
  usage
  ;;
esac
