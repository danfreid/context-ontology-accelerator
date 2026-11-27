#!/usr/bin/env bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
# Install what `make setup` and scripts/deploy.sh need on the CodeBuild arm64 image
# (Amazon Linux 2023, root): Node.js, uv, pnpm, Java 17-24 and a running Docker daemon.
# Run by the buildspec in infra/codebuild/coa-deploy-project.yaml from the repo root.
# Each tool is installed only when the one on PATH does not already meet the repo's
# own checks (scripts/check-tool-version.sh, scripts/check-java-version.sh), so those
# scripts stay the single definition of the supported versions.
set -euo pipefail
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"
# Version pinned in .mise.toml: `key = "x.y.z"`.
mise_pin() {
  sed -n "s/^$1 = \"\\(.*\\)\"\$/\\1/p" "$REPO_ROOT/.mise.toml" | head -n 1
}
# ── Node.js ──────────────────────────────────────────────────────────────
if ! ./scripts/check-tool-version.sh node >/dev/null 2>&1; then
  NODE_VERSION="$(mise_pin node)"
  [ -n "$NODE_VERSION" ] || { echo "ERROR: .mise.toml pins no node version" >&2; exit 1; }
  NODE_TARBALL="node-v${NODE_VERSION}-linux-arm64.tar.xz"
  NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}"
  echo "Installing Node.js ${NODE_VERSION} (arm64)..."
  TMP="$(mktemp -d)"
  curl -fsSL -o "$TMP/$NODE_TARBALL" "$NODE_URL/$NODE_TARBALL"
  curl -fsSL -o "$TMP/SHASUMS256.txt" "$NODE_URL/SHASUMS256.txt"
  (cd "$TMP" && grep " $NODE_TARBALL\$" SHASUMS256.txt | sha256sum -c -)
  tar -xJf "$TMP/$NODE_TARBALL" -C /usr/local --strip-components=1 --no-same-owner
  rm -rf "$TMP"
  hash -r
fi
./scripts/check-tool-version.sh node
# ── uv ───────────────────────────────────────────────────────────────────
if ! ./scripts/check-tool-version.sh uv >/dev/null 2>&1; then
  echo "Installing uv..."
  curl -LsSf https://astral.sh/uv/install.sh | UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
  hash -r
fi
./scripts/check-tool-version.sh uv
# ── pnpm (the version .mise.toml pins; a no-op when pnpm is already there) ─
./scripts/install-pnpm.sh
# ── Java 17-24 (Smithy code generation, the Gradle wrapper downloads Gradle itself) ─
if ! ./scripts/check-java-version.sh >/dev/null 2>&1; then
  echo "Installing Amazon Corretto 21..."
  dnf install -y java-21-amazon-corretto-devel
  hash -r
fi
./scripts/check-java-version.sh
# ── Docker (CDK asset bundling and image builds; needs privileged mode) ──────
if ! docker info >/dev/null 2>&1; then
  echo "Starting the Docker daemon..."
  if ! command -v dockerd >/dev/null 2>&1; then
    dnf install -y docker
  fi
  nohup dockerd >/var/log/dockerd.log 2>&1 &
  for _ in $(seq 1 30); do
    docker info >/dev/null 2>&1 && break
    sleep 2
  done
fi
docker info >/dev/null 2>&1 || {
  echo "ERROR: the Docker daemon is not running. The project needs privileged mode (it has it unless edited)." >&2
  tail -n 20 /var/log/dockerd.log 2>/dev/null || true
  exit 1
}
echo "Docker $(docker version --format '{{.Server.Version}}') on $(uname -m)"
