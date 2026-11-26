#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# OPTIONAL. Create (or update) the CodeBuild project that can deploy COA from AWS
# instead of your laptop: the CloudFormation stack in infra/codebuild/coa-deploy-project.yaml.
# Nothing else in the repo creates it. See external-docs/content/deploying.md,
# "Optional: deploy from AWS CodeBuild".
#
# Usage: make codebuild-setup        (or ./scripts/codebuild/setup.sh)
#
# Settings (environment variables, all optional except where noted):
#   COA_CODEBUILD_REPO_URL   https:// URL of the repo to deploy. Default: this checkout's
#                            "origin" remote, when it is a public https/ssh GitHub-style URL.
#   COA_CODEBUILD_REF        Default branch, tag or commit (default: main)
#   COA_CODEBUILD_STACK      CloudFormation stack name (default: coa-codebuild-deploy)
#   COA_CODEBUILD_COMPUTE    BUILD_GENERAL1_SMALL|MEDIUM|LARGE|XLARGE|2XLARGE (default LARGE)
#   COA_CODEBUILD_TIMEOUT    Build timeout in minutes, 5-480 (default 120)
#   COA_CODEBUILD_LOG_DAYS   CloudWatch Logs retention in days (default 30)
#   COA_CODEBUILD_QUALIFIER  CDK bootstrap qualifier (default hnb659fds)
#   COA_DEPLOY_ENV           Default deploy environment (default dev)
#   SCL_PREFIX               Resource prefix of the deployment (default coa)
# The AWS region is resolved like the deploy scripts do (see scripts/lib/resolve-region.sh);
# the project deploys into the region it is created in.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
# shellcheck source=../lib/resolve-region.sh source-path=SCRIPTDIR
source "$REPO_ROOT/scripts/lib/resolve-region.sh"
REGION="$(coa_resolve_region)"
STACK="${COA_CODEBUILD_STACK:-coa-codebuild-deploy}"
TEMPLATE="$REPO_ROOT/infra/codebuild/coa-deploy-project.yaml"
REPO_URL="${COA_CODEBUILD_REPO_URL:-}"
if [ -z "$REPO_URL" ]; then
  ORIGIN="$(git -C "$REPO_ROOT" remote get-url origin 2>/dev/null || true)"
  case "$ORIGIN" in
    https://*@*) ORIGIN="" ;; # credentials in the URL: never copy them anywhere
    https://*) REPO_URL="${ORIGIN%.git}" ;;
    git@*:*)
      SSH_REST="${ORIGIN#git@}"
      REPO_URL="https://${SSH_REST%%:*}/${SSH_REST#*:}"
      REPO_URL="${REPO_URL%.git}"
      ;;
  esac
fi
if [ -z "$REPO_URL" ]; then
  echo "ERROR: could not work out the repository URL. Set COA_CODEBUILD_REPO_URL=https://github.com/<owner>/<repo> (a public repository)." >&2
  exit 1
fi
if ! command -v aws >/dev/null 2>&1; then
  echo "ERROR: the AWS CLI is not installed." >&2
  exit 1
fi
echo "=== CodeBuild deploy project (optional) ==="
echo "Region:      $REGION"
echo "Stack:       $STACK"
echo "Repository:  $REPO_URL (public; no credentials are used)"
echo "Default ref: ${COA_CODEBUILD_REF:-main}"
echo "Review the IAM policy first: infra/codebuild/coa-deploy-project.yaml (BuildRole)."
echo ""
aws cloudformation deploy \
  --region "$REGION" \
  --stack-name "$STACK" \
  --template-file "$TEMPLATE" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --tags "app=coa" "purpose=optional-codebuild-deploy" \
  --parameter-overrides \
    "RepoUrl=$REPO_URL" \
    "RepoRef=${COA_CODEBUILD_REF:-main}" \
    "DeployEnv=${COA_DEPLOY_ENV:-dev}" \
    "ResourcePrefix=${SCL_PREFIX:-coa}" \
    "BootstrapQualifier=${COA_CODEBUILD_QUALIFIER:-hnb659fds}" \
    "ComputeType=${COA_CODEBUILD_COMPUTE:-BUILD_GENERAL1_LARGE}" \
    "TimeoutInMinutes=${COA_CODEBUILD_TIMEOUT:-120}" \
    "LogRetentionInDays=${COA_CODEBUILD_LOG_DAYS:-30}"
echo ""
echo "Project ready. Run a deploy with: make deploy-dev-codebuild"
echo "Remove it with:                    make codebuild-remove"
