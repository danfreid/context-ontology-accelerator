#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# OPTIONAL. Run the platform deploy (scripts/deploy.sh) in the CodeBuild project created by
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
# Ctrl-C stops only the log display. The build keeps running; stop it with
# `aws codebuild stop-build --id <id>` (stopping a deploy mid-way can leave a stack
# in a rollback).
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=../lib/resolve-region.sh source-path=SCRIPTDIR
source "$REPO_ROOT/scripts/lib/resolve-region.sh"
REGION="$(coa_resolve_region)"
STACK="${COA_CODEBUILD_STACK:-coa-codebuild-deploy}"
DEPLOY_ENV="${1:-${COA_DEPLOY_ENV:-}}"
REF="${COA_CODEBUILD_REF:-}"
POLL_SECONDS="${COA_CODEBUILD_POLL_SECONDS:-15}"
die() { echo "ERROR: $*" >&2; exit 1; }
command -v aws >/dev/null 2>&1 || die "the AWS CLI is not installed."
# `aws logs tail` needs AWS CLI v2.
[[ "$(aws --version 2>&1)" == aws-cli/2.* ]] || die "AWS CLI v2 is required (aws logs tail)."
[ -z "$DEPLOY_ENV" ] || [[ "$DEPLOY_ENV" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,19}$ ]] || die "invalid environment name '$DEPLOY_ENV'"
[ -z "$REF" ] || [[ "$REF" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$ ]] || die "invalid COA_CODEBUILD_REF '$REF'"
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
OVERRIDES=""
add_override() {
  OVERRIDES+="${OVERRIDES:+,}{\"name\":\"$1\",\"value\":\"$(json_escape "$2")\",\"type\":\"PLAINTEXT\"}"
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
[ -n "$OVERRIDES" ] || echo "  (none: the project's defaults)"
START_ARGS=(--region "$REGION" --project-name "$PROJECT" --query 'build.id' --output text)
[ -z "$OVERRIDES" ] || START_ARGS+=(--environment-variables-override "[$OVERRIDES]")
BUILD_ID="$(aws codebuild start-build "${START_ARGS[@]}")"
echo "Started build $BUILD_ID"
trap 'echo; echo "Stopped watching. Build $BUILD_ID is still running; stop it with: aws codebuild stop-build --region $REGION --id $BUILD_ID"; kill "${TAIL_PID:-0}" 2>/dev/null || true; exit 130' INT TERM
build_field() {
  aws codebuild batch-get-builds --region "$REGION" --ids "$BUILD_ID" --query "builds[0].$1" --output text
}
TAIL_PID=""
FAILURES=0
STATUS="IN_PROGRESS"
while [ "$STATUS" = "IN_PROGRESS" ]; do
  if [ -z "$TAIL_PID" ]; then
    STREAM="$(build_field logs.streamName)"
    GROUP="$(build_field logs.groupName)"
    if [ -n "$STREAM" ] && [ "$STREAM" != "None" ]; then
      aws logs tail "$GROUP" --region "$REGION" --log-stream-names "$STREAM" --since 15m --follow &
      TAIL_PID=$!
    fi
  fi
  sleep "$POLL_SECONDS"
  # Ride out a throttled or dropped call; give up after 5 failures in a row.
  if NEXT="$(build_field buildStatus)"; then
    STATUS="$NEXT"
    FAILURES=0
  else
    FAILURES=$((FAILURES + 1))
    [ "$FAILURES" -lt 5 ] || { kill "$TAIL_PID" 2>/dev/null || true; die "cannot read the status of build $BUILD_ID; it may still be running"; }
  fi
done
if [ -n "$TAIL_PID" ]; then
  sleep 10 # let the tail print the last lines
  kill "$TAIL_PID" 2>/dev/null || true
  wait "$TAIL_PID" 2>/dev/null || true
fi
echo ""
echo "Build $BUILD_ID finished: $STATUS"
if [ "$STATUS" != "SUCCEEDED" ]; then
  echo "Full log: CloudWatch Logs group $(build_field logs.groupName), stream $(build_field logs.streamName)" >&2
  exit 1
fi
