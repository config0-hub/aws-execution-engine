"""The deploy smoke script runs every check to its end under ``set -euo pipefail``.

``tests/smoke/test_deploy.sh`` is run against a stub ``aws`` on PATH, so no AWS
account is touched. The stub answers each call the script makes as a healthy
engine would, and a call named in ``STUB_AWS_FAIL`` answers as a missing resource.
"""

import os
from pathlib import Path
import subprocess

import pytest

SMOKE_SCRIPT = Path(__file__).resolve().parents[1] / "smoke" / "test_deploy.sh"

PREFIX = "smoke"
REGION = "us-east-1"

# Answers for a healthy engine. Any call the stub does not know fails loud.
_STUB_AWS = r"""#!/usr/bin/env bash
call="$1 $2"
for arg in "$@"; do
	if [ "$arg" = "${STUB_AWS_FAIL:-}" ]; then
		echo "stub aws: $call $arg not found" >&2
		exit 254
	fi
done
case "$call" in
"lambda get-function" | "s3api head-bucket" | "stepfunctions describe-state-machine")
	exit 0
	;;
"codebuild batch-get-projects")
	shift 2
	while [ $# -gt 0 ]; do
		if [ "$1" = "--names" ]; then
			echo "{\"projects\": [{\"name\": \"$2\"}], \"projectsNotFound\": []}"
			exit 0
		fi
		shift
	done
	echo "stub aws: batch-get-projects without --names" >&2
	exit 2
	;;
"sts get-caller-identity")
	echo "123456789012"
	exit 0
	;;
esac
echo "stub aws: unexpected call: $*" >&2
exit 99
"""


def _run(tmp_path: Path, fail_on: str = "") -> subprocess.CompletedProcess[str]:
    stub = tmp_path / "aws"
    stub.write_text(_STUB_AWS)
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
        "PREFIX": PREFIX,
        "AWS_REGION": REGION,
        "STUB_AWS_FAIL": fail_on,
    }
    return subprocess.run(["bash", str(SMOKE_SCRIPT)], env=env, capture_output=True, text=True, timeout=30)


def test_healthy_engine_runs_every_check_and_exits_zero(tmp_path):
    proc = _run(tmp_path)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert proc.stdout.count("PASS: ") == 7, proc.stdout
    assert "FAIL: " not in proc.stdout
    assert proc.stdout.rstrip().endswith("Results: 7 passed, 0 failed"), proc.stdout


@pytest.mark.parametrize(
    "fail_on, failed_check",
    [
        (f"{PREFIX}-init-job", f"FAIL: Lambda {PREFIX}-init-job not found"),
        (f"{PREFIX}-done", f"FAIL: S3 bucket {PREFIX}-done not found"),
    ],
)
def test_one_failed_check_runs_to_the_end_and_exits_non_zero(tmp_path, fail_on, failed_check):
    proc = _run(tmp_path, fail_on=fail_on)

    assert proc.returncode == 1, proc.stdout + proc.stderr
    assert failed_check in proc.stdout
    assert proc.stdout.count("PASS: ") == 6, proc.stdout
    assert proc.stdout.count("FAIL: ") == 1, proc.stdout
    assert proc.stdout.rstrip().endswith("Results: 6 passed, 1 failed"), proc.stdout
