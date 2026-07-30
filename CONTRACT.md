# AWS execution engine wire contract

Version: 3.0

`aws_exe_sys` is a generic command runner. It accepts a prepared command payload,
executes it on a selected target (`lambda` or `codebuild`), and guarantees one attempted terminal result write to S3.

Version 3.0 removes the unused `ssm` execution target. The payload still contains exactly seven fields.

## Submission

A caller submits the flat JSON payload below to `init_job` (Lambda, SNS, or direct invoke).

| Field | Rule |
| --- | --- |
| `trigger_id` | Required. Uniquely identifies the execution. |
| `s3_package_uri` | Required. S3 URI of the prepared zip package. |
| `sops_type` | Nullable. One of `ssm`, `kms`, or `null`. |
| `sops_path` | Nullable. Required when `sops_type` is `ssm`. |
| `commands_b64` | Required. Base64-encoded JSON array of shell commands. |
| `done_endpoint` | Required. S3 URI where the result marker is written. |
| `execution_target` | Required. One of `lambda` or `codebuild`. |

Here, `sops_type="ssm"` means that the SOPS age key is stored in AWS Systems Manager Parameter Store. It is independent of the removed SSM execution target.

Acknowledgement on dispatch:

```json
{
  "status": "ok",
  "trigger_id": "string"
}
```

## Dispatch and execution

`init_job` validates the payload and routes to:

- `lambda`: asynchronous invocation of `AWS_EXE_SYS_WORKER_LAMBDA`.
- `codebuild`: asynchronous start of the Standard workflow in `AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN`.
  The workflow starts the managed CodeBuild project with all seven fields as environment variables,
  waits for a terminal build state, and invokes the finalizer.

The payload is passed as plain string values to each target. Successful `init_job` acknowledgement for
CodeBuild means Step Functions accepted the execution; it does not mean the build completed.

## Result

`worker` writes one `ExecutionResult` object to `done_endpoint`:

```json
{
  "trigger_id": "string",
  "status": "succeeded|failed",
  "steps": [
    {
      "step_name": "step-0",
      "status": "succeeded|failed",
      "exit_code": 0,
      "duration_seconds": 1.23,
      "output": "combined stdout+stderr"
    }
  ],
  "error": "present only when status is failed"
}
```

Presence of this object is terminal. A result-write failure is raised by the worker rather than being reported as successful execution.

For CodeBuild, the worker remains the primary writer of the detailed result. After CodeBuild reaches a
terminal state, the finalizer performs an atomic conditional S3 write (`If-None-Match: *`). It preserves an
existing worker result. If no result exists, it writes a canonical `failed` result with an empty `steps` list
and a stable `codebuild_*_without_result` error classification. A successful build without a worker marker is
therefore a failed contract outcome. Other S3 errors propagate and fail the Step Functions execution.
