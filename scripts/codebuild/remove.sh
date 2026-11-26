#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# OPTIONAL. Delete the CodeBuild deploy project created by scripts/codebuild/setup.sh:
# the project, its IAM role and its CloudWatch log group (the build logs go with it).
# The COA stacks that earlier builds deployed are NOT touched.
#
# Usage: make codebuild-remove
#   COA_CODEBUILD_STACK  stack name (default: coa-codebuild-deploy)
#   COA_CODEBUILD_YES=1  skip the confirmation prompt
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=../lib/resolve-region.sh source-path=SCRIPTDIR
source "$REPO_ROOT/scripts/lib/resolve-region.sh"
REGION="$(coa_resolve_region)"
STACK="${COA_CODEBUILD_STACK:-coa-codebuild-deploy}"
echo "This deletes stack $STACK in $REGION: the CodeBuild project, its role and its build logs."
echo "Stacks deployed by earlier builds stay."
if [ "${COA_CODEBUILD_YES:-}" != "1" ]; then
  if [ ! -t 0 ]; then
    echo "ERROR: not a terminal. Set COA_CODEBUILD_YES=1 to confirm." >&2
    exit 1
  fi
  read -r -p "Delete it? [y/N] " ANSWER
  [[ "$ANSWER" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 1; }
fi
aws cloudformation delete-stack --region "$REGION" --stack-name "$STACK"
aws cloudformation wait stack-delete-complete --region "$REGION" --stack-name "$STACK"
echo "Removed $STACK."
