resource "aws_lambda_layer_version" "sops_age" {
  layer_name               = "${local.prefix}-sops-age"
  s3_bucket                = var.engine_zip_s3_bucket
  s3_key                   = var.sops_age_layer_s3_key
  compatible_runtimes      = ["python3.14"]
  compatible_architectures = ["x86_64"]
  description              = "sops + age + age-keygen, mounted at /opt/bin/{sops,age,age-keygen}"
}
