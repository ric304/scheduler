#!/usr/bin/env bash
set -euo pipefail

# Update the scheduler worker Deployment image and watch rollout.
# Usage:
#   ./update_worker_image.sh <image:tag>
# Optional env:
#   NAMESPACE (default: scheduler)
#   DEPLOYMENT (default: scheduler-worker)
#   CONTAINER  (default: worker)

if [[ $# -lt 1 ]]; then
  echo "Usage: $0 <image:tag>" >&2
  exit 1
fi

IMAGE="$1"
NAMESPACE="${NAMESPACE:-scheduler}"
DEPLOYMENT="${DEPLOYMENT:-scheduler-worker}"
CONTAINER="${CONTAINER:-worker}"

echo "[set image] deployment/${DEPLOYMENT} ${CONTAINER}=${IMAGE} (ns=${NAMESPACE})" >&2
kubectl -n "${NAMESPACE}" set image "deployment/${DEPLOYMENT}" "${CONTAINER}=${IMAGE}"

echo "[rollout status]" >&2
kubectl -n "${NAMESPACE}" rollout status "deployment/${DEPLOYMENT}"

echo "[pods]" >&2
kubectl -n "${NAMESPACE}" get pods -o wide
