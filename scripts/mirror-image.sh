#!/usr/bin/env bash
# mirror-image.sh — mirror the public GHCR image into the tenant ECR repo.
#
# Pulls ghcr.io/config0-hub/aws-execution-engine:<tag> (anonymous, public),
# logs into the tenant ECR repo created by infra/01-ecr, then tags and pushes
# both <tag> and 'latest'.
#
# Usage: scripts/mirror-image.sh [tag]
#   tag - defaults to the current checkout's `git rev-parse --short=7 HEAD`.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

SOURCE_IMAGE="ghcr.io/config0-hub/aws-execution-engine"
TAG="${1:-$(git -C "$ROOT_DIR" rev-parse --short=7 HEAD)}"

for command in docker aws tofu; do
	if ! command -v "$command" >/dev/null 2>&1; then
		echo "ERROR: required command not found: $command" >&2
		exit 1
	fi
done

: "${AWS_REGION:?required}"

echo "=== Pull ${SOURCE_IMAGE}:${TAG} from GHCR (anonymous) ==="
if ! docker pull "${SOURCE_IMAGE}:${TAG}"; then
	echo "ERROR: ${SOURCE_IMAGE}:${TAG} does not exist on GHCR (or pull failed)" >&2
	exit 1
fi

echo "=== Resolve tenant ECR repository URL from infra/01-ecr ==="
ECR_URL="$(tofu -chdir="$ROOT_DIR/infra/01-ecr" output -raw repository_url)"
if [ -z "$ECR_URL" ]; then
	echo "ERROR: infra/01-ecr produced an empty repository_url output" >&2
	exit 1
fi
ECR_REGISTRY="${ECR_URL%%/*}"

echo "=== Log in to ECR (${ECR_REGISTRY}) ==="
aws ecr get-login-password --region "$AWS_REGION" |
	docker login --username AWS --password-stdin "$ECR_REGISTRY"

echo "=== Tag and push ${ECR_URL}:${TAG} ==="
docker tag "${SOURCE_IMAGE}:${TAG}" "${ECR_URL}:${TAG}"
docker push "${ECR_URL}:${TAG}"

echo "=== Tag and push ${ECR_URL}:latest ==="
docker tag "${SOURCE_IMAGE}:${TAG}" "${ECR_URL}:latest"
docker push "${ECR_URL}:latest"

echo "=== Mirrored ${SOURCE_IMAGE}:${TAG} -> ${ECR_URL}:{${TAG},latest} ==="
