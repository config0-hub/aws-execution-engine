# Production image for aws-execution-engine: runs the worker as a CodeBuild
# custom image (`infra/02-deploy/codebuild.tf` currently installs sops/age via
# buildspec and unzips engine.zip; this image ships the same runtime content
# pre-baked). Base Python version matches the Lambda runtime in
# infra/02-deploy/lambdas.tf. No restrictive ENTRYPOINT: CodeBuild must be
# free to run its own buildspec commands in this container.
FROM python:3.14-slim

# Pinned to match infra/02-deploy/codebuild.tf (SOPS_URL/AGE_URL) and
# scripts/build-zip.sh (TOFU_VERSION). Bump deliberately, in both places.
ARG TOFU_VERSION=1.8.8
ARG SOPS_VERSION=3.9.4
ARG AGE_VERSION=1.2.1

RUN apt-get update \
    && apt-get install -y --no-install-recommends bash ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# OpenTofu
RUN curl -fsSL "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" \
        -o /tmp/tofu.zip \
    && unzip -j /tmp/tofu.zip tofu -d /usr/local/bin \
    && chmod +x /usr/local/bin/tofu \
    && rm /tmp/tofu.zip

# sops
RUN curl -fsSL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
        -o /usr/local/bin/sops \
    && chmod +x /usr/local/bin/sops

# age / age-keygen
RUN curl -fsSL "https://dl.filippo.io/age/v${AGE_VERSION}?for=linux/amd64" \
        | tar xz --strip-components=1 -C /usr/local/bin age/age age/age-keygen \
    && chmod +x /usr/local/bin/age /usr/local/bin/age-keygen

# Fixed code dir. entrypoint.sh reads ENGINE_TASK_ROOT (defaulting to
# /var/task otherwise); setting it here means entrypoint.sh works unmodified.
ENV ENGINE_TASK_ROOT=/opt/engine
WORKDIR ${ENGINE_TASK_ROOT}

# Runtime deps installed flat into ENGINE_TASK_ROOT, mirroring how
# scripts/build-zip.sh assembles engine.zip (deps + aws_exe_sys/ side by side
# at the PYTHONPATH root).
COPY requirements.txt ./
RUN python3 -m pip install --no-cache-dir -r requirements.txt --target "${ENGINE_TASK_ROOT}"

COPY aws_exe_sys/ ${ENGINE_TASK_ROOT}/aws_exe_sys/

# CodeBuild overrides this with buildspec commands; CMD only matters for
# `docker run`.
CMD ["bash", "aws_exe_sys/worker/entrypoint.sh"]
