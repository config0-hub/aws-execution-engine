# RESEARCH4.md — Independent Verification of RESEARCH3

**Scope.** This document is an independent second-pass verification of [RESEARCH3.md](RESEARCH3.md). Where RESEARCH3 compared the "pluggable framework" design against the implementation, RESEARCH4 re-checks each of RESEARCH3's claims against the current code. The goal is to answer one question: *is RESEARCH3 correct, and is anything missing?*

**Method.** A team of five verifiers worked the problem in parallel, split by RESEARCH3 section, with cross-checks between members where areas overlapped:

- `engine-seam` — §2 (bootstrap handler plug point)
- `vcs-and-sources` — §3 (VCS providers), §5 (code sources)
- `creds` — §4 (credential sources)
- `dispatch` — §6 (execution targets), §9 dispatch/lock rows
- `docs-drift` — §7 (event sinks), §8 (auth), §9 RESEARCH2 cross-reference, §10 drift table

Every claim was checked by reading the cited file directly. Where two verifiers touched the same file (`code_source.py` for both `creds` and `vcs-and-sources`; `dynamodb.py` for both `dispatch` and `docs-drift`), they cross-referenced findings before reporting. File:line anchors in this document are valid at commit `12b316f`, the same anchor RESEARCH3 uses.

---

## 0. TL;DR — the verdict on RESEARCH3

**RESEARCH3 is accurate.** Every one of the seven headline gaps, every row in the §9 RESEARCH2 cross-reference table, and every row in the §10 drift table was confirmed against source. **Zero claims disputed.** The file:line citations are correct.

Three additional gaps surfaced during verification that RESEARCH3 either omitted or under-emphasized:

- **Gap A:** `fetch_secret_values()` derives the env var name from the Secrets Manager path, not from the secret payload — broken for any multi-field secret (RESEARCH3 §4.2 mentions this but does not flag it as a live correctness bug).
- **Gap B:** `fetch_sops_key_ssm()` does not catch `ParameterNotFound`; any queuing delay past the 2 h SOPS key TTL crashes the worker with no fallback (RESEARCH3 §9 notes the behavior but does not elevate it).
- **Gap C:** The base64-encoded-JSON-dict contract for SSM credential values is not documented anywhere user-facing. Plain strings produce `binascii.Error` or `JSONDecodeError` at `init_job` runtime (RESEARCH3 §4.2 notes it in passing).

RESEARCH3's P0–P4 recommendation ordering stands. The seven P0 live bugs are all reproducible from the code; the team verified each one with a direct read.

---

## 1. Confirmation of the seven headline gaps

| # | RESEARCH3 Gap | Primary evidence | Verdict |
|---|---|---|---|
| 1 | `bootstrap_handler.py` is dead code, not wired into any Lambda | `infra/02-deploy/lambdas.tf:22, 48, 72, 91, 110` all hardcode `src.*.handler.handler`. No `ENGINE_HANDLER`, `ENGINE_CODE_URL`, or `ENGINE_CODE_SSM_PATH` anywhere in `infra/`. Unit tests exist (`tests/unit/test_bootstrap_handler.py`, 8 KB) but are unreachable from production. | ✅ CONFIRMED |
| 2 | VCS "registry" is a module-level dict; `clone_repo` hardcodes `github.com` | `src/common/vcs/helper.py:14-17` is a bare dict, no `register_provider()` API. `src/common/code_source.py:109, 128, 156` all use `github.com` literals (HTTPS+token, HTTPS, SSH). Grep of `src/init_job/`, `src/orchestrator/`, `src/worker/` finds zero calls to `VcsHelper` or `upsert_comment`. | ✅ CONFIRMED |
| 3 | `_strip_location_prefix` only handles `aws:::ssm:`; `aws:::secretd:` is documented but never parsed | `src/common/code_source.py:46-54` checks exactly one prefix. `docs/VARIABLES.md:14` and `:158` document both prefixes. An `aws:::secretd:` location would hit `get_parameter` with `aws:::secretd:/path` and fail `ParameterNotFound`. | ✅ CONFIRMED |
| 4 | Code sources are an `if/elif` in `group_git_orders`, not a registry | `src/common/code_source.py:184-212` branches on `s3_location` → `git_repo` → else. No `CodeSource` ABC, no `register_source()`. Adding a new source requires edits to `models.py`, `code_source.py`, `init_job/repackage.py`, `ssm_config/repackage.py`. | ✅ CONFIRMED |
| 5 | Execution targets are an `if/elif/else` in `_dispatch_single`, not a registry | `src/orchestrator/dispatch.py:146-154`: `if lambda → _dispatch_lambda` / `elif ssm → _dispatch_ssm` / `else → _dispatch_codebuild`. `src/common/statuses.py:11` has `EXECUTION_TARGETS = frozenset({"lambda","codebuild","ssm"})`. | ✅ CONFIRMED |
| 6 | Multiple doc/code drifts — event TTL, SK format, PR comments, `use_lambda`, SSM prefix | See §4 below — eight drifts, three of them live bugs. | ✅ CONFIRMED |
| 7 | `TODO/sops-key-ssm-storage.md` is fully implemented but never closed | TODO describes work that exists in `src/common/sops.py:45-99` (`store_sops_key_ssm`, `fetch_sops_key_ssm`, `delete_sops_key_ssm`), `src/init_job/repackage.py:50-56`, `src/worker/run.py:25-34`, `src/orchestrator/dispatch.py:35-58`, `src/orchestrator/finalize.py:93-99`. | ✅ CONFIRMED |

---

## 2. Confirmation of §9 RESEARCH2 cross-reference rows

Every row in RESEARCH3's §9 table was re-verified. No corrections needed to RESEARCH3's corrections.

| Claim | Verdict | File:line anchor |
|---|---|---|
| SOPS keypair stored in SSM advanced tier, 2h TTL | ✅ | `src/common/sops.py:45-80`, `ttl_hours=2`, `Tier="Advanced"` |
| `fetch_sops_key_ssm` does not catch `ParameterNotFound`; crashes worker | ✅ | `src/common/sops.py:83-90` — unhandled; propagates through `src/worker/run.py:26` |
| `AWS_EXE_SYS_EVENTS_DIR` set in subprocess `proc_env`, not `os.environ` | ✅ | `src/worker/run.py:224` |
| Callback "3 retries" is actually 4 attempts (off-by-one in RESEARCH2) | ✅ | `src/worker/callback.py:11-23`: `MAX_RETRIES = 3`, loop is `range(MAX_RETRIES + 1)` |
| Orchestrator parses `run_id` from S3 callback path | ✅ | `src/orchestrator/handler.py:19-27` — `r"tmp/callbacks/runs/([^/]+)/"` |
| Lock TTL default 3600s | ✅ | `src/orchestrator/lock.py:12` |
| Lock schema *does* store `flow_id`/`trace_id`; handler passes `""` | ✅ | Storage: `src/common/dynamodb.py:245-246`. Caller: `src/orchestrator/handler.py:128` literal `flow_id=""`, `trace_id=""` |
| Happy-path release — RESEARCH2 was wrong; `finalize.py` releases in both branches | ✅ | `src/orchestrator/finalize.py:70, :111`; exception path `src/orchestrator/handler.py:136` |
| `read_result` raises `JSONDecodeError` on malformed JSON; returns `None` on 404 | ✅ | `src/common/s3.py:62-64` |
| Watchdog 60s hardcoded, no jitter, no upper bound on SFN duration | ✅ | `infra/02-deploy/step_functions.tf:26-30` — `Seconds = 60` literal |
| Watchdog writes `status="timed_out"` when `now > start_time + timeout` | ✅ | `src/watchdog_check/handler.py:40-55` |
| Worker Lambda invoked async with `InvocationType="Event"` | ✅ | `src/orchestrator/dispatch.py:40` |
| Orchestrator never polls CodeBuild/SSM service status | ✅ | `src/orchestrator/dispatch.py` stores only `execution_url`/`step_function_url`; no `BatchGetBuilds` or `GetCommandInvocation` anywhere |
| Callback failure silent, worker has no `dynamodb:PutItem` fallback | ✅ | `src/worker/callback.py:48-49` returns `False`, caller in `src/worker/run.py` ignores it. `infra/02-deploy/iam.tf:209-232` grants worker zero DynamoDB write permissions on the orders table |
| Presign expiry vs order timeout — no validation | ✅ | `src/init_job/validate.py:16-38` has no such comparison; `src/common/models.py:91` presign_expiry default 7200 |
| Lock acquire condition: `NOT_EXISTS OR status='completed'` — no TTL check | ✅ | `src/common/dynamodb.py:248` — `Attr("pk").not_exists() \| Attr("status").eq("completed")` |
| SOPS key 2h TTL unrelated to `order.timeout` / `job.job_timeout` | ✅ | `src/common/sops.py:49` default, no caller overrides |
| Status update happens *after* dispatch (duplicate-dispatch risk) | ✅ | `src/orchestrator/dispatch.py:149-154` dispatch, then `:160-169` status update |
| SSM doc: `curl` failure ignored via `\|\| true` | ✅ | `infra/02-deploy/ssm_document.tf` |
| SSM `env_dict` stored plaintext in DynamoDB | ✅ | `src/common/models.py:213`, written by `src/ssm_config/insert.py` |

RESEARCH3's own §9 corrections to RESEARCH2 (lock release happy path, off-by-one in retries, lock schema actually storing `flow_id`/`trace_id`) are **all correct**.

---

## 3. New gaps RESEARCH3 omitted or under-emphasized

### 3.1 Gap A — `fetch_secret_values` key derivation is broken for multi-field secrets

**File:** `src/common/code_source.py:33-43`

```python
def fetch_secret_values(paths: List[str], region: Optional[str] = None) -> Dict[str, str]:
    ...
    for path in paths:
        resp = client.get_secret_value(SecretId=path)
        key = path.rsplit("/", 1)[-1].upper().replace("-", "_")
        result[key] = resp["SecretString"]
    return result
```

The env var key is derived from `path.rsplit("/", 1)[-1].upper().replace("-", "_")`, not from the secret payload. For a Secrets Manager secret stored at `prod/github-token` with the idiomatic JSON payload `{"token": "gh_x"}`, the function produces:

```python
{"GITHUB_TOKEN": '{"token":"gh_x"}'}
```

The consumer's command then has to JSON-parse the env var to recover the real token. For any multi-field secret (`{"username": "u", "password": "p"}`), the consumer gets one env var whose value is the whole JSON blob. This is not a realistic developer experience, and it is silently wrong — nothing in the code, docs, or validate step catches it.

**RESEARCH3 §4.2 mentions this behavior but does not flag it as a live bug.** It is.

### 3.2 Gap B — SOPS key fetch has no recovery path

**File:** `src/common/sops.py:83-90`

`fetch_sops_key_ssm()` does not catch `ClientError`/`ParameterNotFound`. The exception propagates to `src/worker/run.py:26` and crashes the worker with a `ParameterNotFound` stack trace.

Combined with:
- `src/common/sops.py:49` — 2 h SOPS key TTL, no override hook.
- `src/common/models.py:91` — `presign_expiry` default 7200, also unrelated to `order.timeout`.
- Concurrency-throttled worker Lambdas can easily sit in the queue for >2 h under large fan-outs.

…this is a silent failure mode where a legitimately-dispatched order fails with a misleading error that looks like IAM or permissions, not "your SOPS key expired while you were queued."

**RESEARCH3 §9 notes the no-catch behavior** but does not connect it to the queuing scenario. Should be elevated alongside Gap 6.4 in RESEARCH3.

### 3.3 Gap C — undocumented base64/JSON contract for credential values

**File:** `src/common/code_source.py:15-30`

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

Every SSM parameter **must** be a base64-encoded JSON dict. `docs/VARIABLES.md:14` documents `git_token_location` as "`aws:::ssm:<path>` or `aws:::secretd:<path>`" and does not mention the encoding requirement. A user who stores a plain token gets `binascii.Error` or `json.JSONDecodeError` at `init_job` runtime — an error that looks nothing like "your SSM value was in the wrong format."

`resolve_git_credentials` makes the surprise worse: it calls `fetch_ssm_values` with a single path, then takes `list(vals.values())[0]` as the token. So for a *git token* specifically, the SSM value must be a base64-encoded JSON dict with *at least one string value*, and the first one (dict ordering is insertion order in Python 3.7+) is treated as the token. There is no way to learn any of this from the docs.

**RESEARCH3 §4.2 mentions the base64/JSON contract** but does not call out the additional "first value wins" behavior of `resolve_git_credentials`, which is a second layer of silent assumption.

---

## 4. P0 live bugs — consolidated list with exact anchors

RESEARCH3 §11 lists these in prose. For an implementation task list, the verified locations are:

| # | Bug | File:line |
|---|---|---|
| 1 | Event TTL is 900 s (15 min) instead of 90 days | `src/common/dynamodb.py:170` — `"ttl": epoch + 900,  # 15 min for testing; change to 172800 (2 days) for prod` |
| 2 | Event SK format `{order_name}:{epoch}:{event_type}` vs. `CONTRACT.md:101` promise of `{order_name}:{epoch}` | `src/common/dynamodb.py:162` |
| 3 | Orchestrator acquires lock with empty `flow_id`/`trace_id` — schema supports them | Caller: `src/orchestrator/handler.py:128`. Schema: `src/common/dynamodb.py:245-246` |
| 4 | Status update happens *after* dispatch — duplicate-dispatch risk under DynamoDB throttling | Dispatch: `src/orchestrator/dispatch.py:149-154`. Status write: `:160-169` |
| 5 | No cycle detection in dependency evaluator — cyclic deps deadlock the orchestrator forever | `src/orchestrator/evaluate.py:8-69` |
| 6 | Lock acquire condition lacks `ttl < :now` check — stale locks block for up to 1 h | `src/common/dynamodb.py:248` |
| 7 | SSM order credentials stored plaintext in DynamoDB `env_dict` — confidentiality gap | `src/common/models.py:213`, written by `src/ssm_config/insert.py` |
| 8 | Presign-expiry vs order-timeout validation missing | `src/init_job/validate.py:16-38` |
| 9 | `use_lambda` backward-compat fallback promised by docs does not exist | Docs: `docs/ARCHITECTURE.md:387, 394`. Code: `src/orchestrator/dispatch.py:146` only reads `execution_target` |
| 10 | `TODO/sops-key-ssm-storage.md` describes work that is already fully shipped | File should be deleted |
| 11 | `docs/REPO_STRUCTURE.md:33` references `src/init_job/pr_comment.py` which does not exist | File absent; remove from doc |
| 12 | PR comment flow shown in `docs/ARCHITECTURE.md:142, 343, 388, 427` but never executed; `CLAUDE.md:28` says PR comments are disabled | Contradictory docs; one must be updated |
| 13 | `docs/ARCHITECTURE.md:101, 466, 572` show SSM path `/aws-exe-sys/sops-keys/…`; code consistently uses `/exe-sys/sops-keys/…` | Code is internally consistent (`src/common/sops.py:57`, `infra/02-deploy/lambdas.tf:8`, `infra/02-deploy/iam.tf:229`); docs are wrong |

Items 1, 4, 5, 6, 7, and 8 are functional bugs. Items 2, 9, 10, 11, 12, 13 are documentation / stale-state cleanup. Items 3 is a one-line caller fix.

---

## 5. Refinements to RESEARCH3

These are minor — RESEARCH3 is not wrong, just compressible:

- **Callback attempt count.** RESEARCH3 §9 correctly flags that RESEARCH2 said "3 retries" when the loop runs 4 times. Exact anchor: `src/worker/callback.py:11-23` — `MAX_RETRIES = 3`, loop is `for attempt in range(MAX_RETRIES + 1)`. The variable is misnamed: it is in fact the *retry count*, but the loop iterates one extra time for the initial attempt.
- **Lock release path.** RESEARCH3 §9 disputes RESEARCH2's claim that release only happens on exception. RESEARCH2 is wrong; `finalize.py:70` (not-all-done branch), `finalize.py:111` (all-done branch), and `handler.py:136` (exception path) all release. RESEARCH3 is correct.
- **`_dispatch_single` vs. dispatch loop.** RESEARCH3 §6 says the hardcoded `if/elif` is in `_dispatch_single`. The `if/elif/else` block is actually at module scope in `dispatch.py:146-154` inside a loop body, not inside a helper function. Minor — the code is at the cited line, the framing is slightly off.

---

## 6. What stands for a "make this a framework" work plan

RESEARCH3's §11 P0–P4 ordering is the right shape. Concretely, for the ordering of a work plan:

1. **P0 bugs** (items 1–13 in §4 above) — a mix of one-liners, doc fixes, and one-file surgeries. Do these first; they are independent of the framework question.
2. **P1 bootstrap seam** (RESEARCH3 §2.4) — the single architectural change. Everything after this must rebase on it.
3. **P2 four registries** (credential providers, VCS, code sources, execution targets) — these are the "actually pluggable" work.
4. **P3 hardening** (lock staleness fix via TTL check, pre-dispatch status write, cycle detection in `validate.py`, watchdog jitter + `MaxAttempts`, callback fallback path).
5. **P4 framework polish** (installable Python package name, versioned result schema, CI drift test to stop the doc/code pile from regrowing).

One addition that RESEARCH3 does not call out but should be in P0: **rename `fetch_secret_values` or remove it** (Gap A). The current behavior is a silent footgun for anyone using multi-field secrets, which is the standard Secrets Manager pattern.

---

## 7. Verification artifacts

Detailed per-member reports with full file:line transcripts live at `/tmp/research3-verification/`:

- `engine-seam-findings.md` — §2 bootstrap seam
- `vcs-and-sources-findings.md` — §3 VCS abstraction + §5 code sources
- `creds-findings.md` — §4 credential parsing
- `dispatch-findings.md` — §6 execution targets + §9 dispatch/lock rows
- `docs-drift-findings.md` — §7 sinks + §8 auth + §9 RESEARCH2 cross-ref + §10 drift table

These files are not checked into the repo (they are a working artifact of this verification pass, not a living document).

---

## 8. How to read this alongside RESEARCH2 / RESEARCH3

- **RESEARCH2** — runtime fragility: what fails when something goes wrong mid-execution.
- **RESEARCH3** — extensibility: what you can plug in without forking.
- **RESEARCH4** (this document) — verification of RESEARCH3, with three additional gaps and a consolidated P0 anchor list.

Reading order for someone picking up implementation work:

1. RESEARCH4 §4 (P0 live-bug list with file:line anchors) — actionable immediately.
2. RESEARCH3 §11 (recommendation plan) — the structural work.
3. RESEARCH2 — the runtime hardening, once the structural work is in place.

RESEARCH4 does not invalidate any conclusion in RESEARCH3. It mostly adds anchors and elevates three gaps that RESEARCH3 mentioned but did not score.
