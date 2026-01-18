#!/usr/bin/env bash
set -euo pipefail

# Build and push an image to GHCR.
# Usage:
#   GHCR_USER=<user> GHCR_TOKEN=<token> ./build_and_push_ghcr.sh ghcr.io/<org>/scheduler-worker <tag> [context]
# Defaults: context='.'

if [[ $# -lt 2 ]]; then
  echo "Usage: GHCR_USER=<user> GHCR_TOKEN=<token> $0 ghcr.io/<org>/image <tag> [context]" >&2
  exit 1
fi

IMAGE_REPO="$1"
TAG="$2"
CONTEXT="${3:-.}"
IMAGE="${IMAGE_REPO}:${TAG}"
DOCKERFILE="${DOCKERFILE:-Dockerfile}"

if [[ -z "${GHCR_USER:-}" || -z "${GHCR_TOKEN:-}" ]]; then
  echo "GHCR_USER / GHCR_TOKEN must be set" >&2
  exit 1
fi

echo "[login] ghcr.io as ${GHCR_USER}" >&2
echo "${GHCR_TOKEN}" | docker login ghcr.io -u "${GHCR_USER}" --password-stdin

echo "[build] ${IMAGE} (context=${CONTEXT}, dockerfile=${DOCKERFILE})" >&2
docker build -t "${IMAGE}" -f "${DOCKERFILE}" "${CONTEXT}"

echo "[push] ${IMAGE}" >&2
docker push "${IMAGE}"

echo "[done] ${IMAGE}" >&2
