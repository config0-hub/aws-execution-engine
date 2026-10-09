"""Contract tests for the engine Lambda functions in Terraform (lambdas.tf).

The run-cycle contract's timeout rule: AWS limits are upper bounds our clocks
never reach, and AWS never re-invokes a dead engine Lambda.
"""

from pathlib import Path
import re

_INFRA = Path(__file__).resolve().parents[2] / "infra" / "02-deploy"
LAMBDAS_TERRAFORM = _INFRA / "lambdas.tf"
IAM_TERRAFORM = _INFRA / "iam.tf"


def _resource_block(source: str, resource_type: str, name: str) -> str:
    match = re.search(
        rf'^resource "{resource_type}" "{name}" \{{\n(.*?)^\}}\n', source, re.M | re.S
    )
    assert match, f"{resource_type}.{name} not found"
    return match.group(1)


class TestAsyncRetriesAreOff:
    def test_both_engine_lambdas_have_zero_retries_and_an_on_failure_queue(self):
        source = LAMBDAS_TERRAFORM.read_text()
        for name in ("init_job", "worker"):
            block = _resource_block(source, "aws_lambda_function_event_invoke_config", name)
            assert f"function_name          = aws_lambda_function.{name}.function_name" in block
            assert "maximum_retry_attempts = 0" in block
            assert "on_failure {" in block
            assert "destination = aws_sqs_queue.lambda_failures.arn" in block
            assert "on_success" not in block

    def test_failures_queue_exists_in_the_engine_module(self):
        source = LAMBDAS_TERRAFORM.read_text()
        block = _resource_block(source, "aws_sqs_queue", "lambda_failures")
        assert 'name = "${local.prefix}-lambda-failures"' in block

    def test_both_roles_may_send_to_the_failures_queue(self):
        source = IAM_TERRAFORM.read_text()
        doc = re.search(
            r'data "aws_iam_policy_document" "lambda_failure_destination" \{\n(.*?)^\}\n',
            source,
            re.M | re.S,
        )
        assert doc, "lambda_failure_destination policy document not found"
        assert 'actions   = ["sqs:SendMessage"]' in doc.group(1)
        assert "resources = [aws_sqs_queue.lambda_failures.arn]" in doc.group(1)
        for name in ("init_job", "worker"):
            block = _resource_block(source, "aws_iam_role_policy", f"{name}_failure_destination")
            assert f"role   = aws_iam_role.{name}.id" in block
            assert "policy = data.aws_iam_policy_document.lambda_failure_destination.json" in block
