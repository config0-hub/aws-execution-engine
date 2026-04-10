# RESEARCH3.md — Pluggable Framework: Design vs Implementation Gap Analysis

**Scope.** This document compares what `aws-execution-engine` looks like as a *pluggable execution framework* (what you could build on top of it without forking) against what is actually implemented in the code today. It is a companion to `RESEARCH.md` (initial survey) and `RESEARCH2.md` (execution-path deep dive). Where `RESEARCH2` was about runtime vulnerabilities inside the current design, `RESEARCH3` is about extensibility seams: where the code lets callers plug in, where it doesn't, and where the documentation promises a seam that the code doesn't deliver.

**Method.** Every source file under `src/` was read directly. Key infra files (`lambdas.tf`, `iam.tf`, `step_functions.tf`, `s3_notifications.tf`) were read either directly or via an exploration subagent and the subagent's claims were re-verified against the code before being used here. File:line references are anchored to the tree at commit `12b316f`.

---

## 0. TL;DR — the seven gaps that actually matter

| # | Gap | Where | Severity |
|---|---|---|---|
| 1 | `src/bootstrap_handler.py` is the only real "plug your own engine code in" seam, and it is **dead code** — not wired into any Lambda's `image_config.command`. | `infra/02-deploy/lambdas.tf:22,48,72,91,111` | CRITICAL |
| 2 | "Pluggable VCS providers" is a paper abstraction — `PROVIDERS = {"github": GitHubProvider}` is the registry, there is no provider-registry API, and `clone_repo()` ignores the abstraction and **hardcodes `github.com`**. | `src/common/vcs/helper.py:15`, `src/common/code_source.py:109,128,156` | HIGH |
| 3 | Credential locations use a string-prefix mini-DSL (`aws:::ssm:`, `aws:::secretd:`) in the docs, but the parser (`_strip_location_prefix`) **only handles `aws:::ssm:`**; `aws:::secretd:` is documented and never parsed. There is no credential-provider registry. | `src/common/code_source.py:46-54`, `docs/VARIABLES.md:14` | HIGH |
| 4 | Code sources (git / S3 / commands-only) are an `if/elif` branch in `group_git_orders`, not a registry. You cannot plug in HTTP, Codecommit, GCS, or a tar artifact URL without editing `code_source.py`. | `src/common/code_source.py:184-230` | MEDIUM |
| 5 | Execution targets (`lambda` / `codebuild` / `ssm`) are an `if/elif` in `_dispatch_single`, not a registry. Adding an ECS Fargate target requires editing `dispatch.py`. | `src/orchestrator/dispatch.py:146-157` | MEDIUM |
| 6 | Docs and CONTRACT promise things the code does not do: events SK format, events TTL, PR comment flow, "pluggable" framework framing. Several are drifts; one is a live bug. | `src/common/dynamodb.py:162,170`; `CONTRACT.md:101`; `docs/ARCHITECTURE.md` (multiple); `docs/REPO_STRUCTURE.md:33` | MEDIUM |
| 7 | `TODO/sops-key-ssm-storage.md` has been **fully implemented but never closed** — it describes work that now exists in `sops.py`, `repackage.py`, `dispatch.py`, `worker/run.py`, and `finalize.py`. The TODO is stale documentation. | `TODO/sops-key-ssm-storage.md` vs `src/common/sops.py:45-99` | LOW (confusing) |

Sections 1–6 trace each of these. Section 7 is a cross-reference of every RESEARCH2 claim against the current code. Section 8 is recommendations, ordered for a hypothetical "make this an actual pluggable framework" work plan.

---

## 1. What "pluggable framework" means in this codebase

Nowhere in `CLAUDE.md`, `README.md`, `CONTRACT.md`, or `docs/ARCHITECTURE.md` is the phrase "pluggable framework" used. The word "generic" appears once (`CLAUDE.md:3` — *"generic, event-driven continuous delivery system for infrastructure-as-code and arbitrary command execution"*) and the system is positioned as tool-agnostic: the engine never knows what the commands do. From that positioning, an implicit set of plug points falls out:

1. **Engine code loading.** Can a consumer ship their own orchestrator/init_job/worker logic without rebuilding the Docker image? (This is what `bootstrap_handler.py` is for.)
2. **VCS provider.** Can a consumer use Bitbucket, GitLab, self-hosted Gitea without forking?
3. **Code source.** Can a consumer point at something that isn't git or S3?
4. **Credential source.** Can a consumer fetch from Vault, GCP Secret Manager, HashiCorp Vault?
5. **Execution target.** Can a consumer dispatch to ECS Fargate, on-prem Kubernetes, or a custom runner?
6. **Event/result sink.** Can a consumer push events to something other than `order_events` DynamoDB + `result.json` S3?
7. **Authentication of callers.** Can a consumer bolt a different auth model onto `POST /init`?

Each subsection below maps one of these plug points to the actual code.

---

## 2. Plug point 1 — engine code loading (**dead seam**)

### 2.1 What the design implies

`src/bootstrap_handler.py:1-101` is built for exactly this use case. The module docstring reads:

> *"Bootstrap handler — downloads proprietary engine code at cold start. … 1. Downloads engine code tarball via presigned URL. 2. Extracts to /tmp/engine/. 3. Extends sys.path and PATH. 4. Delegates to the actual handler specified by ENGINE_HANDLER env var."*

The resolution order is:
1. `event["engine_code_url"]` — dispatcher passes a presigned URL directly
2. `ENGINE_CODE_URL` env var — overrides/tests
3. `ENGINE_CODE_SSM_PATH` env var → base64-decoded presigned URL in SSM Parameter Store

After bootstrap it does `__import__(ENGINE_HANDLER)` and calls its `handler`. This is precisely the plug point that would make this a framework: the repo becomes a runtime shell, and any consumer can deploy a Docker image plus a code tarball, without rebuilding the image.

### 2.2 What the code actually does

`infra/02-deploy/lambdas.tf` hardcodes every Lambda's entrypoint *directly* to an in-repo module:

```hcl
# lambdas.tf:22    init_job
image_config { command = ["src.init_job.handler.handler"] }
# lambdas.tf:48    orchestrator
image_config { command = ["src.orchestrator.handler.handler"] }
# lambdas.tf:72    watchdog_check
image_config { command = ["src.watchdog_check.handler.handler"] }
# lambdas.tf:91    worker
image_config { command = ["src.worker.handler.handler"] }
# lambdas.tf:111   ssm_config
image_config { command = ["src.ssm_config.handler.handler"] }
```

`bootstrap_handler.py` is never referenced in the Terraform. Nothing sets `ENGINE_HANDLER`. No Lambda's `image_config.command` is `["src.bootstrap_handler.handler"]`. There is no `tests/unit/test_bootstrap_handler.py` regression on the dispatcher path. The file exists, passes unit tests, and is unreachable.

### 2.3 Why this matters

This is the *single most consequential* gap between "pluggable framework" and the codebase. Every other plug point is localised — you can patch one module to add a provider. This one is architectural: without the bootstrap seam turned on, *every consumer must fork the repo*, edit the Docker image, rebuild, push to ECR, and redeploy Terraform to customise any of the modules. That is not a framework; that is a starter kit.

### 2.4 What "turning it on" would require

Minimum viable wiring (no code changes to `bootstrap_handler.py` itself):

1. Switch each Lambda's `image_config.command` to `["src.bootstrap_handler.handler"]`.
2. Add `ENGINE_HANDLER` and `ENGINE_HANDLER_FUNC` env vars per-function so the dispatcher lands in the right module.
3. Add an `ENGINE_CODE_SSM_PATH` (or `ENGINE_CODE_URL`) env var per-function, and a Terraform variable for the tarball source.
4. IAM: grant each Lambda `ssm:GetParameter` on the engine-code SSM path.
5. Decide whether `_loaded` caching (`bootstrap_handler.py:27,47`) is acceptable — it is *per-Lambda-container*, not per-request, so a warm container pinned to an old tarball will keep serving the old tarball. That may or may not be what a consumer wants.

None of that is in the repo today.

### 2.5 Secondary concerns inside the bootstrap handler itself

- **Line 77** uses `tarfile.extractall(... filter="data")` which is correct for Python 3.12+ hardening, but the comment `# trusted tarball from our own S3` is load-bearing: the handler trusts whoever writes to that SSM path. If SSM is broadly writable, this is an RCE seam. IAM needs to tightly scope `ssm:PutParameter` on the code path.
- **No integrity check.** No SHA, no signature. Whatever the URL serves is loaded and executed. For a genuine framework this needs a manifest + signature step.
- **No PATH isolation.** Extending `os.environ["PATH"]` with `bin/` from the downloaded tarball means the consumer tarball can shadow system `sops`, `git`, `age` binaries. That is both a feature (override tooling) and an attack surface.

---

## 3. Plug point 2 — VCS providers (**leaky abstraction**)

### 3.1 What the design implies

`docs/REPO_STRUCTURE.md:23-24` lists:

```
│   │   └── vcs/
│   │       ├── base.py                # ABC interface for VCS providers
│   │       └── github.py              # GitHub: PR comments
```

`src/common/vcs/base.py:7` defines `VcsProvider(ABC)` with `create_comment`, `update_comment`, `delete_comment`, `find_comment_by_tag`, `get_comments`. `CLAUDE.md` explicitly says:

> *"VCS abstraction: ABC base class in src/common/vcs/base.py. GitHub implementation first, designed for Bitbucket/GitLab extension."*

`src/common/vcs/helper.py:14-17` has a `PROVIDERS` registry:

```python
PROVIDERS: Dict[str, type] = {
    "github": GitHubProvider,
}
```

and a `VcsHelper(provider="github")` factory. On paper this is a clean plug point.

### 3.2 What the code actually does

Three problems.

**(a) The registry is a module-level dict, not a real registration API.** To add Bitbucket, you edit `helper.py` and import `BitbucketProvider`. There is no `register_provider(name, cls)` or `@provider("bitbucket")` decorator. That is not *pluggable*; that is *factored*. For a framework, the registry needs to be open for extension from outside the package — ideally via entry points, at minimum via a public `register_provider()` function.

**(b) `VcsHelper` only wraps *comment* operations.** The ABC defines comment CRUD plus tag search. There is nothing for:
- Cloning a repo
- Resolving a commit hash
- Posting a commit status
- Reading a PR body / labels / metadata
- Fetching PR file diffs

All of those are VCS operations a realistic CI/CD framework needs. Any consumer who wants them is back to editing `code_source.py`.

**(c) `clone_repo()` ignores the abstraction entirely.** `src/common/code_source.py:90-151` hardcodes `github.com`:

```python
# line 109
clone_url = f"https://x-access-token:{token}@github.com/{repo}.git"
# line 128
clone_url = f"https://github.com/{repo}.git"
# line 156 (_clone_via_ssh)
ssh_url = f"git@github.com:{repo}.git"
```

There is no branch on the VCS provider. The `VcsProvider` ABC never sees a clone request. If you set `provider="bitbucket"` (once Bitbucket exists), `upsert_comment` would go to Bitbucket but `clone_repo` would still clone from GitHub. The abstraction splits the VCS concept in half and only implements one half.

**(d) The PR-comment half is wired to nothing.** `CLAUDE.md:28` says:

> *"PR comments are disabled — the caller owns the PR comment lifecycle."*

`docs/REPO_STRUCTURE.md:33` and `docs/ARCHITECTURE.md` (the Step-5 "Init PR Comment" mermaid diagram, lines 142-153, and the Finalize "Final PR comment" diagram, lines 427) still show PR comments as part of the flow. The file the structure doc lists (`src/init_job/pr_comment.py`) does not exist; `grep -r pr_comment src/` returns nothing. So the one half of the VCS abstraction that is *fully implemented* (`helper.py`, `base.py`, `github.py`) is *never called by the engine* — it is a library sitting in `common/` waiting for a caller that was consciously removed.

### 3.3 Net state of plug point 2

- A clean comment-CRUD abstraction exists.
- It is disconnected from everything that actually needs VCS.
- Clone is hardcoded to GitHub.
- The registry is not extensible from outside the package.
- Design docs still describe a PR-comment flow that was removed.

For a real framework, either delete the VCS abstraction (it serves nothing) or rebuild it to cover clone + status + comment + metadata behind a single provider, and expose a registration API.

---

## 4. Plug point 3 — credential sources (**partial, single-provider**)

### 4.1 What the design implies

`docs/VARIABLES.md:14` documents the credential-location format:

> *"`git_token_location` … `aws:::ssm:<path>` or `aws:::secretd:<path>`"*

This implies a provider-prefixed URI. A consumer should be able to say `vault:::kv/foo/bar` or `gcp:::secret-manager/projects/x/secrets/y` and the engine should route to the right provider.

Per-order, `docs/VARIABLES.md:35-36` lists `ssm_paths` and `secret_manager_paths` as separate list fields — suggesting a multi-provider story where each provider has its own field. Both patterns co-exist.

### 4.2 What the code actually does

**Location parsing** — `src/common/code_source.py:46-54`:

```python
def _strip_location_prefix(location: str) -> str:
    if location.startswith("aws:::ssm:"):
        return location[len("aws:::ssm:"):]
    return location
```

Only `aws:::ssm:` is parsed. `aws:::secretd:` is documented and silently passed through — it will be treated as a raw SSM path and fail with `ParameterNotFound` at runtime.

**Per-order credential fetch** — `repackage.py:39-40`:

```python
ssm_values = fetch_ssm_values(order.ssm_paths or [])
secret_values = fetch_secret_values(order.secret_manager_paths or [])
```

The per-order path uses the two-field approach (no location prefix, no routing). The `_strip_location_prefix` helper is used only in `resolve_git_credentials()` — i.e. for `job.git_token_location` and `job.git_ssh_key_location`. So the repo has two *different* credential-fetch models running side by side: a prefix URI for git creds and a two-field list for per-order creds. Neither is extensible.

**`fetch_ssm_values` has a non-obvious contract.** `code_source.py:15-30`:

```python
def fetch_ssm_values(paths: List[str], region: Optional[str] = None) -> Dict[str, str]:
    ...
    for path in paths:
        resp = client.get_parameter(Name=path, WithDecryption=True)
        value = resp["Parameter"]["Value"]
        decoded = json.loads(base64.b64decode(value))
        result.update(decoded)
    return result
```

Every SSM parameter must be a **base64-encoded JSON dict** of env vars. That is an undocumented (and pretty surprising) format contract. `resolve_git_credentials()` calls this with a single-path list and then takes `list(vals.values())[0]` as the token — so the git-token SSM parameter must be a base64-encoded JSON dict with at least one string value, whose value happens to be the token. That is not documented anywhere; `VARIABLES.md:14` just says "SSM path containing git token".

**`fetch_secret_values` has a *different* surprising contract.** `code_source.py:33-43`:

```python
def fetch_secret_values(paths: List[str], region: Optional[str] = None) -> Dict[str, str]:
    ...
    for path in paths:
        resp = client.get_secret_value(SecretId=path)
        key = path.rsplit("/", 1)[-1].upper().replace("-", "_")
        result[key] = resp["SecretString"]
    return result
```

The key under which the secret is stored in the env bundle is *derived from the path*, not from the secret payload. If you have a Secrets Manager secret at `prod/github-token` with a JSON payload `{"token": "gh_x"}`, this code returns `{"GITHUB_TOKEN": '{"token":"gh_x"}'}` — the consumer's command then has to parse the JSON out of the env var. That is not a realistic developer experience and it is silently broken for any multi-field secret.

### 4.3 Net state of plug point 3

- No credential-provider registry.
- One format contract for SSM, a different one for Secrets Manager, both undocumented.
- The documented `aws:::secretd:` prefix is not parsed.
- Extending to Vault/GCP requires editing `code_source.py`.

---

## 5. Plug point 4 — code sources (**hardcoded branch**)

### 5.1 What the design implies

`docs/VARIABLES.md:34` lists `s3_location` as "S3 zip of execution files (alternative to git)". The architecture doc's Step 2 diagram (`docs/ARCHITECTURE.md:97`) says `Code["Get code<br><i>S3 or Git</i>"]`. The split is binary.

### 5.2 What the code actually does

`src/common/code_source.py:184-212` (`group_git_orders`) sorts orders into three buckets:

1. has `s3_location` → S3 branch
2. has `git_repo` (or inherits job's `git_repo`) → git branch, grouped by `(repo, commit_hash)` for clone de-duplication
3. neither → "commands-only", skipped by `group_git_orders`, then handled in Phase 3 of `ssm_config/repackage.py:146-160` by creating a fresh tmpdir

The dispatch is a hardcoded `if order.s3_location: ... elif ... : ...`. To add a new source (HTTP URL, Codecommit, GCS bucket, git mirror protocol), you must:

1. Add a new attribute to `Order` / `SsmOrder` in `src/common/models.py`.
2. Add an `elif` branch in `group_git_orders`.
3. Add a fetch function in `code_source.py`.
4. Add an `elif` in both `repackage.py` (init_job) and `ssm_config/repackage.py`.

There is no `CodeSource` ABC, no registry. The scaffolding is pure conditionals.

### 5.3 Note on `commands-only` drift

Commands-only orders are **only** supported for SSM (`ssm_config/repackage.py:146-160`). `init_job/repackage.py` has Phase 1 (git) and Phase 2 (S3) — no Phase 3. `validate.py:32-36` requires Lambda/CodeBuild orders to have a code source. So if a caller submits a Lambda order with no `s3_location` and no `git_repo`, `validate_orders` catches it:

```python
has_s3 = bool(order.s3_location)
has_git = bool(order.git_repo or job.git_repo) and bool(job.git_token_location)
if not has_s3 and not has_git:
    return [f"{order_label}: no code source ..."]
```

Good. But the asymmetry (SSM can run commands-only, Lambda/CodeBuild cannot) is not in the docs. A consumer reading `VARIABLES.md` would not know.

---

## 6. Plug point 5 — execution targets (**hardcoded branch**)

### 6.1 What the design implies

`CLAUDE.md:26-28` frames three targets as first-class:

> *"Three execution targets: Orders specify execution_target ('lambda', 'codebuild', or 'ssm')."*

`src/common/statuses.py:11` confirms:

```python
EXECUTION_TARGETS = frozenset({"lambda", "codebuild", "ssm"})
```

A new target (ECS, Fargate, Batch, a local runner, another Lambda function) is the canonical "let me extend the framework" question for a CD engine.

### 6.2 What the code actually does

`src/orchestrator/dispatch.py:146-157` is a hardcoded switch:

```python
execution_target = order.get("execution_target", "codebuild")
if execution_target == "lambda":
    execution_id = _dispatch_lambda(order, run_id, internal_bucket)
elif execution_target == "ssm":
    execution_id = _dispatch_ssm(order, run_id, internal_bucket)
else:
    execution_id = _dispatch_codebuild(order, run_id, internal_bucket)
```

Four places need to change to add a target:

1. `src/common/statuses.py:11` — add to `EXECUTION_TARGETS`.
2. `src/init_job/validate.py:28-30` — validator reads `EXECUTION_TARGETS` already, so this is "free".
3. `src/orchestrator/dispatch.py:146` — add an `elif`.
4. `src/orchestrator/dispatch.py` — add a `_dispatch_<target>` helper that builds the right API call and returns an execution ID.
5. `infra/02-deploy/iam.tf` — grant the orchestrator `iam:PassRole` and whatever the target's start-execution API needs.
6. Potentially: a new SSM document or equivalent runner payload.

None of this is a framework — it is a manual, multi-file edit. A real plug point would be:

```python
TARGETS: dict[str, ExecutionTarget] = {}

def register_target(name: str, target: ExecutionTarget) -> None: ...

class ExecutionTarget(Protocol):
    def dispatch(self, order: dict, run_id: str, internal_bucket: str) -> str: ...
    def iam_statements(self) -> list[dict]: ...     # generated at deploy time
    def env_overrides(self) -> list[dict]: ...
```

… with `dispatch.py` reduced to `TARGETS[execution_target].dispatch(order, ...)`. The engine does not have this shape.

### 6.3 `use_lambda` backward-compat note

`docs/ARCHITECTURE.md:387,394` (the Step 3 Dispatch mermaid) still references:

```
Compat["Backward compat<br><i>use_lambda=true → lambda</i><br><i>use_lambda=false → codebuild</i>"]
```

`dispatch.py:146` reads `order.get("execution_target", "codebuild")` — the `use_lambda` fallback the doc promises **does not exist**. If a caller submits an order with `use_lambda=true` and no `execution_target`, it will be dispatched to CodeBuild. Another doc/code drift.

---

## 7. Plug point 6 — event/result sinks (**tightly coupled**)

### 7.1 What the design implies

A real framework would let consumers pipe events to Datadog / OpenSearch / Kafka / a webhook, and let them replace the S3 `result.json` callback with a webhook of their own.

### 7.2 What the code actually does

- `src/common/dynamodb.py:144-176` hardcodes `put_event` to DynamoDB. All event writes (init, dispatched, completed, job_completed, subprocess events from `worker/run.py:106-113`) go through this single function.
- `src/worker/callback.py:15-49` hardcodes `requests.put(callback_url, ...)` to the presigned S3 URL baked into the SOPS bundle (`CALLBACK_URL` env var).
- `src/orchestrator/finalize.py:102-108` hardcodes `write_done_endpoint` to `s3://{done_bucket}/{run_id}/done`.
- `src/orchestrator/handler.py:19-27` hardcodes the run_id regex parser to an S3 ObjectCreated event shape:
  `r"tmp/callbacks/runs/([^/]+)/"`.
- `infra/02-deploy/s3_notifications.tf:9-17` hardcodes the trigger:
  ```
  filter_prefix = "tmp/callbacks/runs/"
  filter_suffix = "result.json"
  ```

All five of these are immovable without code changes. There is no `EventSink` interface, no `ResultWriter` interface. The callback protocol (write to S3 → S3 event → orchestrator) is the *only* way the orchestrator can learn that an order finished. A consumer who wants to keep runs entirely inside a private network (no S3 round trip, webhook only) cannot.

### 7.3 Line-level drift with `CONTRACT.md`

`CONTRACT.md:101` declares the event SK format as:

> *"Sort key: `sk` (String, format: "{order_name}:{epoch}")"*

`src/common/dynamodb.py:162`:

```python
sk = f"{order_name}:{epoch}:{event_type}"
```

The real format is `{order_name}:{epoch}:{event_type}`. Any consumer reading CONTRACT.md and writing a `begins_with(sk, "order_name:")` query against a specific epoch will fail. This is a contract bug, not a doc nit.

### 7.4 Event TTL bug

`src/common/dynamodb.py:170`:

```python
"ttl": epoch + 900,  # 15 min for testing; change to 172800 (2 days) for prod
```

`CONTRACT.md` does not pin a TTL. `CLAUDE.md:85` says *"TTL: 90 days"*. `docs/ARCHITECTURE.md` (TTL diagram, line 778) says *"Order events table 90 day DynamoDB TTL (+ GSI)"*. The code is 15 minutes. Events disappear from DynamoDB 15 minutes after they are written. That is almost certainly a live bug, not a design decision: the inline comment says "change to 172800 (2 days) for prod" — the author knew, intended to change it, and didn't.

This has meaningful user-facing consequences: any consumer trying to use `order_events` as a trace/audit log will see events vanish while a run is still executing.

---

## 8. Plug point 7 — caller authentication (**mostly not there**)

`CONTRACT.md:150-154` says:

> *"Same-account callers: IAM SigV4 on API Gateway … External callers: TBD (JWT planned)"*

`src/init_job/handler.py:42-71` does implement a JWT path: if `credentials_token` is in the event, it fetches a JWT secret from `JWT_SECRET_SSM_PATH`, verifies via `src/common/jwt_creds.verify_credentials_token`, and extracts `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`/`AWS_SESSION_TOKEN` claims into every order's `env_vars`. That is a credential-injection path, not an auth path: it is completely separate from whether the caller is allowed to call `/init` at all (API Gateway still does its own IAM SigV4 check). The auth story is "AWS IAM and that is all".

For a framework this is fine as long as it is documented — but a consumer who wants to expose `/init` to a CI system that doesn't carry AWS credentials will have to bolt their own Cognito/Lambda authoriser in front, which is *not a plug point the repo provides*.

---

## 9. Cross-reference: RESEARCH2 claims against the current code

`RESEARCH2.md` was a deep dive into execution pathways and runtime gaps. Many of its claims hold; several are outdated or subtly wrong. This section lists every RESEARCH2 claim I re-verified and flags the result.

| RESEARCH2 claim | Verified? | Notes |
|---|---|---|
| Part 1a generates SOPS keypair, stores private key in SSM advanced tier with 2h TTL (1.1 step 5) | ✅ CORRECT | `src/common/sops.py:45-80`, `store_sops_key_ssm` with `ttl_hours=2`, advanced tier, expiration policy. |
| Worker fetches key via `fetch_sops_key_ssm`; crashes if `ParameterNotFound` (1.1 step 9) | ✅ CORRECT (about the fetch), PARTIALLY CORRECT (about the crash) | `sops.py:83-90` does not catch `ParameterNotFound`; the exception propagates to `worker/run.py:26` and then up, crashing the worker. No fallback. |
| Worker subprocess env: `AWS_EXE_SYS_EVENTS_DIR` set at runtime (1.1 step 7) | ✅ CORRECT | `worker/run.py:224` sets it in `proc_env`, not `os.environ`. |
| Callback retries 3x with 2s delay (1.1 step 10) | ✅ CORRECT | `src/worker/callback.py:11-12` — `MAX_RETRIES = 3`, `RETRY_DELAY = 2`. Loop runs `MAX_RETRIES + 1 = 4` attempts total (0..3). Worth noting: the `MAX_RETRIES` constant name is actually the count of *retries*, but the loop does `attempt in range(MAX_RETRIES + 1)` so there are 4 attempts, not 3. A minor off-by-one in the RESEARCH2 description. |
| Orchestrator parses `run_id` from `tmp/callbacks/runs/<run_id>/` (1.1 step 1) | ✅ CORRECT | `orchestrator/handler.py:19-27`. |
| Lock TTL default 3600s (1.1 step 2, section 3.2) | ✅ CORRECT | `src/orchestrator/lock.py:13` — `ttl: int = 3600`. |
| "flow_id and trace_id passed but NEVER stored in lock" (section 3.1) | ❌ WRONG at the storage layer, ✅ RIGHT at the caller | `src/common/dynamodb.py:245-246` *does* store `flow_id` and `trace_id` in the lock item. But `orchestrator/handler.py:128` calls `acquire_lock(run_id, flow_id="", trace_id="")` — empty strings. So the fields are *wired*, the *caller* passes empty strings because the handler hasn't read state yet. The end result matches RESEARCH2's observation (lock rows have empty `flow_id`/`trace_id`), but the fix is trivial: read DynamoDB first and acquire the lock with the right values, or update the lock row after `read_state`. RESEARCH2's suggested fix ("store flow_id/trace_id") is a no-op — the schema already does. |
| Orchestrator handler: `release_lock` only on exception, not on success (implied by 3.2 timeline) | ❌ WRONG — release is in `finalize.py`, not `handler.py` | `src/orchestrator/finalize.py:70` and `:111` release the lock in both the "not all done" and "all done" branches. `handler.py:136` has a fallback release in the exception path. The happy path DOES release. RESEARCH2's timeline is based on a misread. |
| `read_result` raises `JSONDecodeError` on malformed result.json (2.3) | ✅ CORRECT | `src/common/s3.py:62` — unhandled `json.loads`. |
| `read_result` returns `None` on 404 (2.3) | ✅ CORRECT | `src/common/s3.py:63-64`. |
| Watchdog hardcoded 60s interval, no jitter (4.2) | ✅ CORRECT | `infra/02-deploy/step_functions.tf` — `Wait` state with `Seconds: 60`, no parameterisation. Verified via subagent. |
| Watchdog: no upper bound on Step Function duration (4.2) | ✅ CORRECT | Step Function execution will loop until `done=True` or until SFN's 1-year limit. |
| Watchdog handler writes `status="timed_out"` when `now > start_time + timeout` (4.2) | ✅ CORRECT | `src/watchdog_check/handler.py:41-55`. |
| Orchestrator invokes worker Lambda async with `InvocationType="Event"` (5.1) | ✅ CORRECT | `src/orchestrator/dispatch.py:40`. |
| Orchestrator does not poll CodeBuild/SSM service status, relies on callback + watchdog only (5.1-5.3) | ✅ CORRECT | `dispatch.py` stores only `execution_url` and `step_function_url`, never queries `codebuild:BatchGetBuilds` or `ssm:GetCommandInvocation`. |
| Gap 6.1: callback failure silent handling — worker logs and exits, no DynamoDB fallback (6.1) | ✅ CORRECT | `src/worker/callback.py:48-49` — returns `False` and the caller in `worker/run.py:237` ignores the return value. Worker has no `dynamodb:PutItem` permission on the orders table (see `iam.tf:209-232`) so even if the fallback were implemented, IAM would block it. |
| Gap 6.2: presigned URL expiry vs order timeout — no validation (6.2) | ✅ CORRECT | `src/init_job/validate.py:16-38` does not compare `job.presign_expiry` (default 7200, `models.py:91`) to any `order.timeout`. A `job_timeout=3600` with `order.timeout=14400` will pass validation and silently break. |
| Gap 6.3: orchestrator lock staleness — no heartbeat, 1h wedged window (6.3) | ✅ CORRECT, plus additional nuance | `acquire_lock` condition is `NOT_EXISTS OR status='completed'` — not `OR ttl < now`. So an "active" lock with a stale but unexpired TTL really does block for up to an hour. DynamoDB TTL cleanup is eventually consistent and can take up to 48h, but the condition does not gate on TTL anyway. |
| Gap 6.4: SOPS key expires mid-execution if order is queued too long (6.4) | ✅ CORRECT | `sops.py:49` — `ttl_hours=2` default, no caller overrides it, no relationship to `order.timeout` or `job.job_timeout`. A concurrency-throttled Lambda can easily exceed 2h in queue. |
| Gap 6.5: status update happens *after* dispatch, so a failed DynamoDB update = duplicate dispatch (6.5) | ✅ CORRECT | `src/orchestrator/dispatch.py:149-157` dispatches, then `:160-169` updates status. There is no pre-dispatch `status=RUNNING` marker. Combined with the idempotency key absence, this is a real duplicate-execution risk under DynamoDB throttling. |
| SSM document: `curl` failure ignored via `|| true` (1.3 step 5) | ✅ CORRECT | Verified via subagent read of `infra/02-deploy/ssm_document.tf`. |
| SSM: credentials stored plaintext in DynamoDB via `env_dict` (1.3 "GAP") | ✅ CORRECT | `src/common/models.py:213` — `env_dict: Optional[Dict[str, str]]` on `OrderRecord`. `src/ssm_config/insert.py` writes it. Any operator with `dynamodb:GetItem` on the orders table can read any SSM order's secrets in plain text. This is a real confidentiality gap that RESEARCH2 correctly flags. |

**New gaps RESEARCH2 did not mention** (found during this verification pass):

1. **Event SK format drift** (§7.3) — `CONTRACT.md` is wrong about the SK shape.
2. **Event TTL bug** (§7.4) — 15 minutes instead of 90 days.
3. **SSM prefix drift** — `sops.py:57` default is `exe-sys`, `lambdas.tf:8` sets `AWS_EXE_SYS_SSM_PREFIX = "exe-sys"`, `iam.tf:229` hardcodes `parameter/exe-sys/sops-keys/*`, but `docs/ARCHITECTURE.md:101,466,572` uses `/aws-exe-sys/sops-keys/...`. Code is internally consistent on `exe-sys`; docs are wrong. A reader debugging from the doc will look in the wrong SSM path.
4. **`fetch_ssm_values` base64/JSON contract is undocumented** (§4.2). Anyone who reads `VARIABLES.md` and puts a plain token in SSM will get a `binascii.Error` or `json.JSONDecodeError` at init_job runtime. Since errors surface only under a load test, this is a silent onboarding hazard.
5. **`use_lambda` backward-compat fallback promised by `docs/ARCHITECTURE.md:387,394` does not exist in `dispatch.py`** (§6.3).
6. **`docs/REPO_STRUCTURE.md:33` lists a `src/init_job/pr_comment.py` that does not exist** (§3.2).
7. **Worker `s3:GetObject` is scoped to `tmp/exec/*`** (`iam.tf:219`), not the whole internal bucket. That is tighter than RESEARCH2 implied but also means any design that relies on workers reading *other* bucket prefixes (shared artifacts, checkpoints, distributed locks) would need IAM changes.
8. **`orchestrator/handler.py:128` uses empty strings for `flow_id` / `trace_id` at lock acquisition time** — the schema stores them (§9 table above), but the caller never populates them. One-line fix.
9. **Cycle detection in the dependency graph is absent**. `src/orchestrator/evaluate.py:29-67` re-evaluates each invocation and never detects `A -> B -> A`. `handler.py:53` is `while True: ready, failed_deps, waiting = evaluate_orders(orders); if not failed_deps: break` — this terminates because `failed_deps` is only populated from explicit `FAILED_STATUSES`, not from cycles, but a cycle means *no order ever becomes ready or failed* so subsequent orchestrator invocations will repeatedly loop through the same state. This was in RESEARCH2's summary table (severity LOW) but not in the detailed gap sections. Worth elevating.

---

## 10. Doc/code drift summary

| Location | Says | Code says | Impact |
|---|---|---|---|
| `CONTRACT.md:101` | SK: `{order_name}:{epoch}` | `{order_name}:{epoch}:{event_type}` (`dynamodb.py:162`) | Breaks contract for consumers |
| `CONTRACT.md:150-154` | "External callers: TBD (JWT planned)" | JWT *credential injection* implemented, not *auth*; API Gateway is still IAM SigV4 | Confusing — the JWT path does something different from what's implied |
| `CLAUDE.md:85`, `docs/ARCHITECTURE.md` TTL diagram | Event TTL 90 days | 900 seconds = 15 minutes (`dynamodb.py:170`) | Live bug |
| `docs/ARCHITECTURE.md` step 5/finalize | Init/final PR comments posted by engine | Not implemented; `CLAUDE.md:28` says PR comments disabled | Docs misleading |
| `docs/REPO_STRUCTURE.md:33` | `src/init_job/pr_comment.py` exists | File absent | Docs reference phantom file |
| `docs/ARCHITECTURE.md:101,466,572` | SSM SOPS path `/aws-exe-sys/sops-keys/…` | `/exe-sys/sops-keys/…` (all infra + code) | Ops docs wrong |
| `docs/ARCHITECTURE.md:387,394` | `use_lambda` backward-compat | Not implemented (`dispatch.py:146`) | Docs overstate |
| `docs/VARIABLES.md:14` | `aws:::secretd:` credential prefix | Not parsed (`code_source.py:46-54`) | Docs overstate |
| `docs/VARIABLES.md:38` | `execution_target` default `"codebuild"` | Agrees (`models.py:64,201`) and `statuses.py:11` | ✅ consistent |
| `docs/VARIABLES.md:14` | "SSM path containing git token" | Must be base64-encoded JSON dict (`code_source.py:15-30`) | Docs under-specify |
| `TODO/sops-key-ssm-storage.md` | Private key lost, workers can't decrypt | Fully implemented in `sops.py:45-99`, `repackage.py:50-56`, `worker/run.py:25-34`, `dispatch.py:35-58`, `finalize.py:93-99` | Stale TODO, should be deleted |

---

## 11. Recommendations, ordered for a real "make this a framework" effort

**P0 — Fix the live bugs first (these are just bugs, not framework work).**

1. **Event TTL.** Change `dynamodb.py:170` from `epoch + 900` to `epoch + 172800` (2 days per the author's inline comment) or to `epoch + 86400*90` (90 days per CLAUDE.md). Pick one, update the docs to match.
2. **Event SK format.** Either change `dynamodb.py:162` to `{order_name}:{epoch}` and find a different dedup mechanism for same-second events, or update `CONTRACT.md` to the current format. Consumers are relying on the contract; the contract is wrong.
3. **Lock flow/trace ids.** Change `orchestrator/handler.py:128` to read the first order's `flow_id`/`trace_id` before acquiring the lock, or move lock acquisition inside `execute_orders` after `read_state`.
4. **Delete the stale TODO.** `TODO/sops-key-ssm-storage.md` describes work that exists. Remove the file.
5. **Remove the dead PR-comment references** from `docs/ARCHITECTURE.md` and `docs/REPO_STRUCTURE.md`, or re-implement them. One or the other.
6. **Presign-expiry validation.** In `init_job/validate.py` enforce `job.presign_expiry >= max(order.timeout for order in orders) + buffer`. Fail fast at submission instead of silently at callback time. This is the `RESEARCH2 6.2` gap.
7. **SSM env_dict secrecy.** Either encrypt `env_dict` with KMS at rest in DynamoDB, or stop storing it there and pass it through the SSM command parameters directly (it already is in `dispatch.py:79-80`, so the DynamoDB copy is arguably redundant).

**P1 — Turn on the pluggable-framework seam that already exists.**

8. **Wire `bootstrap_handler.py` into at least one Lambda as a proof.** Add a `vars.tf` variable `engine_code_url_ssm_path`, add the env var, switch one `image_config.command` to `["src.bootstrap_handler.handler"]`, set `ENGINE_HANDLER`, IAM-grant `ssm:GetParameter` on that path. Test cold-start. Then do the same for the other four Lambdas.
9. **Integrity-verify the bootstrap tarball.** Manifest with SHA256, verify on load. Without this, enabling the seam is a framework-scale RCE risk.

**P2 — Fix the abstractions that look pluggable but aren't.**

10. **Credential providers: introduce a registry.** Parse the full URI (`vendor:::kind:path`) in one place, dispatch to a provider protocol that has `fetch(path) -> dict[str, str]`. Register `aws_ssm`, `aws_secretsmanager`, leave a decorator hook for third-party registrations. Remove the SSM base64/JSON contract and treat values as strings unless the scheme is `aws:::ssmjson:` or similar.
11. **VCS providers: rebuild behind a single interface.** Merge clone + status + comment + metadata into one `VcsProvider` protocol. Move `clone_repo` out of `code_source.py` and into the provider. Expose a `register_provider(name, cls)` public API. Update `clone_repo` to look up the provider by the job's `git_provider` field (default `"github"`).
12. **Code sources: introduce a `CodeSource` protocol.** Implementations: `git`, `s3`, `http_tarball`, `commands_only`. Registry-based. Both `init_job/repackage.py` and `ssm_config/repackage.py` call `sources[order.code_source_kind].fetch(order) -> code_dir`. This deletes the Phase-1/Phase-2/Phase-3 split in both repackage modules.
13. **Execution targets: introduce an `ExecutionTarget` protocol.** Implementations: `lambda`, `codebuild`, `ssm`. Registry-based. `dispatch.py:146-157` collapses to `TARGETS[order.execution_target].dispatch(order, ...)`. Leaves room for ECS, Fargate, Batch.
14. **Event sink: abstract behind `EventSink`.** Default implementation writes to DynamoDB. Optional implementations can mirror to CloudWatch Logs, SNS, or a webhook. Useful for consumers who want events in their own SIEM.

**P3 — The hard problems RESEARCH2 already identified.**

15. **Lock staleness.** Either (a) reduce lock TTL to 60s and have the orchestrator heartbeat, or (b) make the acquire condition `NOT_EXISTS OR status='completed' OR ttl < :now` — `:now` being an expression-attribute-value — so a genuinely expired lock is stealable immediately, not after DynamoDB's eventually-consistent TTL sweep. Option (b) is probably the smaller change.
16. **Pre-dispatch status update.** Move `update_order_status(RUNNING)` in `dispatch.py` *before* the `_dispatch_<target>` call, inside a DynamoDB conditional write (`status == QUEUED`). If the conditional fails, skip dispatch. This is idempotent against orchestrator re-invocation and DynamoDB throttling.
17. **Worker callback fallback.** Decide the canonical failure mode: either grant the worker `dynamodb:UpdateItem` on the orders table (narrow — just status + `last_update`) and have `callback.send_callback` fall back to a direct DynamoDB write, or pipe worker output through CloudWatch Logs and have the watchdog subscribe to a metric filter. The current state — watchdog-only — leaves every callback failure visible as a `TIMED_OUT` even when the work succeeded.
18. **Watchdog jitter + bound.** Add ±10s jitter to the Wait state; add a `MaxAttempts` guard to cap total watchdog lifetime at `order.timeout + N`. Thundering herd becomes a real problem once you have 100s of concurrent orders.
19. **Cycle detection.** One DFS over the dependency graph at `validate.py` time. Return `["order {X} has a cyclic dependency on {Y}"]`. Fail at submission.

**P4 — Nice to have, but essential for a framework claim.**

20. **Public Python API.** Right now a consumer importing `src.init_job.handler` and calling `process_job_and_insert_orders` works, but the surface is undocumented and the module paths start with `src.`, which is a deployment-time hack, not a package layout. Move to `aws_exe_sys/…` as an installable package, keep `src.*` as a compat shim.
21. **Stable result/result-event schema.** Lock it into a Pydantic model (the project already uses dataclasses; Pydantic is a natural upgrade given CLAUDE.md's code-style note). Version it: `v1` in CONTRACT.md, enforced at put-time.
22. **Drift test in CI.** A test that reads `CONTRACT.md` + `CLAUDE.md` + `docs/**.md` for claims (regex on `format:`, `TTL:`, `default:`) and fails if any claim no longer matches the code. This is the *only* way to stop the drift pile from growing again.

---

## 12. How to read this document alongside RESEARCH2

- **RESEARCH2** is about execution-path robustness: what happens when something goes wrong at runtime. Its recommendations are about timeouts, retries, locks, and fallbacks.
- **RESEARCH3** (this document) is about extensibility: what you can plug in without forking. Most of its recommendations are about surfacing registries and decoupling hardcoded branches.

They overlap at the "drift" findings in §9 and §10: a few things RESEARCH2 flagged as runtime bugs are actually doc/code disagreements that should be fixed before touching runtime behavior, because the current contract is not what CONTRACT.md describes.

If you are planning implementation work based on both documents, the order is:

1. P0 from this document — the live bugs and doc drifts (days).
2. RESEARCH2 Priority 1 items, filtered through P3 here (lock, dispatch ordering, presign validation).
3. P1 — bootstrap seam, because it changes the deployment story and everyone else will have to rebase on it.
4. P2 — the real pluggable-framework work.
5. P3/P4 — once there is a stable platform, the hardening and developer-experience work.

---

## Appendix A — File reference index

Every file cited in this document:

- `CLAUDE.md`
- `CONTRACT.md`
- `README.md`
- `RESEARCH2.md`
- `TODO/sops-key-ssm-storage.md`
- `docs/ARCHITECTURE.md`
- `docs/REPO_STRUCTURE.md`
- `docs/VARIABLES.md`
- `infra/02-deploy/iam.tf`
- `infra/02-deploy/lambdas.tf`
- `infra/02-deploy/s3_notifications.tf`
- `infra/02-deploy/step_functions.tf`
- `infra/02-deploy/ssm_document.tf`
- `src/bootstrap_handler.py`
- `src/common/bundler.py`
- `src/common/code_source.py`
- `src/common/dynamodb.py`
- `src/common/flow.py`
- `src/common/lambda_handler.py`
- `src/common/models.py`
- `src/common/s3.py`
- `src/common/sops.py`
- `src/common/statuses.py`
- `src/common/trace.py`
- `src/common/vcs/base.py`
- `src/common/vcs/github.py`
- `src/common/vcs/helper.py`
- `src/init_job/handler.py`
- `src/init_job/insert.py`
- `src/init_job/repackage.py`
- `src/init_job/upload.py`
- `src/init_job/validate.py`
- `src/orchestrator/dispatch.py`
- `src/orchestrator/evaluate.py`
- `src/orchestrator/finalize.py`
- `src/orchestrator/handler.py`
- `src/orchestrator/lock.py`
- `src/orchestrator/read_state.py`
- `src/ssm_config/handler.py`
- `src/ssm_config/insert.py`
- `src/ssm_config/repackage.py`
- `src/ssm_config/validate.py`
- `src/watchdog_check/handler.py`
- `src/worker/callback.py`
- `src/worker/handler.py`
- `src/worker/run.py`
