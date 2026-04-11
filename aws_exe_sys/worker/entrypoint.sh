#!/bin/bash
# CodeBuild entrypoint — reads config from environment and runs the worker
export PYTHONPATH="/var/task:${PYTHONPATH}"
cd /var/task
python -m aws_exe_sys.worker.run
