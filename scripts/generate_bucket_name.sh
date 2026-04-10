#!/usr/bin/env bash
set -euo pipefail

if [ -n "${AWS_EXE_SYS_STATE_BUCKET:-}" ]; then
  echo "$AWS_EXE_SYS_STATE_BUCKET"
else
  echo "aws-exe-sys-state-$(head -c 3 /dev/urandom | xxd -p | cut -c1-5)"
fi
