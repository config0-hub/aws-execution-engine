resource "aws_ssm_document" "run_commands" {
  name            = replace("${local.prefix}-run-commands", "-", "_")
  document_type   = "Command"
  document_format = "YAML"

  content = yamlencode({
    schemaVersion = "2.2"
    description   = "${local.prefix} generic command runner — downloads code from S3, runs commands, sends callback"
    parameters = {
      Commands = {
        type        = "String"
        description = "JSON array of shell commands to execute"
      }
      CallbackUrl = {
        type        = "String"
        description = "Presigned S3 PUT URL for result callback"
      }
      Timeout = {
        type        = "String"
        description = "Timeout in seconds"
        default     = "300"
      }
      EnvVars = {
        type        = "String"
        description = "JSON object of environment variables to set"
        default     = "{}"
      }
      S3Location = {
        type        = "String"
        description = "S3 URI for exec.zip (optional)"
        default     = ""
      }
    }
    mainSteps = [
      {
        action = "aws:runShellScript"
        name   = "runCommands"
        inputs = {
          timeoutSeconds = "{{ Timeout }}"
          runCommand = [
            "#!/bin/bash",
            "set -o pipefail",
            "",
            "WORK_DIR=$(mktemp -d /tmp/${local.prefix}-XXXXXX)",
            "cd \"$WORK_DIR\"",
            "STATUS=succeeded",
            "LOG_FILE=$(mktemp)",
            "",
            "# Export environment variables from EnvVars JSON",
            "ENV_VARS='{{ EnvVars }}'",
            "if [ \"$ENV_VARS\" != '{}' ] && command -v python3 &>/dev/null; then",
            "  eval $(python3 -c \"",
            "import json, sys, shlex",
            "d = json.loads(sys.argv[1])",
            "for k, v in d.items():",
            "    print(f'export {k}={shlex.quote(str(v))}')",
            "\" \"$ENV_VARS\")",
            "fi",
            "",
            "# Download and extract exec.zip if S3 location provided",
            "S3_LOC='{{ S3Location }}'",
            "if [ -n \"$S3_LOC\" ]; then",
            "  aws s3 cp \"$S3_LOC\" exec.zip >> $LOG_FILE 2>&1",
            "  unzip -o exec.zip >> $LOG_FILE 2>&1",
            "  rm -f exec.zip",
            "fi",
            "",
            "# Execute commands sequentially",
            "CMDS='{{ Commands }}'",
            "python3 -c \"",
            "import json, subprocess, sys, os",
            "cmds = json.loads(sys.argv[1])",
            "log_file = sys.argv[2]",
            "work_dir = sys.argv[3]",
            "status = 'succeeded'",
            "for cmd in cmds:",
            "    with open(log_file, 'a') as lf:",
            "        lf.write(f'\\$ {cmd}\\n')",
            "    rc = subprocess.call(cmd, shell=True, cwd=work_dir,",
            "        stdout=open(log_file, 'a'), stderr=subprocess.STDOUT)",
            "    if rc != 0:",
            "        with open(log_file, 'a') as lf:",
            "            lf.write(f'Exit code: {rc}\\n')",
            "        status = 'failed'",
            "        break",
            "with open(os.path.join(work_dir, '.status'), 'w') as f:",
            "    f.write(status)",
            "\" \"$CMDS\" \"$LOG_FILE\" \"$WORK_DIR\"",
            "",
            "# Read status",
            "if [ -f \"$WORK_DIR/.status\" ]; then",
            "  STATUS=$(cat \"$WORK_DIR/.status\")",
            "else",
            "  STATUS=failed",
            "fi",
            "",
            "# Send callback via presigned URL",
            "CALLBACK_URL='{{ CallbackUrl }}'",
            "if [ -n \"$CALLBACK_URL\" ]; then",
            "  PAYLOAD=$(python3 -c \"",
            "import json, sys",
            "status = sys.argv[1]",
            "log = open(sys.argv[2]).read()[:262144]",
            "print(json.dumps({'status': status, 'log': log}))",
            "\" \"$STATUS\" \"$LOG_FILE\")",
            "  curl -s -X PUT -H 'Content-Type: application/json' \\",
            "    -d \"$PAYLOAD\" \"$CALLBACK_URL\" || true",
            "fi",
            "",
            "# Cleanup",
            "rm -rf \"$WORK_DIR\" \"$LOG_FILE\"",
            "",
            "if [ \"$STATUS\" = 'failed' ]; then exit 1; fi",
          ]
        }
      }
    ]
  })
}
