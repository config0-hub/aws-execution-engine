# Repository structure

```text
aws-exe-sys/
├── aws_exe_sys/
│   ├── common/
│   │   ├── payload.py          # SimplePayload + validation
│   │   ├── lambda_handler.py    # event normalization helpers
│   │   ├── result_writer.py     # ExecutionResult schema + S3 writer
│   │   ├── sops.py             # sops decrypt path
│   │   ├── statuses.py
│   │   └── subprocess_runner.py # command execution helpers
│   ├── init_job/
│   │   ├── handler.py          # API/Lambda entry for dispatch
│   │   ├── validate.py         # preflight resource checks
│   │   └── dispatcher.py       # Lambda/Step Functions dispatch targets
│   ├── finalizer/
│   │   └── handler.py          # atomic missing-result fallback
│   └── worker/
│       ├── handler.py          # worker Lambda entry
│       ├── run.py              # execute payload commands and write result
│       └── entrypoint.sh
├── infra/
│   ├── 00-bootstrap
│   └── 02-deploy
├── tests/
│   ├── integration/
│   ├── smoke/
│   ├── unit/
│   └── testdata/
├── docs/
├── scripts/
│   ├── build-release-zip.sh    # standalone public artifact build
│   └── build-zip.sh            # private compatibility build
└── .github/workflows/
```

## Entry points

- `aws_exe_sys.init_job.handler.handler`
- `aws_exe_sys.worker.handler.handler`
- `aws_exe_sys.finalizer.handler.handler`
