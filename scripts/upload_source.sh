#!/usr/bin/env bash
set -euo pipefail

# Upload a COMPLETE copy of the applied Terraform source for one root to the
# state bucket: s3://<bucket>/source/<root_name>/ — single authoritative copy,
# overwritten on every apply (bucket versioning provides history). SSE-S3.
#
# Also writes manifest.json: root, timestamp, terraform version, and variable
# NAMES only (never values). terraform.tfvars itself is NEVER uploaded.
#
# Usage: upload_source.sh <state_bucket> <root_name> <repo_root> <rel_dir> [rel_dir ...]
#   rel_dir paths are relative to <repo_root>; the first is the root itself,
#   extras are shared module directories the root references.

BUCKET="${1:?Usage: upload_source.sh <state_bucket> <root_name> <repo_root> <rel_dir>...}"
ROOT_NAME="${2:?root_name required}"
REPO_ROOT="${3:?repo_root required}"
shift 3
[ $# -ge 1 ] || {
  echo "ERROR: at least one rel_dir required" >&2
  exit 1
}

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

for rel in "$@"; do
  src="${REPO_ROOT}/${rel}"
  [ -d "$src" ] || {
    echo "ERROR: not a directory: $src" >&2
    exit 1
  }
  mkdir -p "$STAGE/$rel"
  # STRICT ALLOWLIST: only Terraform source and the provider lock file are
  # ever uploaded. Variable VALUES in any form (*.tfvars, *.tfvars.json,
  # *.auto.tfvars*), state, plans, overrides, and arbitrary untracked files
  # must never reach the (versioned, forever-retained) source record.
  (cd "$src" && find . \
    -name .terraform -prune -o \
    -type f \
    \( -name '*.tf' -o -name '*.tf.json' -o -name '.terraform.lock.hcl' \) \
    ! -name '*.tfvars' ! -name '*.tfvars.json' \
    ! -name '*_override.tf' ! -name '*_override.tf.json' ! -name 'override.tf' ! -name 'override.tf.json' \
    ! -name 'backend.tf' \
    -print | while IFS= read -r f; do
    mkdir -p "$STAGE/$rel/$(dirname "$f")"
    cp "$f" "$STAGE/$rel/$f"
  done)
  # Regression guard: fail hard if anything value-bearing slipped into the stage.
  if find "$STAGE/$rel" -type f \( -name '*tfvars*' -o -name '*.tfstate*' -o -name '*.tfplan' \) | grep -q .; then
    echo "ERROR: variable-value or state file staged for upload — aborting" >&2
    exit 1
  fi
done

# Manifest: variable names come from the root's terraform.tfvars keys.
ROOT_REL="$1"
TFVARS="${REPO_ROOT}/${ROOT_REL}/terraform.tfvars"
VAR_NAMES="[]"
if [ -f "$TFVARS" ]; then
  VAR_NAMES="$(sed -n 's/^\([a-zA-Z0-9_-]*\)[[:space:]]*=.*/\1/p' "$TFVARS" | jq -R . | jq -sc .)"
fi
TF_VERSION="$(terraform version -json | jq -r .terraform_version)"
jq -n \
  --arg root "$ROOT_NAME" \
  --arg timestamp "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  --arg terraform_version "$TF_VERSION" \
  --argjson variable_names "$VAR_NAMES" \
  '{root: $root, timestamp: $timestamp, terraform_version: $terraform_version, variable_names: $variable_names}' \
  >"$STAGE/manifest.json"

aws s3 sync "$STAGE" "s3://${BUCKET}/source/${ROOT_NAME}/" --delete --sse AES256 --only-show-errors
echo "uploaded source copy: s3://${BUCKET}/source/${ROOT_NAME}/"
