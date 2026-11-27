#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Resume a COA environment paused by scripts/ops/pause-env.sh: start its Neptune clusters,
# restore the OpenSearch Serverless minimum capacity, and scale its ECS services back to
# the task counts saved in SSM (/<prefix>/<env>/paused-state). Waits until Neptune is
# available and the services are stable, then deletes the saved state.
#
# Usage: ./scripts/ops/resume-env.sh [prefix] [env]     (defaults: $SCL_PREFIX or coa, dev)
set -euo pipefail
export AWS_PAGER=""
export MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL="*"

PREFIX="${1:-${SCL_PREFIX:-coa}}"
ENV_NAME="${2:-dev}"
[[ "$PREFIX" =~ ^[a-z][a-z0-9-]{0,19}$ ]] || { echo "invalid prefix '$PREFIX'" >&2; exit 2; }
[[ "$ENV_NAME" =~ ^[a-z0-9][a-z0-9-]{0,19}$ ]] || { echo "invalid env '$ENV_NAME'" >&2; exit 2; }
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region 2>/dev/null || true)}}"
REGION="${REGION:-us-east-1}"
NAME="${PREFIX}-${ENV_NAME}"
STATE_PARAM="/${PREFIX}/${ENV_NAME}/paused-state"
# The AWS CLI on Windows ends lines with CR; strip it so counts and IDs stay clean.
aws_() { aws --region "$REGION" "$@" | tr -d '\r'; }

STATE="$(aws_ ssm get-parameter --name "$STATE_PARAM" --query Parameter.Value --output text 2>/dev/null || true)"
[ -n "$STATE" ] || { echo "${NAME} is not paused (no $STATE_PARAM)."; exit 0; }
echo "=== Resuming ${NAME} in ${REGION} ==="

# Neptune first: it takes longest, and the services need it.
neptune=()
while read -r kind a _; do
  [ "$kind" = "neptune" ] || continue
  neptune+=("$a")
  status="$(aws_ neptune describe-db-clusters --db-cluster-identifier "$a" --query "DBClusters[0].Status" --output text)"
  case "$status" in
    stopped) aws_ neptune start-db-cluster --db-cluster-identifier "$a" >/dev/null; echo "Neptune  $a: starting" ;;
    *) echo "Neptune  $a: $status" ;;
  esac
done <<< "$STATE"

while read -r kind a b c; do
  [ "$kind" = "aoss" ] || continue
  read -r max_idx max_srch <<< "$(aws_ opensearchserverless batch-get-collection-group --ids "$a" \
    --query "collectionGroupDetails[0].capacityLimits.[maxIndexingCapacityInOCU,maxSearchCapacityInOCU]" --output text)"
  aws_ opensearchserverless update-collection-group --id "$a" --capacity-limits \
    "minIndexingCapacityInOCU=$b,minSearchCapacityInOCU=$c,maxIndexingCapacityInOCU=${max_idx%.*},maxSearchCapacityInOCU=${max_srch%.*}" >/dev/null
  echo "AOSS     $a: minimum OCU -> $b/$c (indexing/search)"
done <<< "$STATE"

for cl in "${neptune[@]}"; do
  echo "Waiting for Neptune $cl to be available..."
  for _ in $(seq 1 60); do
    status="$(aws_ neptune describe-db-clusters --db-cluster-identifier "$cl" --query "DBClusters[0].Status" --output text)"
    [ "$status" = "available" ] && break
    sleep 20
  done
  [ "$status" = "available" ] || { echo "Neptune $cl is still '$status' after 20 minutes; rerun this script." >&2; exit 1; }
  # The cluster can report available before its instances do.
  for _ in $(seq 1 45); do
    pending="$(aws_ neptune describe-db-instances --filters "Name=db-cluster-id,Values=$cl" \
      --query "length(DBInstances[?DBInstanceStatus!='available'])" --output text)"
    [ "$pending" = "0" ] && break
    sleep 20
  done
  echo "Neptune  $cl: available"
done

services=()
while read -r kind a b c; do
  [ "$kind" = "ecs" ] || continue
  aws_ ecs update-service --cluster "$a" --service "$b" --desired-count "$c" --query "service.serviceName" --output text >/dev/null
  echo "ECS      $b: desired count -> $c"
  services+=("$a $b")
done <<< "$STATE"
for s in "${services[@]}"; do
  read -r cluster svc <<< "$s"
  echo "Waiting for $svc to be stable..."
  aws_ ecs wait services-stable --cluster "$cluster" --services "$svc" ||
    { echo "$svc did not become stable; check its events in the ECS console." >&2; exit 1; }
done

aws_ ssm delete-parameter --name "$STATE_PARAM" >/dev/null
echo ""
echo "${NAME} is running again."
