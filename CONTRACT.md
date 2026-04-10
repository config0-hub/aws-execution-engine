# ENGINE CONTRACT -- aws-execution-engine

Version: 1.0
Status: Final (aligned with ACTION3.md)

## 1. Job Submission

### Endpoint
- Method: POST
- Path: /init
- Auth: AWS IAM SigV4 (execute-api:Invoke)
- Content-Type: application/json

### Request Body
```json
{
  "job_parameters_b64": "<base64-encoded JSON>"
}
```

### Decoded Job Payload Schema
```json
{
  "git_repo": "string",              // required -- "org/repo"
  "git_token_location": "string",    // required -- "aws:::ssm:/path"
  "username": "string",              // required
  "commit_hash": "string",           // optional -- pins git checkout version
  "flow_label": "string",            // optional -- default "exec"
  "job_timeout": int,                // optional -- default 3600 seconds
  "pr_number": int,                  // one of pr_number/issue_number required
  "issue_number": int,
  "sops_key_ssm_path": "string",    // optional -- SSM path to pre-existing age key
  "orders": [Order]                  // required -- at least 1
}
```

### Order Schema
```json
{
  "order_name": "string",            // optional -- auto-generated if missing
  "cmds": ["string"],                // required -- non-empty list of commands
  "timeout": int,                    // required -- positive seconds
  "execution_target": "lambda|codebuild|ssm",  // required
  "git_folder": "string",            // optional -- subdirectory for checkout
  "must_succeed": bool,              // optional -- default true
  "queue_id": "string",              // optional -- for serialization
  "dependencies": ["order_name"],    // optional -- run after named orders
  "env_vars": {"key": "value"},      // optional
  "ssm_paths": ["/path"],            // optional -- SSM params to inject
  "secret_manager_paths": ["/path"]  // optional -- Secrets Manager refs
}
```

### Success Response (HTTP 200)
```json
{
  "status": "ok",
  "run_id": "uuid",
  "trace_id": "8-char-hex",
  "flow_id": "username:trace_id-flow_label",
  "done_endpt": "s3://bucket/run_id/done"
}
```

### Error Response (HTTP 400/500)
```json
{
  "status": "error",
  "error": "string",
  "errors": ["string"],             // validation errors only
  "run_id": "uuid",                 // if allocated
  "trace_id": "string"              // if allocated
}
```

## 2. Done Endpoint (S3)

### Location
- URI: s3://{done_bucket}/{run_id}/done
- Provided in init response as `done_endpt`
- Consumer parses URI: bucket = URI[5:].split("/",1)[0], key = URI[5:].split("/",1)[1]

### Content
```json
{
  "status": "succeeded|failed|timed_out",
  "summary": {
    "succeeded": int,
    "failed": int,
    "timed_out": int
  }
}
```

## 3. Order Events (DynamoDB)

### Table
- Name: provided via Terraform output (consumer reads from env var)
- Partition key: `trace_id` (String)
- Sort key: `sk` (String, format: "{order_name}:{epoch}")
- GSI: `order_name` (HASH) + `epoch` (RANGE) -- for per-order queries

### Event Record Schema
```json
{
  "trace_id": "string",              // partition key
  "sk": "order_name:epoch",          // sort key
  "order_name": "string",
  "epoch": "string",
  "event_type": "string",            // e.g., "tf_plan", "tf_validate", "tfsec"
  "status": "string",                // e.g., "info", "success", "error"
  "flow_id": "string",               // metadata -- top level
  "run_id": "string",                // metadata -- top level
  "data": {                           // subprocess payload -- NESTED MAP
    "add": int,                       // example: tf_plan fields
    "change": int,
    "destroy": int,
    "output": "string",
    "...": "..."                      // varies by event_type
  }
}
```

### Access Patterns
- All events for a job: Query by `trace_id`
- Events for one order: Query by `trace_id` + `begins_with(sk, "order_name:")`
- Subprocess data: `event["data"]` (nested map, never flattened)

## 4. Orders Table (DynamoDB)

### Table
- Name: provided via Terraform output
- Partition key: `pk` (String, format: "{run_id}:{order_num}")
- GSI: `run_id` (HASH) + `order_num` (RANGE), name: `run_id-order_num-index`

### Access Patterns
- All orders for a run: Query GSI by `run_id`
- Single order: GetItem by `pk`

## 5. Resource Discovery
All resource names are Terraform outputs. Consumers read them via environment variables:
- `AWS_EXE_SYS_ORDERS_TABLE`
- `AWS_EXE_SYS_ORDER_EVENTS_TABLE`
- `AWS_EXE_SYS_LOCKS_TABLE`
- `AWS_EXE_SYS_INTERNAL_BUCKET`
- `AWS_EXE_SYS_DONE_BUCKET`
No hardcoded resource names across system boundaries.

## 6. Authentication
- Same-account callers: IAM SigV4 on API Gateway
- Lambda Function URL: IAM auth (direct invoke path)
- External callers: TBD (JWT planned)
- GitHub -> caller: HMAC (X-Hub-Signature-256). This is NOT engine's concern.
