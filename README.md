# aws-exe-sys

Event-driven execution engine for infrastructure-as-code, built on AWS Lambda, CodeBuild, SSM Run Command, DynamoDB, S3, Step Functions, and API Gateway.

## Overview

aws-exe-sys receives jobs containing orders (shell commands) via API, queues them in DynamoDB, and executes them through Lambda, CodeBuild, or SSM Run Command with full dependency resolution. It handles cross-account AWS credentials via SOPS encryption (Lambda/CodeBuild) or SSM command parameters (SSM) and tracks progress through VCS PR comments.

The system works in two phases:

1. **Job intake** -- validates orders, packages credentials with SOPS (or plain env vars for SSM), uploads execution bundles to S3, inserts state into DynamoDB, and posts an initial PR comment. Two entry points: `POST /init` for Lambda/CodeBuild orders, `POST /ssm` for SSM Run Command orders.
2. **Orchestrator execution** -- triggered by S3 callback events, resolves dependency graphs, dispatches ready orders in parallel to Lambda, CodeBuild, or SSM Run Command, and finalizes when all orders complete.

Everything is serverless. Nothing runs 24/7. All working data auto-cleans via TTL and lifecycle rules.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the full system design.

## What Gets Created

| Resource | Name / Pattern | Notes |
|---|---|---|
| S3 bucket (state) | `aws-exe-sys-state-<id>` | Terraform state, permanent |
| S3 bucket (internal) | `aws-exe-sys-internal-<hash>` | Exec bundles + callbacks, 1-day lifecycle |
| S3 bucket (done) | `aws-exe-sys-done-<hash>` | Completion markers, 1-day lifecycle |
| ECR repository | `aws-exe-sys` | Single Docker image for all functions |
| Lambda functions (x5) | `aws-exe-sys-init-job`, `aws-exe-sys-orchestrator`, `aws-exe-sys-watchdog-check`, `aws-exe-sys-worker`, `aws-exe-sys-ssm-config` | Same image, different entrypoints |
| API Gateway (HTTP) | `aws-exe-sys-api` | POST /init, POST /ssm |
| DynamoDB tables (x3) | `aws-exe-sys-orders`, `aws-exe-sys-order-events`, `aws-exe-sys-locks` | PAY_PER_REQUEST billing |
| CodeBuild project | `aws-exe-sys-worker` | For long-running orders |
| SSM Document | `aws-exe-sys-run-commands` | For SSM Run Command orders |
| Step Function | `aws-exe-sys-watchdog` | Per-order timeout safety |
| IAM roles | `aws-exe-sys-*` | Least-privilege per function |

## Prerequisites

**AWS:**
- An AWS account with IAM credentials that have sufficient permissions
- For initial deployment, `AdministratorAccess` is simplest. For a scoped-down policy, the deploying principal needs access to: S3, ECR, DynamoDB, Lambda, IAM, CodeBuild, API Gateway, Step Functions, CloudWatch Logs, and STS.

**Tools (manual deployment only):**
- [Terraform](https://developer.hashicorp.com/terraform/install) >= 1.5
- [Docker](https://docs.docker.com/get-docker/)
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)

## Deploy

Two paths. Same result.

### Option A: Fork & GitHub Actions

**1. Fork this repository.**

**2. Add AWS secrets.**

Go to your fork's **Settings > Secrets and variables > Actions** and add:
- `AWS_ACCESS_KEY_ID`
- `AWS_SECRET_ACCESS_KEY`

**3. Run the deploy workflow.**

Go to **Actions > "Deploy" > Run workflow** and provide:
- `aws_region` (required) -- e.g. `us-east-1`
- `state_bucket_name` (optional) -- leave empty to auto-generate as `aws-exe-sys-state-<random>`

**4. Verify.**

The workflow runs smoke tests automatically. When complete, check the workflow summary for:
- API Gateway URL
- State bucket name
- Teardown archive location

See [docs/DEPLOY.md](docs/DEPLOY.md) for a detailed breakdown of each pipeline step.

### Option B: Manual CLI

Configure AWS credentials in your shell, then set these variables:

```bash
export AWS_REGION="us-east-1"
export STATE_BUCKET="aws-exe-sys-state-$(head -c 3 /dev/urandom | xxd -p | cut -c1-5)"
export IMAGE_TAG="$(git rev-parse HEAD)"
```

**Step 1: Bootstrap state bucket**

```bash
cd infra/00-bootstrap
terraform init
terraform apply \
  -var "state_bucket_name=$STATE_BUCKET" \
  -var "aws_region=$AWS_REGION"
```

**Step 2: Create ECR repository**

```bash
cd ../01-ecr
bash ../../scripts/generate_backend.sh "$STATE_BUCKET" "ecr" "$AWS_REGION"
terraform init
terraform apply -var "aws_region=$AWS_REGION"
export ECR_REPO=$(terraform output -raw ecr_repo_url)
```

**Step 3: Build and push Docker image**

```bash
cd ../..
aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "$ECR_REPO"

docker build -f docker/Dockerfile -t "$ECR_REPO:$IMAGE_TAG" -t "$ECR_REPO:latest" .
docker push "$ECR_REPO:$IMAGE_TAG"
docker push "$ECR_REPO:latest"
```

**Step 4: Deploy all resources**

```bash
cd infra/02-deploy
bash ../../scripts/generate_backend.sh "$STATE_BUCKET" "deploy" "$AWS_REGION"
bash ../../scripts/generate_tfvars.sh "$ECR_REPO" "$IMAGE_TAG" "$AWS_REGION"
terraform init
terraform apply
```

**Step 5: Verify**

```bash
cd ../..
export API_GATEWAY_URL=$(cd infra/02-deploy && terraform output -raw api_gateway_url)

# Quick check -- API Gateway should return 4xx with no payload
curl -s -o /dev/null -w "%{http_code}" -X POST "$API_GATEWAY_URL/init"
```

To run the full smoke test suite:

```bash
export ORDERS_TABLE=$(cd infra/02-deploy && terraform output -json dynamodb_table_names | jq -r '.orders')
export ORDER_EVENTS_TABLE=$(cd infra/02-deploy && terraform output -json dynamodb_table_names | jq -r '.order_events')
export LOCKS_TABLE=$(cd infra/02-deploy && terraform output -json dynamodb_table_names | jq -r '.orchestrator_locks')
export INTERNAL_BUCKET=$(cd infra/02-deploy && terraform output -json s3_bucket_names | jq -r '.internal')
export DONE_BUCKET=$(cd infra/02-deploy && terraform output -json s3_bucket_names | jq -r '.done')
bash tests/smoke/test_deploy.sh
```

## Teardown

Destroy resources in reverse order:

```bash
cd infra/02-deploy
terraform init
terraform destroy

cd ../01-ecr
terraform init
terraform destroy
```

Then delete the state bucket (not managed by Terraform):

```bash
aws s3 rb "s3://$STATE_BUCKET" --force
```

If you deployed via GitHub Actions and no longer have the Terraform files locally, download the teardown archive first:

```bash
aws s3 cp "s3://<state-bucket>/archive/aws-exe-sys-teardown-<sha>.zip" .
unzip aws-exe-sys-teardown-*.zip
# Then run the terraform destroy commands above from the extracted directories
```

## Usage

Once deployed, send jobs to the API:

```bash
curl -X POST "$API_GATEWAY_URL/init" \
  -H "Content-Type: application/json" \
  -d '{"job_parameters_b64": "<base64-encoded job payload>"}'
```

See [docs/VARIABLES.md](docs/VARIABLES.md) for the full job payload schema and examples.

## Development

```bash
# Run unit tests
cd tests && python -m pytest unit/ -v

# Run unit tests via Docker (no local Python required)
docker build -f docker/Dockerfile.test -t aws-exe-sys-tests .
docker run --rm aws-exe-sys-tests

# Run integration tests
docker run --rm aws-exe-sys-tests tests/integration/ -v

# Build the production image locally
docker build -f docker/Dockerfile -t aws-exe-sys .
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md) -- system design, flows, data model
- [Deployment Pipeline](docs/DEPLOY.md) -- detailed breakdown of all 6 deploy steps
- [Job Payload Reference](docs/VARIABLES.md) -- full job_parameters schema
- [Repository Structure](docs/REPO_STRUCTURE.md) -- codebase layout and file responsibilities
