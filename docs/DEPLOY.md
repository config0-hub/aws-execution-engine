# Deploy

## GitHub Actions Workflow

Single workflow (`deploy.yml`) handles the entire deploy pipeline in 6 steps.

```mermaid
flowchart LR
    S1["Step 1<br><i>Bootstrap</i>"]
    S2["Step 2<br><i>ECR Repo</i>"]
    S3["Step 3<br><i>Docker Build</i>"]
    S4["Step 4<br><i>Deploy System</i>"]
    S5["Step 5<br><i>Smoke Tests</i>"]
    S6["Step 6<br><i>Archive</i>"]

    S1 --> S2 --> S3 --> S4 --> S5 --> S6

    style S1 fill:#2d1052,stroke:#a855f7,color:#e2e8f0
    style S2 fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style S3 fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
    style S4 fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style S5 fill:#003d2b,stroke:#10b981,color:#e2e8f0
    style S6 fill:#3d2b00,stroke:#eab308,color:#e2e8f0
```

### Inputs

| Parameter | Type | Description |
|---|---|---|
| `AWS_ACCESS_KEY_ID` | secret | AWS access key |
| `AWS_SECRET_ACCESS_KEY` | secret | AWS secret key |
| `aws_region` | input (required) | e.g. `us-east-1` |
| `state_bucket_name` | input (optional) | Auto-generated if not provided |

If `state_bucket_name` is not provided, one is generated:

```
state_bucket_name = "aws-exe-sys-state-${random_5_chars}"
```

### Derived / Opinionated Defaults

| Variable | Value |
|---|---|
| `project_name` | `aws-exe-sys` |
| `ecr_repo_name` | `aws-exe-sys` |
| `image_tag` | `<git-sha>` |
| `hash` | `sha256(state_bucket_name + aws_region)[-5:]` |
| `internal_bucket` | `aws-exe-sys-internal-${hash}` |
| `done_bucket` | `aws-exe-sys-done-${hash}` |

---

## Step 1: Bootstrap State Bucket

```mermaid
flowchart LR
    Dir["infra/00-bootstrap/"]
    State["State: LOCAL<br><i>not stored remotely</i>"]
    Create["S3 Bucket<br><i>versioning + encryption</i>"]

    Dir --> State --> Create

    style Dir fill:#2d1052,stroke:#a855f7,color:#e2e8f0
    style State fill:#3d2b00,stroke:#eab308,color:#e2e8f0
    style Create fill:#003d2b,stroke:#10b981,color:#e2e8f0
```

> [!IMPORTANT]
> This is the **one resource** the user must manually delete when tearing down.

---

## Step 2: Create ECR Repo

```mermaid
flowchart LR
    Dir["infra/01-ecr/"]
    State["State: s3://&lt;state_bucket&gt;/ecr/<br><i>terraform.tfstate</i>"]
    Gen["Generate<br><i>backend.tf</i>"]
    Create["ECR Repository<br><i>aws-exe-sys</i>"]

    Dir --> State --> Gen --> Create

    style Dir fill:#2d1052,stroke:#a855f7,color:#e2e8f0
    style State fill:#3d2b00,stroke:#eab308,color:#e2e8f0
    style Gen fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
    style Create fill:#003d2b,stroke:#10b981,color:#e2e8f0
```

---

## Step 3: Build + Push Docker Image

```mermaid
flowchart TB
    Base["Base Image<br><i>public.ecr.aws/lambda/python:3.14</i>"]
    Login["Login to ECR"]
    Build["docker build<br><i>-f docker/Dockerfile -t aws-exe-sys .</i>"]
    Tag1["Tag: &lt;ecr-repo&gt;:&lt;git-sha&gt;"]
    Tag2["Tag: &lt;ecr-repo&gt;:latest"]
    Push["Push both tags"]

    Base --> Login --> Build --> Tag1 & Tag2
    Tag1 --> Push
    Tag2 --> Push

    style Base fill:#2d1052,stroke:#a855f7,color:#e2e8f0
    style Login fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style Build fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
    style Tag1 fill:#3d2b00,stroke:#eab308,color:#e2e8f0
    style Tag2 fill:#3d2b00,stroke:#eab308,color:#e2e8f0
    style Push fill:#003d2b,stroke:#10b981,color:#e2e8f0
```

---

## Step 4: Deploy System

```mermaid
flowchart TB
    Dir["infra/02-deploy/"]
    State["State: s3://&lt;state_bucket&gt;/deploy/<br><i>terraform.tfstate</i>"]

    subgraph Generated["Generated Files"]
        Backend["backend.tf"]
        Tfvars["terraform.tfvars<br><i>image_tag · ecr_repo<br>state_bucket · region</i>"]
    end

    subgraph Resources["Created Resources"]
        APIGW["API Gateway<br><i>HTTP API · POST /init · POST /ssm</i>"]
        Lambda["4 Lambda Functions<br><i>ECR image · different entrypoints</i>"]
        SF["Step Function<br><i>watchdog state machine</i>"]
        DDB["3 DynamoDB Tables<br><i>orders · order_events + GSI · locks</i>"]
        S3["2 S3 Buckets<br><i>internal + done · lifecycle rules</i>"]
        CB["CodeBuild Project"]
        IAM["IAM Roles"]
        S3Event["S3 Event Notification<br><i>triggers orchestrator Lambda</i>"]
    end

    Dir --> State --> Generated --> Resources

    style Dir fill:#2d1052,stroke:#a855f7,color:#e2e8f0
    style State fill:#3d2b00,stroke:#eab308,color:#e2e8f0
    style Generated fill:#1a1a2e,stroke:#3b82f6,color:#e2e8f0
    style Resources fill:#1a1a2e,stroke:#10b981,color:#e2e8f0
    style Backend fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
    style Tfvars fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
    style APIGW fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style Lambda fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style SF fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style DDB fill:#003d2b,stroke:#10b981,color:#e2e8f0
    style S3 fill:#003d2b,stroke:#10b981,color:#e2e8f0
    style CB fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style IAM fill:#2d1052,stroke:#a855f7,color:#e2e8f0
    style S3Event fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
```

---

## Step 5: Smoke Tests

```mermaid
flowchart TB
    T1["Test 1: API Gateway Health<br><i>curl POST /init → expect 400</i>"]
    T2["Test 2: Lambda Invoke<br><i>init_job --payload → expect 200 + run_id</i>"]
    T3["Test 3: DynamoDB Tables<br><i>describe-table for each</i>"]
    T4["Test 4: S3 Buckets<br><i>s3 ls for each bucket</i>"]
    T5["Test 5: ECR Image<br><i>describe-images</i>"]

    T1 --> T2 --> T3 --> T4 --> T5

    style T1 fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style T2 fill:#3d1f00,stroke:#f97316,color:#e2e8f0
    style T3 fill:#003d2b,stroke:#10b981,color:#e2e8f0
    style T4 fill:#003d2b,stroke:#10b981,color:#e2e8f0
    style T5 fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
```

---

## Step 6: Archive for Teardown

```mermaid
flowchart TB
    subgraph Archive["Zip Contents"]
        ECR["infra/01-ecr/<br><i>+ backend.tf + terraform.tfvars</i>"]
        Deploy["infra/02-deploy/<br><i>+ backend.tf + terraform.tfvars</i>"]
        Docs["TEARDOWN.md<br><i>instructions</i>"]
    end

    Upload["Upload to S3<br><i>s3://&lt;state_bucket&gt;/archive/<br>aws-exe-sys-teardown-&lt;sha&gt;.zip</i>"]

    Archive --> Upload

    style Archive fill:#1a1a2e,stroke:#eab308,color:#e2e8f0
    style ECR fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
    style Deploy fill:#0a3544,stroke:#06b6d4,color:#e2e8f0
    style Docs fill:#2d1052,stroke:#a855f7,color:#e2e8f0
    style Upload fill:#003d2b,stroke:#10b981,color:#e2e8f0
```

---

## Teardown

To destroy this system, follow the steps below in order:

```mermaid
flowchart TB
    DL["1. Download Archive<br><i>aws s3 cp s3://&lt;state_bucket&gt;/archive/...</i>"]
    Destroy2["2. Destroy Deploy<br><i>cd infra/02-deploy<br>terraform init && terraform destroy</i>"]
    Destroy1["3. Destroy ECR<br><i>cd infra/01-ecr<br>terraform init && terraform destroy</i>"]
    Manual["4. Delete State Bucket<br><i>aws s3 rb s3://&lt;state_bucket&gt; --force</i>"]

    DL --> Destroy2 --> Destroy1 --> Manual

    style DL fill:#1e3a5f,stroke:#3b82f6,color:#e2e8f0
    style Destroy2 fill:#3d0a0a,stroke:#ef4444,color:#e2e8f0
    style Destroy1 fill:#3d0a0a,stroke:#ef4444,color:#e2e8f0
    style Manual fill:#3d2b00,stroke:#eab308,color:#e2e8f0
```

**1. Download teardown archive:**

```bash
aws s3 cp s3://<state_bucket>/archive/aws-exe-sys-teardown-<sha>.zip .
unzip aws-exe-sys-teardown-<sha>.zip
```

**2. Destroy deploy resources:**

```bash
cd infra/02-deploy
terraform init
terraform destroy
```

**3. Destroy ECR:**

```bash
cd infra/01-ecr
terraform init
terraform destroy
```

**4. Manually delete state bucket:**

```bash
aws s3 rb s3://<state_bucket> --force
```

> [!WARNING]
> The state bucket is the only resource not tracked in Terraform state that must be manually removed.
