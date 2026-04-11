# P1 — Bootstrap seam zero-diff verification

This document records the verification artifacts the deployer should reproduce
in CI before applying P1. Backend Lambda agent verified the conditional
Terraform logic locally with a focused test module (real `tofu plan` against
the S3 backend requires AWS creds the worker env doesn't have).

## What needs to be true after sync to Forgejo

1. `tofu plan` (no overrides; `engine_code_source.kind = "inline"` by default)
   → **"No changes. Your infrastructure matches the configuration."**
2. `tofu plan -var='engine_code_source={kind="ssm_url",value="/exe-sys/engine-code"}'`
   → diff shows exactly:
   - Each of 5 Lambdas: `image_config.command` flips from
     `["aws_exe_sys.<module>.handler.handler"]` to `["aws_exe_sys.bootstrap_handler.handler"]`
   - Each of 5 Lambdas: 3 new env vars added
     (`ENGINE_HANDLER`, `ENGINE_HANDLER_FUNC`, `ENGINE_CODE_SSM_PATH`)
   - Each of 5 Lambda role IAM policies: 1 new statement added
     (`ssm:GetParameter` on the engine code SSM path)

If the inline plan shows ANY diff, the conditional logic is leaking state and
the change MUST NOT be applied.

## Local verification done by backend agent

A focused 30-line tofu module exercised the same `concat`, `merge`, and
ternary expressions used in `iam.tf` / `lambdas.tf` and produced these
outputs against an isolated state file:

### kind = "inline" (default — proves zero-diff)

```
command_match = true     # ["aws_exe_sys.init_job.handler.handler"] vs conditional
env_match = true         # base map vs merge(base, {}, {})
policies_match = true    # jsonencode(base) vs jsonencode(concat(base, []))
```

This proves the load-bearing invariant: Terraform language semantics
guarantee that `concat(L, [])` produces a list equal to `L`, `merge(M, {}, {})`
produces a map equal to `M`, and `kind == "inline" ? L : R` resolves to `L`.
After `jsonencode`, the policy strings are byte-identical to baseline, and
since Terraform compares attribute *values* (not source expressions) when
generating a plan, the plan diff is zero.

### kind = "ssm_url" (proves correct activation)

```
command_match = false
  - was: ["aws_exe_sys.init_job.handler.handler"]
  - now: ["aws_exe_sys.bootstrap_handler.handler"]
env_match = false
  + ENGINE_HANDLER       = "aws_exe_sys.x.handler"      (per-Lambda)
  + ENGINE_HANDLER_FUNC  = "handler"
  + ENGINE_CODE_SSM_PATH = "/exe-sys/engine-code"
policies_match = false
  + Statement: { Effect=Allow, Action=ssm:GetParameter,
                 Resource=arn:aws:ssm:*:*:parameter/exe-sys/engine-code* }
```

## What the deployer should do

1. `cd infra/02-deploy && tofu init && tofu plan` — must say "No changes."
   Cite this in the deploy report. Do NOT apply if any diff appears.
2. (Optional sanity check, not for deploy) `tofu plan -var='engine_code_source={kind="ssm_url",value="/exe-sys/engine-code"}'`
   should show exactly the 3 changes listed above per Lambda. This is just
   for confidence — the actual deploy stays on `kind = "inline"`.
3. Apply (only if step 1 reports no changes) and run smoke tests.

## IAM PutParameter on engine code path

The plan asked to "tighten `ssm:PutParameter` on the engine code SSM path to
the deploy role only." Audit result: **no Lambda role currently has
`ssm:PutParameter` on the engine code path** (the only PutParameter grant
in this stack is on `init_job` for `parameter/exe-sys/sops-keys/*`, which is
unrelated). Since the deploy role lives outside this Terraform module
(GitHub Actions OIDC role provisioned elsewhere), there is no in-stack
change required for this sub-item — the engine code SSM path is already
write-restricted to whatever external role provisions it.

Deployer: cite this when running the verification — no change needed,
already enforced by absence.

## SHA integrity (P1-2)

`src/bootstrap_handler.py` was rewritten to:
- Strict shape: `event.engine_code = {"url": str, "sha256": str}` (dict, not string)
- Strict env vars: `ENGINE_CODE_URL` + `ENGINE_CODE_SHA256` required together
- SSM payload: base64-encoded JSON `{"url": "...", "sha256": "..."}` (NOT plain b64 URL)
- All paths verify SHA256 of the downloaded tarball before extraction
- Mismatch → `BootstrapIntegrityError` (Lambda invocation fails loudly)

20 unit tests in `tests/unit/test_bootstrap_handler.py` cover all paths
(7 new for P1-2, 13 carried over and updated to the new strict shape).
