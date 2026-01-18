#!/bin/bash
set -euo pipefail

export KUBECONFIG=~/.kube/config

echo "[step 1] Create namespace scheduler-infra"
kubectl create namespace scheduler-infra || echo "scheduler-infra namespace already exists"

echo "[step 2] Add Helm repositories"
helm repo add bitnami https://charts.bitnami.com/bitnami
helm repo update

echo "[step 3] Deploy Redis (replication + sentinel)"
helm upgrade --install scheduler-redis bitnami/redis -n scheduler-infra \
  --set architecture=replication \
  --set auth.password="redispassword" \
  --set global.storageClass=local-path \
  --set master.persistence.enabled=true \
  --set master.persistence.size=2Gi \
  --set replica.replicaCount=2 \
  --set replica.persistence.enabled=true \
  --set replica.persistence.size=2Gi \
  --set sentinel.enabled=true \
  --set sentinel.replicaCount=3 \
  --wait

echo "[step 4] Deploy PostgreSQL"
helm upgrade --install scheduler-postgres bitnami/postgresql -n scheduler-infra \
  --set auth.username=postgres \
  --set auth.password=postgres \
  --set auth.database=scheduler \
  --set primary.persistence.size=5Gi \
  --set global.storageClass=local-path \
  --wait

echo "[step 5] Deploy MinIO"
helm upgrade --install scheduler-minio bitnami/minio -n scheduler-infra \
  --set auth.rootUser=minioadmin \
  --set auth.rootPassword=minioadmin \
  --set persistence.size=10Gi \
  --set global.storageClass=local-path \
  --wait

echo "[step 6] Create Secrets in scheduler-infra for reference"
kubectl -n scheduler-infra create secret generic scheduler-db-credentials \
  --from-literal=DATABASE_URL='postgresql://postgres:postgres@scheduler-postgres-postgresql.scheduler-infra.svc.cluster.local:5432/scheduler' \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n scheduler-infra create secret generic scheduler-redis-credentials \
  --from-literal=REDIS_URL='redis://:redispassword@scheduler-redis-master.scheduler-infra.svc.cluster.local:6379/0' \
  --from-literal=REDIS_PASSWORD='redispassword' \
  --dry-run=client -o yaml | kubectl apply -f -

kubectl -n scheduler-infra create secret generic scheduler-minio-credentials \
  --from-literal=MINIO_ENDPOINT='http://scheduler-minio.scheduler-infra.svc.cluster.local:9000' \
  --from-literal=MINIO_ACCESS_KEY='minioadmin' \
  --from-literal=MINIO_SECRET_KEY='minioadmin' \
  --dry-run=client -o yaml | kubectl apply -f -

echo "[done] scheduler-infra setup complete"
kubectl get pods -n scheduler-infra -o wide
kubectl get svc -n scheduler-infra
