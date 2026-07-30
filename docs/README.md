# aws-exe-sys docs

A generic AWS execution interface with three Lambda entry points:

- `init_job`: validates and dispatches work requests.
- `worker`: executes shell-command payloads on Lambda or CodeBuild.
- `finalizer`: creates only a missing failed result after a terminal CodeBuild run.

## Main contracts

- [CONTRACT.md](../CONTRACT.md)
- [variables](VARIABLES.md)

## Quick links

- [`ARCHITECTURE.md`](ARCHITECTURE.md)
- [`ARCHITECTURE_DIAGRAM.md`](ARCHITECTURE_DIAGRAM.md)
- [`DEPLOY.md`](DEPLOY.md)
- [`REPO_STRUCTURE.md`](REPO_STRUCTURE.md)
