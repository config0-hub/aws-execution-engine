#!/usr/bin/env bash
# Internal compatibility build for the existing private release system.
# Public/standalone builds MUST use scripts/build-release-zip.sh instead.
# build-zip.sh — Package aws-execution-engine as a Lambda zip + 2 Lambda Layers.
#
# Produces (in dist/):
#   engine.zip         — aws_exe_sys/ source + pip-installed runtime deps.
#                        Used by init_job + worker Lambdas and extracted by
#                        CodeBuild for the >15min worker path.
#   tofu-layer.zip     — bin/tofu               (worker Lambda only)
#   sops-age-layer.zip — bin/{sops,age,age-keygen} (worker Lambda only)
#
# Lambda Layer extracts to /opt; Lambda PATH includes /opt/bin, so bin/<tool>
# in the zip resolves to /opt/bin/<tool> at runtime.
#
# Remote-daemon-safe: all source is transferred via docker cp / tar pipe, never
# via a host -v bind mount. Works with DOCKER_HOST=tcp://dev101:2375.
#
# Versions pinned below. Bump deliberately.
set -euo pipefail

SVC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

TOFU_VERSION="1.8.8"
SOPS_VERSION="3.9.4"
AGE_VERSION="1.2.1"

FORGEJO_HOST="${FORGEJO_HOST:-forgejo:3000}"

resolve_ci_lib() {
    local service_dir="$1"

    # Branch 1: bundled alongside the script (Woodpecker publish path — sync_aws_execution_engine
    # copies tools/ci-lib into the Forgejo repo root before the pipeline runs).
    if [ -d "$service_dir/tools/ci-lib" ]; then
        printf '%s\n' "$service_dir/tools/ci-lib"
        return 0
    fi

    # Branch 2: host code-repo working tree (build-onboarding-zips/run.sh invokes this script
    # directly against the monorepo checkout). The engine lives 3 dirs under the repo root
    # (src/delegated-execution-components/aws-execution-engine), so $service_dir/../../.. is the
    # code-repo root. Check the ops/ symlink first, then the sibling repo as a fallback.
    local code_root
    code_root="$(cd "$service_dir/../../.." && pwd)"

    local candidate
    for candidate in \
        "$code_root/ops/tools/ci-lib" \
        "$code_root/../jiffy-rewrite-2026-ops/tools/ci-lib"
    do
        if [ -d "$candidate" ]; then
            printf '%s\n' "$(cd "$candidate" && pwd)"
            return 0
        fi
    done

    echo "ERROR: unable to locate tools/ci-lib from $service_dir" >&2
    exit 1
}

CI_LIB="$(resolve_ci_lib "$SVC_DIR")"
# shellcheck source=tools/ci-lib/lambda-zip-lib.sh
source "$CI_LIB/lambda-zip-lib.sh"

lambda_zip_prepare_workspace "$SVC_DIR" "engine.zip"
lambda_zip_require_forgejo_token

# ── engine.zip ─────────────────────────────────────────────────────────────────

CONTAINER="aws-execution-engine-zip-$$"

cleanup() {
    docker rm -f "$CONTAINER" 2>/dev/null || true
}
trap cleanup EXIT

echo "=== Start named build container ==="
lambda_zip_start_container "$CONTAINER" >/dev/null

echo "=== Copy ci-lib into container ==="
lambda_zip_copy_ci_lib "$CI_LIB" "$CONTAINER"

echo "=== Install Python deps inside container ==="
lambda_zip_install_in_container "$CONTAINER" /build/python \
    'boto3>=1.34.0'

echo "=== Include monorepo SHA provenance ==="
# .monorepo-sha is written by sync-forgejo.sh commit_and_push() before the
# Woodpecker build runs. Copy into the container so it ends up in the zip root.
if [ -f "$SVC_DIR/.monorepo-sha" ]; then
    docker cp "$SVC_DIR/.monorepo-sha" "$CONTAINER:/build/python/.monorepo-sha"
    echo "  Included .monorepo-sha: $(cat "$SVC_DIR/.monorepo-sha")"
else
    echo "  WARNING: .monorepo-sha not found at $SVC_DIR — skipping (local build?)"
fi

# stage_layer_provenance <layer_build_dir>
# The two Layer zips hold nothing but bin/, so a consumer could not tell which
# source revision produced them. Drop the same .monorepo-sha marker at the zip
# root that engine.zip carries, so the onboarding bundle's provenance manifest
# can read all three the same way.
stage_layer_provenance() {
    if [ -f "$SVC_DIR/.monorepo-sha" ]; then
        cp "$SVC_DIR/.monorepo-sha" "$1/.monorepo-sha"
    fi
}

echo "=== Copy source into container ==="
lambda_zip_copy_tree_to_container "$SVC_DIR/aws_exe_sys" "$CONTAINER" /build/python

echo "=== Zip inside container ==="
lambda_zip_create_archive_in_container "$CONTAINER" /build/python "/build/engine.zip"

echo "=== Extract zip from container ==="
lambda_zip_copy_from_container "$CONTAINER" /build/engine.zip "$LAMBDA_ZIP_PATH"

echo "=== Verify engine.zip ==="
lambda_zip_verify_archive "$LAMBDA_ZIP_PATH" 52428800 1048576

# ── tofu-layer.zip ─────────────────────────────────────────────────────────────
# curl + host-side zip: no Docker volume mounts, safe with any DOCKER_HOST.

TOFU_BUILD_DIR="$LAMBDA_ZIP_BUILD_DIR/tofu-layer/bin"
mkdir -p "$TOFU_BUILD_DIR"

echo "=== tofu-layer.zip ==="
curl -fsSL "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" \
    -o /tmp/tofu.zip
unzip -j /tmp/tofu.zip tofu -d "$TOFU_BUILD_DIR"
chmod +x "$TOFU_BUILD_DIR/tofu"
rm /tmp/tofu.zip
stage_layer_provenance "$LAMBDA_ZIP_BUILD_DIR/tofu-layer"
( cd "$LAMBDA_ZIP_BUILD_DIR/tofu-layer" && zip -qr "$LAMBDA_ZIP_DIST_DIR/tofu-layer.zip" . )

# ── sops-age-layer.zip ─────────────────────────────────────────────────────────

SOPS_BUILD_DIR="$LAMBDA_ZIP_BUILD_DIR/sops-age-layer/bin"
mkdir -p "$SOPS_BUILD_DIR"

echo "=== sops-age-layer.zip ==="
curl -fsSL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
    -o "$SOPS_BUILD_DIR/sops"
chmod +x "$SOPS_BUILD_DIR/sops"

curl -fsSL "https://dl.filippo.io/age/v${AGE_VERSION}?for=linux/amd64" \
    | tar xz --strip-components=1 -C "$SOPS_BUILD_DIR" age/age age/age-keygen
chmod +x "$SOPS_BUILD_DIR/age" "$SOPS_BUILD_DIR/age-keygen"

stage_layer_provenance "$LAMBDA_ZIP_BUILD_DIR/sops-age-layer"
( cd "$LAMBDA_ZIP_BUILD_DIR/sops-age-layer" && zip -qr "$LAMBDA_ZIP_DIST_DIR/sops-age-layer.zip" . )

# ── Summary ────────────────────────────────────────────────────────────────────

echo "=== Verify all artifacts ==="
for f in engine.zip tofu-layer.zip sops-age-layer.zip; do
    size=$(stat -c%s "$LAMBDA_ZIP_DIST_DIR/$f")
    echo "  $f: $((size / 1024 / 1024)) MB"
done

echo "=== Done: $LAMBDA_ZIP_DIST_DIR ==="
