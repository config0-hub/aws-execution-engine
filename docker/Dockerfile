FROM public.ecr.aws/lambda/python:3.14

# Install system dependencies
RUN dnf install -y jq git tar gzip gettext unzip && dnf clean all

# Install sops
ARG SOPS_VERSION=3.9.4
RUN curl -fsSL "https://github.com/getsops/sops/releases/download/v${SOPS_VERSION}/sops-v${SOPS_VERSION}.linux.amd64" \
      -o /usr/local/bin/sops && chmod +x /usr/local/bin/sops

# Install age
ARG AGE_VERSION=1.2.1
RUN curl -fsSL "https://dl.filippo.io/age/v${AGE_VERSION}?for=linux/amd64" | tar xz --strip-components=1 -C /usr/local/bin

# Install OpenTofu
ARG TOFU_VERSION=1.8.8
RUN curl -fsSL "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.zip" \
      -o /tmp/tofu.zip && unzip -o /tmp/tofu.zip tofu -d /usr/local/bin && rm /tmp/tofu.zip && chmod +x /usr/local/bin/tofu

# Install Python dependencies
COPY requirements.txt ./
RUN python3 -m pip install -r requirements.txt --target ${LAMBDA_TASK_ROOT}

# Copy source
COPY aws_exe_sys/ ${LAMBDA_TASK_ROOT}/aws_exe_sys/

# Copy CodeBuild entrypoint
COPY aws_exe_sys/worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default handler (overridden per Lambda function via image_config.command)
CMD ["aws_exe_sys.worker.handler.handler"]
