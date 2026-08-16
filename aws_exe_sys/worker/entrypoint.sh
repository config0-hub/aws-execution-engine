#!/bin/bash
# CodeBuild entrypoint - reads 10 SimplePayload fields from env vars and runs the worker.
# ENGINE_TASK_ROOT is set by the CodeBuild buildspec to wherever engine.zip was unzipped.
# Defaults to /var/task (the Lambda runtime convention) for backward compat.
set -euo pipefail
TASK_ROOT="${ENGINE_TASK_ROOT:-/var/task}"
export PYTHONPATH="$TASK_ROOT:${PYTHONPATH:-}"
cd "$TASK_ROOT"
python3 -m aws_exe_sys.worker.handler
