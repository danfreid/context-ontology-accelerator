#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# OPTIONAL, CodeBuild only. Deploy a subset of the platform stacks with
# `cdk deploy <stacks> --exclusively`, doing everything scripts/deploy.sh does before
# its `cdk deploy --all`: the same context, preflight, SMUS admin check, locked install
# and build. The CodeBuild project runs it when COA_CDK_STACKS is set, so one deploy can
# be split across several builds on accounts whose builds are capped at 45 minutes (see
# "Optional: deploy from AWS CodeBuild" in external-docs/content/deploying.md).
# scripts/deploy.sh is unchanged and remains the normal path.
#
# Usage: ./scripts/codebuild/deploy-stacks.sh <env> <stack> [<stack> ...]
# Every stack's dependencies must already be deployed (--exclusively skips them).
set -euo pipefail
ENV="${1:?usage: deploy-stacks.sh <env> <stack> [<stack> ...]}"
shift
[ "$#" -gt 0 ] || { echo "deploy-stacks.sh: no stacks given" >&2; exit 2; }
for s in "$@"; do
  [[ "$s" =~ ^[A-Za-z][A-Za-z0-9-]{0,127}$ ]] || { echo "deploy-stacks.sh: invalid stack name '$s'" >&2; exit 2; }
done
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

CONTEXT=(--context "env=$ENV")
# shellcheck source=../lib/cdk-context.sh source-path=SCRIPTDIR
source "$REPO_ROOT/scripts/lib/cdk-context.sh"
ASSET_BUILD_CONCURRENCY="${SCL_ASSET_BUILD_CONCURRENCY:-2}"
[[ "$ASSET_BUILD_CONCURRENCY" =~ ^[1-9][0-9]*$ ]] || {
  echo "deploy-stacks.sh: SCL_ASSET_BUILD_CONCURRENCY must be a positive integer (got '$ASSET_BUILD_CONCURRENCY')" >&2
  exit 1
}
"$REPO_ROOT/scripts/check-smus-admin-principal.sh"
coa_add_jdbc_peering_context

echo "=== Deploying stacks [$*] to $ENV ==="
echo "CDK context: ${CONTEXT[*]}"
./scripts/preflight-deploy.sh "$ENV"
pnpm install --frozen-lockfile
# Synth reads the build output of every stack (the web app bundle, for example),
# even with --exclusively, so build everything.
pnpm nx run-many -t build
# Some stacks exist only with some settings (edge-waf, for example). Skip requested
# stacks this app does not define, instead of failing on them.
LISTED="$(pnpm --silent --filter coa-infra exec cdk list "${CONTEXT[@]}")" || {
  echo "deploy-stacks.sh: cdk list failed" >&2
  exit 1
}
mapfile -t DEFINED < <(tr -d '\r' <<< "$LISTED" | grep -E '^[A-Za-z][A-Za-z0-9-]*$')
[ "${#DEFINED[@]}" -gt 0 ] || { echo "deploy-stacks.sh: cdk list returned no stacks" >&2; exit 1; }
STACKS=()
for s in "$@"; do
  if printf '%s\n' "${DEFINED[@]}" | grep -qx -- "$s"; then
    STACKS+=("$s")
  else
    echo "Skipping $s: this deployment's CDK app does not define it."
  fi
done
[ "${#STACKS[@]}" -gt 0 ] || { echo "None of [$*] is defined by the CDK app; nothing to deploy."; exit 0; }
set -- "${STACKS[@]}"
pnpm --filter coa-infra exec cdk deploy "$@" --exclusively "${CONTEXT[@]}" \
  --require-approval=never \
  --concurrency "${SCL_CDK_CONCURRENCY:-3}" \
  --asset-build-concurrency "$ASSET_BUILD_CONCURRENCY"
echo "=== Stacks [$*] deployed to $ENV ==="
