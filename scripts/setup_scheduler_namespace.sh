#!/bin/bash
set -euo pipefail

export KUBECONFIG=~/.kube/config

echo "[step 1] Create scheduler namespace"
kubectl create namespace scheduler || echo "scheduler namespace already exists"

echo "[step 2] Copy TLS certificate from dev-certs"
if [ ! -f "dev-certs/tls.crt" ] || [ ! -f "dev-certs/tls.key" ]; then
  echo "ERROR: dev-certs/tls.crt or dev-certs/tls.key not found"
  exit 1
fi

echo "[step 3] Create TLS Secret in scheduler namespace"
kubectl -n scheduler create secret generic scheduler-grpc-tls \
  --from-file=tls.crt=dev-certs/tls.crt \
  --from-file=tls.key=dev-certs/tls.key \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[step 4] Create DB Credentials Secret"
kubectl -n scheduler create secret generic scheduler-db-credentials \
  --from-literal=DATABASE_URL='postgresql://postgres:postgres@scheduler-postgres-postgresql.scheduler-infra.svc.cluster.local:5432/scheduler' \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[step 5] Create Redis Credentials Secret"
kubectl -n scheduler create secret generic scheduler-redis-credentials \
  --from-literal=REDIS_URL='redis://:redispassword@scheduler-redis-master.scheduler-infra.svc.cluster.local:6379/0' \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[done] scheduler namespace setup complete"
kubectl get secrets -n scheduler
