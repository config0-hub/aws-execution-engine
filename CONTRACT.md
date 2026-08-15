# AWS execution engine wire contract

Version: 5.0

`aws_exe_sys` is a generic command runner. It accepts a prepared command payload,
executes it on a selected target (`lambda` or `codebuild`), and uses one S3 `ExecutionResult` object as the
terminal marker.

Version 5.0 adds two optional fields, `callback_url` and `callback_token`, as a
backward-compatible extension. The payload contains exactly ten fields; the two new
ones are nullable and absent means today's behavior is unchanged.

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
| `timeout_seconds` | Required. Positive integer: the execution's overall timeout in seconds. No default; a missing or invalid value fails validation. |
| `callback_url` | Optional. An `http://` or `https://` URL. When set, the worker best-effort POSTs the terminal `ExecutionResult` here after writing the done-marker. |
| `callback_token` | Optional. Sent as a bearer token (`Authorization: Bearer <token>`) on the callback POST. Requires `callback_url` to also be set. |

Here, `sops_type="ssm"` means that the SOPS age key is stored in AWS Systems Manager Parameter Store. It is independent of the removed SSM execution target.

### Completion callback

Absent `callback_url` is exactly today's behavior — no callback is attempted. When set, the
worker POSTs the same JSON body it wrote to `done_endpoint` (see Result, below) to
`callback_url` immediately after that S3 write succeeds, with `callback_token` (when
present) as a bearer `Authorization` header. A callback failure (network error, HTTP error
response, timeout) is LOG-ONLY: it never fails the execution and never affects the
already-written done-marker. This is the one sanctioned best-effort seam in this contract
— every other failure mode here is fail-loud.

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
  The workflow starts the managed CodeBuild project with all ten fields as plain-string CodeBuild
  environment overrides, waits for a terminal build state, and invokes the finalizer. The dispatcher derives
  two numeric workflow inputs from `timeout_seconds`: the per-build CodeBuild `TimeoutInMinutesOverride`
  (ceil to minutes plus a small margin) and the Step Functions state timeout (`timeout_seconds` plus the
  queued bound and a provisioning margin). The CodeBuild project's static `build_timeout` is only a generous
  ceiling the per-build override stays under.

The payload is passed as plain string values to each target. Successful `init_job` acknowledgement for
CodeBuild means Step Functions accepted the execution; it does not mean the build completed.

## Result

The primary worker writes the detailed `ExecutionResult` object to `done_endpoint`:

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
