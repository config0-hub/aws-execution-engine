# RESEARCH2.md: Deep Analysis of aws-execution-engine Execution Pathways & Gaps

## Executive Summary

This research provides an exhaustive analysis of the aws-execution-engine's execution pathways (Lambda, CodeBuild, SSM), callback mechanisms, status checking, and identifies critical gaps that could cause jobs to hang, fail silently, or get stuck in inconsistent states.

**Key Finding**: The system has three major vulnerability areas:
1. **Callback Failure Silent Handling** - Workers can fail to report results with no fallback
2. **Presigned URL Expiry** - Long-running orders can expire callback URLs before completion
3. **Orchestrator Lock Staleness** - Crashed instances leave locks blocking future runs

---

## 1. COMPLETE EXECUTION PATHWAYS

### 1.1 Lambda Order Execution Path

**Flow Diagram:**
```
init_job Lambda
  ├─ [1] Parse job_parameters_b64
  ├─ [2] Validate orders (check execution_target, timeout, code source)
  ├─ [3] Git clone + folder extraction (grouped by repo/commit hash)
  ├─ [4] Fetch credentials: SSM → Secrets Manager → JWT injection
  ├─ [5] Generate SOPS keypair (age):
  │    ├─ Private key → SSM Parameter Store (Advanced tier, 2hr TTL)
  │    └─ Public key → encrypt env vars
  ├─ [6] Generate presigned S3 PUT URL (2hr expiry default)
  ├─ [7] OrderBundler.build_env() → merge user vars + engine fields:
  │    ├─ TRACE_ID, RUN_ID, ORDER_ID, ORDER_NUM, FLOW_ID
  │    ├─ CMDS (JSON list), CALLBACK_URL (presigned PUT)
  │    └─ All credentials (AWS keys, git tokens, etc.)
  ├─ [8] SOPS encryption → secrets.enc.json + env_vars.env + secrets.src
  ├─ [9] Zip code directory → exec.zip
  ├─ [10] Upload exec.zip to S3:
  │     └─ s3://{internal_bucket}/tmp/exec/{run_id}/{order_num}/exec.zip
  ├─ [11] Insert into DynamoDB orders table:
  │     ├─ PK: {run_id}:{order_num}
  │     ├─ Status: QUEUED
  │     ├─ Stores: s3_location, callback_url, sops_key_ssm_path, dependencies
  │     └─ TTL: now + 86400s
  └─ [12] Write init trigger → S3 ObjectCreated event → triggers orchestrator

Orchestrator Lambda
  ├─ [1] Parse S3 event key → extract run_id
  ├─ [2] Acquire distributed lock (conditional DynamoDB put):
  │     ├─ PK: lock:{run_id}
  │     ├─ Condition: NOT_EXISTS OR status="completed"
  │     └─ Status: "active", TTL: now + 3600s
  ├─ [3] Query DynamoDB for all orders (GSI run_id-order_num-index)
  ├─ [4] For each RUNNING order:
  │     ├─ Check S3 for result.json
  │     ├─ If found: parse JSON → update order status in DynamoDB
  │     └─ Write order event to order_events table
  ├─ [5] Evaluate dependencies:
  │     ├─ QUEUED + no deps → Ready
  │     ├─ QUEUED + all deps SUCCEEDED → Ready
  │     ├─ QUEUED + any dep FAILED + must_succeed=True → Failed-due-to-deps
  │     ├─ QUEUED + any dep FAILED + must_succeed=False → Ready
  │     └─ Otherwise → Waiting
  ├─ [6] For each ready Lambda order:
  │     ├─ Invoke worker Lambda async:
  │     │    Payload: {s3_location, internal_bucket, sops_key_ssm_path}
  │     │    InvocationType: "Event" (asynchronous)
  │     ├─ Start Step Function watchdog (per-order):
  │     │    Input: {run_id, order_num, timeout, start_time, internal_bucket}
  │     ├─ Update order status → RUNNING
  │     ├─ Write order event (event_type: "dispatched")
  │     └─ Return RequestId
  ├─ [7] Check if all orders terminal (SUCCEEDED, FAILED, TIMED_OUT)
  │     ├─ If no: Release lock, return {"status": "in_progress"}
  │     ├─ Next S3 callback will trigger orchestrator again
  │     └─ If yes: Proceed to finalization
  ├─ [8] Finalization (when all done):
  │     ├─ Resolve job status:
  │     │    ├─ If any TIMED_OUT → TIMED_OUT
  │     │    ├─ Else if any FAILED (must_succeed=True) → FAILED
  │     │    └─ Else → SUCCEEDED
  │     ├─ Write job completion event to order_events
  │     ├─ Delete all SOPS keys from SSM Parameter Store
  │     ├─ Write done endpoint: s3://{done_bucket}/{run_id}/done
  │     │    Payload: {status, summary: {succeeded: N, failed: N, timed_out: N}}
  │     ├─ Release lock (set status="completed")
  │     └─ Return {"status": "finalized", "summary": {...}}
  └─ [9] Return response

Worker Lambda (async invocation)
  ├─ [1] Receive event: {s3_location, internal_bucket, sops_key_ssm_path}
  ├─ [2] Download exec.zip from S3:
  │     └─ boto3 S3 GetObject → extract to /tmp
  ├─ [3] Decrypt SOPS:
  │     ├─ If sops_key_ssm_path provided:
  │     │    ├─ Fetch private key from SSM: /{prefix}/sops-keys/{run_id}/{order_num}
  │     │    ├─ If ParameterNotFound (expired): worker CRASHES
  │     │    └─ If SSM permission denied: worker CRASHES
  │     ├─ Else: Check SOPS_AGE_KEY or SOPS_AGE_KEY_FILE env vars
  │     └─ Run: sops --decrypt --age {key} secrets.enc.json
  ├─ [4] Load env vars from decrypted JSON
  ├─ [5] Setup events directory: /tmp/share/{TRACE_ID}/events
  ├─ [6] Read commands:
  │     ├─ Priority 1: CMDS env var (JSON string)
  │     ├─ Priority 2: cmds.json file in work dir
  │     └─ If neither: return status="failed"
  ├─ [7] Build subprocess environment:
  │     ├─ Copy os.environ
  │     ├─ Update with decrypted env vars
  │     ├─ Set AWS_EXE_SYS_EVENTS_DIR if events dir exists
  │     └─ No mutations to os.environ
  ├─ [8] Execute commands sequentially:
  │     ├─ subprocess.Popen(cmd, shell=True, cwd=work_dir, env=proc_env)
  │     ├─ Capture stdout/stderr merged
  │     ├─ Stop on first failure (exit code != 0)
  │     ├─ Support timeout: proc.kill() if exceeded
  │     └─ Return (status: "succeeded"|"failed"|"timed_out", combined_log)
  ├─ [9] Collect subprocess events:
  │     ├─ Glob *.json in events_dir
  │     ├─ Parse each: {event_type, status, data, ...}
  │     ├─ Write to order_events table via DynamoDB put_event()
  │     └─ Silently skip malformed files (log warning only)
  ├─ [10] Send callback to presigned S3 PUT URL:
  │      ├─ Payload: {status: "<status>", log: "<output>"}
  │      ├─ Method: requests.put()
  │      ├─ Retry: 3x with 2s delay between attempts
  │      ├─ Success criteria: HTTP 200, 201, or 204
  │      ├─ On all retries exhausted: Log error, return False
  │      └─ NO FALLBACK (worker exits normally even if callback fails)
  ├─ [11] S3 ObjectCreated event triggered:
  │      └─ Orchestrator Lambda invoked again → continue execution
  └─ [12] Return {"status": status}

**Execution Timing:**
- Worker Lambda timeout: 15 min (Lambda default) or custom `AWS_LAMBDA_TIMEOUT`
- Subprocess timeout: order.timeout (default 300s)
- Presigned URL expiry: job.presign_expiry (default 7200s = 2 hours)
- Watchdog polling interval: 60s (hardcoded in Step Function state machine)
- Watchdog timeout: order.timeout (passed from orchestrator)
```

### 1.2 CodeBuild Order Execution Path

**Flow Diagram:**
```
init_job Lambda
  ├─ [1-12] SAME AS LAMBDA (generate SOPS keys, presigned URLs, upload to S3, insert DynamoDB)

Orchestrator Lambda (Part 2)
  ├─ [1-5] SAME AS LAMBDA (lock, read state, evaluate deps)
  ├─ [6] For each ready CodeBuild order:
  │     ├─ codebuild_client.start_build():
  │     │    ├─ ProjectName: AWS_EXE_SYS_CODEBUILD_PROJECT env var
  │     │    ├─ EnvironmentVariablesOverride:
  │     │    │   ├─ S3_LOCATION: {s3_location}
  │     │    │   ├─ INTERNAL_BUCKET: {internal_bucket}
  │     │    │   └─ SOPS_KEY_SSM_PATH: {sops_key_ssm_path} (if present)
  │     │    └─ Return: BuildId
  │     ├─ Start watchdog Step Function (same as Lambda)
  │     ├─ Update order status → RUNNING
  │     └─ Write order event
  └─ [7-9] SAME AS LAMBDA (check finalization, cleanup, release lock)

CodeBuild Job Container
  ├─ [1] Docker image pulls from ECR
  ├─ [2] Buildspec execution (AWS::CodeBuild::Project resource):
  │     └─ version: 0.2
  │        phases:
  │          build:
  │            commands:
  │              - /entrypoint.sh
  ├─ [3] /entrypoint.sh script:
  │     ├─ export PYTHONPATH=/var/task
  │     ├─ cd /var/task
  │     └─ python -m src.worker.run
  ├─ [4] src/worker/run.py main block:
  │     ├─ Read S3_LOCATION from os.environ (set by orchestrator)
  │     ├─ Read INTERNAL_BUCKET from os.environ
  │     ├─ Read SOPS_KEY_SSM_PATH from os.environ (if set)
  │     └─ Call run(s3_location, internal_bucket, sops_key_ssm_path)
  │
  ├─ [5-11] SAME AS WORKER LAMBDA (download, decrypt, execute, collect events, send callback)
  │
  └─ [12] CodeBuild job completes with status 0 or exit code

Callback Trigger
  ├─ Worker calls: requests.put(presigned_url, json={status, log})
  ├─ S3 ObjectCreated event triggered
  └─ Orchestrator Lambda invoked → continue execution

**Key Difference**: CodeBuild runs in a long-lived container, NOT Lambda. Can support:
  - Longer execution times (no 15-min Lambda limit)
  - More memory/CPU resources (customizable per project)
  - Docker build capabilities
```

### 1.3 SSM Run Command Order Execution Path

**Flow Diagram:**
```
ssm_config Lambda (Part 1b)
  ├─ [1] Parse job_parameters_b64 (SsmJob type)
  ├─ [2] Validate orders:
  │     ├─ Check: cmds non-empty, timeout > 0, ssm_targets provided
  │     ├─ Fail-fast on first error
  │     └─ Gap: No validation that instance IDs/tags exist
  ├─ [3] Git clone + folder extraction (same as init_job)
  ├─ [4] Fetch credentials (same as init_job)
  ├─ [5-9] Repackage orders (NO SOPS ENCRYPTION):
  │       ├─ Write cmds.json: JSON list of commands
  │       ├─ Write env_vars.json: plaintext env dict (NOT encrypted)
  │       ├─ Generate presigned callback URL (same as Lambda)
  │       └─ Zip code directory
  ├─ [10] Upload to S3 (same as Lambda)
  ├─ [11] Insert into DynamoDB:
  │      ├─ Status: QUEUED
  │      ├─ execution_target: "ssm" (implicit)
  │      ├─ env_dict: plaintext dict (stored directly in DynamoDB)
  │      │   ⚠️ GAP: Credentials visible in DynamoDB table
  │      ├─ ssm_targets: {instance_ids?: [...], tags?: {...}}
  │      └─ ssm_document_name: optional custom document
  └─ [12] Write init trigger → orchestrator triggered

Orchestrator Lambda (Part 2)
  ├─ [1-5] SAME AS LAMBDA (lock, read state, evaluate deps)
  ├─ [6] For each ready SSM order:
  │     ├─ ssm_client.send_command():
  │     │    ├─ DocumentName: order.ssm_document_name or AWS_EXE_SYS_SSM_DOCUMENT env var
  │     │    ├─ Parameters:
  │     │    │   ├─ Commands: order.cmds (JSON array)
  │     │    │   ├─ CallbackUrl: presigned PUT URL
  │     │    │   ├─ Timeout: order.timeout
  │     │    │   ├─ EnvVars: env_dict (JSON object)
  │     │    │   └─ S3Location: s3://{internal_bucket}/tmp/exec/{run_id}/{order_num}/exec.zip
  │     │    ├─ Targets:
  │     │    │   ├─ Key: "tag:{key}" or direct InstanceIds: [...]
  │     │    │   └─ Values: instance tags or IDs
  │     │    └─ Return: CommandId
  │     ├─ Start watchdog Step Function (same as Lambda/CodeBuild)
  │     ├─ Update order status → RUNNING
  │     └─ Write order event
  └─ [7-9] SAME AS LAMBDA (check finalization, cleanup, release lock)

SSM Document (Bash script on EC2)
  ├─ [1] Create temp work directory
  ├─ [2] Export environment variables:
  │     ├─ Parse EnvVars JSON parameter
  │     ├─ export VAR=value for each
  │     └─ No SOPS decryption (plaintext already)
  ├─ [3] Download and extract exec.zip (if S3Location provided):
  │     └─ aws s3 cp s3://... exec.zip && unzip exec.zip
  ├─ [4] Execute commands sequentially:
  │     ├─ Commands from Commands JSON parameter
  │     ├─ Shell execution (bash -c "cmd")
  │     ├─ Capture stdout/stderr → log file
  │     ├─ Stop on first failure
  │     └─ Log file limited to 262KB (truncated if larger)
  ├─ [5] Send callback:
  │     ├─ Read status from subprocess
  │     ├─ Build JSON: {status, log: "<first 256KB of log>"}
  │     ├─ curl -X PUT CallbackUrl (presigned S3 PUT URL)
  │     ├─ No retry logic (|| true means curl failure is ignored)
  │     └─ ⚠️ GAP: If curl fails, callback lost permanently
  ├─ [6] Cleanup:
  │     └─ rm -rf temp dirs
  └─ [7] Exit (code = last command exit code)

**Execution Flow:**
  - SSM sends command to EC2 instances (broadcast to all matching tags/IDs)
  - Instances execute document in parallel (if multiple instances)
  - Each instance sends callback via presigned URL
  - Multiple callbacks from different instances trigger orchestrator multiple times
  - Orchestrator uses run_id + lock to ensure single processing

**Key Differences from Lambda/CodeBuild:**
  - No SOPS encryption (credentials plaintext in DynamoDB)
  - Multiple instances can execute same order (no consolidation)
  - Callback is from EC2 instance shell, not managed by Lambda
  - SSM agent must be running and responsive (no AWS guarantee)
```

---

## 2. PRESIGNED URL & CALLBACK MECHANISM DEEP DIVE

### 2.1 Presigned URL Lifecycle

**Generation Phase (init_job/ssm_config):**
```
s3_ops.generate_callback_presigned_url(
    bucket="{internal_bucket}",
    run_id="{run_id}",
    order_num="{order_num}",
    expiry=job.presign_expiry  # Default: 7200 seconds (2 hours)
)

S3 Key: tmp/callbacks/runs/{run_id}/{order_num}/result.json
Method: PUT
Signature: AWS SigV4 (regional)
Returns: Full presigned URL with expiry baked into query params
```

**Embedding Phase:**
```
Presigned URL stored in:
1. Lambda orders: Encrypted in secrets.enc.json under CALLBACK_URL key
2. CodeBuild orders: Same as Lambda
3. SSM orders: Passed as CallbackUrl parameter to SSM document

All paths: URL embedded at packaging time, valid for 2 hours
```

**Usage Phase (Worker):**
```
Worker execution:
├─ Worker runs for order.timeout seconds (default 300s)
├─ Commands execute sequentially
├─ Subprocess output captured
├─ Worker waits to completion
├─ Calls send_callback(callback_url, status, log)
│   ├─ requests.put(callback_url, data=json.dumps({status, log}))
│   ├─ Retry up to 3x with 2-second delay
│   └─ Success: HTTP 200, 201, or 204
└─ Returns to async Lambda caller

⚠️ TIMING ISSUE:
If order.timeout = 7200s (2 hours):
  ├─ order starts at T+0
  ├─ Presigned URL expires at T+7200
  ├─ Worker finishes at T+7100 (1 min before expiry)
  └─ Callback PUT succeeds (barely)

If order.timeout > 7200s:
  ├─ order starts at T+0
  ├─ Presigned URL expires at T+7200
  ├─ Worker finishes at T+7300 (100s after expiry)
  └─ Callback PUT FAILS with 403 Forbidden
```

### 2.2 Callback Failure Scenarios

**Scenario 1: Callback URL Already Expired**
```
Trigger: order.timeout > job.presign_expiry (e.g., 10800s > 7200s)
Flow:
  ├─ Worker executes for 10800s
  ├─ Finishes successfully at T+10800
  ├─ Attempts callback PUT to presigned URL
  ├─ S3 returns 403 Forbidden (signature expired)
  ├─ Worker retries 3x (all fail with 403)
  ├─ send_callback() returns False
  ├─ Worker logs error and exits normally (status=0)
  ├─ Orchestrator sees no result.json in S3
  └─ Order stays in RUNNING state

Mitigation: Watchdog timeout
  ├─ Watchdog polls every 60s: check S3 for result.json
  ├─ After order.timeout seconds: write timed_out status to S3
  ├─ Orchestrator reads timed_out, marks order TIMED_OUT
  ├─ Job can complete (if must_succeed=False, continues)
  └─ Total delay: order.timeout + watchdog overhead

⚠️ ISSUE: If watchdog timeout = order.timeout but callback expired partway,
         job marked as TIMED_OUT instead of SUCCEEDED
```

**Scenario 2: Callback Network Failure**
```
Trigger: Network partition, S3 service issue, or client connection drop
Flow:
  ├─ Worker finishes successfully
  ├─ Calls send_callback()
  ├─ requests.put() raises ConnectionError/Timeout
  ├─ Retries 3x with 2s delay (total ~6s of retries)
  ├─ After 3 retries: log error, return False
  ├─ Worker exits normally
  ├─ Orchestrator sees no result.json
  └─ Order stuck in RUNNING until watchdog timeout

Mitigation: Same as above (watchdog polls every 60s)
```

**Scenario 3: Callback Malformed Response**
```
Trigger: Presigned URL expires but S3 still responds (unlikely)
Flow:
  ├─ Worker calls requests.put(url, data=json.dumps({status, log}))
  ├─ S3 returns 403 or 400 (signature validation error)
  ├─ Status code not in [200, 201, 204]
  ├─ Retries 3x
  └─ Returns False

NO SPECIAL HANDLING: Same as network failure
```

### 2.3 S3 Event Notification & Orchestrator Trigger

**S3 Event Configuration:**
```
Bucket: {internal_bucket}
Event: ObjectCreated:* (any put/post/copy operation)
Filter:
  ├─ Prefix: tmp/callbacks/runs/
  └─ Suffix: result.json
Destination: orchestrator Lambda (async invoke)
Notification latency: Typically <1s, can be up to 60s
```

**Race Condition:**
```
Scenario: Worker writes result.json twice (e.g., retry callback logic in future)
Flow:
  ├─ T1: Worker writes result.json (1st attempt)
  │   └─ S3 event → orchestrator Lambda invoked
  ├─ T2: Orchestrator running, parsing run_id
  ├─ T3: Worker overwrites result.json (2nd attempt, different status)
  │   └─ S3 event → orchestrator Lambda invoked AGAIN
  ├─ T4: Both orchestrator instances try to acquire lock
  │   └─ First instance wins, second skipped
  ├─ T5: First orchestrator reads S3, sees result.json
  │   └─ Reads the 2nd write (correct, but race condition exists)

⚠️ NO IDEMPOTENCY CHECK: System assumes only one write per order per run
```

**Callback Result Parsing:**
```
read_result(bucket, run_id, order_num) → dict or None
  ├─ S3 GetObject: tmp/callbacks/runs/{run_id}/{order_num}/result.json
  ├─ On 404: return None (not yet written)
  ├─ On success: json.loads(body) → dict
  ├─ Expected keys: status, log (optional)
  ├─ ⚠️ GAP: No validation of result structure
  └─ If malformed JSON: raises JSONDecodeError (unhandled)

Status Values Accepted:
  ├─ "succeeded" → order status = SUCCEEDED
  ├─ "failed" → order status = FAILED
  ├─ "timed_out" → order status = TIMED_OUT
  └─ Any other value: results in unknown behavior (not validated)
```

---

## 3. ORCHESTRATOR LOCK & CONCURRENCY

### 3.1 Distributed Lock Mechanism

**Lock Record Structure:**
```
Table: orchestrator_locks
PK: lock:{run_id}
Fields:
  ├─ pk: lock:{run_id}
  ├─ run_id: {run_id}
  ├─ orchestrator_id: unique UUID per orchestrator instance
  ├─ status: "active" or "completed"
  ├─ acquired_at: timestamp
  ├─ ttl: now + max(job timeouts) (typically 3600s)
  ├─ flow_id: initially "" (not updated)
  └─ trace_id: initially "" (not updated)
```

**Acquisition Logic:**
```
acquire_lock(run_id, flow_id="", trace_id="", orchestrator_id=UUID):
  ├─ Conditional put:
  │   ├─ Condition: attribute_not_exists(pk) OR status="completed"
  │   ├─ Put: {pk, run_id, orchestrator_id, status="active", acquired_at=now, ttl=now+3600}
  │   └─ Return: True if condition succeeded, False if failed
  ├─ If acquired: orchestrator proceeds
  └─ If not acquired: orchestrator returns {"status": "skipped"}, releases lock

⚠️ ISSUE: flow_id and trace_id passed but NEVER stored in lock
          Lock metadata is incomplete for debugging
```

**Release Logic:**
```
release_lock(run_id):
  ├─ Update: {pk: lock:{run_id}}
  │   └─ Set status="completed"
  ├─ Lock record remains in DynamoDB until TTL expires
  ├─ Next ObjectCreated event for same run_id:
  │   ├─ Condition check: status="completed" (matches)
  │   ├─ New orchestrator acquires lock
  │   └─ Continues processing
  └─ Total lock duration per run: <1 second (while orchestrator runs)
```

### 3.2 Lock Timeout & Staleness

**Staleness Scenario: Orchestrator Crashes**
```
Timeline:
  T0: Orchestrator A acquires lock for run_id=abc
  T1: Orchestrator A reads DynamoDB orders
  T2: Orchestrator A dispatches first batch of orders
  T3: Orchestrator Lambda times out (15 minutes, default) during execution
      ├─ Lock still in DynamoDB with status="active"
      ├─ Lock TTL: now + 3600s (1 hour)
      └─ Orchestrator A process killed
  T4: Worker finishes order, writes result.json to S3
      └─ S3 ObjectCreated event triggered
  T5: Orchestrator B attempts to acquire lock
      ├─ Condition check: NOT_EXISTS OR status="completed"
      ├─ Both conditions FALSE (lock exists and status="active")
      ├─ Lock acquisition FAILS
      ├─ Orchestrator B returns {"status": "skipped"}
      └─ orchestrator B releases lock (sets status="completed"?  NO - skipped before release)
  T6: More workers finish, more callbacks written
      ├─ Orchestrator C, D, E... all try to acquire same lock
      ├─ All fail (status still "active" from A)
      ├─ Run stuck UNTIL lock TTL expires (1 hour)
      └─ ⚠️ CRITICAL: Run stops for 1 hour

Mitigation:
  ├─ Orchestrator implements TTL on locks
  ├─ After TTL expires, next orchestrator can acquire
  ├─ But run is stuck for entire TTL duration
  └─ DEFAULT TTL NOT VISIBLE IN CODE (assume 3600s)
```

**Mitigation Strategy:**
```
Option 1: Reduce TTL
  ├─ Shorter TTL = faster recovery from crashes
  ├─ Risk: Very short TTL (e.g., 30s) with slow orchestrator = concurrent executions
  └─ Trade-off: Typically 5-10x job timeout

Option 2: Heartbeat/Lease
  ├─ Orchestrator updates lock timestamp periodically
  ├─ Next orchestrator checks: if (now - acquired_at) > threshold, lock is stale
  └─ Not implemented in current system

Option 3: Orchestrator UUID + External Monitor
  ├─ Lock stores orchestrator_id
  ├─ External monitor checks if Lambda instance still alive
  └─ Not implemented in current system
```

---

## 4. STATUS CHECKING & COMPLETION DETECTION

### 4.1 How Orchestrator Detects Order Completion

**Current Implementation:**
```
read_state(run_id, internal_bucket):
  ├─ Query DynamoDB: get_all_orders(run_id)
  ├─ For each order with status=RUNNING:
  │   ├─ read_result(bucket, run_id, order_num)
  │   │   └─ S3 GetObject: tmp/callbacks/runs/{run_id}/{order_num}/result.json
  │   ├─ If result found:
  │   │   ├─ Parse JSON: {status, log}
  │   │   ├─ Update DynamoDB: order.status = result.status
  │   │   ├─ Update DynamoDB: order.last_update = now
  │   │   └─ Write event: {trace_id, order_name, event_type="completed", status}
  │   ├─ Else (404):
  │   │   └─ Order still RUNNING (no change)
  │   └─ Callback from worker or watchdog writes result.json
  └─ Returns updated orders list

Event-Driven Triggers:
  ├─ S3 ObjectCreated on result.json
  │   └─ → orchestrator Lambda invoked
  ├─ No polling in orchestrator (event-driven only)
  └─ Watchdog is the only poller (60s interval per order)
```

**Example Execution Timeline:**
```
T0: init_job writes orders to DynamoDB (status=QUEUED)
    └─ write_init_trigger() → S3 ObjectCreated

T1: orchestrator invoked (S3 event)
    ├─ read_state() → no RUNNING orders
    ├─ evaluate_orders() → order_1 READY (no deps)
    ├─ dispatch_orders() → invoke worker Lambda + start watchdog
    ├─ update_order_status(order_1, RUNNING)
    ├─ No result.json yet, no finalization
    └─ return {"status": "in_progress", "dispatched": 1}

T5: Worker finishes execution
    ├─ send_callback() → PUT result.json to S3
    └─ S3 ObjectCreated event

T6: orchestrator invoked (S3 event)
    ├─ read_state():
    │   ├─ read_result(order_1) → found result.json
    │   ├─ update_order_status(order_1, SUCCEEDED)
    │   └─ write_event(order_1, "completed")
    ├─ evaluate_orders() → no more QUEUED/WAITING orders
    ├─ check_and_finalize() → all terminal:
    │   ├─ write_event(_job, "job_completed", SUCCEEDED)
    │   ├─ delete_sops_keys()
    │   ├─ write_done_endpoint(done_bucket, run_id/done, SUCCEEDED)
    │   └─ release_lock()
    └─ return {"status": "finalized", "summary": {succeeded: 1}}

T7: Caller checks for completion:
    ├─ Option 1: poll done_bucket for run_id/done file
    ├─ Option 2: query order_events table for job_completed event
    └─ Option 3: query orders table by run_id for terminal statuses
```

### 4.2 Watchdog Timeout Detection

**Watchdog Step Function State Machine:**
```
State Machine: {watchdog_sfn}
Execution Name: {run_id}-{order_num}
Execution Input:
  {
    "run_id": "{run_id}",
    "order_num": "{order_num}",
    "timeout": 300,          # order.timeout in seconds
    "start_time": 1234567890, # Unix timestamp
    "internal_bucket": "{bucket}"
  }

States:
  1. CheckResult (Lambda: watchdog_check handler)
     ├─ Call: s3_ops.check_result_exists(bucket, run_id, order_num)
     ├─ Return: {done: true/false}
     └─ Pass

  2. IsDone (Choice state)
     ├─ If done=true → Succeed
     ├─ Else → WaitStep

  3. WaitStep (Wait state)
     ├─ Seconds: 60 (hardcoded, no jitter)
     └─ Next: CheckResult

Flow:
  ├─ T0: Watchdog starts (Step Function execution created)
  ├─ T0: CheckResult invokes handler:
  │    ├─ If result.json exists: return {done: true} → Succeed
  │    ├─ Else if elapsed > timeout: write timed_out result.json, return {done: true}
  │    └─ Else: return {done: false} → WaitStep
  ├─ T60: WaitStep completes, goes back to CheckResult
  ├─ T60: CheckResult invokes handler again
  │    ├─ Repeat logic
  ├─ T120, T180, ... (repeat every 60s)
  ├─ T{timeout}: Handler writes timed_out result.json
  │    └─ S3 ObjectCreated event → orchestrator triggered
  ├─ Orchestrator reads timed_out status → order marked TIMED_OUT
  └─ Step Function eventually terminates (done=true)

⚠️ ISSUE: Hardcoded 60-second intervals
  ├─ Thundering herd: if 100 orders timeout simultaneously, all check S3 at T+60, T+120, etc.
  ├─ Creates spike in S3 API calls
  └─ No jitter or exponential backoff

⚠️ ISSUE: No upper bound on Step Function execution duration
  ├─ If timeout = 1 day = 86400s
  ├─ Watchdog runs for 1+ days
  ├─ Step Function execution lives that long
  └─ Cost: $0.000025 per step function state transition * (86400/60) ≈ $0.036 per day
```

**Watchdog Handler Logic:**
```python
def handler(event, context):
    run_id = event["run_id"]
    order_num = event["order_num"]
    timeout = event["timeout"]
    start_time = event["start_time"]
    bucket = event["internal_bucket"]

    now = time.time()
    elapsed = now - start_time

    if s3_ops.check_result_exists(bucket, run_id, order_num):
        return {"done": True}

    if elapsed > timeout:
        s3_ops.write_result(
            bucket, run_id, order_num,
            status="timed_out",
            log="Worker unresponsive, timed out by watchdog"
        )
        return {"done": True}

    return {"done": False}
```

---

## 5. STATUS CHECKING FOR CODEBUILD & LAMBDA

### 5.1 Lambda Invocation Monitoring

**Current Implementation:**
```
orchestrator:
  ├─ lambda_client.invoke(FunctionName, InvocationType="Event", Payload)
  │   ├─ InvocationType: "Event" = asynchronous
  │   └─ Returns: RequestId (NOT execution status)
  ├─ Store RequestId in DynamoDB: order.execution_url
  └─ No polling of Lambda invocation status

Lambda Worker Status Sources:
  ├─ Source 1: Worker calls send_callback() → writes result.json to S3
  ├─ Source 2: Watchdog polls S3 every 60s → writes timed_out if timeout exceeded
  └─ Source 3: CloudWatch Logs (indirect, not used by system)

⚠️ ISSUE: No direct monitoring of Lambda execution
  ├─ If Worker Lambda never starts (e.g., insufficient concurrent executions quota)
  │   └─ Order stuck in RUNNING state until watchdog timeout
  ├─ If Worker Lambda crashes before callback
  │   └─ Order stuck until watchdog timeout
  ├─ If Worker Lambda times out before callback (15 min, Lambda limit)
  │   └─ Worker may not have callback_url available at crash time
  │   └─ Order stuck until watchdog timeout

Mitigation: Watchdog timeout must be >= Worker timeout
  ├─ If order.timeout = 300s but Lambda times out at 900s, order never completes
  ├─ Current implementation: order.timeout is used for both subprocess timeout and watchdog timeout
  └─ RISK: If subprocess fast (100s) and network slow (callback takes 200s):
           └─ Callback timeout exceeded before watchdog detects
```

### 5.2 CodeBuild Build Monitoring

**Current Implementation:**
```
orchestrator:
  ├─ codebuild_client.start_build(projectName, environmentVariablesOverride)
  │   ├─ Returns: BuildId
  │   └─ No wait/polling
  ├─ Store BuildId in DynamoDB: order.execution_url
  └─ No checking of build status from AWS CodeBuild service

CodeBuild Build Status Sources:
  ├─ Source 1: Worker (entrypoint.sh) calls send_callback() → result.json
  ├─ Source 2: Watchdog polls S3 → writes timed_out
  └─ Source 3: AWS CodeBuild service (not queried)

⚠️ ISSUE: No integration with CodeBuild service status
  ├─ If CodeBuild build fails to start (insufficient capacity)
  │   └─ Order stuck in RUNNING state
  ├─ If CodeBuild job crashes (OOM, disk full) before callback
  │   └─ AWS reports build FAILED, but system sees no result.json
  │   └─ Order stuck until watchdog timeout
  ├─ If CodeBuild job is terminated by AWS (e.g., instance termination)
  │   └─ No way for system to know; waits for callback
  │   └─ Order stuck until watchdog timeout

Mitigation: Watchdog timeout is only defense
  ├─ Must be set >= maximum realistic build duration
  ├─ Includes: startup time, execution time, cleanup time
```

### 5.3 SSM Command Monitoring

**Current Implementation:**
```
orchestrator:
  ├─ ssm_client.send_command(DocumentName, Parameters, Targets, Timeout)
  │   ├─ Returns: CommandId
  │   └─ No wait/polling
  ├─ Store CommandId in DynamoDB: order.execution_url
  └─ No checking of command status

SSM Command Status Sources:
  ├─ Source 1: EC2 instance executes document → send_callback() → result.json
  ├─ Source 2: Watchdog polls S3 → writes timed_out
  └─ Source 3: AWS SSM service (not queried)

⚠️ ISSUE: No integration with SSM service
  ├─ If SSM agent is down on all instances (matching targets)
  │   └─ Command never executes
  │   └─ Order stuck in RUNNING state
  ├─ If instance is terminated during command execution
  │   └─ Document partially executes, callback never sent
  │   └─ Order stuck until watchdog timeout
  ├─ If document syntax error exists
  │   └─ Document fails to execute, callback never sent
  │   └─ Order stuck until watchdog timeout
  ├─ If multiple instances match targets:
  │   ├─ Document executed on all instances
  │   ├─ Each writes separate result.json (one per order_num)
  │   ├─ First callback triggers orchestrator
  │   └─ Other callbacks are redundant (re-processing same order_num)

Mitigation: Watchdog timeout + SSM document logging
  ├─ SSM sends results to CloudWatch Logs (can be monitored externally)
  └─ But system doesn't query these logs
```

---

## 6. CRITICAL EXECUTION GAPS

### 6.1 Callback Failure Silent Handling

**Gap Details:**
```
Current behavior:
  ├─ Worker executes commands successfully
  ├─ Worker calls send_callback(url, status, log)
  ├─ If ALL retries fail (network, expired URL, S3 issue):
  │   ├─ send_callback() returns False
  │   ├─ Worker logs error message
  │   └─ Worker exits with status=0 (success, from subprocess perspective)
  ├─ NO EXCEPTION RAISED
  ├─ NO DynamoDB WRITE (other than events from subprocess)
  ├─ NO FALLBACK MECHANISM
  └─ Orchestrator never learns of job completion

Result:
  ├─ Order stays in RUNNING state indefinitely
  ├─ Next S3 event processed, orchestrator still sees RUNNING
  ├─ Waiting for orchestrator never triggers finalization
  └─ Only watchdog timeout saves the day (60s polling)

Impact:
  ├─ Up to order.timeout + (watchdog_polling_interval * N) delay before detection
  ├─ If order.timeout = 300s and polling every 60s:
  │   └─ Worst case: 300 + 60 = 360 seconds = 6 minutes
  ├─ For high-volume systems: many orders stuck simultaneously
  └─ User must wait for watchdog to timeout and mark as TIMED_OUT
      (even though job SUCCEEDED)
```

**Potential Causes:**
```
1. Presigned URL expired (order.timeout > presign_expiry)
2. Network partition (transient or prolonged)
3. S3 service outage
4. S3 bucket gone (permission revoked, bucket deleted)
5. Presigned URL signature invalid (clock skew, key rotation)
6. Worker network isolation (security group, IAM role change)
```

**Recommended Fix:**
```
Option A: DynamoDB Fallback
  ├─ If callback fails after retries, worker writes to DynamoDB directly
  ├─ Requires: Worker IAM role has DynamoDB write permission (currently does for events only)
  ├─ Implementation: update_order_status(run_id, order_num, SUCCEEDED)
  └─ Trade-off: Increases worker IAM scope

Option B: Callback URL Refresh
  ├─ If first presigned URL expires, worker fetches new URL from SSM
  ├─ Requires: Store fresh presigned URL in SSM instead of baking into bundle
  ├─ Implementation: ssm_client.put_parameter(Name, Value) + read before callback
  └─ Trade-off: Increases latency and SSM API calls

Option C: Adaptive Watchdog
  ├─ Reduce watchdog polling interval based on order progress
  ├─ If subprocess is running: poll more frequently (10s vs 60s)
  ├─ Requires: Subprocess to write heartbeat files to S3
  └─ Trade-off: More complexity, more S3 API calls

Option D: CloudWatch Logs Integration
  ├─ Worker writes output to CloudWatch Logs
  ├─ Orchestrator queries CloudWatch for worker status
  ├─ If logs exist but no callback: worker succeeded, callback failed
  └─ Trade-off: Complex log parsing, CloudWatch API costs
```

### 6.2 Presigned URL Expiry with Long-Running Orders

**Gap Details:**
```
Problem:
  ├─ Presigned URL TTL: job.presign_expiry (default 7200s = 2 hours)
  ├─ Order timeout: order.timeout (can be any value, default 300s)
  ├─ If order.timeout > 7200s:
  │   ├─ Order can run for >2 hours
  │   ├─ Presigned URL expires after 2 hours
  │   └─ Callback fails with 403 Forbidden
  └─ No warning, no adjustment

Scenario:
  ├─ Job submitted with order.timeout = 14400s (4 hours)
  ├─ Presigned URL: valid for 7200s (2 hours)
  ├─ Order execution: T+0 to T+14400
  │   ├─ T+3600: Subprocess running, half done
  │   ├─ T+7200: Subprocess running, 3/4 done
  │   │   └─ Presigned URL expired (no warning)
  │   ├─ T+10800: Subprocess finishes successfully
  │   ├─ T+10800: send_callback() fails with 403 Forbidden
  │   ├─ Retries 3x (all fail)
  │   └─ Worker exits normally
  ├─ Orchestrator sees no result.json
  ├─ Watchdog polls every 60s for remaining 3600s (1 hour)
  ├─ T+14400: Watchdog timeout exceeded, writes timed_out result.json
  ├─ Order marked TIMED_OUT (even though it SUCCEEDED)
  └─ Job status: TIMED_OUT (WRONG)

Detection:
  ├─ No clear indication that callback URL expired
  ├─ Worker log would show 403 error (if checked)
  └─ Job status incorrect (TIMED_OUT vs SUCCEEDED)

Frequency:
  ├─ Depends on job timeout distribution
  ├─ If most jobs < 2 hours: rare
  ├─ If many jobs 2+ hours: common problem
```

**Recommended Fix:**
```
Option A: Adjust Presigned URL TTL
  ├─ Increase job.presign_expiry to match or exceed max order.timeout
  ├─ Implementation: presign_expiry = max(7200, max(order.timeouts) + buffer)
  └─ Trade-off: Longer presigned URLs = longer attack window if URL leaked

Option B: Refresh Presigned URL (Complex)
  ├─ Store template for presigned URL generation in SSM/env
  ├─ Worker refreshes URL periodically (e.g., every 1 hour)
  ├─ Update CALLBACK_URL in environment before subprocess
  └─ Trade-off: Adds complexity, requires credentials in worker

Option C: Reduce Order Timeouts
  ├─ Split long-running orders into smaller steps
  ├─ Use dependencies to chain steps
  ├─ Each step < presigned URL TTL
  └─ Trade-off: More complex job definitions

Option D: Use S3 SDK Inside Callback
  ├─ Store AWS credentials in worker env (encrypted)
  ├─ Worker calls S3 SDK directly instead of presigned URL
  ├─ Requires: Worker IAM role with S3 write permission (current: read-only)
  └─ Trade-off: Increases worker IAM scope, expires with worker credentials
```

### 6.3 Orchestrator Lock Staleness

**Gap Details:**
```
Problem:
  ├─ Orchestrator acquires lock: lock:{run_id}
  ├─ Lock TTL: typically 3600s (1 hour)
  ├─ If orchestrator Lambda crashes after lock acquisition:
  │   ├─ Lock remains in DynamoDB with status="active"
  │   ├─ Lock TTL clock ticking
  │   └─ No mechanism to detect stale lock
  ├─ Next S3 callback (result.json from worker):
  │   ├─ New orchestrator attempts acquire_lock()
  │   ├─ Condition check: NOT_EXISTS OR status="completed"
  │   ├─ Lock exists with status="active" → BOTH CONDITIONS FALSE
  │   ├─ Lock acquisition FAILS
  │   ├─ Orchestrator returns {"status": "skipped"}
  │   └─ Run processing STOPS
  └─ Run stuck for entire lock TTL (1 hour)

Timeline Example:
  T0:00  Orchestrator A acquires lock(run_id=abc, ttl=T1:00)
  T0:30  Orchestrator A crashes (Lambda timeout during execution)
  T1:00  Worker finishes, writes result.json
         └─ S3 ObjectCreated event
  T1:01  Orchestrator B invokes (S3 event)
         ├─ Tries acquire_lock(abc)
         ├─ Lock exists with status="active" (from A at T0:00)
         ├─ Fails to acquire
         └─ Returns "skipped"
  T1:02 to T2:00: All callbacks processed but skipped
  T2:00  Lock TTL expires
         └─ DynamoDB auto-deletes (TTL attribute)
  T2:01  Next S3 callback (if any)
         ├─ Orchestrator C invokes
         ├─ acquire_lock() succeeds (lock doesn't exist)
         └─ Run processing resumes

Severity:
  ├─ High: Complete stop of run processing
  ├─ Duration: Up to 1 hour (lock TTL)
  ├─ Visibility: Only if you query lock table
  └─ Silent failure: System appears to be processing, actually skipped
```

**Recommended Fix:**
```
Option A: Reduce Lock TTL
  ├─ Decrease from 3600s to 30-60s
  ├─ Detection time: up to 60s after crash
  └─ Risk: Normal orchestrator execution > 60s might lose lock prematurely

Option B: Heartbeat/Lease Pattern
  ├─ Orchestrator updates lock.updated_at every 30s
  ├─ Acquisition check: if (now - updated_at) > threshold → lock is stale
  ├─ Detection time: threshold (e.g., 5 min)
  └─ Requires: Async update task, more complexity

Option C: Orchestrator ID Validation
  ├─ Store orchestrator_id in lock record
  ├─ Check if Lambda instance still alive (CloudWatch, X-Ray tracing)
  ├─ If not alive: treat lock as stale
  └─ Requires: External monitoring, complex implementation

Option D: Step Function Orchestration
  ├─ Replace Lambda orchestration with Step Function
  ├─ Step Function manages lock lifecycle explicitly
  ├─ Automatic timeout handling
  └─ Trade-off: Different architecture, cost implications
```

### 6.4 SOPS Key Expiration During Worker Execution

**Gap Details:**
```
Problem:
  ├─ SOPS private key stored in SSM Parameter Store
  ├─ Expiration policy: 2 hours (TTL attribute)
  ├─ If worker doesn't retrieve key within 2 hours:
  │   ├─ Key is auto-deleted from SSM
  │   └─ Worker crashes when trying to fetch
  ├─ If order.timeout > 2 hours:
  │   ├─ SOPS key expires while worker is running
  │   ├─ Worker doesn't need key until execution end (decryption happens early)
  │   └─ But key is deleted before worker even starts

Timeline:
  T0:00  init_job generates SOPS keypair
         ├─ Private key: /{prefix}/sops-keys/{run_id}/{order_num}
         └─ TTL: T2:00 (2 hours from now)
  T0:05  Orchestrator dispatches Lambda worker
  T0:10  Lambda cold start, downloading exec.zip, starting execution
  T0:20  Worker tries to fetch SOPS key from SSM
         ├─ SSM.GetParameter() succeeds (T < T2:00)
         ├─ Key available
         └─ Decryption proceeds
  T1:30  Worker still executing commands (order.timeout = 5400s)
  T2:00  SOPS key TTL expires
         └─ SSM auto-deletes /{prefix}/sops-keys/{run_id}/{order_num}
  T2:05  Worker finishes, tries to callback
         ├─ But decryption happened at T0:20
         └─ No issue here

  BUT if delayed execution:
  T0:00  init_job creates key, TTL=T2:00
  T0:05  Orchestrator dispatches Lambda
  T0:10  Lambda queued (throttled by concurrent executions limit)
  T1:50  Lambda finally starts execution
  T1:55  Worker tries to fetch SOPS key
         ├─ Key expired at T2:00
         ├─ SSM.GetParameter() returns ParameterNotFound
         ├─ Worker crashes with exception
         └─ No callback sent

Result:
  ├─ Order stuck in RUNNING state
  ├─ Watchdog timeout is only detection
  └─ Order marked TIMED_OUT (even if crash happened immediately)

Risk Factors:
  ├─ Lambda concurrency throttling
  ├─ CodeBuild queue depth (high-volume builds)
  ├─ SSM delays (instance not immediately available)
  └─ Network issues (EC2 instance unreachable)
```

**Recommended Fix:**
```
Option A: Extend SOPS Key TTL
  ├─ Increase TTL from 2 hours to match max order.timeout + buffer
  ├─ Implementation: sops.store_sops_key_ssm(..., ttl_hours=max_hours + 1)
  └─ Risk: Longer-lived keys = longer attack window if compromised

Option B: Lazy Key Storage
  ├─ Don't delete SOPS key until after order completes
  ├─ After finalization: delete all SOPS keys (current behavior)
  ├─ Remove TTL auto-expiration (rely on cleanup only)
  └─ Risk: Keys persist if finalization fails; cleanup job needed

Option C: Embed Key in Bundle
  ├─ Instead of SSM, store encrypted private key inside exec.zip
  ├─ Worker decrypts key from bundle (double encryption)
  ├─ No SSM dependency for key retrieval
  └─ Trade-off: Larger bundle size, security concern (key on disk)

Option D: Multiple Key Stores
  ├─ Primary: SSM (fast retrieval)
  ├─ Fallback: Environment variable or S3 (slower)
  ├─ Worker tries SSM first, falls back if expired
  └─ Requires: Multiple copies of key
```

### 6.5 DynamoDB Conditional Write Failures in Dispatch

**Gap Details:**
```
Current behavior:
  ├─ Orchestrator reads orders from DynamoDB
  ├─ Evaluates dependencies
  ├─ Dispatches worker (Lambda invoke, CodeBuild start, SSM send_command)
  ├─ AFTER dispatch: Updates order status → RUNNING in DynamoDB
  ├─ If update fails (throttled, permission error):
  │   ├─ Worker is already running
  │   ├─ Order status still QUEUED in DynamoDB
  │   ├─ Next orchestrator invocation reads QUEUED
  │   ├─ May re-dispatch same order (duplicate execution)
  │   └─ No check for in-flight executions

Example:
  T0: Orchestrator reads order, status=QUEUED
  T1: Orchestrator invokes worker Lambda
  T2: DynamoDB write FAILS (ProvisionedThroughputExceededException)
  T3: Orchestrator exception handling logs error, continues
  T4: Worker running in Lambda
  T5: Next callback from different order triggers orchestrator
  T6: New orchestrator reads orders, sees order still QUEUED
  T7: New orchestrator invokes SAME order again (duplicate)
  T8: Two Lambda instances running same order
  T9: Both write result.json (race condition on S3 object)
  T10: First callback: order marks SUCCEEDED
  T11: Second callback: S3 ObjectCreated event triggered again (duplicate result)
  T12: Orchestrator re-processes, sees SUCCEEDED, no issue
       (idempotent for reads, but wasteful)

Impact:
  ├─ Duplicate order execution
  ├─ Wasted compute resources
  ├─ Potential double-execution of side effects
  │   ├─ If order deploys infrastructure: double-apply Terraform
  │   ├─ If order triggers payment: double-charge
  │   └─ Depends on order idempotency
  └─ Results in order_events table may have duplicate entries

Frequency:
  ├─ Depends on DynamoDB write throughput and throttling
  ├─ Higher likelihood during traffic spikes
  ├─ Retry logic in dynamodb.py may mitigate (4 retries, exponential backoff)
  └─ But after retries exhausted, exception propagates
```

**Recommended Fix:**
```
Option A: Pre-Update Before Dispatch
  ├─ Update order status to RUNNING BEFORE invoking worker
  ├─ This atomically marks worker as in-flight
  ├─ If update fails: don't invoke worker (no dispatch)
  ├─ Implementation: reorder operations in dispatch
  └─ Trade-off: Worker not yet running, but DynamoDB claims it is

Option B: Idempotent Dispatch Keys
  ├─ Store dispatch request ID in DynamoDB (RequestId for Lambda, BuildId for CodeBuild)
  ├─ Before dispatch: check if request ID already exists
  ├─ If exists: don't re-dispatch, use existing RequestId
  └─ Prevents duplicate invocations

Option C: Eventual Consistency Delay
  ├─ After dispatch: wait for DynamoDB write with exponential backoff
  ├─ Give orchestrator time to complete before next event
  └─ Risk: adds latency to orchestration loop

Option D: Separate Status Field
  ├─ Add `dispatch_attempted=true` flag separate from `status`
  ├─ Mark flag before dispatch, then update status after
  ├─ If flag exists: don't re-dispatch
  └─ Requires: Schema migration
```

---

## 7. SUMMARY TABLE OF GAPS & SEVERITY

| Gap | Severity | Detection Method | Impact | TTM (Time To Mitigation) |
|-----|----------|------------------|--------|-------------------------|
| Callback URL expiry (timeout > 2hr) | HIGH | Order marked TIMED_OUT incorrectly | Wrong job status | 1-3 minutes (watchdog) |
| Presigned URL signature validation fails | HIGH | Callback fails silently | Order stuck in RUNNING | 1+ order.timeout |
| Orchestrator lock staleness (crash) | HIGH | Manual inspection of DynamoDB | Run processing stops | 1 hour (lock TTL) |
| SOPS key expires before retrieval | MEDIUM | Worker crashes with ParameterNotFound | Order marked TIMED_OUT | Order.timeout + watchdog |
| DynamoDB write fails during dispatch | MEDIUM | Duplicate order execution | Order runs twice | Until next finalization |
| Callback network failure | MEDIUM | Order stuck in RUNNING | Waits for watchdog | Order.timeout + 60s |
| CodeBuild OOM before callback | MEDIUM | No indication from CodeBuild service | Order stuck in RUNNING | Order.timeout |
| SSM agent down on all targets | MEDIUM | SSM command never executes | Order stuck in RUNNING | Order.timeout |
| Result.json malformed JSON | LOW | JSONDecodeError (unhandled) | Orchestrator exception | Exception handling |
| Event collection failures | LOW | Events silently dropped | Missing event history | Masked by status |
| Dependency cycle detection | LOW | Orders hang indefinitely | Infinite waiting | Never (manual intervention) |
| S3 event race (duplicate callback) | LOW | Multiple orchestrator invocations | Idempotent reads (OK) | Mitigated by lock |

---

## 8. RECOMMENDATIONS FOR IMPLEMENTATION

**Priority 1: Critical Fixes (address immediately)**
1. Validate presigned URL TTL >= max(order.timeouts)
2. Add callback fallback: if callback fails after retries, write to DynamoDB
3. Reduce orchestrator lock TTL from 3600s to 30-60s
4. Pre-update order status BEFORE dispatch (prevent duplicate executions)

**Priority 2: Important Improvements (within 1-2 sprints)**
1. Add watchdog jitter (avoid thundering herd at 60s boundaries)
2. Implement SOPS key TTL validation at job submission time
3. Add cycle detection in dependency graph validation
4. Query CodeBuild/SSM service status (supplement callback detection)

**Priority 3: Nice-to-Have (future enhancements)**
1. CloudWatch Logs integration for worker output
2. Adaptive watchdog polling (faster when worker is running)
3. Presigned URL refresh mechanism for long-running orders
4. VCS integration for PR comments (mentioned in CLAUDE.md, not implemented)

---

## 9. TESTING RECOMMENDATIONS

Create test cases for:
1. Order.timeout > presign_expiry (callback URL expiry)
2. Orchestrator crash during lock acquisition (manual: kill process, check recovery)
3. DynamoDB throttling during dispatch (mock: simulate ProvisionedThroughputExceededException)
4. SOPS key expires before retrieval (mock: set early TTL, delay worker start)
5. Callback network failure (mock: 503 Service Unavailable)
6. CodeBuild OOM/crash (mock: CodeBuild process exits before callback)
7. SSM agent down (mock: sed_command on non-existent target)
8. Dependency cycles (job def with A→B→A)
9. Malformed result.json (write invalid JSON to S3 key)
10. Race condition: concurrent orchestrator invocations (multiple S3 events)

---

## File References

All critical files mentioned in this research:
- `/home/gary/project/repos/aws-execution-engine/src/init_job/handler.py`
- `/home/gary/project/repos/aws-execution-engine/src/orchestrator/handler.py`
- `/home/gary/project/repos/aws-execution-engine/src/orchestrator/dispatch.py`
- `/home/gary/project/repos/aws-execution-engine/src/worker/handler.py`
- `/home/gary/project/repos/aws-execution-engine/src/worker/run.py`
- `/home/gary/project/repos/aws-execution-engine/src/worker/callback.py`
- `/home/gary/project/repos/aws-execution-engine/src/watchdog_check/handler.py`
- `/home/gary/project/repos/aws-execution-engine/src/common/s3.py` (presigned URL generation)
- `/home/gary/project/repos/aws-execution-engine/src/common/sops.py` (key management)
- `/home/gary/project/repos/aws-execution-engine/src/common/dynamodb.py` (lock mechanism)
- `/home/gary/project/repos/aws-execution-engine/infra/02-deploy/step_functions.tf` (watchdog definition)
- `/home/gary/project/repos/aws-execution-engine/infra/02-deploy/ssm_document.tf` (SSM callback)
- `/home/gary/project/repos/aws-execution-engine/.github/workflows/deploy.yml` (deployment pipeline)

---

**RESEARCH2.md Complete**
