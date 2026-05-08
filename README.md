# LUKS CSI Driver

A Python implementation of a Container Storage Interface (CSI) driver that provides
transparent LUKS disk encryption as a Kubernetes StorageClass. Users create a PVC
with `storageClassName: luks-encrypted` and get an encrypted filesystem — no custom
resources, no helper pods, no per-workload operator configuration.

---

## How it works

```
User PVC (storageClassName: luks-encrypted)
        │
        ▼
  external-provisioner sidecar
        │  calls CreateVolume
        ▼
  Controller plugin (Deployment)
  ├── creates a backing PVC against the configured backingStorageClass
  ├── waits for it to Bind → records backingPvName in VolumeContext
  └── auto-generates LUKS key in Vault (idempotent)
        │
        │  kubelet calls NodeStageVolume on the node where the pod is scheduled
        ▼
  Node plugin (DaemonSet, privileged)
  ├── NodeStageVolume:
  │     creates a VolumeAttachment → triggers backing CSI driver to attach
  │     the raw block device to this node (Cinder, Ceph, Longhorn, EBS, etc.)
  │     resolves the device path via the RESOLVERS registry:
  │       LocalResolver      → /dev/<path> (local / hostPath)
  │       LonghornResolver   → /dev/longhorn/<volumeHandle>
  │       CephRBDResolver    → /dev/rbd/<pool>/<image>
  │       ByIdResolver       → scan /dev/disk/by-id/ (Cinder, EBS, GCE, …)
  │     fetches LUKS key from Vault
  │     cryptsetup isLuks  → luksFormat (first use only)
  │     cryptsetup luksOpen → /dev/mapper/luks-<id>
  │     mkfs.<fs>           (first use only)
  │     mount               → global staging path
  ├── NodePublishVolume:
  │     bind-mount staging path → pod-specific path
  ├── NodeUnpublishVolume: umount pod path
  └── NodeUnstageVolume:  cryptsetup luksClose
                          delete VolumeAttachment → backing driver detaches
```

When a PVC is created, the Controller plugin auto-generates a cryptographically secure
LUKS key in HashiCorp Vault at `secret/tenants/{institution}/luks-keys/{volume-name}`
(idempotent — no-op if the key already exists). At mount time, the Node plugin fetches
the key directly from Vault using the pod's Kubernetes service account JWT. Users do not
create or manage key material. The only prerequisite is a running Vault instance with the
Kubernetes auth method configured — see [Vault prerequisites](#vault-prerequisites) below.

For architecture and sequence diagrams see [architecture.md](architecture.md).

---

## Project structure

```
csi-driver/
├── main.py               # gRPC server entry point; CSI_MODE=controller|node|all
├── driver.py             # Identity service (GetPluginInfo, Probe)
├── controller.py         # Controller service (CreateVolume / DeleteVolume)
├── node.py               # Node service (NodeStageVolume, NodePublishVolume, ...)
├── device.py             # Device resolution and attachment:
│                         #   extensible RESOLVERS registry (Longhorn, Ceph, ByIdResolver, …)
│                         #   attach_and_resolve() — VolumeAttachment lifecycle + device wait
├── luks.py               # cryptsetup and mkfs subprocess wrappers
├── vault.py              # HashiCorp Vault KV v2 client (ensure/read/delete key, rotation)
├── k8s.py                # Kubernetes API helpers (PVC/PV/Event/VolumeAttachment)
├── requirements.txt
├── Dockerfile
├── generate_proto.sh     # Generates Python gRPC stubs from csi.proto
├── architecture.md       # Architecture diagram (Mermaid)
├── SECURITY.md           # Security review and known issues for sensitive data workloads
├── proto/
│   └── csi.proto         # CSI spec v1 (from container-storage-interface/spec)
├── generated/            # Auto-generated — run generate_proto.sh to create
│   ├── csi_pb2.py
│   └── csi_pb2_grpc.py
└── manifests/
    ├── csidriver.yaml     # CSIDriver registration
    ├── storageclass.yaml  # Example StorageClass (luks-encrypted)
    ├── controller.yaml    # Deployment: luks-csi (controller) + external-provisioner
    ├── node.yaml          # DaemonSet: luks-csi (node) + node-driver-registrar
    ├── rbac.yaml          # ServiceAccounts, ClusterRoles, ClusterRoleBindings
    └── test-resources.yaml  # Loop-device test: SC, PV, Secret, PVC, Pod
```

---

## Prerequisites

**Tools required on your workstation:**

- Python 3.13+
- `grpcio-tools` (to generate gRPC stubs from `csi.proto`)
- Docker (to build the container image)
- `kubectl` 1.28+

**Cluster requirements:**

- Kubernetes 1.28+ (k3s, RKE2, GKE, EKS, AKS, etc.)
- A block-mode StorageClass — see [Supported backing storage](#supported-backing-storage) below
- CSI sidecar images accessible from the cluster
  (`registry.k8s.io/sig-storage/csi-provisioner:v5.1.0` and
  `registry.k8s.io/sig-storage/csi-node-driver-registrar:v2.12.0`)

**HashiCorp Vault (required):**

- Vault 1.12+ with the KV v2 secrets engine enabled at the `secret/` mount
- Kubernetes auth method enabled and configured against your cluster
- A Vault role bound to the `luks-csi-controller` and `luks-csi-node` service accounts
  in the `kube-system` namespace (role name configurable via `luks-csi-driver/values.yaml`)
- A Vault policy granting `create/read/update/delete/list` on `secret/data/tenants/*`
  and `secret/metadata/tenants/*`

See [Vault prerequisites](#vault-prerequisites) for the exact setup commands.

**For local development with Lima + k3s (macOS):**

- [Lima](https://lima-vm.io/) 2.0+ — `brew install lima`
- A running k3s Lima VM (see [Local development with Lima](#local-development-with-lima) below)

---

## Supported backing storage

The driver works with any block-mode StorageClass. At `NodeStageVolume` time it
creates a `VolumeAttachment` to trigger the backing CSI driver's attach flow, then
resolves the on-node device path via an ordered **RESOLVERS** registry:

| Resolver | Backing driver | Device path |
|---|---|---|
| `LocalResolver` | local / hostPath static PVs | path from PV spec |
| `LonghornResolver` | `driver.longhorn.io` | `/dev/longhorn/<volumeHandle>` |
| `CephRBDResolver` | `rbd.csi.ceph.com` / rook-ceph | `/dev/rbd/<pool>/<imageName>` |
| `ByIdResolver` *(fallback)* | Cinder, EBS, GCE PD, Azure Disk, … | scan `/dev/disk/by-id/` for handle |

`local` and `hostPath` PVs skip the VolumeAttachment step entirely (the device is always
present). For all CSI-backed storage, the driver holds the `VolumeAttachment` open for the
lifetime of the mount and deletes it during `NodeUnstageVolume` so the backing driver
cleanly detaches the block device.

**Adding a new provider** — subclass `Resolver` in `device.py` and insert it into
`RESOLVERS` before `ByIdResolver`. No other changes are needed.

---

## Vault prerequisites

The CSI driver uses HashiCorp Vault as the LUKS key backend. The controller
auto-generates keys at provisioning time and the node fetches them at mount time — both
authenticate via the pod's Kubernetes service account JWT.

### 1. Install and unseal Vault

Follow the Vault HA setup steps in the [operator repo README](https://github.com/nfcs-secure-k8s/k8s-block-storage-luks-operator-config#installation--setup)
(helm install, operator init, unseal, raft join). Those steps are shared between the
operator and CSI driver.

### 2. Enable KV v2

```bash
kubectl exec vault-0 -- vault secrets enable -path=secret kv-v2
```

Skip this if KV v2 is already enabled (you'll get an error if it's already mounted).

### 3. Create the CSI driver Vault role

The CSI driver uses **two** service accounts (`luks-csi-controller` and `luks-csi-node`
in the `kube-system` namespace), both of which must be included in the role. This is
different from the kopf operator, which uses `encrypted-volume-operator` in `default`.

```bash
kubectl exec vault-0 -- vault write auth/kubernetes/role/luks-operator-role \
    bound_service_account_names="luks-csi-controller,luks-csi-node" \
    bound_service_account_namespaces="kube-system" \
    policies="luks-policy" \
    ttl="24h"
```

The role name (`luks-operator-role`) matches the default in `luks-csi-driver/values.yaml`.
Change both if you use a different name.

### 4. Create the Vault policy

The policy path is identical to the kopf operator — LUKS keys are stored at
`secret/tenants/{institution}/luks-keys/{volume-name}` in both implementations.

```bash
kubectl exec -i vault-0 -- vault policy write luks-policy - <<EOF
path "secret/data/tenants/*" {
  capabilities = ["create", "read", "update", "delete", "list"]
}

path "secret/metadata/tenants/*" {
  capabilities = ["read", "list", "delete"]
}

path "secret/metadata/tenants" {
  capabilities = ["list"]
}

path "secret/metadata" {
  capabilities = ["list"]
}

path "sys/internal/ui/mounts/secret" {
  capabilities = ["read"]
}
EOF
```

### 5. Verify after deployment

Once the CSI driver is deployed and a PVC is created, confirm the key was provisioned:

```bash
kubectl describe pvc <your-pvc-name>
# Look for an event like:
# Normal  LuksKeyProvisioned  ...  LUKS key auto-generated in Vault at
#   "secret/tenants/default/luks-keys/<volume-id>" (v1)
```

---

## Generate gRPC stubs

The `generated/` directory is not included in the repository. Run this once after
cloning (requires `grpcio-tools`):

```bash
pip install grpcio-tools
cd csi-driver
bash generate_proto.sh
```

This writes `generated/csi_pb2.py` and `generated/csi_pb2_grpc.py` and is also
run automatically inside the Docker build.

---

## Quick start (any cluster)

### 1. Build the container image

```bash
docker build -t luks-csi:dev ./csi-driver/
```

Push to a registry accessible from your cluster, or load it directly into your
cluster's container runtime (see [Local development with Lima](#local-development-with-lima)
for the k3s import workflow).

```bash
docker tag luks-csi:dev <your-registry>/luks-csi:dev
docker push <your-registry>/luks-csi:dev
```

### 2. Set up a backing StorageClass

Identify a StorageClass in your cluster that provisions raw block volumes (e.g.
`csi-cinder-sc-retain`, `rbd-sc`, `ebs-sc`). You'll pass this as
`storageClass.backingStorageClass` in the Helm install below.

### 3. Deploy the CSI driver

**Via Helm (recommended):**

```bash
helm install luks-csi-driver ./luks-csi-driver/ \
  --namespace kube-system \
  --set vault.address="http://vault.default.svc.cluster.local:8200" \
  --set vault.role="luks-operator-role" \
  --set storageClass.backingStorageClass="<your-block-storageclass>" \
  --set storageClass.institution="<your-institution>"
```

Key values to customise (all in `luks-csi-driver/values.yaml`):

| Value | Default | Description |
|---|---|---|
| `vault.address` | `http://vault.default.svc.cluster.local:8200` | Vault API URL reachable from the cluster |
| `vault.role` | `luks-operator-role` | Vault Kubernetes auth role (must match Vault prerequisites) |
| `storageClass.backingStorageClass` | `local-path` | Underlying raw block StorageClass |
| `storageClass.institution` | `default` | Namespaces LUKS keys in Vault per tenant |
| `storageClass.deletionPolicy` | `Delete` | `Delete` destroys the Vault key on PVC deletion; `Retain` keeps it |
| `image.repository` / `image.tag` | `luks-csi:dev` | Your built image |

**Via raw manifests (alternative):**

Edit `manifests/storageclass.yaml` and set `backingStorageClass`:

```yaml
parameters:
  backingStorageClass: csi-cinder-sc-retain   # or rbd-sc, ebs-sc, etc.
  luksType: luks2
  filesystem: ext4
```

Then deploy:

```bash
kubectl apply -f csi-driver/manifests/csidriver.yaml \
              -f csi-driver/manifests/rbac.yaml \
              -f csi-driver/manifests/storageclass.yaml \
              -f csi-driver/manifests/controller.yaml \
              -f csi-driver/manifests/node.yaml
```

Wait for both workloads to be ready:

```bash
kubectl rollout status deployment/luks-csi-controller -n kube-system
kubectl rollout status daemonset/luks-csi-node -n kube-system
```

### 4. Provision an encrypted volume

No key Secret is needed — the controller auto-generates the LUKS key in Vault at
provisioning time. Simply create the PVC:

```yaml
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: my-pvc
  namespace: default
spec:
  storageClassName: luks-encrypted
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 10Gi
```

Confirm the Vault key was auto-provisioned:

```bash
kubectl describe pvc my-pvc
# Look for:
# Normal  LuksKeyProvisioned  ...  LUKS key auto-generated in Vault at
#   "secret/tenants/default/luks-keys/<volume-id>" (v1)
```

---

## Local development with Lima

These instructions set up a self-contained test environment on macOS using
[Lima](https://lima-vm.io/) with no external registry.

### One-time setup

**1. Create and start a k3s Lima VM:**

```bash
limactl create --name=k3s template://k3s
limactl start k3s
```

**2. Configure kubectl to point at the Lima k3s cluster:**

```bash
mkdir -p ~/.kube
limactl shell k3s -- sudo cat /etc/rancher/k3s/k3s.yaml > ~/.kube/config
# The kubeconfig uses 127.0.0.1 — update the IP to the VM's address:
K3S_IP=$(limactl shell k3s -- hostname -I | awk '{print $1}')
sed -i '' "s/127.0.0.1/${K3S_IP}/g" ~/.kube/config
kubectl get nodes   # should show k3s node as Ready
```

**3. Create a Lima Docker VM for image builds (no registry needed):**

```bash
limactl create --name=docker template://docker
limactl start docker
```

The Docker Lima VM mounts your home directory read-only inside the VM, so the
project directory is accessible directly at its host path.

### Build and import the image

Run these steps after any code change:

```bash
# Build inside the Docker Lima VM
limactl shell docker -- docker build \
  -t luks-csi:dev \
  "${HOME}/Documents/k8s-luks-operator/csi-driver/"

# Import into k3s (no registry needed — pipes directly into k3s containerd)
limactl shell docker -- docker save luks-csi:dev \
  | limactl shell k3s -- sudo k3s ctr images import -
```

After import, restart the workloads to pick up the new image:

```bash
kubectl rollout restart deployment/luks-csi-controller -n kube-system
kubectl rollout restart daemonset/luks-csi-node -n kube-system
```

### Set up a loop device (test block storage)

Run inside the k3s VM once per VM restart:

```bash
limactl shell k3s -- sudo bash -c "
  dd if=/dev/zero of=/tmp/test-block.img bs=1M count=1200
  losetup /dev/loop0 /tmp/test-block.img
"
```

### Deploy the CSI driver

```bash
kubectl apply -f csi-driver/manifests/csidriver.yaml \
              -f csi-driver/manifests/rbac.yaml \
              -f csi-driver/manifests/storageclass.yaml \
              -f csi-driver/manifests/controller.yaml \
              -f csi-driver/manifests/node.yaml
```

Wait for both workloads to be ready:

```bash
kubectl rollout status deployment/luks-csi-controller -n kube-system
kubectl rollout status daemonset/luks-csi-node -n kube-system
```

### Run the end-to-end test

```bash
kubectl apply -f csi-driver/manifests/test-resources.yaml
```

This creates:
- `loop-backing` StorageClass (static provisioner for the loop device)
- `luks-loop-pv` PV pointing at `/dev/loop0`
- `luks-encrypted-loop` StorageClass (our CSI driver, backed by `loop-backing`)
- `test-pvc-luks-key` Secret with the LUKS passphrase
- `test-pvc` PVC using `luks-encrypted-loop`
- `luks-test-pod` that writes `hello from luks` to `/mnt/data/test.txt`

Watch it converge:

```bash
kubectl get pvc test-pvc -w          # should reach Bound within ~30s
kubectl get pod luks-test-pod -w     # should reach Running
```

Verify the encrypted volume works:

```bash
# File written by the pod
kubectl exec luks-test-pod -- cat /mnt/data/test.txt

# Filesystem mounted on the decrypted device
kubectl exec luks-test-pod -- df -h /mnt/data

# Active LUKS mapper on the node (confirms real encryption)
limactl shell k3s -- sudo cryptsetup status $(
  ls /dev/mapper/ | grep ^luks- | head -1
)
```

Expected output from `cryptsetup status`:
```
type:    LUKS2
cipher:  aes-xts-plain64
keysize: 512 bits
device:  /dev/loop0
mode:    read/write
```

### Tear down

```bash
kubectl delete -f csi-driver/manifests/test-resources.yaml
```

---

## Security considerations

See [`SECURITY.md`](SECURITY.md) for a full review of known issues and mitigations,
including concerns specific to health data and other sensitive workloads.

---

## Comparison to the kopf operator approach

The original implementation lives in [k8s-block-storage-luks-operator-config](https://github.com/nfcs-secure-k8s/k8s-block-storage-luks-operator-config)
and uses the [kopf](https://github.com/nolar/kopf) framework with a custom `EncryptedVolume` CRD.

| Aspect | kopf Operator | CSI Driver |
|---|---|---|
| **User interface** | Custom Resource (`EncryptedVolume`) | Standard PVC (`storageClassName: luks-encrypted`) |
| **Key generation** | Auto-generated in Vault at CR creation (`main.py`) | Auto-generated in Vault at `CreateVolume` (`controller.py` via `vault.py`) |
| **Key storage path** | `secret/tenants/{institution}/luks-keys/{name}` | Same path |
| **Key fetch at mount** | Vault Agent sidecar injects key into pod via annotations | `node.py` calls `vault.py::read_secret()` directly using service account JWT |
| **Key rotation trigger** | 30s timer detects Vault version bump → patches `vaultVersion` annotation | 30s sync thread annotates PV; rotation auto-applied in `NodeStageVolume` |
| **Rotation mechanism** | Privileged rekey Job: `luksAddKey` + `luksRemoveKey` | `_open_with_rotation()` in `node.py`: `luksAddKey` + `luksRemoveKey` |
| **AppArmor deployment** | SSH script (`scripts/k8s-luks-restricted.sh`) run on each worker | Loader DaemonSet (`apparmor-profile.yaml`) — automatic, no SSH required |
| **Device path** | Hard-coded `/dev/vdc` in pod shell script | Resolved at node time via extensible RESOLVERS registry; VolumeAttachment triggers backing driver attach/detach |
| **Encryption setup** | init container inside every workload pod (installs cryptsetup at runtime) | Node plugin runs once per volume on the node; no changes to user pods |
| **Privileged pods** | Every user workload pod needs `privileged: true` | Only the node DaemonSet is privileged; user pods are unprivileged |
| **Lifecycle management** | No delete handler; LUKS device left open on pod exit | `NodeUnstageVolume` calls `cryptsetup luksClose`; clean unmount on pod deletion |
| **Multi-pod attach** | Not handled | CSI capabilities enforce RWO; kubelet manages attach/detach |
| **Backend portability** | Tied to OpenStack Cinder (`/dev/vdc` path assumption) | Works with any block StorageClass; built-in support for Longhorn, Ceph RBD, Cinder, EBS, GCE PD, Azure Disk, and local/hostPath |
| **Kubernetes integration** | Operator must be running for volumes to work | Standard CSI; volumes work independently of the operator process |
| **Observability** | kopf events + custom status fields | Standard PVC/PV events; `kubectl describe pvc` shows provisioning errors |

### Key limitations of the operator approach

1. **Hard-coded device path** — `/dev/vdc` is assumed in the init container shell
   script. Different clouds or StorageClasses assign different device names, causing
   silent failures.

2. **Privileged workload pods** — Every pod that needs an encrypted volume must run
   with `privileged: true`. This widens the blast radius of any container escape and
   is typically blocked by Pod Security Admission in production clusters. The CSI
   driver confines privilege to only the node DaemonSet; user workload pods remain
   unprivileged.

3. **No unmount lifecycle** — There is no `@kopf.on.delete` handler. When the
   `EncryptedVolume` CR or pod is deleted, the LUKS mapper stays open on the node
   until it is manually closed or the node reboots.

4. **Encryption runs inside the user container** — `cryptsetup` is installed via
   `apk` inside the init container on every startup. This is slow, requires internet
   access from the pod, and runs encryption setup as part of the application
   container's lifecycle.

5. **No StorageClass abstraction** — Users must reference specific PVC names and
   storage classes manually; there is no self-service provisioning path.

### When to use each approach

| Scenario | Recommendation |
|---|---|
| Production cluster, standard storage | **CSI driver** — standard interface, no privileged workloads |
| Existing cluster with a block StorageClass | **CSI driver** — drop-in replacement via StorageClass |
| Quick PoC on a known device path | Operator is faster to deploy |
| Cluster where CSI sidecars are unavailable | Operator (no sidecar dependencies) |
