# Deployment (optional)

The repo includes Terraform in `infra/` for deploying the generic engine API.

## Directory layout

- `infra/00-bootstrap`: S3 state backend bucket.
- `infra/02-deploy`: Lambda, API Gateway, S3, IAM, CodeBuild, and Step Functions resources.

## Minimal local flow

1. Build the public release artifacts and upload them to S3:

   ```bash
   bash scripts/build-release-zip.sh
   ```

   `engine.zip` contains all three handlers: `init_job`, `worker`, and `finalizer`.

2. Prepare the engine artifact references:
   - `project_prefix`
   - `kms_key_arn`
   - `engine_zip_s3_bucket`
   - `engine_zip_s3_key`
   - `sops_age_layer_s3_key`
3. Generate Terraform vars and deploy:

```bash
cd infra/02-deploy
cat > terraform.tfvars <<EOF
project_prefix        = "xe"
kms_key_arn           = "arn:aws:kms:..."
engine_zip_s3_bucket  = "<bucket>"
engine_zip_s3_key     = "engine.zip"
sops_age_layer_s3_key = "sops-age-layer.zip"

# Optional: grant access to caller-managed package/result buckets.
additional_package_bucket_arns = ["arn:aws:s3:::my-package-bucket"]
additional_result_bucket_arns  = ["arn:aws:s3:::my-result-bucket"]
EOF
terraform init
terraform apply
```

By default, deployed roles read packages from `<project_prefix>-engine-internal-<account-id>`
and write results to `<project_prefix>-engine-done-<account-id>` (account-suffixed because S3
names are global; engine-segmented so the iac-ci foundation's `<prefix>-done-<account-id>`
bucket is never dual-owned). Configure the additional bucket ARN lists when payloads use
other buckets.

## CodeBuild orchestration

Deployments provision:

- `<project_prefix>-codebuild`: Standard Step Functions state machine
- `<project_prefix>-worker`: managed CodeBuild worker project
- `<project_prefix>-finalizer`: missing-result fallback Lambda

Terraform wires `AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN` into `init_job`. CodeBuild submission is accepted
when `StartExecution` succeeds. The workflow then waits for CodeBuild and invokes the finalizer. The worker
writes the detailed result; the finalizer only conditionally creates a missing failed result.

Useful outputs are `codebuild_state_machine_arn`, `codebuild_state_machine_name`, and
`finalizer_function_name`. These are operator metadata and are not caller payload fields.

For a safe rollout, first deploy and directly test the finalizer and state machine, then update the `init_job`
environment/role and publish the dispatcher switch. For rollback, restore direct CodeBuild dispatch and its
scoped `codebuild:StartBuild` permission; do not alter existing terminal markers.

If you do not use the Terraform deployment, you must provision the state machine/finalizer and set the two
dispatch environment variables yourself:

- `AWS_EXE_SYS_WORKER_LAMBDA`
- `AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN`

See `infra/` for full module details.
