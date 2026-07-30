# Architecture diagram

```text
caller / producer
    |
    v
  [init_job]
    | dispatch (lambda|codebuild)
    +----------------------+------------------------------+
    |                                                     |
    v                                                     v
 [worker Lambda]                           [Standard Step Functions]
    |                                                     |
    |                                          CodeBuild StartBuild.sync
    |                                                     |
    |                                             [CodeBuild worker]
    |                                                     |
    |                                      detailed result attempt
    |                                                     |
    |                                             [finalizer Lambda]
    |                                                     |
    |                              conditional PutObject If-None-Match: *
    |                                                     |
    +----------------------+------------------------------+
                           |
                           v
                  `done_endpoint` in S3
```

The finalizer preserves an existing worker result. It creates only a missing failed fallback and propagates
all non-precondition S3 errors.
