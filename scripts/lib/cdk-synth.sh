# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# shellcheck shell=bash

# Runs a `cdk synth` command, retrying one Windows-only failure.
#
# On Windows, CDK's asset staging intermittently fails with
#   EPERM: operation not permitted, rename 'infra\cdk.out\asset.<hash>-building' -> 'infra\cdk.out\asset.<hash>'
# (another process, typically a virus scanner or the search indexer, still has
# the freshly bundled directory open), on a different asset each time. Bundles
# that finished are kept in cdk.out, so a re-run carries on where it stopped.
# CDK has no setting to retry the rename, so under Git Bash, MSYS2 or Cygwin
# the command is re-run, up to COA_CDK_SYNTH_ATTEMPTS times, when that rename
# is what failed. Any other failure, and every run on Linux or macOS, returns
# the command's status after one attempt, exactly as running it directly.
#
# Sourced, not run. Plain bash 3.2, since the scripts also run on macOS:
#   source "${REPO_ROOT}/scripts/lib/cdk-synth.sh"
#   coa_cdk_synth pnpm --filter coa-infra exec cdk synth "${CONTEXT[@]}" >/dev/null

COA_CDK_SYNTH_ATTEMPTS=5

coa_cdk_synth() {
  case "$(uname -s)" in
    MINGW* | MSYS* | CYGWIN*) ;;
    *)
      "$@"
      return
      ;;
  esac
  local attempt=1 rc log
  log="$(mktemp)"
  while :; do
    # stdout goes where the caller sent it; stderr is shown and kept in $log.
    rc=0
    { "$@" 2>&1 1>&3 | tee "$log" >&2; rc=${PIPESTATUS[0]}; } 3>&1 || true
    if [ "$rc" -eq 0 ]; then
      break
    fi
    if [ "$attempt" -ge "$COA_CDK_SYNTH_ATTEMPTS" ] \
      || ! grep -Eq "EPERM: operation not permitted, rename .*asset\.[0-9a-f]+-building" "$log"; then
      break
    fi
    attempt=$((attempt + 1))
    echo "⚠ cdk synth hit the Windows asset-staging EPERM rename; finished bundles are cached, retrying (attempt $attempt of $COA_CDK_SYNTH_ATTEMPTS)..." >&2
  done
  rm -f "$log"
  return "$rc"
}
