# Architecture

This repository implements a minimal, provider-agnostic executor.

## Components

- **`init_job`** (`aws_exe_sys.init_job`):
  - accepts and validates a `SimplePayload`
  - dispatches Lambda work directly or starts the CodeBuild workflow
- **`worker`** (`aws_exe_sys.worker`):
  - fetches and unpacks `s3_package_uri`
  - decrypts optional secrets (`sops_type`)
  - runs the command list from `commands_b64`
  - writes the detailed terminal `ExecutionResult` to `done_endpoint`
- **CodeBuild workflow** (Standard Step Functions):
  - starts CodeBuild with all eight payload fields as plaintext environment overrides
  - waits for CodeBuild to reach a terminal state
  - invokes the finalizer on both success and caught failure
- **`finalizer`** (`aws_exe_sys.finalizer`):
  - atomically writes a failed fallback with `If-None-Match: *`
  - treats S3 precondition failure as proof that the worker result exists
  - propagates every other S3 failure

## Execution flow

```text
caller
  -> init_job
      validate payload + references
      dispatch by execution_target
         -> lambda worker
              -> detailed ExecutionResult -> done_endpoint
         -> Standard Step Functions workflow
              -> CodeBuild StartBuild.sync
                   -> worker attempts detailed ExecutionResult
              -> finalizer
                   -> preserve existing marker, or create failed fallback
```

## Target matrix

| `execution_target` | `init_job` behavior | Lifecycle owner |
| --- | --- | --- |
| `lambda` | asynchronous Lambda invoke | worker Lambda |
| `codebuild` | asynchronous Step Functions `StartExecution` | Standard workflow |

For CodeBuild, `init_job` acknowledgement means only that Step Functions accepted the execution. The caller
still waits for `done_endpoint`; Step Functions history is operational metadata, not a completion API.

## Completion contract

Presence of `done_endpoint` is the only completion marker. The worker is always the primary writer. The
finalizer never overwrites that result. A successful CodeBuild run without a worker marker becomes a canonical
failed result because the completion contract was not satisfied.
