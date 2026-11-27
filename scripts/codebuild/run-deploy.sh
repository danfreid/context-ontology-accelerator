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
#   COA_CODEBUILD_DRY_RUN=1  print the planned build(s) and exit, without any AWS call
#
# Batched mode, for accounts whose CodeBuild builds are capped at 45 minutes (new accounts,
# whatever the project's timeout says). It runs one build per batch of stacks, in dependency
# order, each deploying only its stacks (`cdk deploy --exclusively`, scripts/codebuild/deploy-stacks.sh).
# The ref is resolved to one commit SHA before the first build, so every batch and retry
# deploys the same revision:
#   COA_CODEBUILD_BATCHED=1         turn it on
#   COA_CODEBUILD_FROM_BATCH=N      resume at batch N (1-based; default 1)
#   COA_CODEBUILD_BATCH_RETRIES=K   reruns of a batch that timed out (default 2). Images a
#                                   timed-out build published are reused, so reruns progress.
#   COA_CODEBUILD_SETTLE_MINUTES=M  before a rerun, wait up to M minutes (default 30) for the
#                                   batch's CloudFormation stacks to stop changing
#   COA_CODEBUILD_BATCHES="a b;c d" batches to use instead of the default list below
#                                   (stack names, ';' between batches)
# A batch that fails for any reason other than a timeout stops the run.
#
# Ctrl-C stops only the log display. The build keeps running; stop it with
# `aws codebuild stop-build --id <id>` (stopping a deploy mid-way can leave a stack
# in a rollback).
#
# Plain bash 3.2 (macOS) and bash 5: no mapfile, no associative arrays.
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
SETTLE_MINUTES="${COA_CODEBUILD_SETTLE_MINUTES:-30}"
SETTLE_POLL_SECONDS="${COA_CODEBUILD_SETTLE_POLL_SECONDS:-30}"
DRY_RUN="${COA_CODEBUILD_DRY_RUN:-}"
die() { echo "ERROR: $*" >&2; exit 1; }
[ -z "$DEPLOY_ENV" ] || [[ "$DEPLOY_ENV" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,19}$ ]] || die "invalid environment name '$DEPLOY_ENV'"
[ -z "$REF" ] || [[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]] || die "invalid COA_CODEBUILD_REF '$REF'"
[[ "$FROM_BATCH" =~ ^[1-9][0-9]*$ ]] || die "COA_CODEBUILD_FROM_BATCH must be a positive integer"
[[ "$BATCH_RETRIES" =~ ^[0-9]+$ ]] || die "COA_CODEBUILD_BATCH_RETRIES must be a non-negative integer"
[[ "$SETTLE_MINUTES" =~ ^[0-9]+$ ]] || die "COA_CODEBUILD_SETTLE_MINUTES must be a non-negative integer"
[[ "$SETTLE_POLL_SECONDS" =~ ^[0-9]+$ ]] || die "COA_CODEBUILD_SETTLE_POLL_SECONDS must be a non-negative integer"
# Platform stacks in dependency order (infra/bin/app.ts), as <suffix> after "<prefix>-<env>-".
# Grouped so that no build needs more than about 30 minutes on a LARGE arm host, except
# "sources": one stack with three images (about 40 minutes), which cannot be split further.
DEFAULT_BATCHES=(
  "network eni-warm auth guardrail"
  "storage authnz edge-waf vkg namespace"
  "sources"
  "ontology serve mcp"
  "metric-service data-layer api web"
)
# The batch list with the prefix and environment applied, one batch per line.
plan_batches() {
  local prefix="$1" env="$2" b s out names
  if [ -n "${COA_CODEBUILD_BATCHES:-}" ]; then
    tr ';' '\n' <<< "$COA_CODEBUILD_BATCHES" | sed 's/^ *//; s/ *$//' | grep -v '^$' || true
    return
  fi
  for b in "${DEFAULT_BATCHES[@]}"; do
    out=""
    read -r -a names <<< "$b"
    for s in "${names[@]}"; do out+="${out:+ }${prefix}-${env}-${s}"; done
    echo "$out"
  done
}
BATCHES=()
# load_batches <prefix> <env>: fill BATCHES and check every stack name before any build starts.
load_batches() {
  local line b s names
  BATCHES=()
  while IFS= read -r line; do
    if [ -n "$line" ]; then BATCHES+=("$line"); fi
  done < <(plan_batches "$1" "$2")
  [ "${#BATCHES[@]}" -gt 0 ] || die "no batches: COA_CODEBUILD_BATCHES holds no stack names"
  for b in "${BATCHES[@]}"; do
    read -r -a names <<< "$b"
    for s in "${names[@]}"; do
      [[ "$s" =~ ^[A-Za-z][A-Za-z0-9-]{0,127}$ ]] ||
        die "invalid stack name '$s' in the batch list (separate stacks with spaces and batches with ';')"
    done
  done
}
# JSON string escaping for the overrides (backslash, quote); control characters are refused.
json_escape() {
  local v="$1"
  [[ "$v" != *[[:cntrl:]]* ]] || die "a setting contains a control character or newline"
  v="${v//\\/\\\\}"
  printf '%s' "${v//\"/\\\"}"
}
# Overrides shared by every build: environment, ref and SCL_* settings.
BASE_OVERRIDES=""
SETTINGS_LOG=""
add_override() {
  BASE_OVERRIDES+="${BASE_OVERRIDES:+,}{\"name\":\"$1\",\"value\":\"$(json_escape "$2")\",\"type\":\"PLAINTEXT\"}"
  SETTINGS_LOG+="  $1=${3:-$2}"$'\n'
}
show_settings() {
  echo "Settings passed to the build:"
  if [ -n "$SETTINGS_LOG" ]; then printf '%s' "$SETTINGS_LOG"; else echo "  (none: the project's defaults)"; fi
}
[ -z "$DEPLOY_ENV" ] || add_override COA_DEPLOY_ENV "$DEPLOY_ENV"
# In batched mode the ref is resolved to a commit SHA first (below).
{ [ -z "$REF" ] || [ -n "$BATCHED" ]; } || add_override COA_REPO_REF "$REF"
while IFS= read -r NAME; do
  [ -n "${!NAME}" ] || continue # deploy.sh treats an empty SCL_* like an unset one
  add_override "$NAME" "${!NAME}"
done < <(compgen -e | grep -E '^SCL_[A-Z0-9_]+$' | sort)

if [ -n "$DRY_RUN" ]; then
  # No AWS call, so the project's own prefix and environment are not known: assume the defaults.
  echo "Dry run: no AWS call is made and no build is started."
  if [ -n "$BATCHED" ]; then
    load_batches "${SCL_PREFIX:-coa}" "${DEPLOY_ENV:-dev}"
    echo "Planned CodeBuild batches (assuming prefix ${SCL_PREFIX:-coa} and environment ${DEPLOY_ENV:-dev}; a real run reads the project's values):"
    for i in "${!BATCHES[@]}"; do
      n=$((i + 1))
      [ "$n" -ge "$FROM_BATCH" ] || continue
      echo "  batch $n: ${BATCHES[$i]}"
    done
    echo "A real run resolves ${REF:-the project ref} to one commit SHA and passes it to every build."
  else
    echo "Planned CodeBuild build: one build running scripts/deploy.sh (all stacks)."
  fi
  show_settings
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

BUILD_ID=""
TAIL_PID=""
CURRENT_BATCH=""
PINNED_REF=""
stop_tail() {
  if [ -n "${TAIL_PID:-}" ]; then kill "$TAIL_PID" 2>/dev/null || true; fi
}
# Every non-success exit in batched mode says where to resume.
on_exit() {
  local rc=$?
  if [ "$rc" -ne 0 ] && [ -n "$BATCHED" ] && [ -n "$CURRENT_BATCH" ]; then
    echo "Resume at batch $CURRENT_BATCH (if build ${BUILD_ID:-?} is still running, let it finish or stop it first) with:" >&2
    echo "  COA_CODEBUILD_BATCHED=1 COA_CODEBUILD_FROM_BATCH=$CURRENT_BATCH${PINNED_REF:+ COA_CODEBUILD_REF=$PINNED_REF} make deploy-dev-codebuild   # with the same SCL_* settings" >&2
  fi
}
trap on_exit EXIT
trap 'echo; echo "Stopped watching. Build ${BUILD_ID:-?} is still running; stop it with: aws codebuild stop-build --region $REGION --id ${BUILD_ID:-<id>}"; stop_tail; exit 130' INT TERM
build_field() {
  aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" --query "builds[0].$1" --output text
}
project_var() {
  aws codebuild batch-get-projects --region "$REGION" --names "$PROJECT" \
    --query "projects[0].environment.environmentVariables[?name=='$1'].value | [0]" --output text
}
# resolve_ref <git-url> <ref>: print the commit SHA of a branch, tag or full SHA.
resolve_ref() {
  local url="$1" ref="$2" out sha name best="" rank=0 r
  if [[ "$ref" =~ ^[0-9a-f]{40}$ ]]; then
    echo "$ref"
    return 0
  fi
  out="$(GIT_TERMINAL_PROMPT=0 git ls-remote "$url" "refs/heads/$ref" "refs/tags/$ref" "refs/tags/$ref^{}")" || return 1
  while IFS=$'\t' read -r sha name; do
    case "$name" in
      "refs/tags/$ref^{}") r=3 ;; # an annotated tag's commit
      "refs/heads/$ref") r=2 ;;
      "refs/tags/$ref") r=1 ;;
      *) r=0 ;;
    esac
    if [ "$r" -gt "$rank" ]; then
      rank=$r
      best="$sha"
    fi
  done <<< "$out"
  [[ "$best" =~ ^[0-9a-f]{40}$ ]] || return 1
  echo "$best"
}
# run_build [stacks]: start one build (deploying only <stacks> when given), follow its log
# and set RESULT to SUCCEEDED, TIMED_OUT or the failed status.
run_build() {
  local stacks="${1:-}" overrides="$BASE_OVERRIDES" failures=0 status="IN_PROGRESS" next stream group phases
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
      stream="$(build_field logs.streamName)" || stream=""
      group="$(build_field logs.groupName)" || group=""
      if [ -n "$stream" ] && [ "$stream" != "None" ] && [ -n "$group" ] && [ "$group" != "None" ]; then
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
      [ "$failures" -lt 5 ] || { stop_tail; die "cannot read the status of build $BUILD_ID; it may still be running"; }
    fi
  done
  if [ -n "$TAIL_PID" ]; then
    sleep 10 # let the tail print the last lines
    stop_tail
    wait "$TAIL_PID" 2>/dev/null || true
  fi
  RESULT="$status"
  # A timeout is reported as FAILED overall with a TIMED_OUT phase (or as TIMED_OUT). A
  # failed or unreadable phase query is never taken as a timeout.
  if [ "$status" != "SUCCEEDED" ] && [ "$status" != "TIMED_OUT" ]; then
    if phases="$(build_field "phases[?phaseStatus=='TIMED_OUT'] | length(@)")" && [[ "$phases" =~ ^[0-9]+$ ]]; then
      if [ "$phases" -gt 0 ]; then RESULT="TIMED_OUT"; fi
    else
      echo "Could not read the phases of build $BUILD_ID; treating $status as a failure, not a timeout." >&2
    fi
  fi
  echo ""
  echo "Build $BUILD_ID finished: $RESULT"
  if [ "$RESULT" != "SUCCEEDED" ]; then
    echo "Full log: CloudWatch Logs group $(build_field logs.groupName || true), stream $(build_field logs.streamName || true)" >&2
    if [ "$RESULT" = "TIMED_OUT" ]; then
      echo "Build limit: $(build_field timeoutInMinutes || true) minutes. New accounts are capped at 45 minutes; use batched mode (COA_CODEBUILD_BATCHED=1) or ask AWS Support to raise it." >&2
    fi
  fi
}

echo "=== Deploy through CodeBuild project $PROJECT ($REGION) ==="
if [ -z "$BATCHED" ]; then
  show_settings
  run_build
  [ "$RESULT" = "SUCCEEDED" ] || exit 1
  exit 0
fi

# Batched mode: the prefix and environment the build will use (overrides, else project defaults).
PREFIX="${SCL_PREFIX:-$(project_var SCL_PREFIX)}"
ENV_NAME="${DEPLOY_ENV:-$(project_var COA_DEPLOY_ENV)}"
[[ "$PREFIX" =~ ^[A-Za-z][A-Za-z0-9-]{0,19}$ ]] || die "cannot work out the resource prefix (got '$PREFIX'); set SCL_PREFIX"
[[ "$ENV_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,19}$ ]] || die "cannot work out the environment (got '$ENV_NAME'); set COA_DEPLOY_ENV"
load_batches "$PREFIX" "$ENV_NAME"
[ "$FROM_BATCH" -le "${#BATCHES[@]}" ] || die "COA_CODEBUILD_FROM_BATCH=$FROM_BATCH but there are ${#BATCHES[@]} batches"
# One revision for the whole run: every batch and retry gets the same commit SHA.
REPO_URL="$(project_var COA_REPO_URL)"
UNRESOLVED_REF="${REF:-$(project_var COA_REPO_REF)}"
[[ "$REPO_URL" =~ ^https://[A-Za-z0-9._~/:-]+$ ]] || die "cannot read the project's repository URL"
PINNED_REF="$(resolve_ref "$REPO_URL" "$UNRESOLVED_REF")" ||
  die "cannot resolve '$UNRESOLVED_REF' in $REPO_URL to a commit; use a branch, a tag or a full 40-character commit SHA"
add_override COA_REPO_REF "$PINNED_REF" "$PINNED_REF (resolved from $UNRESOLVED_REF)"
show_settings

# stack_status <stack> <region>: the stack's status, ABSENT, or UNKNOWN when it cannot be read.
stack_status() {
  local out
  if out="$(aws cloudformation describe-stacks --region "$2" --stack-name "$1" --query 'Stacks[0].StackStatus' --output text 2>&1)"; then
    echo "$out"
  elif [[ "$out" == *"does not exist"* ]]; then
    echo ABSENT
  else
    echo "Cannot read stack $1 in $2: $out" >&2
    echo UNKNOWN
  fi
}
# wait_for_stacks "<stack> ...": before a rerun, wait until CloudFormation has finished any
# stack operation the timed-out build left running. Stops the run when a stack needs attention.
wait_for_stacks() {
  local names s region regions st busy unknown=0 deadline=$((SECONDS + SETTLE_MINUTES * 60))
  read -r -a names <<< "$1"
  echo "Waiting for CloudFormation to finish what the timed-out build started (up to $SETTLE_MINUTES minutes)..."
  while :; do
    busy=""
    for s in "${names[@]}"; do
      regions="$REGION"
      # The edge-waf stack always lives in us-east-1.
      if [[ "$s" == *-edge-waf ]] && [ "$REGION" != "us-east-1" ]; then regions="$REGION us-east-1"; fi
      for region in $regions; do
        st="$(stack_status "$s" "$region")"
        case "$st" in
          *_IN_PROGRESS) busy="$busy $s" ;;
          UNKNOWN)
            unknown=$((unknown + 1))
            busy="$busy $s"
            ;;
          ROLLBACK_COMPLETE | ROLLBACK_FAILED | UPDATE_ROLLBACK_FAILED | DELETE_FAILED | CREATE_FAILED | UPDATE_FAILED)
            die "stack $s is $st; fix or delete it first, then resume (a rerun would fail the same way)"
            ;;
        esac
      done
    done
    [ "$unknown" -lt 3 ] || die "cannot read the status of the batch's stacks"
    [ -n "$busy" ] || return 0
    [ "$SECONDS" -lt "$deadline" ] || die "stacks still changing after $SETTLE_MINUTES minutes:$busy"
    echo "  still in progress:$busy"
    sleep "$SETTLE_POLL_SECONDS"
  done
}

for i in "${!BATCHES[@]}"; do
  n=$((i + 1))
  [ "$n" -ge "$FROM_BATCH" ] || continue
  CURRENT_BATCH="$n"
  echo ""
  echo "=== Batch $n/${#BATCHES[@]}: ${BATCHES[$i]} ==="
  attempt=0
  while :; do
    run_build "${BATCHES[$i]}"
    if [ "$RESULT" = "SUCCEEDED" ]; then break; fi
    if [ "$RESULT" = "TIMED_OUT" ] && [ "$attempt" -lt "$BATCH_RETRIES" ]; then
      attempt=$((attempt + 1))
      wait_for_stacks "${BATCHES[$i]}"
      echo "Batch $n timed out; rerunning ($attempt/$BATCH_RETRIES). Images it published are reused."
      continue
    fi
    echo "Batch $n failed ($RESULT)." >&2
    exit 1
  done
done
CURRENT_BATCH=""
echo ""
echo "All ${#BATCHES[@]} batches deployed (commit $PINNED_REF)."
