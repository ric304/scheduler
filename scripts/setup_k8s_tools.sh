#!/bin/bash
set -euo pipefail

mkdir -p ~/.local/bin ~/.kube

# kubectl をインストール
if ! command -v kubectl &>/dev/null; then
  echo "[install] kubectl v1.30.7"
  curl -LO https://dl.k8s.io/release/v1.30.7/bin/linux/amd64/kubectl
  chmod +x kubectl
  mv kubectl ~/.local/bin/
  export PATH="$HOME/.local/bin:$PATH"
fi

# helm をインストール
if ! command -v helm &>/dev/null; then
  echo "[install] helm"
  curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
fi

# kubeconfig をセット
export KUBECONFIG="$HOME/.kube/config"
kubectl get nodes
