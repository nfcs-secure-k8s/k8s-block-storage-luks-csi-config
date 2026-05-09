#!/usr/bin/env bash
# End-to-end test: LUKS CSI driver backed by Rook-Ceph (RBD) in a Lima k3s VM.
#
# Prerequisites:
#   - Lima installed (brew install lima)
#   - A k3s Lima VM named "k3s" already running with an extra disk for the Ceph OSD
#     (see README for Lima VM disk configuration)
#   - A docker Lima VM named "docker" already running
#   - helm installed on the host (brew install helm)
#   - HashiCorp Vault running and configured (see README Vault prerequisites)
#
# The k3s Lima VM needs a second disk (e.g. 20 GiB) for the Ceph OSD. Add this
# to your Lima k3s template before starting the VM:
#
#   additionalDisks:
#     - name: ceph-osd
#       size: "20GiB"
#
# The disk will appear as /dev/vdb (or /dev/sdb) inside the VM. Rook will claim
# it automatically via deviceFilter.
#
# Usage:
#   bash testing/test-ceph.sh [--skip-build] [--skip-ceph] [--skip-vault] [--teardown]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

SKIP_BUILD=false
SKIP_CEPH=false
SKIP_VAULT=false
TEARDOWN=false
# Lima VM name — matches the template started as: limactl start lima-k3s-ceph.yaml --name k3s-ceph
K3S_VM="k3s-ceph"
DOCKER_VM="docker"

for arg in "$@"; do
  case $arg in
    --skip-build) SKIP_BUILD=true ;;
    --skip-ceph)  SKIP_CEPH=true ;;
    --skip-vault) SKIP_VAULT=true ;;
    --teardown)   TEARDOWN=true ;;
  esac
done

# ---------------------------------------------------------------------------
# Kubeconfig — extracted from the Lima VM so we never touch the host context
# ---------------------------------------------------------------------------

KUBECONFIG_FILE="$(mktemp /tmp/lima-k3s-kubeconfig.XXXXXX)"
trap 'rm -f "${KUBECONFIG_FILE}"' EXIT

echo "[INFO]  Extracting kubeconfig from Lima ${K3S_VM} VM ..."
limactl shell "${K3S_VM}" -- sudo cat /etc/rancher/k3s/k3s.yaml > "${KUBECONFIG_FILE}"

# k3s-ceph listens on 6445 (avoids clashing with the base k3s VM on 6443).
# Lima binds 127.0.0.1:6445 on the host and k3s writes 127.0.0.1:6445 in
# the kubeconfig, so no rewrite is needed — use it as-is.

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
  kubectl delete -f "${SCRIPT_DIR}/ceph-test-resources.yaml" --ignore-not-found

  info "Uninstalling LUKS CSI driver ..."
  helm uninstall luks-csi-driver --namespace kube-system 2>/dev/null || true

  info "Removing Vault dev server ..."
  kubectl delete pod/vault svc/vault -n default --ignore-not-found
  kubectl delete secret/vault-reviewer-token serviceaccount/vault -n default --ignore-not-found
  kubectl delete clusterrolebinding/vault-auth-delegator --ignore-not-found

  info "Uninstalling Rook-Ceph ..."
  limactl shell "${K3S_VM}" -- sudo bash -c "
    export KUBECONFIG=/etc/rancher/k3s/k3s.yaml
    helm uninstall rook-ceph-cluster --namespace rook-ceph 2>/dev/null || true
    helm uninstall rook-ceph         --namespace rook-ceph 2>/dev/null || true
  "
  # Rook leaves finalizers on the namespace; patch them away first.
  kubectl patch cephcluster rook-ceph -n rook-ceph \
    --type merge -p '{"metadata":{"finalizers":[]}}' 2>/dev/null || true
  kubectl delete namespace rook-ceph --ignore-not-found

  ok "Done. The Lima VM is still running — stop it with: limactl stop ${K3S_VM}"
  exit 0
fi

# ---------------------------------------------------------------------------
# Step 1: detect the OSD disk inside the Lima VM
# ---------------------------------------------------------------------------

info "Checking OSD loop device inside Lima VM ..."
# The Lima template provisions /dev/loop10 backed by a 20 GiB sparse file.
# We use a fixed device name rather than scanning so we never accidentally
# pick the root disk or a Lima-mounted additionalDisk (Lima formats those).
OSD_DEVICE="/dev/loop10"
OSD_DEVICE_NAME="loop10"

limactl shell "${K3S_VM}" -- sudo losetup "${OSD_DEVICE}" &>/dev/null \
  || die "OSD loop device ${OSD_DEVICE} is not set up in the Lima VM. Recreate the VM with: limactl delete k3s-ceph && limactl start testing/lima-k3s-ceph.yaml --name k3s-ceph"

info "Using ${OSD_DEVICE} as the Ceph OSD device."

# ---------------------------------------------------------------------------
# Step 2: install Rook-Ceph (skip if already present)
# ---------------------------------------------------------------------------

if [[ "${SKIP_CEPH}" == "false" ]]; then
  if kubectl get ns rook-ceph &>/dev/null 2>&1; then
    info "rook-ceph namespace already exists — skipping Rook install (use --skip-ceph to suppress this check)."
  else
    info "Installing Rook-Ceph operator and cluster (running helm inside Lima VM) ..."
    limactl shell "${K3S_VM}" -- sudo bash -c "
      set -euo pipefail
      export KUBECONFIG=/etc/rancher/k3s/k3s.yaml

      if ! command -v helm &>/dev/null; then
        echo '[INFO]  Installing helm inside Lima VM ...'
        curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | bash
      fi

      helm repo add rook-release https://charts.rook.io/release --force-update

      # Rook operator
      # allowLoopDevices=true: required so the OSD can use /dev/loop10.
      # enableCephfsDriver=false: removes the 5-container CephFS CSI ctrlplugin
      #   which we don't need for block storage testing and causes CPU exhaustion
      #   on a single-node dev cluster.
      helm upgrade --install rook-ceph rook-release/rook-ceph \
        --namespace rook-ceph --create-namespace \
        --set csi.enableRbdDriver=true \
        --set csi.enableCephfsDriver=false \
        --set allowLoopDevices=true \
        --wait --timeout 5m

      # Single-node Ceph cluster
      # - mon.count=1: only one monitor (not HA, fine for testing)
      # - replicated.size=1: no replication — stores data on the single OSD
      # - storage.useAllDevices=false / deviceFilter: only claim the OSD disk we found
      helm upgrade --install rook-ceph-cluster rook-release/rook-ceph-cluster \
        --namespace rook-ceph \
        --set operatorNamespace=rook-ceph \
        --set cephClusterSpec.mon.count=1 \
        --set cephClusterSpec.mon.allowMultiplePerNode=true \
        --set cephClusterSpec.mgr.count=1 \
        --set cephClusterSpec.storage.useAllDevices=false \
        --set 'cephClusterSpec.storage.devices[0].name=/dev/${OSD_DEVICE_NAME}' \
        --set cephBlockPools[0].name=replicapool \
        --set cephBlockPools[0].spec.failureDomain=osd \
        --set cephBlockPools[0].spec.replicated.size=1 \
        --set cephBlockPools[0].storageClass.enabled=true \
        --set cephBlockPools[0].storageClass.name=rook-ceph-block \
        --set cephBlockPools[0].storageClass.isDefault=false \
        --set cephFileSystems=null \
        --set cephObjectStores=null \
        --wait --timeout 15m
    "
    ok "Rook-Ceph installed."
  fi
else
  info "Skipping Rook-Ceph install (--skip-ceph)."
fi

info "Waiting for Rook-Ceph operator to be ready ..."
kubectl rollout status deployment/rook-ceph-operator -n rook-ceph --timeout=5m
ok "Rook-Ceph operator ready."

info "Waiting for RBD CSI provisioner to be ready (up to 5m) ..."
# The pod name differs between CSI operator mode (*.rbd.csi.ceph.com-ctrlplugin-*)
# and legacy mode (csi-rbdplugin-provisioner-*). Match by name substring.
DEADLINE_CSI=$(( $(date +%s) + 300 ))
while true; do
  READY=$(kubectl get pods -n rook-ceph --no-headers 2>/dev/null \
    | grep -E 'ctrlplugin|csi-rbdplugin-provisioner' \
    | grep -c 'Running' || true)
  if [[ "${READY}" -gt 0 ]]; then
    ok "RBD CSI provisioner is ready."
    break
  fi
  if (( $(date +%s) >= DEADLINE_CSI )); then
    die "RBD CSI provisioner did not become ready within 5 minutes."
  fi
  info "  Waiting for RBD CSI provisioner ..."
  sleep 10
done

info "Waiting for CephCluster to be healthy (up to 10m) ..."
# Accept HEALTH_OK or HEALTH_WARN — with replicated.size=1 Ceph always emits
# POOL_NO_REDUNDANCY which keeps it at HEALTH_WARN on a single-node cluster.
DEADLINE=$(( $(date +%s) + 600 ))
while true; do
  HEALTH=$(kubectl get cephcluster rook-ceph -n rook-ceph \
    -o jsonpath='{.status.ceph.health}' 2>/dev/null || true)
  if [[ "${HEALTH}" == "HEALTH_OK" || "${HEALTH}" == "HEALTH_WARN" ]]; then
    ok "CephCluster is ${HEALTH}."
    break
  fi
  if (( $(date +%s) >= DEADLINE )); then
    die "CephCluster did not reach HEALTH_OK within 10 minutes (current: ${HEALTH:-unknown})."
  fi
  info "  CephCluster health: ${HEALTH:-unknown} — waiting ..."
  sleep 15
done

# ---------------------------------------------------------------------------
# Step 3: recreate the rook-ceph-block StorageClass with full CSI parameters
# ---------------------------------------------------------------------------
# The helm chart's --set cephBlockPools[0].storageClass.* generates an incomplete
# StorageClass (missing CSI secret refs). Delete and apply a correct one.
info "Recreating rook-ceph-block StorageClass with CSI secret parameters ..."
kubectl delete storageclass rook-ceph-block --ignore-not-found
kubectl apply -f - <<'STORAGECLASS'
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: rook-ceph-block
provisioner: rook-ceph.rbd.csi.ceph.com
parameters:
  clusterID: rook-ceph
  pool: replicapool
  imageFormat: "2"
  imageFeatures: layering
  csi.storage.k8s.io/provisioner-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/provisioner-secret-namespace: rook-ceph
  csi.storage.k8s.io/controller-expand-secret-name: rook-csi-rbd-provisioner
  csi.storage.k8s.io/controller-expand-secret-namespace: rook-ceph
  csi.storage.k8s.io/node-stage-secret-name: rook-csi-rbd-node
  csi.storage.k8s.io/node-stage-secret-namespace: rook-ceph
reclaimPolicy: Delete
allowVolumeExpansion: true
volumeBindingMode: Immediate
STORAGECLASS
ok "rook-ceph-block StorageClass ready."

# ---------------------------------------------------------------------------
# Step 4: deploy Vault dev server
# ---------------------------------------------------------------------------

if [[ "${SKIP_VAULT}" == "false" ]]; then
  if kubectl get pod/vault -n default &>/dev/null 2>&1; then
    info "Vault pod already exists — skipping Vault deploy (use --skip-vault to suppress this check)."
  else
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
# Step 5: build and import the LUKS CSI image (skip if requested)
# ---------------------------------------------------------------------------

if [[ "${SKIP_BUILD}" == "false" ]]; then
  info "Building luks-csi:dev inside Docker Lima VM ..."
  limactl shell "${DOCKER_VM}" -- docker build \
    -t luks-csi:dev \
    "${HOME}/Documents/k8s-block-storage-luks-csi-config/"

  info "Importing image into k3s containerd ..."
  limactl shell "${DOCKER_VM}" -- docker save luks-csi:dev \
    | limactl shell "${K3S_VM}" -- sudo k3s ctr images import -
  ok "Image imported."
else
  info "Skipping image build (--skip-build)."
fi

# ---------------------------------------------------------------------------
# Step 6: deploy the LUKS CSI driver
# ---------------------------------------------------------------------------

info "Removing any leftover LUKS CSI resources ..."
kubectl delete storageclass/luks-encrypted --ignore-not-found
kubectl delete deployment  -n kube-system -l app.kubernetes.io/name=luks-csi-driver --ignore-not-found
kubectl delete daemonset   -n kube-system -l app.kubernetes.io/name=luks-csi-driver --ignore-not-found

info "Deploying LUKS CSI driver (Helm) ..."
helm upgrade --install luks-csi-driver "${REPO_ROOT}/luks-csi-driver/" \
  --namespace kube-system \
  --set storageClass.backingStorageClass=rook-ceph-block \
  --set apparmor.enabled=false \
  --set apparmor.annotate=false
ok "LUKS CSI driver deployed."

kubectl rollout status deployment/luks-csi-driver-controller -n kube-system --timeout=5m
kubectl rollout status daemonset/luks-csi-driver-node        -n kube-system --timeout=5m

# ---------------------------------------------------------------------------
# Step 7: apply test resources
# ---------------------------------------------------------------------------

info "Applying Ceph test resources ..."
kubectl apply -f "${SCRIPT_DIR}/ceph-test-resources.yaml"

info "Waiting for PVC to bind ..."
kubectl wait pvc/ceph-test-pvc \
  --for=jsonpath='{.status.phase}'=Bound --timeout=120s
ok "PVC bound."

wait_for_pod luks-ceph-test-pod default 180

# ---------------------------------------------------------------------------
# Step 8: verify
# ---------------------------------------------------------------------------

info "Verifying file written by test pod ..."
FILE_CONTENT=$(kubectl exec luks-ceph-test-pod -- cat /mnt/data/test.txt)
if [[ "${FILE_CONTENT}" == "hello from luks+ceph" ]]; then
  ok "File contents correct: '${FILE_CONTENT}'"
else
  die "Unexpected file contents: '${FILE_CONTENT}'"
fi

info "Filesystem on encrypted mount:"
kubectl exec luks-ceph-test-pod -- df -h /mnt/data

info "Active LUKS mapper on node:"
MAPPER=$(limactl shell "${K3S_VM}" -- ls /dev/mapper/ 2>/dev/null | grep '^luks-' | head -1 || true)
if [[ -n "${MAPPER}" ]]; then
  limactl shell "${K3S_VM}" -- sudo cryptsetup status "${MAPPER}"
else
  echo "  (no luks- mapper found — NodeStageVolume may not have run yet)"
fi

echo ""
ok "Ceph LUKS test passed."
echo ""
echo "To tear down:  bash testing/test-ceph.sh --teardown"
