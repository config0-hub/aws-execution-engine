# Architecture diagram

```text
caller / producer
    |
    v
  [init_job]
    | dispatch (lambda|codebuild)
    +-------------------+
    |                   |
    v                   v
 [worker Lambda]    [CodeBuild worker]
    \___________________/
             |
             v
      write `ExecutionResult`
       to `done_endpoint`
```
