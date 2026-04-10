# Architecture Diagram

Visual overview of the aws-exe-sys system. For an interactive version with full details, open [architecture-diagram.html](architecture-diagram.html) in a browser.

---

## System Overview

```mermaid
flowchart LR
    VCS["Caller<br><i>SigV4 + JWT</i>"]
    AWS["Your AWS Account<br><i>Validates, queues, executes</i>"]
    Results["Results<br><i>PR comment + logs</i>"]

    VCS -- "POST /init<br>job payload" --> AWS
    AWS -- "PR comment<br>+ status update" --> Results

    style VCS fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style AWS fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style Results fill:#003d2b,stroke:#10b981,color:#e2e8f0
```

**Key properties:**
- Fully serverless -- nothing runs 24/7
- All working data auto-cleans via TTL and S3 lifecycle
- Single Docker image for all Lambda functions and CodeBuild

---

## Part 1: init_job

Job intake -- validates, packages, and queues orders.

```mermaid
sequenceDiagram
    participant VCS as Caller
    participant APIGW as API Gateway
    participant IJ as init_job Lambda
    participant SSM as SSM Parameter Store
    participant S3 as S3 (internal)
    participant DDB as DynamoDB
    participant PR as VCS PR

    VCS->>APIGW: POST /init (job payload)
    APIGW->>IJ: Invoke (AWS_PROXY)

    Note over IJ: Validate orders<br>(cmds, timeout, code source)

    IJ->>IJ: Fetch code (S3/Git)<br>Fetch secrets (SSM/SecretsManager)<br>Encrypt with SOPS (age key)<br>Generate presigned callback URLs

    IJ->>SSM: Store SOPS private key<br>as SecureString<br>/aws-exe-sys/sops-keys/<run_id>/<order_num>

    IJ->>S3: PUT exec.zip per order<br>tmp/exec/<run_id>/<order_num>/exec.zip

    IJ->>DDB: Insert orders (status: queued)<br>Write order_events (trace_id)

    IJ->>PR: Post initial PR comment<br>(order list with "queued" status)

    IJ->>S3: Write init trigger<br>tmp/callbacks/runs/<run_id>/0000/result.json

    Note over S3: S3 ObjectCreated event<br>triggers orchestrator
```

---

## Part 2: execute_orders

Event-driven orchestration -- resolves dependencies and dispatches work.

```mermaid
sequenceDiagram
    participant S3 as S3 (internal)
    participant Orch as orchestrator Lambda
    participant DDB as DynamoDB
    participant SSM as SSM Parameter Store
    participant Worker as Worker (Lambda/CodeBuild)
    participant SFN as Watchdog (Step Function)
    participant PR as VCS PR

    S3-->>Orch: ObjectCreated event<br>(result.json written)

    Orch->>DDB: Acquire lock (conditional put)<br>lock:<run_id>

    Orch->>DDB: Read all orders for run_id
    Orch->>S3: Check for new result.json files
    Orch->>DDB: Update completed order statuses

    Note over Orch: Evaluate dependency graph<br>Ready / Failed / Waiting

    par Dispatch ready orders
        Orch->>Worker: Invoke Lambda or start CodeBuild
        Orch->>SFN: Start watchdog (polls every 60s)
        Orch->>DDB: Update status → running
    end

    Worker->>SSM: Fetch SOPS private key<br>/aws-exe-sys/sops-keys/<run_id>/<order_num>
    Worker->>Worker: Decrypt SOPS → env vars<br>Run commands<br>Capture stdout/stderr

    Worker->>S3: PUT result.json via presigned URL<br>(status + log)

    S3-->>Orch: ObjectCreated event (loop)

    Note over Orch: Re-evaluate graph<br>Dispatch next wave

    Orch->>S3: Write done marker<br>(when all orders complete)
    Orch->>SSM: Delete SOPS key parameters<br>(cleanup)
    Orch->>PR: Final PR comment with summary
    Orch->>DDB: Release lock
```

---

## AWS Resources

```mermaid
graph TB
    subgraph Ingress
        APIGW["API Gateway<br><i>aws-exe-sys-api</i><br>POST /init"]
        InitJob["init_job Lambda<br><i>300s · 512MB</i>"]
    end

    subgraph Storage
        S3Int["S3 Internal<br><i>exec.zip + callbacks</i><br>1-day lifecycle"]
        S3Done["S3 Done<br><i>completion markers</i><br>1-day lifecycle"]
        S3State["S3 State<br><i>TF state</i><br>permanent"]
        SSM["SSM Parameter Store<br><i>SOPS private keys</i><br>SecureString, cleanup on finalize"]
        Orders["DynamoDB orders<br><i>PK: run_id:order_num</i><br>TTL: 1 day"]
        Events["DynamoDB order_events<br><i>PK: trace_id, SK: order:epoch</i><br>TTL: 90 days"]
        Locks["DynamoDB locks<br><i>PK: lock:run_id</i><br>TTL: dynamic"]
    end

    subgraph Orchestration
        Orch["orchestrator Lambda<br><i>600s · 512MB</i>"]
        Watchdog["watchdog Step Function<br><i>polls every 60s</i>"]
        WDCheck["watchdog_check Lambda<br><i>60s · 256MB</i>"]
    end

    subgraph Execution
        WorkerL["worker Lambda<br><i>600s · 1024MB</i>"]
        WorkerCB["worker CodeBuild<br><i>long-running orders</i>"]
    end

    subgraph Infrastructure
        ECR["ECR Repository<br><i>single image, all functions</i>"]
        IAM["IAM Roles<br><i>per-function least privilege</i>"]
    end

    APIGW --> InitJob
    InitJob --> S3Int
    InitJob --> Orders
    InitJob -- "store SOPS key" --> SSM
    S3Int -- "S3 event" --> Orch
    Orch --> WorkerL
    Orch --> WorkerCB
    Orch --> Watchdog
    Watchdog --> WDCheck
    WorkerL -- "fetch SOPS key" --> SSM
    WorkerCB -- "fetch SOPS key" --> SSM
    WorkerL -- "presigned PUT" --> S3Int
    WorkerCB -- "presigned PUT" --> S3Int
    Orch -- "cleanup keys" --> SSM
    Orch --> S3Done

    style APIGW fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style InitJob fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style Orch fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style WDCheck fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style WorkerL fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style WorkerCB fill:#2d1052,stroke:#a855f7,color:#e2e8f0
    style Watchdog fill:#2d1052,stroke:#a855f7,color:#e2e8f0
    style S3Int fill:#003d2b,stroke:#10b981,color:#e2e8f0
    style S3Done fill:#003d2b,stroke:#10b981,color:#e2e8f0
    style S3State fill:#003d2b,stroke:#10b981,color:#e2e8f0
    style Orders fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style Events fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style Locks fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style SSM fill:#3d2b00,stroke:#eab308,color:#e2e8f0
    style ECR fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
    style IAM fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
```

---

## Deployment Pipeline

```mermaid
flowchart LR
    B["1. Bootstrap<br><i>S3 state bucket</i><br>Terraform, local state"]
    E["2. ECR<br><i>Create repository</i><br>Terraform, remote state"]
    D["3. Docker<br><i>Build & push image</i><br>SHA + latest tags"]
    T["4. Deploy<br><i>All AWS resources</i><br>Terraform, remote state"]
    S["5. Smoke Tests<br><i>Verify API, Lambda,</i><br><i>DDB, S3, ECR</i>"]
    A["6. Archive<br><i>Zip TF + tfvars</i><br>Upload for teardown"]

    B --> E --> D --> T --> S --> A

    style B fill:#003d2b,stroke:#10b981,color:#e2e8f0
    style E fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
    style D fill:#2d1052,stroke:#a855f7,color:#e2e8f0
    style T fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style S fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style A fill:#3d2b00,stroke:#eab308,color:#e2e8f0
```
