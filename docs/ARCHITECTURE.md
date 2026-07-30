# Architecture

This repository implements a minimal, provider-agnostic executor.

## Components

- **`init_job`** (`aws_exe_sys.init_job`):
  - accepts a `SimplePayload`
  - validates the payload and required resources
  - dispatches to an execution target
- **`worker`** (`aws_exe_sys.worker`):
  - fetches and unpacks `s3_package_uri`
  - decrypts optional secrets (`sops_type`)
  - runs command list from `commands_b64`
  - writes a terminal `ExecutionResult` to `done_endpoint`

## Execution flow

```text
caller
  -> init_job
      validate payload + references
      dispatch by execution_target
         -> lambda worker / codebuild job
             -> worker executes shell commands
                 -> write done marker (S3)
```

## Target matrix

| execution_target | `init_job` behavior |
| --- | --- |
| `lambda` | async Lambda invoke |
| `codebuild` | start CodeBuild build |

### Completion contract

`done_endpoint` being present is the only completion marker. Presence means execution has finished and must include one `ExecutionResult` JSON document.
