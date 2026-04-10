# CLAUDE.md

## Project Overview

aws-execution-engine is a generic, event-driven continuous delivery system for infrastructure-as-code and arbitrary command execution. It is AWS-native (Lambda, DynamoDB, S3, CodeBuild, SSM, Step Functions, API Gateway, ECR) and uses a single Docker image with multiple entrypoints for all Lambda functions and CodeBuild workers.

The system processes jobs containing multiple orders, queues them in DynamoDB, and executes them via Lambda, CodeBuild, or SSM Run Command with full dependency resolution, cross-account credential management via SOPS, and PR comment tracking via VCS.

## Architecture

Three execution paths, two entry points:

- **Part 1a (init_job):** `init_job` Lambda receives job parameters, validates orders, repackages with SOPS-encrypted credentials, uploads to S3, inserts into DynamoDB, and triggers the orchestrator via S3 event. Handles Lambda and CodeBuild orders. PR comments are disabled — the caller owns the PR comment lifecycle.
- **Part 1b (ssm_config):** `ssm_config` Lambda is a separate entry point for SSM orders. Packages code (no SOPS), fetches credentials, uploads to S3, inserts into the shared DynamoDB orders table, and triggers the orchestrator. Handles SSM Run Command orders.
- **Part 2 (execute_orders):** `orchestrator` Lambda is triggered by S3 events (callbacks), acquires a per-run_id lock, evaluates dependency graphs, dispatches ready orders in parallel to Lambda, CodeBuild, or SSM, and finalizes when all orders complete. Workers callback via presigned S3 PUT URLs. A Step Function watchdog per order handles timeout safety.

## Repo Structure

```
aws-execution-engine/
├── src/
│   ├── common/          # shared libraries (dynamodb, s3, sops, vcs, trace, flow, models, code_source)
│   ├── init_job/        # Part 1a: init_job Lambda (Lambda/CodeBuild orders)
│   ├── ssm_config/      # Part 1b: ssm_config Lambda (SSM orders)
│   ├── orchestrator/    # Part 2: execute_orders Lambda
│   ├── watchdog_check/  # Step Function timeout watchdog Lambda
│   └── worker/          # dual-purpose: Lambda handler + CodeBuild entrypoint
├── docker/
│   └── Dockerfile       # single image, all functions
├── infra/
│   ├── 00-bootstrap/    # state bucket (local TF state)
│   ├── 01-ecr/          # ECR repo
│   └── 02-deploy/       # all AWS resources
├── scripts/             # generate backend.tf + terraform.tfvars
├── tests/
│   ├── smoke/
│   ├── unit/
│   └── integration/
└── .github/workflows/   # deploy pipeline
```

## Environment Variables

All env vars use the `AWS_EXE_SYS_` prefix:

- `AWS_EXE_SYS_ORDERS_TABLE` — DynamoDB orders table name
- `AWS_EXE_SYS_ORDER_EVENTS_TABLE` — DynamoDB order events table name
- `AWS_EXE_SYS_LOCKS_TABLE` — DynamoDB orchestrator locks table name
- `AWS_EXE_SYS_INTERNAL_BUCKET` — Internal S3 bucket (exec.zip + callbacks)
- `AWS_EXE_SYS_DONE_BUCKET` — Done S3 bucket (finalization)
- `AWS_EXE_SYS_WORKER_LAMBDA` — Worker Lambda function name
- `AWS_EXE_SYS_CODEBUILD_PROJECT` — CodeBuild project name
- `AWS_EXE_SYS_WATCHDOG_SFN` — Watchdog Step Function ARN
- `AWS_EXE_SYS_EVENTS_DIR` — Worker events directory (set at runtime)

## Key Technical Decisions

- **Single Docker image:** Based on `public.ecr.aws/lambda/python:3.14`. All Lambda functions use the same image with different `image_config.command` overrides. CodeBuild uses the default CMD (`entrypoint.sh`).
- **Three execution targets:** Orders specify `execution_target` ("lambda", "codebuild", or "ssm"). Lambda for short tasks (<15 min), CodeBuild for long-running containerized tasks, SSM Run Command for executing on existing EC2 instances.
- **SSM config provider:** Separate entry point (`POST /ssm`) for server configuration. Packages code (e.g. Ansible playbooks, shell scripts), uploads to S3, and dispatches via SSM SendCommand to EC2 instances. Tool-agnostic — the SSM document is a general command runner.
- **SOPS key persistence:** Age keypair generated at repackage time. Private key stored in SSM Parameter Store (advanced tier with expiration policy). Workers retrieve via `SOPS_KEY_SSM_PATH`. Finalize cleans up SSM parameters.
- **Cross-account execution:** No IAM role assumption at worker level. Target account credentials are fetched from SSM/Secrets Manager during repackage, encrypted into SOPS bundle, and unpacked as env vars at execution time.
- **Git clone strategy:** HTTPS + token primary, SSH fallback. Credentials resolved from SSM paths once per job, shared across all clones.
- **Worker callbacks:** Presigned S3 PUT URLs baked into SOPS bundle. Workers write `result.json` with status + logs. No DynamoDB write permissions needed on worker.
- **Orchestrator is event-driven:** Triggered by S3 `ObjectCreated` events on `tmp/callbacks/runs/` prefix. No polling, no chained Lambda loops.
- **Timeout safety:** Per-order Step Function watchdog polls S3 every 60s and writes `timed_out` result.json if worker is unresponsive.
- **Event data model:** `put_event()` separates metadata (flow_id, run_id at top level via `extra_fields`) from subprocess payload (nested under `data` key).
- **VCS abstraction:** ABC base class in `src/common/vcs/base.py`. GitHub implementation first, designed for Bitbucket/GitLab extension.

## DynamoDB Tables

- **orders** — PK: `<run_id>:<order_num>`, GSI: `run_id-order_num-index` (PK: `run_id`, SK: `order_num`), TTL: 1 day
- **order_events** — PK: `trace_id`, SK: `order_name:epoch`, GSI PK: `order_name`, GSI SK: `epoch`, TTL: 90 days. Job-level events use `_job` as order_name.
- **orchestrator_locks** — PK: `lock:<run_id>`, TTL: max_timeout

## S3 Buckets

- **Internal** (1 day lifecycle): `tmp/exec/<run_id>/<order_num>/exec.zip` and `tmp/callbacks/runs/<run_id>/<order_num>/result.json`
- **Done** (1 day lifecycle, separate bucket): `<run_id>/done`
- **State** (permanent): TF state files + teardown archive

## Tracing

- Format: `<trace_id>:<epoch_time>`
- Same `trace_id` across entire run, new epoch per leg
- `flow_id`: `<username>:<trace_id>-<flow_label>`

## Build and Deploy

Single GitHub Actions workflow with 6 steps:
1. Bootstrap state bucket (local TF state)
2. Create ECR repo (state in bucket)
3. Build + push Docker image to ECR
4. Deploy system via Terraform (state in bucket)
5. Smoke tests
6. Archive TF code + tfvars to state bucket for teardown

Inputs: `aws_region` (required), `state_bucket_name` (optional, auto-generated)

## Code Style

- Python, primary language for all Lambda functions and common libraries
- Use type hints
- Use dataclasses or Pydantic for models
- Keep functions small and testable
- No CloudWatch logging dependency — capture stdout/stderr directly via subprocess
- Use `os.system` or `subprocess.Popen` for command execution, no output buffering
- Tests use pytest with moto for AWS mocking

## Commands

```bash
# Run unit tests
cd tests && python -m pytest unit/ -v

# Run smoke tests (post-deploy)
bash tests/smoke/test_deploy.sh

# Build Docker image locally
docker build -f docker/Dockerfile -t aws-execution-engine .

# Generate terraform backend
bash scripts/generate_backend.sh <bucket_name> <stage> <region>

# Generate terraform.tfvars
bash scripts/generate_tfvars.sh <ecr_repo_url> <image_tag> <region> <project_prefix>
```
