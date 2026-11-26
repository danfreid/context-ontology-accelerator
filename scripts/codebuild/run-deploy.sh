#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# OPTIONAL. Run the platform deploy in the CodeBuild project created by
# scripts/codebuild/setup.sh, show the build log, and exit with the build's status.
# `make deploy-dev` is unchanged and does not use this.
#
# Usage: make deploy-dev-codebuild      (or ./scripts/codebuild/run-deploy.sh [env])
#
# Every SCL_* variable set in this shell is passed to the build, exactly as it would be to
# scripts/deploy.sh, so these behave as in the documented deploy-dev examples:
#   SCL_LAMBDA_RESERVED_CONCURRENCY=0 SCL_SMUS_ADMIN_ARNS=arn:aws:iam::123456789012:role/X \
#     make deploy-dev-codebuild
# Do not put secrets in SCL_* variables: they appear in the build's settings and log.
# Other settings:
#   COA_DEPLOY_ENV        deploy environment (default: the project's, normally dev); or $1
#   COA_CODEBUILD_REF     branch, tag or commit to deploy (default: the project's)
#   COA_CODEBUILD_STACK   stack name used by setup.sh (default: coa-codebuild-deploy)
#
# Batched mode, for accounts whose CodeBuild builds are capped at 45 minutes (new accounts,
# whatever the project's timeout says). It runs one build per batch of stacks, in dependency
# order, each deploying only its stacks (`cdk deploy --exclusively`, scripts/codebuild/deploy-stacks.sh):
#   COA_CODEBUILD_BATCHED=1         turn it on
#   COA_CODEBUILD_FROM_BATCH=N      resume at batch N (1-based; default 1)
#   COA_CODEBUILD_BATCH_RETRIES=K   reruns of a batch that timed out (default 2). Images a
#                                   timed-out build published are reused, so reruns progress.
#   COA_CODEBUILD_BATCHES="a b;c d" batches to use instead of the default list below
#                                   (stack names, ';' between batches)
#   COA_CODEBUILD_DRY_RUN=1         print the planned builds and exit, without AWS calls
# A batch that fails for any reason other than a timeout stops the run.
#
# Ctrl-C stops only the log display. The build keeps running; stop it with
# `aws codebuild stop-build --id <id>` (stopping a deploy mid-way can leave a stack
# in a rollback).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=../lib/resolve-region.sh source-path=SCRIPTDIR
source "$REPO_ROOT/scripts/lib/resolve-region.sh"
STACK="${COA_CODEBUILD_STACK:-coa-codebuild-deploy}"
DEPLOY_ENV="${1:-${COA_DEPLOY_ENV:-}}"
REF="${COA_CODEBUILD_REF:-}"
POLL_SECONDS="${COA_CODEBUILD_POLL_SECONDS:-15}"
BATCHED="${COA_CODEBUILD_BATCHED:-}"
FROM_BATCH="${COA_CODEBUILD_FROM_BATCH:-1}"
BATCH_RETRIES="${COA_CODEBUILD_BATCH_RETRIES:-2}"
DRY_RUN="${COA_CODEBUILD_DRY_RUN:-}"
die() { echo "ERROR: $*" >&2; exit 1; }
[ -z "$DEPLOY_ENV" ] || [[ "$DEPLOY_ENV" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,19}$ ]] || die "invalid environment name '$DEPLOY_ENV'"
[ -z "$REF" ] || [[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]] || die "invalid COA_CODEBUILD_REF '$REF'"
[[ "$FROM_BATCH" =~ ^[1-9][0-9]*$ ]] || die "COA_CODEBUILD_FROM_BATCH must be a positive integer"
[[ "$BATCH_RETRIES" =~ ^[0-9]+$ ]] || die "COA_CODEBUILD_BATCH_RETRIES must be a non-negative integer"

# Platform stacks in dependency order (infra/bin/app.ts), as <suffix> after "<prefix>-<env>-".
# Grouped so that no build needs more than about 30 minutes on a LARGE arm host; the
# stacks with the emulated linux/amd64 images (sources, ontology) get their own room.
DEFAULT_BATCHES=(
  "network eni-warm auth guardrail"
  "storage authnz edge-waf vkg namespace"
  "sources"
  "ontology serve mcp"
  "metric-service data-layer api web"
)

# The batch list with the prefix and environment applied, one batch per line.
plan_batches() {
  local prefix="$1" env="$2" b s out
  if [ -n "${COA_CODEBUILD_BATCHES:-}" ]; then
    tr ';' '\n' <<< "$COA_CODEBUILD_BATCHES" | sed 's/^ *//; s/ *$//' | grep -v '^$'
    return
  fi
  for b in "${DEFAULT_BATCHES[@]}"; do
    out=""
    for s in $b; do out+="${out:+ }${prefix}-${env}-${s}"; done
    echo "$out"
  done
}

if [ -n "$BATCHED" ] && [ -n "$DRY_RUN" ]; then
  mapfile -t BATCHES < <(plan_batches "${SCL_PREFIX:-coa}" "${DEPLOY_ENV:-dev}")
  echo "Planned CodeBuild batches (prefix ${SCL_PREFIX:-coa}, environment ${DEPLOY_ENV:-dev}):"
  for i in "${!BATCHES[@]}"; do
    n=$((i + 1))
    [ "$n" -ge "$FROM_BATCH" ] || continue
    echo "  batch $n: ${BATCHES[$i]}"
  done
  exit 0
fi

REGION="$(coa_resolve_region)"
command -v aws >/dev/null 2>&1 || die "the AWS CLI is not installed."
# `aws logs tail` needs AWS CLI v2.
[[ "$(aws --version 2>&1)" == aws-cli/2.* ]] || die "AWS CLI v2 is required (aws logs tail)."
PROJECT="$(aws cloudformation describe-stacks --region "$REGION" --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='ProjectName'].OutputValue | [0]" --output text 2>/dev/null || true)"
if [ -z "$PROJECT" ] || [ "$PROJECT" = "None" ]; then
  die "no CodeBuild deploy project found (stack $STACK in $REGION). Create it once with: make codebuild-setup"
fi

# JSON string escaping for the overrides (backslash, quote); control characters are refused.
json_escape() {
  local v="$1"
  [[ "$v" != *[[:cntrl:]]* ]] || die "a setting contains a control character or newline"
  v="${v//\\/\\\\}"
  printf '%s' "${v//\"/\\\"}"
}
# Overrides shared by every build: environment, ref and SCL_* settings.
BASE_OVERRIDES=""
add_override() {
  BASE_OVERRIDES+="${BASE_OVERRIDES:+,}{\"name\":\"$1\",\"value\":\"$(json_escape "$2")\",\"type\":\"PLAINTEXT\"}"
  echo "  $1=$2"
}
echo "=== Deploy through CodeBuild project $PROJECT ($REGION) ==="
echo "Settings passed to the build:"
[ -z "$DEPLOY_ENV" ] || add_override COA_DEPLOY_ENV "$DEPLOY_ENV"
[ -z "$REF" ] || add_override COA_REPO_REF "$REF"
while IFS= read -r NAME; do
  [ -n "${!NAME}" ] || continue # deploy.sh treats an empty SCL_* like an unset one
  add_override "$NAME" "${!NAME}"
done < <(compgen -e | grep -E '^SCL_[A-Z0-9_]+$' | sort)
[ -n "$BASE_OVERRIDES" ] || echo "  (none: the project's defaults)"

BUILD_ID=""
TAIL_PID=""
trap 'echo; echo "Stopped watching. Build ${BUILD_ID:-?} is still running; stop it with: aws codebuild stop-build --region $REGION --id ${BUILD_ID:-<id>}"; kill "${TAIL_PID:-0}" 2>/dev/null || true; exit 130' INT TERM
build_field() {
  aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" --query "builds[0].$1" --output text
}

# run_build [stacks]: start one build (deploying only <stacks> when given), follow its log
# and set RESULT to SUCCEEDED, TIMED_OUT or the failed status.
run_build() {
  local stacks="${1:-}" overrides="$BASE_OVERRIDES" failures=0 status="IN_PROGRESS" next stream group
  if [ -n "$stacks" ]; then
    overrides+="${overrides:+,}{\"name\":\"COA_CDK_STACKS\",\"value\":\"$(json_escape "$stacks")\",\"type\":\"PLAINTEXT\"}"
  fi
  local args=(--region "$REGION" --project-name "$PROJECT" --query 'build.id' --output text)
  [ -z "$overrides" ] || args+=(--environment-variables-override "[$overrides]")
  BUILD_ID="$(aws codebuild start-build "${args[@]}")"
  echo "Started build $BUILD_ID${stacks:+ for [$stacks]}"
  TAIL_PID=""
  while [ "$status" = "IN_PROGRESS" ]; do
    if [ -z "$TAIL_PID" ]; then
      stream="$(build_field logs.streamName)"
      group="$(build_field logs.groupName)"
      if [ -n "$stream" ] && [ "$stream" != "None" ]; then
        aws logs tail "$group" --region "$REGION" --log-stream-names "$stream" --since 15m --follow &
        TAIL_PID=$!
      fi
    fi
    sleep "$POLL_SECONDS"
    # Ride out a throttled or dropped call; give up after 5 failures in a row.
    if next="$(build_field buildStatus)"; then
      status="$next"
      failures=0
    else
      failures=$((failures + 1))
      [ "$failures" -lt 5 ] || { kill "$TAIL_PID" 2>/dev/null || true; die "cannot read the status of build $BUILD_ID; it may still be running"; }
    fi
  done
  if [ -n "$TAIL_PID" ]; then
    sleep 10 # let the tail print the last lines
    kill "$TAIL_PID" 2>/dev/null || true
    wait "$TAIL_PID" 2>/dev/null || true
  fi
  RESULT="$status"
  # A timeout is reported as FAILED overall with a TIMED_OUT phase.
  if [ "$status" != "SUCCEEDED" ] &&
    [ "$(build_field "phases[?phaseStatus=='TIMED_OUT'] | length(@)")" != "0" ]; then
    RESULT="TIMED_OUT"
  fi
  echo ""
  echo "Build $BUILD_ID finished: $RESULT"
  if [ "$RESULT" != "SUCCEEDED" ]; then
    echo "Full log: CloudWatch Logs group $(build_field logs.groupName), stream $(build_field logs.streamName)" >&2
    if [ "$RESULT" = "TIMED_OUT" ]; then
      echo "Build limit: $(build_field timeoutInMinutes) minutes. New accounts are capped at 45 minutes; use batched mode (COA_CODEBUILD_BATCHED=1) or ask AWS Support to raise it." >&2
    fi
  fi
}

if [ -z "$BATCHED" ]; then
  run_build
  [ "$RESULT" = "SUCCEEDED" ] || exit 1
  exit 0
fi

# Batched mode: the prefix and environment the build will use (overrides, else project defaults).
project_var() {
  aws codebuild batch-get-projects --region "$REGION" --names "$PROJECT" \
    --query "projects[0].environment.environmentVariables[?name=='$1'].value | [0]" --output text
}
PREFIX="${SCL_PREFIX:-$(project_var SCL_PREFIX)}"
ENV_NAME="${DEPLOY_ENV:-$(project_var COA_DEPLOY_ENV)}"
[[ "$PREFIX" =~ ^[A-Za-z][A-Za-z0-9-]{0,19}$ ]] || die "cannot work out the resource prefix (got '$PREFIX'); set SCL_PREFIX"
[[ "$ENV_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,19}$ ]] || die "cannot work out the environment (got '$ENV_NAME'); set COA_DEPLOY_ENV"
mapfile -t BATCHES < <(plan_batches "$PREFIX" "$ENV_NAME")
[ "$FROM_BATCH" -le "${#BATCHES[@]}" ] || die "COA_CODEBUILD_FROM_BATCH=$FROM_BATCH but there are ${#BATCHES[@]} batches"
for i in "${!BATCHES[@]}"; do
  n=$((i + 1))
  [ "$n" -ge "$FROM_BATCH" ] || continue
  echo ""
  echo "=== Batch $n/${#BATCHES[@]}: ${BATCHES[$i]} ==="
  attempt=0
  while :; do
    run_build "${BATCHES[$i]}"
    [ "$RESULT" = "SUCCEEDED" ] && break
    if [ "$RESULT" = "TIMED_OUT" ] && [ "$attempt" -lt "$BATCH_RETRIES" ]; then
      attempt=$((attempt + 1))
      echo "Batch $n timed out; rerunning ($attempt/$BATCH_RETRIES). Images it published are reused."
      continue
    fi
    echo "Batch $n failed ($RESULT). Fix the cause, then resume with: COA_CODEBUILD_BATCHED=1 COA_CODEBUILD_FROM_BATCH=$n make deploy-dev-codebuild" >&2
    exit 1
  done
done
echo ""
echo "All ${#BATCHES[@]} batches deployed."
