# aws-exe-sys

`aws_exe_sys` is a small AWS-native execution adapter.

It provides a two-step flow:

1. `init_job` receives a **7-field payload** and dispatches to a target.
2. `worker` executes the commands and writes an `ExecutionResult` to S3.

Supported execution targets:
- `lambda`
- `codebuild`

## Supported payload

| Field | Required | Description |
|---|---|---|
| `trigger_id` | Yes | Correlation ID for the run |
| `s3_package_uri` | Yes | `s3://` path to `exec.zip` |
| `sops_type` | No | `null`, `ssm`, or `kms` |
| `sops_path` | No | Required when `sops_type == "ssm"` |
| `commands_b64` | Yes | Base64 JSON array of shell commands |
| `done_endpoint` | Yes | `s3://` path for the final marker |
| `execution_target` | Yes | `lambda` or `codebuild` |

Example:

```json
{
  "trigger_id": "job-123",
  "s3_package_uri": "s3://my-bucket/jobs/job-123/exec.zip",
  "sops_type": null,
  "sops_path": null,
  "commands_b64": "WyJlY2hvIGhlbGxvIl0=",
  "done_endpoint": "s3://my-done-bucket/job-123/result.json",
  "execution_target": "lambda"
}
```

The Terraform deployment grants access to its managed package/result buckets by default.
Set `additional_package_bucket_arns` and `additional_result_bucket_arns` when using caller-managed buckets like those in the example.

## Runtime behavior

- `init_job` validates payload shape and references (`s3://...`, optional SSM key).
- `worker` downloads and unpacks the zip, applies SOPS if configured, then runs commands sequentially.
- `worker` writes a terminal `ExecutionResult` to `done_endpoint` on success or execution failure.
- If the result cannot be persisted, the worker raises instead of falsely reporting success.

See `CONTRACT.md` for the exact response and result schema.

## Required environment variables

The matching variable is required for each dispatch path:

- `AWS_EXE_SYS_WORKER_LAMBDA` (for `lambda` dispatch)
- `AWS_EXE_SYS_CODEBUILD_PROJECT` (for `codebuild` dispatch)

The Terraform deployment also sets the managed package and result bucket names:

- `AWS_EXE_SYS_INTERNAL_BUCKET`
- `AWS_EXE_SYS_DONE_BUCKET`

## Build release artifacts

The standalone public build uses only Docker and public downloads:

```bash
bash scripts/build-release-zip.sh
```

It produces `dist/engine.zip` and `dist/sops-age-layer.zip`. The existing
`scripts/build-zip.sh` is reserved for compatibility with the private release system.

## Development

Run tests with Docker:

```bash
docker build -f docker/Dockerfile.test -t aws-exe-sys-tests .
docker run --rm aws-exe-sys-tests tests/unit/ -v
docker run --rm aws-exe-sys-tests tests/integration/ -v
```
