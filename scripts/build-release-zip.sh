#!/usr/bin/env bash
# Build the public release artifacts without private CI helpers or registries.
#
# Produces:
#   dist/engine.zip         - Python handlers and public runtime dependencies
#   dist/sops-age-layer.zip - sops, age, and age-keygen under bin/
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUILD_DIR="$ROOT_DIR/.build/release"
DIST_DIR="$ROOT_DIR/dist"
ENGINE_DIR="$BUILD_DIR/engine"
LAYER_DIR="$BUILD_DIR/sops-age-layer"

SOPS_VERSION="${SOPS_VERSION:-3.9.4}"
AGE_VERSION="${AGE_VERSION:-1.2.1}"
SOURCE_DATE_EPOCH="${SOURCE_DATE_EPOCH:-315532800}"

for command in docker curl tar; do
	if ! command -v "$command" >/dev/null 2>&1; then
		echo "ERROR: required command not found: $command" >&2
		exit 1
	fi
done

rm -rf "$BUILD_DIR"
mkdir -p "$ENGINE_DIR" "$LAYER_DIR/bin" "$DIST_DIR"
rm -f "$DIST_DIR/engine.zip" "$DIST_DIR/sops-age-layer.zip"

printf '%s\n' "=== Build engine.zip ==="
docker run --rm \
	-e OUTPUT_UID="$(id -u)" \
	-e OUTPUT_GID="$(id -g)" \
	--entrypoint /bin/bash \
	-v "$ROOT_DIR:/src:ro" \
	-v "$ENGINE_DIR:/out" \
	public.ecr.aws/lambda/python:3.14 \
	-lc 'set -euo pipefail
         python3 -m pip install --disable-pip-version-check --no-cache-dir \
             -r /src/requirements.txt --target /out
         cp -a /src/aws_exe_sys /out/aws_exe_sys
         chown -R "$OUTPUT_UID:$OUTPUT_GID" /out'
find "$ENGINE_DIR" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$ENGINE_DIR" -type f \( -name "*.pyc" -o -name "*.pyo" \) -delete

printf '%s\n' "=== Build sops-age-layer.zip ==="
curl -fsSL \
	"https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
	-o "$LAYER_DIR/bin/sops"
curl -fsSL "https://dl.filippo.io/age/v${AGE_VERSION}?for=linux/amd64" |
	tar xz --strip-components=1 -C "$LAYER_DIR/bin" age/age age/age-keygen
chmod 0755 "$LAYER_DIR/bin/sops" "$LAYER_DIR/bin/age" "$LAYER_DIR/bin/age-keygen"

SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" python3 - "$ENGINE_DIR" "$DIST_DIR/engine.zip" <<'PY'
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import zipfile

source_dir = Path(sys.argv[1])
zip_path = Path(sys.argv[2])
epoch = max(int(os.environ["SOURCE_DATE_EPOCH"]), 315532800)
fixed_time = time.gmtime(epoch)[:6]

with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
    for path in sorted(p for p in source_dir.rglob("*") if p.is_file()):
        info = zipfile.ZipInfo(path.relative_to(source_dir).as_posix(), fixed_time)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = (stat.S_IMODE(path.stat().st_mode) & 0o7777) << 16
        with path.open("rb") as source, archive.open(info, "w") as destination:
            shutil.copyfileobj(source, destination)
PY

SOURCE_DATE_EPOCH="$SOURCE_DATE_EPOCH" python3 - "$LAYER_DIR" "$DIST_DIR/sops-age-layer.zip" <<'PY'
import os
from pathlib import Path
import shutil
import stat
import sys
import time
import zipfile

source_dir = Path(sys.argv[1])
zip_path = Path(sys.argv[2])
epoch = max(int(os.environ["SOURCE_DATE_EPOCH"]), 315532800)
fixed_time = time.gmtime(epoch)[:6]

with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
    for path in sorted(p for p in source_dir.rglob("*") if p.is_file()):
        info = zipfile.ZipInfo(path.relative_to(source_dir).as_posix(), fixed_time)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.create_system = 3
        info.external_attr = (stat.S_IMODE(path.stat().st_mode) & 0o7777) << 16
        with path.open("rb") as source, archive.open(info, "w") as destination:
            shutil.copyfileobj(source, destination)
PY

python3 - "$DIST_DIR/engine.zip" "$DIST_DIR/sops-age-layer.zip" <<'PY'
from pathlib import Path
import sys
import zipfile

engine_path = Path(sys.argv[1])
layer_path = Path(sys.argv[2])

required_engine = {
    "aws_exe_sys/init_job/handler.py",
    "aws_exe_sys/worker/handler.py",
    "boto3/__init__.py",
}
required_layer = {"bin/sops", "bin/age", "bin/age-keygen"}

for path, required in ((engine_path, required_engine), (layer_path, required_layer)):
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member is not None:
            raise SystemExit(f"ERROR: corrupt archive member in {path}: {bad_member}")
        missing = required.difference(archive.namelist())
        if missing:
            raise SystemExit(f"ERROR: {path} is missing: {sorted(missing)}")

if engine_path.stat().st_size > 50 * 1024 * 1024:
    raise SystemExit("ERROR: compressed engine.zip exceeds Lambda's 50 MiB direct-upload limit")
PY

docker run --rm \
	--entrypoint python3 \
	-e PYTHONPATH=/artifacts/engine.zip \
	-v "$DIST_DIR:/artifacts:ro" \
	public.ecr.aws/lambda/python:3.14 \
	-c 'from aws_exe_sys.init_job.handler import handler as init_job_handler; from aws_exe_sys.worker.handler import handler as worker_handler; assert callable(init_job_handler) and callable(worker_handler)'

printf '%s\n' "=== Release artifacts ==="
for artifact in "$DIST_DIR/engine.zip" "$DIST_DIR/sops-age-layer.zip"; do
	printf '  %s (%s bytes)\n' "$artifact" "$(stat -c%s "$artifact")"
done
