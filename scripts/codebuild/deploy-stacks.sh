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
# A requested stack the CDK app does not define fails the run, except "<...>-edge-waf",
# which exists only with some WAF settings and is skipped when absent.
# Docker only: it does not reproduce deploy.sh's Finch selection, and it is meant for the
# CodeBuild host (arm64 Linux, Docker), not for local use. Plain bash 3.2 syntax.
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
# Parallel image builds each need their own memory (deploy.sh defaults to 1). The default is 2
# only on a host with at least 12 GiB, which the LARGE compute size has; SMALL and MEDIUM get 1.
DEFAULT_CONCURRENCY=1
MEM_KB="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || true)"
if [[ "$MEM_KB" =~ ^[0-9]+$ ]] && [ "$MEM_KB" -ge 12582912 ]; then DEFAULT_CONCURRENCY=2; fi
ASSET_BUILD_CONCURRENCY="${SCL_ASSET_BUILD_CONCURRENCY:-$DEFAULT_CONCURRENCY}"
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
DEFINED=()
while IFS= read -r line; do
  DEFINED+=("$line")
done < <(tr -d '\r' <<< "$LISTED" | grep -E '^[A-Za-z][A-Za-z0-9-]*$' || true)
[ "${#DEFINED[@]}" -gt 0 ] || { echo "deploy-stacks.sh: cdk list returned no stacks" >&2; exit 1; }
STACKS=()
MISSING=()
for s in "$@"; do
  if printf '%s\n' "${DEFINED[@]}" | grep -qx -- "$s"; then
    STACKS+=("$s")
  elif [[ "$s" == *-edge-waf ]]; then
    echo "Skipping $s: this deployment's CDK app does not define it."
  else
    MISSING+=("$s")
  fi
done
if [ "${#MISSING[@]}" -gt 0 ]; then
  echo "deploy-stacks.sh: the CDK app does not define: ${MISSING[*]}" >&2
  echo "Defined stacks: ${DEFINED[*]}" >&2
  exit 1
fi
[ "${#STACKS[@]}" -gt 0 ] || { echo "Only skipped stacks were requested; nothing to deploy."; exit 0; }
set -- "${STACKS[@]}"
pnpm --filter coa-infra exec cdk deploy "$@" --exclusively "${CONTEXT[@]}" \
  --require-approval=never \
  --concurrency "${SCL_CDK_CONCURRENCY:-3}" \
  --asset-build-concurrency "$ASSET_BUILD_CONCURRENCY"
echo "=== Stacks [$*] deployed to $ENV ==="
