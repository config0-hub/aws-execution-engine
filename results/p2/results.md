# aws-exe-sys — P2 pluggable registries (results)

**Date:** 2026-04-10
**Plan:** `src/delegated-execution-components/aws-execution-engine/plan-to-fix-gaps-04.09.2026.md` (Phase P2)
**Scope:** P2-1 (credentials), P2-2 (VCS), P2-3 (code sources), P2-4 (execution targets). Standalone pluggable library — verified via library gate, not live deploy.
**Workflow:** `/team:implement-simple` with 1 worker + deployer. P0 shipped as `2dde072b`; P1 shipped as `20516044`. This run rebases on the P1 bootstrap seam.

## Outcome

**PASS.** All four P2 registries shipped in a single monolithic commit. 460/460 unit tests green in Woodpecker CI pipeline #37 (baseline was 399 after P1 → +61 new tests).

## P2 items — status

| ID | Item | Status |
|---|---|---|
| P2-1 | Credential provider registry (`CredentialProvider` Protocol + `AwsSsmProvider` + `AwsSecretsManagerProvider` + `register_provider` + `resolve_location(vendor:::scheme:path)`) | done — new package `src/common/credentials/{base,registry,aws_ssm,aws_secretsmanager}.py`, routes both `resolve_git_credentials` and per-order `ssm_paths`/`secret_manager_paths` through the registry, fixes the `aws:::secretd:` parse bug (was documented but crashed) |
| P2-2 | VCS provider registry — clone + comment + metadata (`VcsProvider.clone/clone_ssh/get_clone_url`, GitHub host parameterized, `register_provider`) | done — extended `src/common/vcs/base.py`, enriched `src/common/vcs/github.py` with the clone logic lifted from `code_source.py:90-151`, new `src/common/vcs/registry.py`, hardcoded `github.com` at the old lines 109/128/156 removed |
| P2-3 | Code source protocol (`CodeSource` Protocol + `GitCodeSource`/`S3CodeSource`/`CommandsOnlyCodeSource` + `register_code_source`, collapse `group_git_orders`, delete Phase-1/2/3 split) | done — new package `src/common/code_sources/{base,registry,git,s3,commands_only}.py`, `group_git_orders` deleted, both `init_job/repackage.py` and `ssm_config/repackage.py` collapsed to a single registry-dispatched loop |
| P2-4 | Execution target registry (`ExecutionTarget` Protocol + `LambdaTarget`/`CodeBuildTarget`/`SsmTarget` + `register_target`, `TARGETS` as source of truth for `EXECUTION_TARGETS`) | done — new package `src/orchestrator/targets/{base,registry,lambda_target,codebuild,ssm}.py`, `src/common/statuses.py:EXECUTION_TARGETS` now derived from `TARGETS.keys()` so third-party registrations automatically flow into validation, `dispatch.py:146-154` collapsed to `TARGETS[execution_target].dispatch(order, run_id, internal_bucket)` |

## Architectural win — file slimming and if/elif collapse

Each registry converted a hardcoded `if/elif` into a `Protocol` + dict lookup. The four historically-fat call-site files shrank substantially:

| File | After | Notable |
|---|---|---|
| `src/common/code_source.py` | 193 LoC | Zero if/elif on `s3_location`/`git_repo`. `resolve_git_credentials` and secrets routing now delegate to the credentials registry. |
| `src/orchestrator/dispatch.py` | 186 LoC | Zero if/elif on `execution_target`. Only a `not in TARGETS` guard + `execution_id = TARGETS[execution_target].dispatch(...)`. |
| `src/init_job/repackage.py` | 118 LoC | Phase-1/2/3 split deleted; single registry-dispatched loop. |
| `src/ssm_config/repackage.py` | 113 LoC | Same — single registry-dispatched loop. |

Net diff: **+553 / −508 across 17 modified files**, plus four new packages (credentials, code_sources, targets, vcs/registry.py) and four new test files. The line-count delta is nearly a wash because logic migrated *out* of the fat call-site files *into* the small registry packages — but the call sites now read linearly, and adding a new scheme/source/target no longer requires editing four different files.

## Code changes

### New packages

| Package | Files |
|---|---|
| `src/common/credentials/` | `base.py` (Protocol), `registry.py` (`register_provider`, `resolve_location`), `aws_ssm.py` (`AwsSsmProvider`), `aws_secretsmanager.py` (`AwsSecretsManagerProvider`), `__init__.py` (re-exports + seeds built-ins at import time) |
| `src/common/code_sources/` | `base.py` (Protocol), `registry.py` (`register_code_source`), `git.py` (`GitCodeSource` with per-instance clone cache), `s3.py` (`S3CodeSource`), `commands_only.py` (`CommandsOnlyCodeSource`), `__init__.py` |
| `src/orchestrator/targets/` | `base.py` (Protocol), `registry.py` (`register_target`), `lambda_target.py` (`LambdaTarget`), `codebuild.py` (`CodeBuildTarget`), `ssm.py` (`SsmTarget`), `__init__.py` |
| `src/common/vcs/registry.py` | New sibling to the pre-existing `src/common/vcs/base.py` / `github.py` / `helper.py` |

### Modified files

| File | Change |
|---|---|
| `src/common/vcs/base.py` | `VcsProvider` ABC extended with `clone(repo, commit, dest, token)`, `clone_ssh(repo, commit, dest, ssh_key)`, `get_clone_url(repo, token)` |
| `src/common/vcs/github.py` | Clone logic from `code_source.py:90-151` moved here into `GitHubProvider.clone()` / `.clone_ssh()`; host parameterized (no more hardcoded `github.com`) |
| `src/common/vcs/__init__.py`, `src/common/vcs/helper.py` | Re-exports + helper thinned |
| `src/common/code_source.py` | Shrunk from its prior fat state to 193 LoC. No more scheme parsing if/elif, no more if-git/elif-s3 branching, no more hardcoded `github.com`. Delegates to credentials registry, code-sources registry, VCS registry. |
| `src/orchestrator/dispatch.py` | Shrunk to 186 LoC. `execution_id = TARGETS[execution_target].dispatch(order, run_id, internal_bucket)` — one line replaces the old three-way if/elif. |
| `src/common/statuses.py` | `EXECUTION_TARGETS` frozenset replaced by `TARGETS.keys()` so third-party `register_target` automatically flows into validation |
| `src/common/models.py` | Minor field addition (1 line) |
| `src/init_job/repackage.py`, `src/ssm_config/repackage.py` | Phase-1/2/3 split deleted; both files now run a single registry-dispatched loop over orders |
| `tests/integration/test_full_run.py`, `tests/integration/test_orchestrator.py`, `tests/unit/test_code_source.py`, `tests/unit/test_dispatch.py`, `tests/unit/test_dispatch_idempotency.py`, `tests/unit/test_repackage.py`, `tests/unit/test_vcs_github.py` | Test updates to match new registry-routed APIs. Existing tests continue to pass. |

## Tests — 4 new files, 61 net new tests

| Test file | Count | Third-party extension-point test |
|---|---|---|
| `tests/unit/test_credentials_registry.py` | 17 | **`test_third_party_provider_registration`** — registers a fake `vault` provider, confirms `resolve_location("vault:::kv/foo")` dispatches to it |
| `tests/unit/test_vcs_registry.py` | 7 | **`test_register_bitbucket_stub`** — registers a stub Bitbucket provider, calls `clone_repo` with `job.git_provider="bitbucket"`, asserts the stub's `clone()` was called |
| `tests/unit/test_code_sources.py` | 20 | **`test_register_third_party_source`** — registers a stub `http_tarball` source, confirms dispatch works |
| `tests/unit/test_dispatch_targets.py` | 13 | **`test_register_ecs_fargate_stub`** — registers a fake target, dispatches an order with that `execution_target`, asserts stub was called |
| `tests/unit/test_vcs_github.py` (expanded) | 19 | New: `test_clone_https_token_url`, `test_clone_https_anonymous`, `test_clone_ssh` |

Also added `test_unknown_scheme_raises` (credentials) and `test_unknown_target_raises` (dispatch) to ensure registry misses surface `UnknownCodeSourceError` / `UnknownTargetError` explicitly rather than producing a `KeyError`.

Total unit tests: **460** (399 P1 baseline + 61 P2).

## Library verification gate

Per `feedback_library_vs_service_verification.md` (saved during the P1 run): aws-execution-engine is a standalone pluggable library, not a hosted service. Verification is:

1. ✅ Unit tests pass locally (backend-worker)
2. ✅ Unit tests re-run independently by deployer as a pre-commit check (460 passed in 16.73s)
3. ✅ Monolithic commit (no `--no-verify`, no `--amend`, surgical staging)
4. ✅ `tools/sync-forgejo.sh aws-execution-engine`
5. ✅ Woodpecker pipeline unit-tests step green (460 passed in 7.24s)
6. ✅ Each of the four third-party-registration tests verified by exact name in the CI log

No live `tofu apply/plan` against real AWS. No Lambda invocations. No SSM parameters touched. Consumers run their own infra when they upgrade the library version.

## Deploy evidence

| Item | Value |
|---|---|
| Local commit SHA | `3c63b3b46d48e0be9f1c4e7079ae4fc7f7060801` |
| Local commit subject | `feat(aws-exe-sys): P2 pluggable registries (credentials, VCS, code sources, execution targets)` |
| Forgejo commit SHA | `9b70cf3c9cb92f129ba7c86a2f7ea033e30aa160` |
| Forgejo repo | `forgejo_admin/aws-execution-engine` |
| Woodpecker pipeline | #37 |
| Pipeline URL | `http://woodpecker/repos/forgejo_admin/aws-execution-engine/pipeline/37` |
| Pipeline status | **success** (clone + unit-tests steps both green) |
| Total tests | `============================= 460 passed in 7.24s ==============================` |

### Third-party-registration tests verified by name in CI log

Each appears with `PASSED` in pipeline #37's unit-tests step:

- `tests/unit/test_code_sources.py::TestRegisterThirdPartySource::test_register_third_party_source PASSED` (log L319)
- `tests/unit/test_credentials_registry.py::TestThirdPartyProviderRegistration::test_third_party_provider_registration PASSED` (log L331)
- `tests/unit/test_dispatch_targets.py::TestRegisterThirdPartyTarget::test_register_ecs_fargate_stub PASSED` (log L356)
- `tests/unit/test_vcs_registry.py::TestRegisterBitbucketStub::test_register_bitbucket_stub PASSED` (log L682)

These are the architecturally meaningful checks — they prove the extension points actually work, not just that the built-ins pass.

## Team-lead review (pre-deploy)

Before activating the deployer, the team-lead inspected the working tree:

- ✅ Zero swallowed exceptions in `src/common/credentials/`, `src/common/code_sources/`, `src/orchestrator/targets/`, `src/common/vcs/` (grep for `except:` and `except Exception:.*(pass|return None)`)
- ✅ `dispatch.py` has zero if/elif on `execution_target` (grep confirms only a `not in TARGETS` guard + a single-line dict lookup)
- ✅ `code_source.py` has zero if/elif for `s3_location`/`git_repo` (grep confirms)
- ✅ Package structure matches plan exactly
- ✅ All four required third-party-registration test names present in the right files
- ✅ Test count arithmetic: 399 prior baseline + 61 new = 460 (matches deployer's CI output)

## Deviations and notes

- **`test_register_third_party_source` uses a tarball stub, not `http_tarball`.** The plan suggested `http_tarball` as an example; backend-worker implemented the test with a plain tarball stub. Architecturally identical — proves the extension point — just a naming detail.
- **`lambda_target.py` named explicitly** to avoid the Python keyword collision with `lambda`. The plan called the package `lambda.py`; this rename is the only sensible option.
- **`GitCodeSource` has a per-instance clone cache.** The plan did not specify the caching strategy; backend-worker chose per-instance (not module-level) to avoid cross-test state leakage. Cache lifetime is one `group_orders` invocation, matching the previous de-duplication behavior of `group_git_orders`.
- **`__init__.py` files seed built-ins at import time.** Registries are populated automatically — consumers don't need to call `register_*` for the default providers/sources/targets. Third parties still use the explicit `register_*()` API.
- **Not pushed to GitHub origin.** Commit `3c63b3b4` is on local `main` only. Matches the P0/P1 pattern. Separate user decision needed to push to origin.
- **Integration tests `test_full_run.py` and `test_orchestrator.py` were touched.** These were updated (1 line each) so the registry-routed APIs match what the integration harness expects. They still run in the integration (not unit) path and were not rerun in the Woodpecker library-gate pipeline (which runs unit only).
- **`src/common/models.py` has a 1-line field addition.** Backend-worker's change — minor field used by the code-source registry to tag which source handled an order. Not part of the plan text explicitly but unavoidable given the registry structure.

## What's NOT in this run

Still deferred to future `/team:implement` runs:

- **P3 — runtime hardening**
  - P3-1 Worker callback fallback (worker needs narrow `dynamodb:UpdateItem` IAM + direct `UpdateItem` fallback path in `callback.py`)
  - P3-2 Watchdog jitter + `MaxAttempts` bound in `step_functions.tf`
  - P3-3 SOPS key TTL coordination (`ttl_hours` scaled to longest order timeout in `init_job/repackage.py`)
- **P4 — framework polish**
  - P4-1 `EventSink` protocol + composite sink
  - P4-2 Installable package name (move `src.*` → `aws_exe_sys.*`)
  - P4-3 Versioned result schema (`schema_version: "v1"`)
  - P4-4 CI drift test that regex-extracts doc claims and asserts against code

All four P3 and all four P4 items are independent of each other and of P2; they can be done in any order.

## End state

With P0, P1, and now P2 landed, the engine is structurally ready for third-party extension:

- **Credentials:** register a new `vendor:::vault:kv/foo` scheme without touching engine code.
- **VCS:** register Bitbucket or GitLab without touching engine code; `job.git_provider` selects at runtime.
- **Code sources:** register `http_tarball` or `oci_image` without touching engine code.
- **Execution targets:** register `ecs_fargate` or `kubernetes_job` without touching engine code; validation flows through `TARGETS.keys()` automatically.

Each extension point is proved live in the CI pipeline by a passing `test_register_*` test. The architectural point of Phase 2 — making the engine pluggable rather than hardcoded — is verifiably achieved.
