# Security Review: Health / Sensitive Data Considerations

This document records known security considerations for the LUKS CSI driver,
particularly in the context of research workloads handling sensitive data
(health records, PII, HIPAA/GDPR-regulated data).

Findings are grouped by severity. Items marked **Critical** or **High** should
be addressed before the driver is used with real health data.

> **Note on key management model:** This document was originally written when LUKS keys
> were stored as Kubernetes Secrets. The current implementation stores keys in
> HashiCorp Vault (`vault.py`). Findings **C1** (Secrets not encrypted at rest) and
> **C2** (cluster-wide Secret ClusterRole) describe risks that are mitigated by the
> Vault backend — LUKS keys are no longer stored in etcd, and the node ClusterRole no
> longer requires `secrets/get`. The remaining findings (C3 onwards) are
> architecture-independent and remain relevant regardless of the key backend.

---

## Critical

### C1 — Kubernetes Secrets are not encrypted at rest by default

`luksKey` is stored as a Kubernetes Secret. Without etcd encryption-at-rest
configured on the API server, the passphrase lives effectively plaintext in
etcd. For HIPAA/GDPR workloads, key material should come from an external KMS
(HashiCorp Vault, AWS KMS, GCP KMS). The CSI external secrets mechanism
supports this — but the driver currently has no KMS adapter.

**Affected files:** [`manifests/storageclass.yaml`](manifests/storageclass.yaml)

**Mitigation:** Configure etcd encryption-at-rest as a deployment prerequisite,
or integrate a Vault Agent sidecar that injects the key at mount time rather
than storing it as a native Secret.

---

### C2 — Node Secret ClusterRole is cluster-wide

[`manifests/rbac.yaml`](manifests/rbac.yaml) grants `get` on **all Secrets in
all namespaces** to the node DaemonSet ServiceAccount. On a shared research
cluster, the node plugin on any node can read any project's LUKS key.

**Mitigation options:**
- Use per-namespace `Role` + `RoleBinding` instead of `ClusterRole`
- Route key retrieval through a namespace-scoped Vault Agent sidecar
- Use Vault with per-role policies so each volume's key is only accessible to
  the node currently mounting it

---

### C3 — No authenticated encryption (dm-integrity not enabled)

[`luks.py`](luks.py) formats with LUKS2 AES-XTS, which provides
**confidentiality but not integrity**. An attacker with access to the raw block
device (cloud console snapshot, storage admin, physical media) can flip
individual sectors without detection.

LUKS2 supports `--integrity` (backed by dm-integrity + AEAD) which detects
any sector-level tampering. Relevant to HIPAA §164.312(c) (integrity controls).

**Cost:** ~10–20% storage overhead; small read performance impact.

---

## High

### H1 — Raw exception messages returned to gRPC callers

[`node.py`](node.py) passes `str(e)` directly to `context.set_details()`.
This sends cryptsetup stderr, device paths, and filesystem state back to the
RPC caller, leaking internal topology information.

**Fix:** Log the full exception internally; return a generic "volume staging
failed" message externally.

---

### H2 — `readonly` flag silently ignored in NodePublishVolume ✓ Fixed

~~[`node.py`](node.py) `NodePublishVolume` ignores `request.readonly`. A pod
requesting read-only access to a health dataset is silently mounted
read-write.~~

**Resolved:** `NodePublishVolume` now reads `request.readonly`. When true, the
bind mount is followed immediately by a `mount -o remount,ro,bind` call on the
same target path (a separate remount is required because Linux ignores the `ro`
flag on the initial bind). See [`node.py`](node.py) `NodePublishVolume`.

---

### H3 — ValidateVolumeCapabilities accepts ReadWriteMany ✓ Fixed

~~[`controller.py`](controller.py) `ValidateVolumeCapabilities` confirms any
requested capability, including `ReadWriteMany`. Two pods writing simultaneously
to the same LUKS block device would cause silent filesystem corruption and data
loss.~~

**Resolved:** `ValidateVolumeCapabilities` now rejects any access mode other
than `SINGLE_NODE_WRITER` (`ReadWriteOnce`), returning an error message rather
than a `Confirmed` response. See [`controller.py`](controller.py)
`ValidateVolumeCapabilities`.

---

### H4 — Key material not zeroed from memory after use

After `luks_key_str.encode()` in [`node.py`](node.py), the passphrase exists as
a Python `bytes` object. Python's GC frees but does not zero memory. Under
memory pressure the page can be swapped to disk and later recovered.

**Mitigations:**
- Disable or encrypt swap on nodes running health data workloads
- Use a `ctypes`-based `memset` on the key buffer immediately after the
  `cryptsetup` subprocess call returns

---

### H5 — No admission control on StorageClass usage

Nothing prevents any namespace or user from creating a PVC with
`storageClassName: luks-encrypted`. On a multi-tenant research cluster this
means any project can provision encrypted volumes, and the LUKS key Secret
mechanism offers no isolation between projects.

**Fix:** Deploy an OPA/Gatekeeper or Kyverno policy that restricts the
`luks-encrypted` StorageClass to authorised namespaces only.

---

## Medium

### M1 — No audit trail

No Kubernetes Events or structured log entries are emitted at volume lifecycle
transitions (create, delete, mount, unmount). HIPAA requires access records for
systems holding PHI.

**Recommended actions:**
- Emit a `kubernetes.io/event` at each `NodeStageVolume`, `NodeUnstageVolume`,
  `CreateVolume`, and `DeleteVolume` call
- Log in structured JSON format (parseable by SIEM / log aggregation tooling)
- **Deployment prerequisite:** Enable API server audit logging so that Secret
  reads by the node plugin are captured in the cluster audit log

---

### M2 — LUKS key derivation parameters not enforced

[`luks.py`](luks.py) passes no `--pbkdf`, `--pbkdf-time`, or `--hash` flags to
`cryptsetup luksFormat`. LUKS2 defaults to Argon2id (strong), but `luksType:
luks1` silently falls back to PBKDF2-SHA1 (weak). A misconfigured StorageClass
silently uses weak key derivation.

**Fix:** Either reject `luks1` outright, or always pass `--pbkdf argon2id`
regardless of `luksType`.

---

### M3 — DeleteVolume does not securely wipe

[`controller.py`](controller.py) deletes the backing PVC, returning the block
device to the storage pool with ciphertext intact. Deleting the key Secret
renders the data cryptographically inaccessible (acceptable if key entropy was
strong), but some regulators require explicit erasure evidence (NIST SP 800-88).

**Recommended actions:**
- Emit a clear log/Event on deletion stating: "data rendered cryptographically
  inaccessible; LUKS key Secret deleted; raw ciphertext remains on storage
  backend"
- Optionally run `cryptsetup erase <device>` to overwrite the LUKS header
  before deleting the PVC, providing a stronger erasure guarantee

---

### M4 — Full `/dev` mounted into the privileged DaemonSet

[`manifests/node.yaml`](manifests/node.yaml) mounts the entire host `/dev`
directory. Combined with `privileged: true`, a compromised node plugin process
can read any other block device on the node, including volumes belonging to
other tenants.

**Mitigation:** Use device-specific bind mounts (mount only the specific device
path for the volume being operated on) or cgroup device allowlists to limit
access to only the required block device.

---

### M5 — No seccomp profile

[`manifests/node.yaml`](manifests/node.yaml) specifies no `seccompProfile`.
While `privileged: true` already bypasses Linux capability restrictions, a
seccomp profile at minimum `RuntimeDefault` limits the available syscall surface
and is required by many CIS benchmarks used in health data infrastructure.

---

### M6 — `_backing_pvc_name` truncation can cause name collisions

[`controller.py`](controller.py) truncates backing PVC names at 63 characters.
The `luks-backing-` prefix occupies 13 characters, leaving only 50 characters
from the volume name. Two long volume names that differ only after character 50
map to the **same** backing PVC name — the second `CreateVolume` call silently
reuses the first volume's storage, a data isolation failure.

**Fix:** Append a short hash (e.g. first 8 hex chars of SHA-256 of the full
volume name) before truncation to make names collision-resistant.

---

## Low / Informational

### L1 — `read_secret_key` in k8s.py is dead code

[`k8s.py`](k8s.py) contains a `read_secret_key()` function that is never
called by the driver. Keys arrive via the CSI secrets mechanism (kubelet reads
the Secret and passes its contents to `NodeStageVolumeRequest.secrets`). The
unused function could mislead future developers into thinking the driver reads
Secrets directly. Should be removed or annotated clearly.

---

### L2 — No volume expansion or snapshot support

No `ControllerExpandVolume`, `NodeExpandVolume`, or snapshot RPCs are
implemented. Research datasets grow over time; without expansion support users
must over-provision or manually migrate data. This is not a security gap but a
significant operational limitation for health data workflows.

---

### L3 — Backing PVC namespace defaults to `kube-system` on fallback

[`controller.py`](controller.py): if `csi.storage.k8s.io/pvc-namespace` is not
injected by the external-provisioner sidecar, backing PVCs are created in
`kube-system`. On a multi-tenant cluster this mixes data resources from
different research projects inside a privileged namespace.

---

## Deployment Prerequisites for Health Data

The following cluster-level controls should be in place before using this driver
with health/PHI data:

- [ ] etcd encryption-at-rest enabled on the API server (or Vault KMS integration)
- [ ] Kubernetes API audit logging enabled and log forwarding to SIEM configured
- [ ] Swap disabled or encrypted on all nodes running health data workloads
- [ ] Admission policy (Gatekeeper/Kyverno) restricting `luks-encrypted` StorageClass to authorised namespaces
- [ ] Network policies preventing cross-namespace pod-to-pod traffic on data nodes
- [ ] Node isolation: data nodes tainted/labelled so only authorised workloads are scheduled
