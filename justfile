set positional-arguments

# ref 4353245 - iac-ci remote executor consistency naming
ENGINE_PROJECT := env_var_or_default("ENGINE_PROJECT", "iac-ci")
ENGINE_REGION := env_var_or_default("AWS_REGION", env_var_or_default("AWS_DEFAULT_REGION", "us-east-1"))
export AWS_REGION := ENGINE_REGION
export SSM_CONFIG_PROJECT := "engine"

# Set or read /iac-ci/install/engine/<key>. Values are passed as argv (never
# interpolated into shell source); secrets go via stdin: just config set-stdin <key>
config action key value="":
    #!/usr/bin/env bash
    set -euo pipefail
    action="$1"; key="$2"; value="${3:-}"
    case "$action" in
    set) ./scripts/ssm_config.sh set "$key" "$value" ;;
    set-stdin) ./scripts/ssm_config.sh set-stdin "$key" ;;
    get) ./scripts/ssm_config.sh get "$key" ;;
    *) echo "Usage: just config set|set-stdin|get <key> [value]" >&2; exit 1 ;;
    esac

# State bucket. When the shared iac-ci bucket already exists (combined install,
# positively tagged ManagedBy=iac-ci-bootstrap) this recipe adopts it; a
# standalone engine install creates the bucket + lock table (local state, then
# migrated) tagged ManagedBy=engine-00-bootstrap. NOTE: S3 CreateBucket carries
# no tags — terraform tags in a follow-up call — so a hard crash can leave an
# engine-created bucket untagged. Recovery proof is the surviving LOCAL
# bootstrap state; an untagged bucket WITHOUT local state is ambiguous and
# aborts for explicit operator recovery (never silently adopted).
bootstrap:
    #!/usr/bin/env bash
    set -euo pipefail
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="$(./scripts/ssm_config.sh get-or state_bucket_name "{{ENGINE_PROJECT}}-state-${ACCT}")"
    LOCK_TABLE="{{ENGINE_PROJECT}}-tf-locks"
    set +e; ./scripts/bucket_exists.sh "$BUCKET"; probe_rc=$?; set -e
    [ "$probe_rc" = 0 ] || [ "$probe_rc" = 1 ] || exit "$probe_rc"
    if [ "$probe_rc" = 0 ]; then
        OWNER="$(./scripts/bucket_owner.sh "$BUCKET")"   # aborts on unreadable tags
        if [ "$OWNER" = "engine-00-bootstrap" ]; then
            # Owned by a previous engine bootstrap: re-apply against remote state.
            ./scripts/write_tfvars.sh infra/00-bootstrap "aws_region={{ENGINE_REGION}}" "state_bucket_name=${BUCKET}" "lock_table_name=${LOCK_TABLE}"
            ./scripts/generate_backend.sh "$BUCKET" engine-00-bootstrap "{{ENGINE_REGION}}" infra/00-bootstrap "$LOCK_TABLE"
            terraform -chdir=infra/00-bootstrap init -reconfigure -input=false
            terraform -chdir=infra/00-bootstrap apply -input=false -auto-approve
        elif [ "$OWNER" = "untagged" ] && [ -s infra/00-bootstrap/terraform.tfstate ]; then
            # Crash window recovery: the surviving LOCAL bootstrap state proves
            # the interrupted creation is ours — resume the apply (which
            # re-tags), then migrate as in a fresh create.
            echo "untagged bucket ${BUCKET} with local engine bootstrap state: resuming interrupted owned creation"
            ./scripts/write_tfvars.sh infra/00-bootstrap "aws_region={{ENGINE_REGION}}" "state_bucket_name=${BUCKET}" "lock_table_name=${LOCK_TABLE}"
            rm -f infra/00-bootstrap/backend.tf
            terraform -chdir=infra/00-bootstrap init -reconfigure -input=false
            terraform -chdir=infra/00-bootstrap apply -input=false -auto-approve
            ./scripts/generate_backend.sh "$BUCKET" engine-00-bootstrap "{{ENGINE_REGION}}" infra/00-bootstrap "$LOCK_TABLE"
            terraform -chdir=infra/00-bootstrap init -migrate-state -force-copy -input=false
            rm -f infra/00-bootstrap/terraform.tfstate infra/00-bootstrap/terraform.tfstate.backup
        elif [ "$OWNER" = "untagged" ]; then
            # Ambiguous: untagged bucket and no local proof either way. Never
            # silently adopt — stop for explicit operator recovery.
            echo "ERROR: bucket ${BUCKET} exists but is untagged and no local engine bootstrap state survives." >&2
            echo "Cannot prove ownership. Recover explicitly: tag it ManagedBy=<owner> if known, or" >&2
            echo "delete it if it was an interrupted engine creation, then re-run 'just bootstrap'." >&2
            exit 1
        else
            echo "state bucket ${BUCKET} already exists (owner: ${OWNER}); adopting without owning it"
            exit 0
        fi
    else
        ./scripts/write_tfvars.sh infra/00-bootstrap "aws_region={{ENGINE_REGION}}" "state_bucket_name=${BUCKET}" "lock_table_name=${LOCK_TABLE}"
        rm -f infra/00-bootstrap/backend.tf
        terraform -chdir=infra/00-bootstrap init -reconfigure -input=false
        terraform -chdir=infra/00-bootstrap apply -input=false -auto-approve
        ./scripts/generate_backend.sh "$BUCKET" engine-00-bootstrap "{{ENGINE_REGION}}" infra/00-bootstrap "$LOCK_TABLE"
        terraform -chdir=infra/00-bootstrap init -migrate-state -force-copy -input=false
        rm -f infra/00-bootstrap/terraform.tfstate infra/00-bootstrap/terraform.tfstate.backup
    fi
    ./scripts/upload_source.sh "$BUCKET" engine-00-bootstrap . infra/00-bootstrap

bootstrap-destroy:
    #!/usr/bin/env bash
    set -euo pipefail
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="$(./scripts/ssm_config.sh get-or state_bucket_name "{{ENGINE_PROJECT}}-state-${ACCT}")"
    LOCK_TABLE="{{ENGINE_PROJECT}}-tf-locks"
    set +e; ./scripts/bucket_exists.sh "$BUCKET"; probe_rc=$?; set -e
    [ "$probe_rc" = 0 ] || [ "$probe_rc" = 1 ] || exit "$probe_rc"
    set +e; ./scripts/table_exists.sh "$LOCK_TABLE"; table_rc=$?; set -e
    [ "$table_rc" = 0 ] || [ "$table_rc" = 1 ] || exit "$table_rc"
    ./scripts/write_tfvars.sh infra/00-bootstrap "aws_region={{ENGINE_REGION}}" "state_bucket_name=${BUCKET}" "lock_table_name=${LOCK_TABLE}"
    if [ "$probe_rc" = 1 ]; then
        # Bucket gone — but a partial standalone bootstrap may have left local
        # state and/or the lock table. Do not strand them.
        if [ -s infra/00-bootstrap/terraform.tfstate ]; then
            echo "no bucket but local engine bootstrap state survives: destroying tracked resources from local state"
            rm -f infra/00-bootstrap/backend.tf
            terraform -chdir=infra/00-bootstrap init -reconfigure -input=false
            terraform -chdir=infra/00-bootstrap destroy -input=false -auto-approve
            rm -f infra/00-bootstrap/terraform.tfstate infra/00-bootstrap/terraform.tfstate.backup
        elif [ "$table_rc" = 0 ]; then
            TABLE_OWNER="$(aws dynamodb list-tags-of-resource --resource-arn "arn:aws:dynamodb:{{ENGINE_REGION}}:${ACCT}:table/${LOCK_TABLE}" --query "Tags[?Key=='ManagedBy'].Value" --output text)"
            if [ "$TABLE_OWNER" = "engine-00-bootstrap" ]; then
                echo "stranded engine-owned lock table ${LOCK_TABLE} found without state; deleting directly"
                aws dynamodb delete-table --table-name "$LOCK_TABLE" >/dev/null
                aws dynamodb wait table-not-exists --table-name "$LOCK_TABLE"
            else
                echo "lock table ${LOCK_TABLE} is not engine-owned (owner: ${TABLE_OWNER:-untagged}); leaving it"
                LEFT_FOREIGN_TABLE=1
            fi
        else
            echo "state bucket ${BUCKET} not found; nothing to destroy"
        fi
        # Postconditions for the owned/standalone paths we just handled: the
        # bucket must be gone, and the lock table must be gone too unless we
        # explicitly left a foreign-owned table in place.
        set +e; ./scripts/bucket_exists.sh "$BUCKET"; post_rc=$?; set -e
        [ "$post_rc" = 1 ] || { echo "ERROR: ${BUCKET} still exists or is unverifiable (rc=${post_rc})" >&2; exit 1; }
        if [ "${LEFT_FOREIGN_TABLE:-0}" != 1 ]; then
            set +e; ./scripts/table_exists.sh "$LOCK_TABLE"; post_table_rc=$?; set -e
            [ "$post_table_rc" = 1 ] || { echo "ERROR: ${LOCK_TABLE} still exists or is unverifiable (rc=${post_table_rc})" >&2; exit 1; }
        fi
        exit 0
    fi
    OWNER="$(./scripts/bucket_owner.sh "$BUCKET")"   # aborts on unreadable tags
    if [ "$OWNER" = "engine-00-bootstrap" ]; then
        # Normal owned teardown: state is remote — migrate it out, empty, destroy.
        ./scripts/generate_backend.sh "$BUCKET" engine-00-bootstrap "{{ENGINE_REGION}}" infra/00-bootstrap "$LOCK_TABLE"
        terraform -chdir=infra/00-bootstrap init -reconfigure -input=false
        rm -f infra/00-bootstrap/backend.tf
        terraform -chdir=infra/00-bootstrap init -migrate-state -force-copy -input=false
        ./scripts/empty_bucket.sh "$BUCKET"
        terraform -chdir=infra/00-bootstrap destroy -input=false -auto-approve
        rm -f infra/00-bootstrap/terraform.tfstate infra/00-bootstrap/terraform.tfstate.backup
    elif [ "$OWNER" = "untagged" ] && [ -s infra/00-bootstrap/terraform.tfstate ]; then
        # Interrupted owned creation: state never migrated — it is still LOCAL.
        # Do NOT migrate from the (empty) remote key; destroy from local state.
        echo "untagged bucket ${BUCKET} with local engine bootstrap state: destroying interrupted owned creation from local state"
        rm -f infra/00-bootstrap/backend.tf
        terraform -chdir=infra/00-bootstrap init -reconfigure -input=false
        ./scripts/empty_bucket.sh "$BUCKET"
        terraform -chdir=infra/00-bootstrap destroy -input=false -auto-approve
        rm -f infra/00-bootstrap/terraform.tfstate infra/00-bootstrap/terraform.tfstate.backup
    elif [ "$OWNER" = "untagged" ]; then
        echo "ERROR: bucket ${BUCKET} is untagged and no local engine bootstrap state survives." >&2
        echo "Cannot prove ownership — refusing to skip OR destroy. Recover explicitly (tag or delete it)." >&2
        exit 1
    else
        echo "state bucket ${BUCKET} is not owned by the engine install (owner: ${OWNER}); skipping"
        exit 0
    fi
    # Postconditions: an owned destroy must leave neither bucket nor table.
    set +e; ./scripts/bucket_exists.sh "$BUCKET"; post_bucket_rc=$?; set -e
    [ "$post_bucket_rc" = 1 ] || { echo "ERROR: ${BUCKET} still exists or is unverifiable (rc=${post_bucket_rc})" >&2; exit 1; }
    set +e; ./scripts/table_exists.sh "$LOCK_TABLE"; post_table_rc=$?; set -e
    [ "$post_table_rc" = 1 ] || { echo "ERROR: ${LOCK_TABLE} still exists or is unverifiable (rc=${post_table_rc})" >&2; exit 1; }

# Build engine.zip + sops-age-layer.zip and upload them to the state bucket.
build:
    #!/usr/bin/env bash
    set -euo pipefail
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="$(./scripts/ssm_config.sh get-or state_bucket_name "{{ENGINE_PROJECT}}-state-${ACCT}")"
    ./scripts/build-release-zip.sh
    aws s3 cp dist/engine.zip "s3://${BUCKET}/engine/artifacts/engine.zip" --sse AES256 --only-show-errors
    aws s3 cp dist/sops-age-layer.zip "s3://${BUCKET}/engine/artifacts/sops-age-layer.zip" --sse AES256 --only-show-errors
    echo "uploaded engine artifacts to s3://${BUCKET}/engine/artifacts/"

deploy:
    #!/usr/bin/env bash
    set -euo pipefail
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="$(./scripts/ssm_config.sh get-or state_bucket_name "{{ENGINE_PROJECT}}-state-${ACCT}")"
    PREFIX="$(./scripts/ssm_config.sh get-or project_prefix "{{ENGINE_PROJECT}}")"
    KMS_KEY_ARN="$(aws kms describe-key --key-id "alias/{{ENGINE_PROJECT}}-foundation" --query KeyMetadata.Arn --output text)"
    ./scripts/write_tfvars.sh infra/02-deploy \
        "project_prefix=${PREFIX}" \
        "kms_key_arn=${KMS_KEY_ARN}" \
        "engine_zip_s3_bucket=${BUCKET}" \
        "engine_zip_s3_key=engine/artifacts/engine.zip" \
        "sops_age_layer_s3_key=engine/artifacts/sops-age-layer.zip" \
        "additional_package_bucket_arns=[\"arn:aws:s3:::{{ENGINE_PROJECT}}-package-${ACCT}\",\"arn:aws:s3:::{{ENGINE_PROJECT}}-tmp-${ACCT}\"]" \
        "additional_result_bucket_arns=[\"arn:aws:s3:::{{ENGINE_PROJECT}}-done-${ACCT}\",\"arn:aws:s3:::{{ENGINE_PROJECT}}-tmp-${ACCT}\"]"
    ./scripts/generate_backend.sh "$BUCKET" engine-02-deploy "{{ENGINE_REGION}}" infra/02-deploy "{{ENGINE_PROJECT}}-tf-locks"
    terraform -chdir=infra/02-deploy init -reconfigure -input=false
    terraform -chdir=infra/02-deploy apply -input=false -auto-approve
    ./scripts/upload_source.sh "$BUCKET" engine-02-deploy . infra/02-deploy

deploy-destroy:
    #!/usr/bin/env bash
    set -euo pipefail
    ACCT="$(aws sts get-caller-identity --query Account --output text)"
    BUCKET="$(./scripts/ssm_config.sh get-or state_bucket_name "{{ENGINE_PROJECT}}-state-${ACCT}")"
    set +e; ./scripts/bucket_exists.sh "$BUCKET"; probe_rc=$?; set -e
    [ "$probe_rc" = 0 ] || [ "$probe_rc" = 1 ] || exit "$probe_rc"
    if [ "$probe_rc" = 1 ]; then
        echo "state bucket ${BUCKET} not found; nothing to destroy"
        exit 0
    fi
    PREFIX="$(./scripts/ssm_config.sh get-or project_prefix "{{ENGINE_PROJECT}}")"
    KMS_KEY_ARN="$(aws kms describe-key --key-id "alias/{{ENGINE_PROJECT}}-foundation" --query KeyMetadata.Arn --output text)"
    ./scripts/write_tfvars.sh infra/02-deploy \
        "project_prefix=${PREFIX}" \
        "kms_key_arn=${KMS_KEY_ARN}" \
        "engine_zip_s3_bucket=${BUCKET}" \
        "engine_zip_s3_key=engine/artifacts/engine.zip" \
        "sops_age_layer_s3_key=engine/artifacts/sops-age-layer.zip"
    ./scripts/generate_backend.sh "$BUCKET" engine-02-deploy "{{ENGINE_REGION}}" infra/02-deploy "{{ENGINE_PROJECT}}-tf-locks"
    terraform -chdir=infra/02-deploy init -reconfigure -input=false
    terraform -chdir=infra/02-deploy destroy -input=false -auto-approve
    # Reverse of `build`: purge uploaded artifacts even when the bucket is
    # shared (adopted) and therefore survives this uninstall.
    ./scripts/empty_bucket.sh "$BUCKET" "engine/artifacts/"

# Full engine install: bootstrap -> build -> deploy
install:
    @just bootstrap
    @just build
    @just deploy

# Exact reverse of install. deploy-destroy purges build artifacts explicitly
# (the state bucket may be shared/adopted and survive); a standalone install
# also removes its own SSM namespace.
uninstall:
    @just deploy-destroy
    @just bootstrap-destroy
    ./scripts/ssm_config.sh delete-all
