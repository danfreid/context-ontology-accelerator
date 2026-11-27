#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Pause a deployed COA environment to cut its idle cost, without losing data:
#   - ECS services (ontology engine, VKG, ...) are scaled to 0 tasks,
#   - Neptune clusters are stopped (storage is still billed; AWS restarts a stopped
#     cluster after 7 days, so rerun this script to stop it again),
#   - OpenSearch Serverless collection groups get a minimum capacity of 0 OCU, so they
#     scale to zero when idle (data is kept; the first request after idle is slow).
# VPC endpoints, the NAT gateway and usage-billed services are left as they are.
#
# The previous settings are saved in SSM (/<prefix>/<env>/paused-state) and restored by
# scripts/ops/resume-env.sh. Running this script again on a paused environment keeps the
# saved settings and only re-applies the pause (for example after Neptune auto-restarted).
#
# These are direct changes, not CloudFormation updates: a `cdk deploy` of the affected
# stacks while paused puts their settings back. Resume before deploying.
#
# Usage: ./scripts/ops/pause-env.sh [prefix] [env]      (defaults: $SCL_PREFIX or coa, dev)
set -euo pipefail
export AWS_PAGER=""
# Git Bash: do not turn /coa/... arguments into Windows paths.
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

echo "=== Pausing ${NAME} in ${REGION} ==="
STATE="$(aws_ ssm get-parameter --name "$STATE_PARAM" --query Parameter.Value --output text 2>/dev/null || true)"

if [ -z "$STATE" ]; then
  # Record what is running now, before changing anything.
  lines=()
  for cluster in $(aws_ ecs list-clusters --query "clusterArns[?contains(@, ':cluster/${NAME}-')]" --output text); do
    for svc in $(aws_ ecs list-services --cluster "$cluster" --query "serviceArns" --output text); do
      desired="$(aws_ ecs describe-services --cluster "$cluster" --services "$svc" --query "services[0].desiredCount" --output text)"
      [ "$desired" -gt 0 ] && lines+=("ecs ${cluster##*/} ${svc##*/} $desired")
    done
  done
  for id in $(aws_ opensearchserverless list-collection-groups --query "collectionGroupSummaries[?starts_with(name, '${NAME}-')].id" --output text); do
    read -r min_idx min_srch <<< "$(aws_ opensearchserverless batch-get-collection-group --ids "$id" \
      --query "collectionGroupDetails[0].capacityLimits.[minIndexingCapacityInOCU,minSearchCapacityInOCU]" --output text)"
    lines+=("aoss $id ${min_idx%.*} ${min_srch%.*}")
  done
  for cl in $(aws_ neptune describe-db-clusters --query "DBClusters[?starts_with(DBClusterIdentifier, '${NAME}-') && Engine=='neptune'].DBClusterIdentifier" --output text); do
    lines+=("neptune $cl")
  done
  if [ "${#lines[@]}" -eq 0 ]; then
    echo "Nothing to pause for ${NAME} (no ECS services with tasks, collection groups or Neptune clusters found)."
    exit 0
  fi
  STATE="$(printf '%s\n' "${lines[@]}")"
  aws_ ssm put-parameter --name "$STATE_PARAM" --type String --overwrite \
    --description "Settings saved by scripts/ops/pause-env.sh; restored by resume-env.sh" \
    --value "$STATE" >/dev/null
  echo "Saved current settings to $STATE_PARAM"
else
  echo "Already paused: keeping the settings saved in $STATE_PARAM and re-applying the pause."
fi

while read -r kind a b c; do
  case "$kind" in
    ecs)
      aws_ ecs update-service --cluster "$a" --service "$b" --desired-count 0 --query "service.serviceName" --output text >/dev/null
      echo "ECS      $b: desired count $c -> 0"
      ;;
    aoss)
      read -r max_idx max_srch <<< "$(aws_ opensearchserverless batch-get-collection-group --ids "$a" \
        --query "collectionGroupDetails[0].capacityLimits.[maxIndexingCapacityInOCU,maxSearchCapacityInOCU]" --output text)"
      aws_ opensearchserverless update-collection-group --id "$a" --capacity-limits \
        "minIndexingCapacityInOCU=0,minSearchCapacityInOCU=0,maxIndexingCapacityInOCU=${max_idx%.*},maxSearchCapacityInOCU=${max_srch%.*}" >/dev/null
      echo "AOSS     $a: minimum OCU $b/$c -> 0/0 (indexing/search)"
      ;;
    neptune)
      status="$(aws_ neptune describe-db-clusters --db-cluster-identifier "$a" --query "DBClusters[0].Status" --output text)"
      if [ "$status" = "available" ]; then
        aws_ neptune stop-db-cluster --db-cluster-identifier "$a" >/dev/null
        echo "Neptune  $a: stopping (takes a few minutes)"
      else
        echo "Neptune  $a: $status (left as is)"
      fi
      ;;
  esac
done <<< "$STATE"

echo ""
echo "${NAME} is paused. Resume with: ./scripts/ops/resume-env.sh ${PREFIX} ${ENV_NAME}"
echo "Neptune restarts by itself after 7 days stopped; rerun this script to stop it again."
