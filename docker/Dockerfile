FROM public.ecr.aws/lambda/python:3.14

# Install system dependencies
RUN dnf install -y jq git tar gzip && dnf clean all

# Install sops (pre-downloaded to avoid GitHub connectivity issues in Docker)
COPY docker/sops /usr/local/bin/sops
RUN chmod +x /usr/local/bin/sops

# Install age (pre-downloaded to avoid GitHub connectivity issues in Docker)
COPY docker/age /usr/local/bin/age
COPY docker/age-keygen /usr/local/bin/age-keygen
RUN chmod +x /usr/local/bin/age /usr/local/bin/age-keygen

# Install OpenTofu (pre-downloaded)
COPY docker/tofu /usr/local/bin/tofu
RUN chmod +x /usr/local/bin/tofu

# Install Python dependencies
COPY requirements.txt ./
RUN python3 -m pip install -r requirements.txt --target ${LAMBDA_TASK_ROOT}

# Copy source
COPY src/ ${LAMBDA_TASK_ROOT}/src/

# Copy CodeBuild entrypoint
COPY src/worker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Default handler (overridden per Lambda function via image_config.command)
CMD ["src.worker.handler.handler"]
