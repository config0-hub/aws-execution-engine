# Deployment (optional)

The repo includes Terraform in `infra/` for deploying the generic engine API.

## Directory layout

- `infra/00-bootstrap`: S3 state backend bucket.
- `infra/02-deploy`: Lambda, API Gateway, S3 buckets, IAM, and CodeBuild resources.

## Minimal local flow

1. Build the public release artifacts and upload them to S3:

   ```bash
   bash scripts/build-release-zip.sh
   ```

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

By default, the deployed roles read packages from `<project_prefix>-internal` and write results to
`<project_prefix>-done`. Configure the additional bucket ARN lists when payloads use other buckets.

If you do not use the Terraform deployment, you can still invoke `init_job`/`worker` directly in AWS.

See `infra/` for full module details.
