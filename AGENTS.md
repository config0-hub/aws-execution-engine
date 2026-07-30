# AWS execution engine contract

`aws_exe_sys` is a generic AWS-native execution helper.

It exposes three Lambda entry points:

- `init_job` — validate payload and dispatch work.
- `worker` — execute commands and write the detailed terminal result to S3.
- `finalizer` — atomically create a missing failed CodeBuild fallback result.

## Contract

### 1. Submission (caller → `init_job`)

Callers submit a JSON payload with the seven fields below.

```text
SimplePayload:
  trigger_id      (required)
  s3_package_uri  (required, s3://...) - zip with executable files
  sops_type       (optional: null | "ssm" | "kms")
  sops_path       (required when sops_type == "ssm")
  commands_b64    (required base64-encoded JSON array of commands)
  done_endpoint   (required, s3://...) - marker sink
  execution_target(required: "lambda" | "codebuild")
```

`init_job` responds with:

- `{"status": "ok", "trigger_id": "..."}` on successful dispatch.
- `{"status": "error", ...}` when validation or pre-flight checks fail.

### 2. Dispatch

`init_job` selects target behavior by `execution_target`:

- `lambda` → async `Invoke` of `AWS_EXE_SYS_WORKER_LAMBDA`
- `codebuild` → async `StartExecution` of `AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN`

The Standard workflow starts the managed CodeBuild worker with all seven payload fields as plain-string
environment overrides, waits for a terminal build state, and invokes the finalizer.

### 3. Worker run and completion

`worker`:

1. downloads and unpacks `s3_package_uri`
2. optionally decrypts secrets according to `sops_type`/`sops_path`
3. runs `commands_b64` sequentially
4. writes an `ExecutionResult` JSON object to `done_endpoint`; a write failure propagates

For CodeBuild, the worker is the primary result writer. The finalizer uses `If-None-Match: *` to preserve an
existing marker or create a canonical failed fallback when the marker is missing. Non-precondition S3 errors
propagate. Completion is detected by the presence of the `done_endpoint` object (no callbacks or polling API).
