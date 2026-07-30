# AWS execution engine guidance

`CONTRACT.md` owns the caller payload, dispatch, and result contract.

Keep these high-value invariants in mind when changing the engine:

- `aws_exe_sys` is a generic AWS-native execution helper with three Lambda entry points: `init_job`, `worker`,
  and `finalizer`.
- Preserve the seven-field `SimplePayload` caller contract and the existing Lambda dispatch path.
- `lambda` dispatch asynchronously invokes `AWS_EXE_SYS_WORKER_LAMBDA`; `codebuild` dispatch asynchronously
  starts `AWS_EXE_SYS_CODEBUILD_STATE_MACHINE_ARN`.
- The Standard CodeBuild workflow passes all seven payload fields as plain-string CodeBuild environment
  overrides, waits for a terminal build state, and invokes the finalizer.
- `done_endpoint` is the only caller completion marker. The worker is the primary result writer; the finalizer
  only creates a missing failed fallback with `If-None-Match: *` and propagates non-precondition S3 errors.
