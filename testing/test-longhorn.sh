#!/usr/bin/env bash
# End-to-end test: LUKS CSI driver backed by Longhorn in a Lima k3s VM.
#
# Prerequisites:
#   - Lima installed (brew install lima)
#   - A k3s Lima VM named "k3s" already running
#   - A docker Lima VM named "docker" already running
#   - helm installed on the host (brew install helm)
#   - HashiCorp Vault running and configured (see README Vault prerequisites)
#
# Usage:
#   bash testing/test-longhorn.sh [--skip-build] [--skip-longhorn] [--skip-vault] [--teardown]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SKIP_BUILD=false
SKIP_LONGHORN=false
SKIP_VAULT=false
TEARDOWN=false

for arg in "$@"; do
  case $arg in
    --skip-build)    SKIP_BUILD=true ;;
    --skip-longhorn) SKIP_LONGHORN=true ;;
    --skip-vault)    SKIP_VAULT=true ;;
    --teardown)      TEARDOWN=true ;;
  esac
done

# ---------------------------------------------------------------------------
# Kubeconfig — extracted from the Lima VM so we never touch the host context
# ---------------------------------------------------------------------------

KUBECONFIG_FILE="$(mktemp /tmp/lima-k3s-kubeconfig.XXXXXX)"
trap 'rm -f "${KUBECONFIG_FILE}"' EXIT

echo "[INFO]  Extracting kubeconfig from Lima k3s VM ..."
limactl shell k3s -- sudo cat /etc/rancher/k3s/k3s.yaml > "${KUBECONFIG_FILE}"

# k3s writes 127.0.0.1 — Lima port-forwards 6443 to the host's localhost, so this is correct as-is.

export KUBECONFIG="${KUBECONFIG_FILE}"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

info()  { echo "[INFO]  $*"; }
ok()    { echo "[OK]    $*"; }
die()   { echo "[ERROR] $*" >&2; exit 1; }

wait_for_pod() {
  local pod=$1 ns=${2:-default} timeout=${3:-120}
  info "Waiting for pod/${pod} in namespace ${ns} (up to ${timeout}s) ..."
  kubectl wait pod/"${pod}" -n "${ns}" \
    --for=condition=Ready --timeout="${timeout}s"
}

# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------

if [[ "${TEARDOWN}" == "true" ]]; then
  info "Removing test PVC and pod ..."
  kubectl delete -f "${SCRIPT_DIR}/longhorn-test-resources.yaml" --ignore-not-found

  info "Uninstalling LUKS CSI driver ..."
  helm uninstall luks-csi-driver --namespace kube-system 2>/dev/null || true

  info "Removing Vault dev server ..."
  kubectl delete pod/vault svc/vault -n default --ignore-not-found
  kubectl delete secret/vault-reviewer-token serviceaccount/vault -n default --ignore-not-found
  kubectl delete clusterrolebinding/vault-auth-delegator --ignore-not-found

  info "Uninstalling Longhorn ..."
  limactl shell k3s -- sudo bash -c "
    export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
    helm uninstall longhorn --namespace longhorn-system 2>/dev/null || true
  "
  kubectl delete namespace longhorn-system --ignore-not-found

  ok "Done. The Lima k3s VM is still running — stop it with: limactl stop k3s"
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 1: install open-iscsi in the Lima k3s VM (Longhorn requirement)
# ---------------------------------------------------------------------------

info "Installing open-iscsi in Lima k3s VM ..."
limactl shell k3s -- sudo bash -c "
  if ! dpkg -s open-iscsi &>/dev/null 2>&1; then
    apt-get update -qq && apt-get install -y open-iscsi
  fi
  systemctl enable --now iscsid
" && ok "open-iscsi ready."

# ---------------------------------------------------------------------------
# Step 2: install Longhorn (skip if already present)
# ---------------------------------------------------------------------------

if [[ "${SKIP_LONGHORN}" == "false" ]]; then
  if kubectl get ns longhorn-system &>/dev/null 2>&1; then
    info "Longhorn namespace already exists — skipping Longhorn install (use --skip-longhorn to suppress this check)."
  else
    info "Installing Longhorn (running helm inside Lima VM to avoid host network issues) ..."
    limactl shell k3s -- sudo bash -c "
      set -euo pipefail
      export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
      if ! command -v helm &>/dev/null; then
        echo '[INFO]  Installing helm inside Lima VM ...'
        curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
      fi
      helm repo add longhorn https://charts.longhorn.io --force-update
      helm upgrade --install longhorn longhorn/longhorn \
        --namespace longhorn-system --create-namespace \
        --set defaultSettings.defaultReplicaCount=1 \
        --wait --timeout 10m
    "
    ok "Longhorn installed."
  fi
else
  info "Skipping Longhorn install (--skip-longhorn)."
fi

info "Waiting for Longhorn manager to be ready ..."
kubectl rollout status daemonset/longhorn-manager -n longhorn-system --timeout=5m
ok "Longhorn manager ready."

# ---------------------------------------------------------------------------
# Step 3: deploy Vault dev server
# ---------------------------------------------------------------------------

if [[ "${SKIP_VAULT}" == "false" ]]; then
  if kubectl get pod/vault -n default &>/dev/null 2>&1; then
    info "Vault pod already exists — skipping Vault deploy (use --skip-vault to suppress this check)."
  else
    # Create a dedicated ServiceAccount + long-lived token Secret for Vault.
    # Projected SA tokens (the default pod mount) expire in ~1h and cannot be
    # used as a stable token_reviewer_jwt in Vault's Kubernetes auth config.
    info "Creating Vault service account and long-lived token ..."
    kubectl apply -f - <<'EOF'
apiVersion: v1
kind: ServiceAccount
metadata:
  name: vault
  namespace: default
---
apiVersion: v1
kind: Secret
metadata:
  name: vault-reviewer-token
  namespace: default
  annotations:
    kubernetes.io/service-account.name: vault
type: kubernetes.io/service-account-token
EOF

    kubectl create clusterrolebinding vault-auth-delegator \
      --clusterrole=system:auth-delegator \
      --serviceaccount=default:vault \
      --dry-run=client -o yaml | kubectl apply -f -

    # Wait for the controller to populate the token into the Secret.
    info "Waiting for vault reviewer token to be populated ..."
    until kubectl get secret vault-reviewer-token -n default \
        -o jsonpath='{.data.token}' 2>/dev/null | grep -q .; do sleep 2; done

    info "Deploying Vault dev server ..."
    kubectl apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: vault
  namespace: default
  labels:
    app: vault
spec:
  serviceAccountName: vault
  containers:
    - name: vault
      image: hashicorp/vault:1.15
      args: ["server", "-dev", "-dev-root-token-id=root", "-dev-listen-address=0.0.0.0:8200"]
      ports:
        - containerPort: 8200
      securityContext:
        capabilities:
          add: ["IPC_LOCK"]
---
apiVersion: v1
kind: Service
metadata:
  name: vault
  namespace: default
spec:
  selector:
    app: vault
  ports:
    - port: 8200
      targetPort: 8200
EOF
    wait_for_pod vault default 60

    info "Waiting for Vault to be ready to serve requests ..."
    until kubectl exec vault -n default -- sh -c \
        'VAULT_ADDR=http://127.0.0.1:8200 vault status' &>/dev/null; do
      sleep 2
    done

    info "Configuring Vault Kubernetes auth ..."
    REVIEWER_JWT=$(kubectl get secret vault-reviewer-token -n default \
      -o jsonpath='{.data.token}' | base64 -d)
    kubectl exec vault -n default -- sh -c "
      export VAULT_TOKEN=root VAULT_ADDR=http://127.0.0.1:8200
      vault auth enable kubernetes 2>/dev/null || true
      vault write auth/kubernetes/config \
        kubernetes_host=https://kubernetes.default.svc:443 \
        kubernetes_ca_cert=@/var/run/secrets/kubernetes.io/serviceaccount/ca.crt \
        token_reviewer_jwt='${REVIEWER_JWT}'
      vault policy write luks-policy - <<POLICY
path \"secret/data/tenants/*\"    { capabilities = [\"create\",\"read\",\"update\",\"delete\",\"list\"] }
path \"secret/metadata/tenants/*\" { capabilities = [\"read\",\"list\",\"delete\"] }
POLICY
      vault write auth/kubernetes/role/luks-csi-role \
        bound_service_account_names=luks-csi-driver-controller,luks-csi-driver-node \
        bound_service_account_namespaces=kube-system \
        policies=luks-policy \
        ttl=24h
    "
    ok "Vault ready."
  fi
else
  info "Skipping Vault deploy (--skip-vault)."
fi

# ---------------------------------------------------------------------------
# Step 4: build and import the LUKS CSI image (skip if requested)
# ---------------------------------------------------------------------------

if [[ "${SKIP_BUILD}" == "false" ]]; then
  info "Building luks-csi:dev inside Docker Lima VM ..."
  limactl shell docker -- docker build \
    -t luks-csi:dev \
    "${HOME}/Documents/k8s-block-storage-luks-csi-config/"

  info "Importing image into k3s containerd ..."
  limactl shell docker -- docker save luks-csi:dev \
    | limactl shell k3s -- sudo k3s ctr images import -
  ok "Image imported."
else
  info "Skipping image build (--skip-build)."
fi

# ---------------------------------------------------------------------------
# Step 5: deploy the LUKS CSI driver
# ---------------------------------------------------------------------------

# Delete resources that are either immutable (StorageClass.parameters) or
# would block a fresh Helm install if left over from a previous kubectl/Helm deploy.
info "Removing any leftover LUKS CSI resources ..."
kubectl delete storageclass/luks-encrypted --ignore-not-found
kubectl delete deployment  -n kube-system -l app.kubernetes.io/name=luks-csi-driver --ignore-not-found
kubectl delete daemonset   -n kube-system -l app.kubernetes.io/name=luks-csi-driver --ignore-not-found

info "Deploying LUKS CSI driver (Helm) ..."
helm upgrade --install luks-csi-driver "${REPO_ROOT}/luks-csi-driver/" \
  --namespace kube-system \
  --set storageClass.backingStorageClass=longhorn \
  --set apparmor.enabled=false \
  --set apparmor.annotate=false
ok "LUKS CSI driver deployed."

kubectl rollout status deployment/luks-csi-driver-controller -n kube-system --timeout=5m
kubectl rollout status daemonset/luks-csi-driver-node        -n kube-system --timeout=5m

# ---------------------------------------------------------------------------
# Step 6: apply test resources
# ---------------------------------------------------------------------------

info "Applying Longhorn test resources ..."
kubectl apply -f "${SCRIPT_DIR}/longhorn-test-resources.yaml"

info "Waiting for PVC to bind ..."
kubectl wait pvc/longhorn-test-pvc \
  --for=jsonpath='{.status.phase}'=Bound --timeout=120s
ok "PVC bound."

wait_for_pod luks-longhorn-test-pod default 180

# ---------------------------------------------------------------------------
# Step 7: verify
# ---------------------------------------------------------------------------

info "Verifying file written by test pod ..."
FILE_CONTENT=$(kubectl exec luks-longhorn-test-pod -- cat /mnt/data/test.txt)
if [[ "${FILE_CONTENT}" == "hello from luks+longhorn" ]]; then
  ok "File contents correct: '${FILE_CONTENT}'"
else
  die "Unexpected file contents: '${FILE_CONTENT}'"
fi

info "Filesystem on encrypted mount:"
kubectl exec luks-longhorn-test-pod -- df -h /mnt/data

info "Active LUKS mapper on node:"
MAPPER=$(limactl shell k3s -- ls /dev/mapper/ 2>/dev/null | grep '^luks-' | head -1 || true)
if [[ -n "${MAPPER}" ]]; then
  limactl shell k3s -- sudo cryptsetup status "${MAPPER}"
else
  echo "  (no luks- mapper found — NodeStageVolume may not have run yet)"
fi

echo ""
ok "Longhorn LUKS test passed."
echo ""
echo "To tear down:  bash testing/test-longhorn.sh --teardown"
