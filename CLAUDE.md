# AWS execution engine usage contract

This repository documents a single generic execution interface:

- **`init_job`** validates and dispatches work.
- **`worker`** runs commands on the selected target and writes a detailed terminal result to S3.
- **`finalizer`** atomically creates a missing failed fallback after a terminal CodeBuild run.

## Scope

The engine receives a pre-resolved command payload and has no stack-specific business logic.

## Data contract

`init_job` accepts a `SimplePayload` with seven fields:

1. `trigger_id`
2. `s3_package_uri`
3. `sops_type` (`null`, `ssm`, or `kms`)
4. `sops_path` (required only when `sops_type == "ssm"`)
5. `commands_b64`
6. `done_endpoint`
7. `execution_target` (`lambda` or `codebuild`)

`worker` writes an `ExecutionResult` to `done_endpoint`; marker-write failures propagate.

## Run flow

```text
caller
  -> init_job
    -> validate payload + referenced resources
      -> Lambda worker, or Standard Step Functions workflow for CodeBuild
        -> worker executes command list and writes ExecutionResult
        -> CodeBuild workflow invokes finalizer
          -> preserve worker result or create missing failed fallback
```

## Notes

- `done_endpoint` is the single completion marker and is treated as terminal when present.
- Step Functions execution history is operational metadata, not the caller completion API.
- `sops_type == null` runs with no secret decryption.
- `sops_type == ssm` uses an SSM parameter at `sops_path`.
- `sops_type == kms` uses SOPS metadata in the package, no explicit `sops_path` required.
