# Payload variables

## SimplePayload fields

```text
trigger_id:        string  (required)
s3_package_uri:    s3://.../exec.zip
sops_type:         null | "ssm" | "kms"
sops_path:         string | null
commands_b64:      base64(JSON string array)
done_endpoint:     s3://.../result.json
execution_target:   "lambda" | "codebuild"
```

## Rules

- `sops_path` is required only when `sops_type == "ssm"`.
- `commands_b64` must decode to a non-empty array of strings.
- `s3_package_uri` and `done_endpoint` must be valid S3 URIs.
- `execution_target` must be one of: `lambda`, `codebuild`.
- `sops_type="ssm"` refers only to SOPS key storage in Parameter Store; it is not an execution target.

## Command list example

```python
import base64, json
commands = ["echo hello", "ls -la"]
commands_b64 = base64.b64encode(json.dumps(commands).encode()).decode()
```

## Result payload (`done_endpoint` object)

```json
{
  "trigger_id": "string",
  "status": "succeeded" | "failed",
  "steps": [
    {
      "step_name": "step-0",
      "status": "succeeded",
      "exit_code": 0,
      "duration_seconds": 1.23,
      "output": "combined output"
    }
  ],
  "error": "string when failed"
}
```

`error` is omitted when execution succeeds.
