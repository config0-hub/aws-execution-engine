# AWS Execution Engine — Lambda Structure & Shared Pattern Research

**Task**: Map Lambda function structure and identify opportunities for shared pattern extraction and code deduplication.

**Scope**: All Lambda modules, common library, and infrastructure code.

---

## 1. Architecture Overview

### Execution Flow

The system has **three Lambda entry points** that form a two-part execution pipeline:

```
Part 1a (init_job):     Generic job submission
                        ├─ Normalize event (direct invoke, SNS, API GW)
                        ├─ Validate orders
                        ├─ Repackage with SOPS encryption
                        ├─ Upload exec.zip to S3
                        ├─ Insert into DynamoDB orders table
                        └─ Trigger orchestrator via S3 init event

Part 1b (ssm_config):   SSM-only submission (separate entry point)
                        ├─ Normalize event (direct invoke, SNS, API GW)
                        ├─ Validate orders
                        ├─ Repackage with NO SOPS (env dict instead)
                        ├─ Upload exec.zip to S3
                        ├─ Insert into DynamoDB orders table
                        └─ Trigger orchestrator via S3 init event

Part 2 (orchestrator):  S3-event triggered execution coordinator
                        ├─ Parse run_id from S3 key
                        ├─ Acquire distributed lock (per run_id)
                        ├─ Read orders from DynamoDB
                        ├─ Evaluate dependency graph (cascade failures)
                        ├─ Dispatch ready orders (Lambda / CodeBuild / SSM)
                        ├─ Start watchdog Step Function per order
                        └─ Finalize when all orders complete

Watchdog (Step Fn):     Per-order timeout safety
                        ├─ Poll S3 every 60s for result.json
                        └─ Write timed_out if worker unresponsive
```

### Bootstrap Flow

All Lambda functions use a **single Docker image** with different entry points:

```
bootstrap_handler.py (always runs first)
    ├─ Download engine code tarball via presigned URL
    │   ├─ Priority 1: engine_code_url from event payload
    │   ├─ Priority 2: ENGINE_CODE_URL env var
    │   └─ Priority 3: ENGINE_CODE_SSM_PATH → read from SSM
    ├─ Extract to /tmp/engine/
    ├─ Extend sys.path and PATH
    └─ Delegate to actual handler via:
       ├─ ENGINE_HANDLER env var (e.g., "src.init_job.handler")
       └─ ENGINE_HANDLER_FUNC env var (e.g., "handler")
```

This allows **code-at-rest** — the engine code itself is versioned and deployed to S3, not baked into the Docker image.

---

## 2. Handler Patterns (All Entry Points)

### 2.1 init_job.handler (`src/init_job/handler.py`)

**Lines: 187–223**

```python
def handler(event: Dict[str, Any], context: Any = None) -> dict:
    is_apigw = "httpMethod" in event or ...
    try:
        payload = _normalize_event(event)
        if "_apigw_error" in payload:
            return _apigw_response(405, {...})
        job_parameters_b64 = payload.get("job_parameters_b64", "")
        if not job_parameters_b64:
            result = {"status": "error", ...}
            return _apigw_response(400, result) if is_apigw else result
        result = process_job_and_insert_orders(...)
        if is_apigw:
            code = 200 if result.get("status") == "ok" else 400
            return _apigw_response(code, result)
        return result
    except Exception as e:
        logger.exception("init_job failed")
        result = {"status": "error", "error": str(e)}
        return _apigw_response(500, result) if is_apigw else result
```

**Pattern**: Normalize event → Extract payload → Validate → Call core function → Format response

**Inputs**: Supports 3 invocation sources:
1. **Direct Lambda invoke**: `{"job_parameters_b64": "...", ...}`
2. **SNS trigger**: `{"Records": [{"Sns": {"Message": "{...}"}}]}`
3. **API Gateway**: `{"httpMethod": "POST", "body": "..."}` (format 1.0 & 2.0)

**Outputs**: Direct invoke returns `dict`; API GW returns AWS Lambda proxy response format

---

### 2.2 ssm_config.handler (`src/ssm_config/handler.py`)

**Lines: 143–175**

**IDENTICAL PATTERN to init_job.handler**, only difference:
- Calls `process_ssm_job()` instead of `process_job_and_insert_orders()`
- Uses `SsmJob.from_b64()` instead of `Job.from_b64()`

**Lines that are 100% identical**:
- `_normalize_event()` — lines 31–61 (init_job) vs 31–61 (ssm_config)
- `_apigw_response()` — lines 178–184 (init_job) vs 134–140 (ssm_config)
- Handler outer structure — pattern is identical

---

### 2.3 orchestrator.handler (`src/orchestrator/handler.py`)

**Lines: 111–138**

```python
def handler(event: Dict[str, Any], context: Any = None) -> dict:
    run_id = ""
    if "Records" in event:
        for record in event["Records"]:
            s3_key = record.get("s3", {}).get("object", {}).get("key", "")
            run_id = _parse_run_id_from_s3_key(s3_key)
            if run_id:
                break

    if not run_id:
        logger.error("Could not extract run_id from event")
        return {"status": "error", "message": "Missing run_id"}

    if not acquire_lock(run_id, flow_id="", trace_id=""):
        logger.info("Lock not acquired, another instance is handling")
        return {"status": "skipped", "message": "Lock not acquired"}

    try:
        return execute_orders(run_id)
    except Exception as e:
        logger.exception("Orchestrator failed for run_id=%s", run_id)
        release_lock(run_id)
        return {"status": "error", "message": str(e)}
```

**Pattern**: Parse S3 event → Extract run_id → Acquire lock → Call core function → Handle errors

**Differs from init_job/ssm_config**:
- No event normalization (S3 events are fixed format)
- Has distributed lock pattern
- No API Gateway response formatting

---

### 2.4 worker.handler (`src/worker/handler.py`)

**Lines: 12–32**

```python
def handler(event: Dict[str, Any], context: Any = None) -> dict:
    s3_location = event.get("s3_location", "")
    internal_bucket = event.get("internal_bucket", "")

    sops_key_ssm_path = event.get("sops_key_ssm_path", "")
    if sops_key_ssm_path:
        os.environ["SOPS_KEY_SSM_PATH"] = sops_key_ssm_path

    if not s3_location:
        logger.error("Missing s3_location in event")
        return {"status": "failed", "error": "Missing s3_location"}

    try:
        status = run(s3_location, internal_bucket)
        return {"status": status}
    except Exception as e:
        logger.exception("Worker failed")
        return {"status": "failed", "error": str(e)}
```

**Pattern**: Extract parameters → Set env vars → Call core function → Return status

**Minimal** — just a thin wrapper around `run()` from `src/worker/run.py`

---

### 2.5 watchdog_check.handler (`src/watchdog_check/handler.py`)

**Lines: 12–63**

```python
def handler(event: Dict[str, Any], context: Any = None) -> dict:
    run_id = event["run_id"]
    order_num = event["order_num"]
    timeout = event["timeout"]
    start_time = event["start_time"]
    internal_bucket = event["internal_bucket"]

    exists = s3_ops.check_result_exists(...)
    if exists:
        return {"done": True}

    now = int(time.time())
    if now > start_time + timeout:
        logger.warning("Timeout exceeded...")
        s3_ops.write_result(bucket=..., status="timed_out", log="...")
        return {"done": True}

    return {"done": False}
```

**Pattern**: Extract parameters → Check state → Return signal

**Invoked by Step Function** — runs on 60s loop until returns `{"done": True}`

---

## 3. Handler Pattern Summary

| Handler | Entry Point | Event Source | Duplication | Opportunity |
|---------|-------------|--------------|-------------|-------------|
| init_job | Lambda | Direct/SNS/API GW | _normalize_event, _apigw_response | Extract to common |
| ssm_config | Lambda | Direct/SNS/API GW | _normalize_event, _apigw_response | Extract to common |
| orchestrator | Lambda | S3 ObjectCreated | None specific | — |
| worker | Lambda | invoke (dispatch) | None specific | — |
| watchdog_check | Lambda | Step Function loop | None specific | — |

**Key Insight**: `init_job` and `ssm_config` are nearly identical handlers. The **only difference is which core function they call** (`process_job_and_insert_orders` vs `process_ssm_job`). Everything else (event normalization, error handling, response formatting) is identical.

---

## 4. Core Processing Functions

### 4.1 Order Processing (init_job vs ssm_config)

#### init_job.repackage.repackage_orders() (Lines 87–162)
- Phases: Group git orders → Clone repos → Process each order → Upload → Return metadata
- _process_order() (lines 24–84): Fetch credentials → Generate SOPS keypair → Encrypt → Zip

#### ssm_config.repackage.repackage_ssm_orders() (Lines 84–150)
- **IDENTICAL STRUCTURE** to repackage_orders()
- Phases: Group git orders → Clone repos → Process each order → Upload → Return metadata
- _process_ssm_order() (lines 24–81): Fetch credentials → **NO SOPS** → Build env dict → Zip

**Code Duplication Analysis**:

```
repackage_orders()       vs      repackage_ssm_orders()
────────────────────────────────────────────────────────

[Lines 100–106 identical]  Phase 1: Group git orders
[Lines 108–112 identical]  Resolve git credentials
[Lines 114–134 identical]  Clone & process loop
[Lines 137–149 identical]  Process S3-sourced orders

_process_order()         vs      _process_ssm_order()
────────────────────────────────────────────────────

[Lines 36–40 identical]   Fetch SSM/secret values
[Lines 42–48 identical]   Generate presigned URL
[Lines 50–56 DIFFERENT]   SOPS handling (init_job only)
[Lines 51–62 DIFFERENT]   OrderBundler call (different params)
[Lines 71–75 identical]   Zip directory

Total identical code: ~220 lines
```

**Opportunity**: Extract to `_process_order_base()` with conditional SOPS handling.

---

### 4.2 Order Insertion (init_job vs ssm_config)

#### init_job.insert.insert_orders() (Lines 12–90)
- Builds `git_b64` if using git source
- Constructs `order_data` dict
- Writes to DynamoDB
- Writes job-level event

#### ssm_config.insert.insert_ssm_orders() (Lines 11–73)
- **NO git_b64** (SSM orders have different code source pattern)
- Constructs `order_data` dict (similar fields, plus `ssm_targets`, `env_dict`)
- Writes to DynamoDB
- Writes job-level event (identical)

**Code Duplication Analysis**:

```
insert_orders()          vs      insert_ssm_orders()
───────────────────────────────────────────────────

[Lines 22–23 identical]   TTL calculation
[Lines 25–28 identical]   Loop through orders
[Lines 30–43 DIFFERENT]   git_b64 construction (SSM has no git)
[Lines 45–76 DIFFERENT]   order_data building (different fields)
[Lines 78–90 identical]   Write job-level event

Total identical code: ~30 lines
```

**Opportunity**: Extract shared fields to helper, conditionally add git_b64 and ssm-specific fields.

---

## 5. Order/Job Model Duplication

### Two parallel model hierarchies:

#### Common Module (models.py)
- `Job` + `Order`
- Shared by init_job

#### SSM Config Module (ssm_config/models.py)
- `SsmJob` + `SsmOrder`
- Shared by ssm_config

**Field Comparison**:

```
              Order                    SsmOrder
              ─────                    ────────
cmds          ✓                        ✓
timeout       ✓                        ✓
order_name    ✓                        ✓
git_repo      ✓                        ✓
git_folder    ✓                        ✓
commit_hash   ✓                        ✓
s3_location   ✓                        ✓
env_vars      ✓                        ✓
ssm_paths     ✓                        ✓
secret_manager_paths  ✓                ✓
sops_key      ✓ (init_job specific)    —
execution_target  ✓                    —
queue_id      ✓                        ✓
dependencies  ✓                        ✓
must_succeed  ✓                        ✓
callback_url  ✓                        ✓
ssm_targets   — (hardcoded to "ssm")   ✓ (required)
```

**Methods** (identical in both):
- `to_dict()` — strips None values
- `from_dict()` — filters known fields
- `to_b64()` — JSON encode + base64
- `from_b64()` — base64 decode + JSON parse

**Opportunity**: Merge into single `Order` model with optional SSM-specific fields.

---

## 6. Validation Patterns

### init_job/validate.py (Lines 8–38)
```python
def validate_orders(job: Job) -> List[str]:
    # Check: has orders
    # Check: cmds non-empty
    # Check: timeout positive
    # Check: execution_target valid
    # Check: has code source (s3_location OR git+token)
```

### ssm_config/validate.py (Lines 8–37)
```python
def validate_ssm_orders(job: SsmJob) -> List[str]:
    # Check: has orders
    # Check: cmds non-empty
    # Check: timeout positive
    # Check: ssm_targets required
    # Check: ssm_targets has instance_ids or tags
```

**Difference**: SSM-specific validation for `ssm_targets` instead of code source validation.

**Opportunity**: Merge into single validator with conditional checks.

---

## 7. AWS Client Initialization Patterns

### Scattered across codebase:

#### src/common/dynamodb.py
```python
def _get_table(table_env_var: str, dynamodb_resource=None):
    if dynamodb_resource is None:
        dynamodb_resource = boto3.resource("dynamodb")
    ...
```
**Pattern**: Lazy initialization with optional injection for testing.

#### src/common/s3.py
```python
def _get_client(s3_client=None):
    if s3_client is None:
        s3_client = boto3.client("s3", config=Config(signature_version="s3v4"))
    ...
```
**Pattern**: Same as DynamoDB — lazy init + injection.

#### src/orchestrator/dispatch.py
```python
def _dispatch_lambda(order: dict, ...):
    lambda_client = boto3.client("lambda")

def _dispatch_codebuild(order: dict, ...):
    codebuild_client = boto3.client("codebuild")

def _dispatch_ssm(order: dict, ...):
    ssm_client = boto3.client("ssm")

def _start_watchdog(order: dict, ...):
    sfn_client = boto3.client("stepfunctions")
```
**Pattern**: Direct creation without injection support.

#### src/worker/run.py
```python
s3_client = boto3.client("s3")
ssm = boto3.client("ssm")
```
**Pattern**: Direct creation.

#### src/init_job/handler.py (JWT injection)
```python
import boto3
ssm = boto3.client("ssm")
```
**Pattern**: Direct creation.

**Observation**: Inconsistent patterns — some support testing injection, others don't.

---

## 8. Common Library (src/common/)

### Well-Designed Modules

**dynamodb.py** (248 lines)
- `retry_on_throttle` decorator with exponential backoff
- Separate functions for orders, events, locks tables
- All operations support optional `dynamodb_resource` parameter for testing
- Comprehensive error handling for throttling

**s3.py** (125 lines)
- `_get_client()` abstraction
- Clear separation: upload, presigned URLs, read, write, check operations
- All operations support optional `s3_client` parameter for testing

**models.py** (188 lines)
- Clean dataclass definitions: Job, Order, OrderEvent, LockRecord, OrderRecord
- Status constants clearly defined
- All models support serialization/deserialization

**code_source.py** (200+ lines)
- Git operations: clone_repo, extract_folder, group_git_orders
- Credential fetching: resolve_git_credentials, fetch_ssm_values, fetch_secret_values
- S3 operations: fetch_code_s3, zip_directory
- Shared by both init_job and ssm_config

**sops.py** (100 lines)
- SOPS key generation: _generate_age_key()
- SSM parameter storage: store_sops_key_ssm(), fetch_sops_key_ssm(), delete_sops_key_ssm()
- Good separation of concerns

**bundler.py** (referenced, not fully read)
- OrderBundler class for repackaging with environment variable handling
- Supports SOPS encryption (init_job) and plain env dict (ssm_config)

**trace.py** (21 lines)
- trace_id generation: `generate_trace_id()` → hex(8)
- leg creation: `create_leg()` → `<trace_id>:<epoch>`
- leg parsing: `parse_leg()` → `(trace_id, epoch)`

**flow.py** (14 lines)
- flow_id generation: `generate_flow_id()` → `<username>:<trace_id>-<flow_label>`
- flow_id parsing: `parse_flow_id()` → `(username, trace_id, flow_label)`

**vcs/** (GitHub provider)
- Abstract base: `src/common/vcs/base.py`
- GitHub impl: `src/common/vcs/github.py`
- Supports: get_comments, create_comment, update_comment, delete_comment, find_comment_by_tag
- Designed for future Bitbucket/GitLab providers

**jwt_creds.py** (referenced)
- JWT token verification for cross-account credentials

---

## 9. Cross-Cutting Concerns

### Logging
- **Pattern**: All modules use `logger = logging.getLogger(__name__)`
- **Consistency**: Good
- **Opportunity**: No centralized error logging utilities

### Environment Variables
- **Pattern**: Retrieved via `os.environ.get("AWS_EXE_SYS_*")`
- **Consistency**: Prefix is consistent (`AWS_EXE_SYS_`)
- **Issues**:
  - No validation at startup
  - Some handlers assume env vars exist, others check
  - Missing vars cause runtime errors, not clear startup failures

**Opportunity**: Centralized env var validation at Lambda cold start.

### Error Handling
- **Pattern**: Try-except with logging
- **Consistency**: All handlers use `logger.exception()` + return error dict
- **Issues**:
  - No custom error types
  - Error messages inconsistent (some use "error", some "message")
  - No distinguishing between validation errors vs runtime errors

### Retry Logic
- **Implemented**: DynamoDB throttling retry in `dynamodb.py::retry_on_throttle`
- **Pattern**: Exponential backoff + jitter
- **Coverage**: Only DynamoDB; S3 and other services don't retry
- **Opportunity**: Generalize to other AWS services

---

## 10. Module Internals Summary

### init_job/ (7 files)
- `handler.py`: Entry point
- `validate.py`: Validate orders
- `repackage.py`: Fetch code, SOPS encrypt, zip
- `upload.py`: Upload exec.zip to S3
- `insert.py`: Write orders to DynamoDB
- `pr_comment.py`: PR comment handling (currently disabled)
- `__init__.py`: Empty

**LOC**: ~400 lines code + ~600 lines tests

### orchestrator/ (4 files)
- `handler.py`: Entry point (S3 triggered)
- `evaluate.py`: Evaluate dependency graph
- `dispatch.py`: Dispatch ready orders (Lambda/CodeBuild/SSM)
- `lock.py`: Distributed lock management
- `read_state.py`: Read orders from DynamoDB + S3 callbacks
- `finalize.py`: Mark complete, write done endpoint
- `__init__.py`: Empty

**LOC**: ~400 lines code + ~500 lines tests

### ssm_config/ (5 files)
- `handler.py`: Entry point (same as init_job)
- `models.py`: SsmJob + SsmOrder dataclasses
- `validate.py`: Validate SSM orders
- `repackage.py`: Fetch code, **NO SOPS**, zip
- `insert.py`: Write orders to DynamoDB
- `__init__.py`: Empty

**LOC**: ~250 lines code + ~400 lines tests

### worker/ (3 files)
- `handler.py`: Entry point (minimal)
- `run.py`: Download, decrypt, execute, callback
- `callback.py`: Send callback result to presigned URL
- `__init__.py`: Empty

**LOC**: ~350 lines code + ~400 lines tests

### watchdog_check/ (2 files)
- `handler.py`: Poll for result or timeout
- `__init__.py`: Empty

**LOC**: ~60 lines code + ~200 lines tests

### common/ (10+ files)
- **Core**: models.py, dynamodb.py, s3.py, code_source.py
- **Utilities**: trace.py, flow.py, sops.py, bundler.py, jwt_creds.py
- **VCS**: vcs/base.py, vcs/github.py, vcs/helper.py
- **Other**: __init__.py

**LOC**: ~1500 lines code + ~1000 lines tests

### bootstrap_handler.py (100 lines)
- Single file, not in a module
- Downloads engine code from S3 presigned URL
- Delegates to actual handler

---

## 11. Code Duplication Quantification

### HIGH IMPACT (Same code, multiple places)

| What | Where | Lines | Opportunity |
|------|-------|-------|-------------|
| `_normalize_event()` | init_job, ssm_config | 32 ea | Extract to common |
| `_apigw_response()` | init_job, ssm_config | 6 ea | Extract to common |
| `repackage_orders()` flow | init_job, ssm_config | 60+ ea | Merge with SOPS conditional |
| `_process_order()` logic | init_job, ssm_config | 60+ ea | Merge with SOPS conditional |
| `insert_orders()` logic | init_job, ssm_config | 70+ ea | Merge with conditional fields |
| Model dataclasses | Order, SsmOrder | 40+ ea | Merge with optional fields |
| Job/SsmJob | Job, SsmJob | 30+ ea | Merge with optional fields |

**Total duplicated code**: ~400-500 lines across init_job/ssm_config modules

### MEDIUM IMPACT (Scattered initialization)

| What | Places | Issue |
|------|--------|-------|
| boto3 client creation | dispatch.py, worker/run.py, handler.py | No centralized factory |
| Environment variable access | All handlers | No validation utility |
| Error handling | All handlers | No custom error types |

---

## 12. Test Coverage

- **Unit tests**: Extensive (test_*.py files exist for all modules)
- **Integration tests**: Exist (tests/integration/)
- **Smoke tests**: Exist (tests/smoke/)
- **Test framework**: pytest + moto for AWS mocking
- **Docker tests**: Uses Dockerfile.test pattern

---

## 13. Design Strengths

1. **Separation of concerns**: Clear module boundaries (init_job, orchestrator, worker)
2. **Testability**: All AWS clients support dependency injection
3. **Retry logic**: Exponential backoff + jitter for DynamoDB
4. **Code versioning**: Bootstrap handler allows code-at-rest versioning
5. **Credential security**: SOPS encryption for sensitive data
6. **Distributed coordination**: Lock management for orchestrator safety
7. **Timeout safety**: Step Function watchdog prevents stuck jobs
8. **VCS abstraction**: GitHub provider allows future extensibility
9. **Event modeling**: Clean dataclass definitions with serialization

---

## 14. Design Weaknesses

1. **Duplicate event normalization**: init_job and ssm_config are nearly identical
2. **Duplicate order processing**: ~200 lines of identical code across modules
3. **Duplicate models**: Job/SsmJob and Order/SsmOrder are 90% identical
4. **No centralized error types**: Error handling is ad-hoc
5. **No environment validation**: Missing env vars fail at runtime
6. **Inconsistent AWS client patterns**: Some support injection, others don't
7. **No centralized logging utilities**: Logging scattered across modules
8. **SSM config as separate module**: Could be extension point instead

---

## 15. Refactoring Opportunities (Prioritized)

### PRIORITY 1: Event Normalization (Eliminate ~80 lines)
**File**: `src/common/http_events.py` (new)
```python
def normalize_event(event: dict) -> dict:
    """Extract payload from direct/SNS/API GW invocation."""
    # Current: duplicated in init_job/handler.py and ssm_config/handler.py

def format_apigw_response(status_code: int, body: dict) -> dict:
    """Wrap result in API GW proxy response format."""
    # Current: duplicated in init_job/handler.py and ssm_config/handler.py
```

**Usage**: Both init_job and ssm_config handlers call these.

### PRIORITY 2: Unified Order Processing (Eliminate ~150 lines)
**File**: `src/common/order_repackage.py` (new)
```python
def _process_order_base(
    job: Union[Job, SsmJob],
    order: Union[Order, SsmOrder],
    order_index: int,
    code_dir: str,
    run_id: str,
    trace_id: str,
    flow_id: str,
    internal_bucket: str,
    use_sops: bool = True,  # Conditional SOPS handling
) -> Dict:
    """Shared order processing for init_job and ssm_config."""
    # Current: nearly identical code in
    # - init_job/repackage.py::_process_order()
    # - ssm_config/repackage.py::_process_ssm_order()
```

**Usage**: Both `repackage_orders()` and `repackage_ssm_orders()` call this.

### PRIORITY 3: Merged Job/Order Models (Eliminate ~80 lines)
**File**: Modify `src/common/models.py`
```python
@dataclass
class Order:
    cmds: List[str]
    timeout: int
    # ... common fields
    execution_target: str = "codebuild"
    ssm_targets: Optional[Dict[str, Any]] = None  # Only for SSM
    sops_key: Optional[str] = None  # Only for init_job
    # ... other fields
```

**Migration**: Update ssm_config to reuse `Order` instead of `SsmOrder`.

### PRIORITY 4: Unified Order Insertion (Eliminate ~50 lines)
**File**: Modify `src/common/dynamodb.py`
```python
def insert_orders(
    job: Union[Job, SsmJob],
    run_id: str,
    flow_id: str,
    trace_id: str,
    repackaged_orders: List[Dict],
    internal_bucket: str,
    dynamodb_resource=None,
    order_type: str = "lambda",  # or "ssm"
) -> None:
    """Insert orders with conditional git_b64 and ssm_targets."""
```

**Usage**: Both init_job and ssm_config call this.

### PRIORITY 5: AWS Client Factory (Nice-to-have)
**File**: `src/common/aws_clients.py` (new)
```python
class AWSClientFactory:
    @staticmethod
    def get_dynamodb(resource=None):
        if resource is None:
            resource = boto3.resource("dynamodb")
        return resource

    @staticmethod
    def get_s3(client=None):
        if client is None:
            client = boto3.client("s3", config=Config(signature_version="s3v4"))
        return client

    @staticmethod
    def get_lambda(client=None):
        if client is None:
            client = boto3.client("lambda")
        return client
    # ... etc
```

### PRIORITY 6: Environment Variable Validation (Nice-to-have)
**File**: `src/common/env_validation.py` (new)
```python
def validate_handler_env(required_vars: List[str]) -> None:
    """Fail fast if required env vars are missing."""
    missing = [var for var in required_vars if not os.environ.get(var)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {missing}")
```

**Usage**: Call at top of each handler.

---

## 16. Conclusion

The aws-execution-engine has **excellent architecture** with clear separation between job submission (init_job/ssm_config), orchestration (orchestrator), and execution (worker). The common library is well-designed with good abstractions for DynamoDB, S3, and credentials.

**However**, there is **significant code duplication** between init_job and ssm_config (~400–500 lines):
- Event normalization
- Order repackaging
- Order insertion
- Job/Order model definitions

**This duplication is not a breaking issue** — the code is testable and maintainable as-is. However, refactoring to shared utilities (especially Priority 1 & 2) would:
- **Reduce maintenance burden**: Changes to event handling only need to happen once
- **Improve consistency**: Validation and error handling are identical across entry points
- **Enable future extensions**: SSM config could be reimplemented as a parameter to the core handlers rather than a separate module
- **Make testing easier**: Shared utilities can be tested independently

**Recommendation**: Implement Priority 1 (event normalization) immediately as a low-risk, high-value change. Combine with Priority 2 (order processing) for a comprehensive refactor of init_job/ssm_config modules.
