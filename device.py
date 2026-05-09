"""
Block device resolution and attachment for the LUKS CSI node plugin.

Architecture
------------
Two concerns are handled here:

1. Staging  (ensure_staged / release_staged)
   For CSI-backed PVs, the backing driver's NodeStageVolume (which maps the
   block device onto the node, e.g. rbd map for Ceph RBD) is only called by
   kubelet when a pod that directly consumes the backing PVC is scheduled.
   Our backing PVC has no such pod, so we create a short-lived "staging pod"
   that mounts the backing PVC as a raw block device.  This triggers the full
   kubelet CSI lifecycle — ControllerPublishVolume + NodeStageVolume — which
   makes the block device appear on the node.  The pod is kept alive until
   NodeUnstageVolume calls release_staged, at which point the device is no
   longer needed.

   For local/hostPath PVs ensure_staged is a no-op (device is always present).

2. Device path resolution  (resolve_device_path / wait_for_device)
   Different storage backends expose the block device at different paths.
   Resolvers are tried in order; the first non-None result wins.
   ByIdResolver is the generic fallback for cloud-provider block storage and
   runs after staging is complete so the device is guaranteed to be present.

Extending for a new storage provider
-------------------------------------
Implement a Resolver subclass and insert it into RESOLVERS before ByIdResolver:

    class MyDriverResolver(Resolver):
        def resolve(self, pv) -> str | None:
            csi = _csi_spec(pv)
            if not csi or "mydriver" not in (csi.driver or ""):
                return None
            return f"/dev/mydriver/{csi.volume_handle}"

    # insert before the generic ByIdResolver fallback
    RESOLVERS.insert(RESOLVERS.index(_by_id_resolver), MyDriverResolver())
"""

import hashlib
import logging
import os
import time
from abc import ABC, abstractmethod

from kubernetes import client

import k8s

LOG = logging.getLogger(__name__)

POLL_INTERVAL   = 3    # seconds between existence / status checks
DEVICE_TIMEOUT  = 120  # seconds to wait for device node to appear
STAGING_TIMEOUT = 300  # seconds to wait for the staging pod to be Running

# Pause image used for the staging pod — no shell, minimal attack surface.
_PAUSE_IMAGE = "registry.k8s.io/pause:3.9"


# ---------------------------------------------------------------------------
# Resolver base and registry
# ---------------------------------------------------------------------------

class Resolver(ABC):
    """Return the expected on-node device path for a PV, or None if not applicable."""

    @abstractmethod
    def resolve(self, pv) -> str | None: ...


class LocalResolver(Resolver):
    """local and hostPath PVs — path is encoded directly in the spec."""

    def resolve(self, pv) -> str | None:
        spec = pv.spec
        local = getattr(spec, "local", None)
        if local and getattr(local, "path", None):
            return local.path
        host_path = getattr(spec, "host_path", None)
        if host_path and getattr(host_path, "path", None):
            return host_path.path
        return None


class LonghornResolver(Resolver):
    """Longhorn (driver.longhorn.io): /dev/longhorn/<volumeHandle>."""

    def resolve(self, pv) -> str | None:
        csi = _csi_spec(pv)
        if not csi or "longhorn" not in (csi.driver or ""):
            return None
        handle = csi.volume_handle or ""
        return f"/dev/longhorn/{handle}" if handle else None


class CephRBDResolver(Resolver):
    """
    Ceph RBD (rbd.csi.ceph.com / rook-ceph.rbd.csi.ceph.com).

    The kernel RBD module creates /dev/rbdX and exposes pool/image in sysfs
    at /sys/bus/rbd/devices/<id>/.  The /dev/rbd/<pool>/<image> symlinks that
    some docs reference are created by ceph-common udev rules, which are NOT
    present on a k3s node that only has the rbd kernel module loaded.  We scan
    sysfs instead, which works without any ceph userspace packages installed.
    """

    def resolve(self, pv) -> str | None:
        csi = _csi_spec(pv)
        if not csi or "rbd" not in (csi.driver or ""):
            return None
        attrs = csi.volume_attributes or {}
        pool  = attrs.get("pool", "")
        image = attrs.get("imageName") or attrs.get("image", "")
        if not pool or not image:
            return None
        return _scan_rbd_sysfs(pool, image)


class ByIdResolver(Resolver):
    """
    Generic fallback for cloud-provider block storage (Cinder, EBS, GCE PD,
    Azure Disk, ...).  Scans /dev/disk/by-id/ for a symlink whose name
    contains the volume handle and resolves it to the real device node.

    Some drivers truncate the handle in the by-id name (e.g. Cinder's
    virtio-* links use only the first 20 hex chars), so we match on substring
    containment with hyphens stripped to handle both full and truncated forms.

    This resolver must run *after* ensure_attached so the device is present.
    """

    def resolve(self, pv) -> str | None:
        csi = _csi_spec(pv)
        if not csi:
            return None
        handle = csi.volume_handle or ""
        return _scan_by_id(handle) if handle else None


# Resolvers are tried in order; driver-specific resolvers come first so they
# take precedence over the generic ByIdResolver fallback.  Keep a reference to
# the ByIdResolver instance so callers can use RESOLVERS.index(_by_id_resolver)
# as an insertion point when registering new resolvers (see module docstring).
_by_id_resolver = ByIdResolver()

RESOLVERS: list[Resolver] = [
    LocalResolver(),
    LonghornResolver(),
    CephRBDResolver(),
    _by_id_resolver,
]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _csi_spec(pv):
    return getattr(pv.spec, "csi", None)


def _scan_rbd_sysfs(pool: str, image: str) -> str | None:
    """
    Find /dev/rbdX by scanning /sys/bus/rbd/devices/ for a matching pool+image.

    The kernel RBD module populates one subdirectory per mapped image, named by
    device index (0, 1, 2, ...).  Each directory has 'pool' and 'name' files.
    The block device is /dev/rbd<index>.
    """
    sysfs = "/sys/bus/rbd/devices"
    try:
        indices = os.listdir(sysfs)
    except OSError:
        return None
    for idx in indices:
        base = os.path.join(sysfs, idx)
        try:
            dev_pool  = open(os.path.join(base, "pool")).read().strip()
            dev_image = open(os.path.join(base, "name")).read().strip()
        except OSError:
            continue
        if dev_pool == pool and dev_image == image:
            return f"/dev/rbd{idx}"
    return None


def _scan_by_id(handle: str) -> str | None:
    """Return realpath of the first /dev/disk/by-id/ entry matching handle."""
    by_id = "/dev/disk/by-id"
    try:
        entries = os.listdir(by_id)
    except OSError:
        return None
    # Normalise: strip hyphens on both sides so "abc-def" matches "abcdef" and
    # vice-versa, covering drivers that truncate or reformat the volume handle.
    needle = handle.lower().replace("-", "")
    for entry in entries:
        if needle in entry.lower().replace("-", ""):
            try:
                return os.path.realpath(os.path.join(by_id, entry))
            except OSError:
                continue
    return None


def _staging_pod_name(volume_id: str) -> str:
    """Deterministic, DNS-safe pod name derived from volume_id."""
    raw = f"luks-stage-{volume_id.replace('/', '-')}"
    if len(raw) <= 63:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"luks-stage-{digest}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_device_path(pv) -> str | None:
    """Try each resolver in RESOLVERS order; return the first non-None result."""
    for resolver in RESOLVERS:
        path = resolver.resolve(pv)
        if path:
            return path
    return None


def wait_for_device(path: str, timeout: int = DEVICE_TIMEOUT) -> None:
    """Block until path exists on the node filesystem, or raise TimeoutError."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if os.path.exists(path):
            return
        LOG.info(
            "Waiting for device %s (%.0fs remaining)...",
            path, deadline - time.monotonic(),
        )
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"Device {path} did not appear within {timeout}s")


def ensure_staged(
    volume_id: str,
    pv,
    backing_pvc_name: str,
    backing_pvc_namespace: str,
    node_name: str,
    timeout: int = STAGING_TIMEOUT,
) -> None:
    """
    Ensure the backing block device is present on node_name.

    For CSI-backed PVs this creates a pause pod that mounts the backing PVC as
    a raw block device, pinned to node_name.  Kubelet's CSI machinery then calls
    the backing driver's NodeStageVolume (e.g. rbd map for Ceph RBD), which makes
    the block device appear on the node.  The pod remains until release_staged is
    called from NodeUnstageVolume.

    No-op for non-CSI PVs (local, hostPath) where the device is always present.
    """
    if not _csi_spec(pv):
        return

    # The staging pod must be in the same namespace as the backing PVC —
    # Kubernetes does not allow cross-namespace PVC references in pod specs.
    ns = backing_pvc_namespace
    pod_name = _staging_pod_name(volume_id)
    core = k8s.core()

    try:
        core.read_namespaced_pod(pod_name, ns)
        LOG.info("Staging pod %s/%s already exists", ns, pod_name)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=pod_name,
                namespace=ns,
                labels={"app.kubernetes.io/managed-by": "luks-csi-driver"},
            ),
            spec=client.V1PodSpec(
                node_name=node_name,
                restart_policy="Never",
                automount_service_account_token=False,
                containers=[
                    client.V1Container(
                        name="pause",
                        image=_PAUSE_IMAGE,
                        volume_devices=[
                            client.V1VolumeDevice(
                                name="block-vol",
                                device_path="/dev/xvda",
                            )
                        ],
                    )
                ],
                volumes=[
                    client.V1Volume(
                        name="block-vol",
                        persistent_volume_claim=client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=backing_pvc_name,
                            read_only=False,
                        ),
                    )
                ],
            ),
        )
        core.create_namespaced_pod(ns, pod)
        LOG.info(
            "Created staging pod %s/%s (node=%s pvc=%s)",
            ns, pod_name, node_name, backing_pvc_name,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pod = core.read_namespaced_pod(pod_name, ns)
        phase = pod.status.phase if pod.status else None
        if phase == "Running":
            LOG.info("Staging pod %s/%s is Running", ns, pod_name)
            return
        if phase in ("Failed", "Succeeded"):
            raise RuntimeError(
                f"Staging pod {ns}/{pod_name} entered phase {phase} unexpectedly"
            )
        LOG.info(
            "Waiting for staging pod %s/%s (phase=%s, %.0fs remaining)...",
            ns, pod_name, phase, deadline - time.monotonic(),
        )
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(
        f"Staging pod {ns}/{pod_name} did not reach Running within {timeout}s"
    )


def release_staged(volume_id: str) -> None:
    """
    Delete the staging pod created by ensure_staged, allowing kubelet to call
    the backing driver's NodeUnstageVolume and release the block device.

    Safe to call when no staging pod exists (e.g. local/hostPath volumes where
    ensure_staged was a no-op).

    The namespace is derived from volume_id, which this driver encodes as
    "<namespace>/<pvc-name>" (set by controller.py).
    """
    # volume_id format: "namespace/pvc-name"
    ns = volume_id.split("/")[0] if "/" in volume_id else "default"
    pod_name = _staging_pod_name(volume_id)
    try:
        k8s.core().delete_namespaced_pod(
            pod_name,
            ns,
            body=client.V1DeleteOptions(grace_period_seconds=0),
        )
        LOG.info("Deleted staging pod %s/%s", ns, pod_name)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        LOG.debug("Staging pod %s/%s already gone", ns, pod_name)


def attach_and_resolve(
    volume_id: str,
    pv_name: str,
    backing_pvc_name: str,
    backing_pvc_namespace: str,
    node_name: str,
    staging_timeout: int = STAGING_TIMEOUT,
    device_timeout: int = DEVICE_TIMEOUT,
) -> str:
    """
    High-level helper used by NodeStageVolume.

    1. Fetch the backing PV.
    2. Create a staging pod so kubelet triggers the backing driver's
       NodeStageVolume (e.g. rbd map), making the block device appear.
    3. Resolve the device path via the RESOLVERS registry.
    4. Wait for the device file to appear on the node.

    Returns the block device path ready for cryptsetup.
    """
    pv = k8s.core().read_persistent_volume(pv_name)
    ensure_staged(volume_id, pv, backing_pvc_name, backing_pvc_namespace, node_name, timeout=staging_timeout)
    path = resolve_device_path(pv)
    if not path:
        raise ValueError(f"Cannot determine block device path from PV {pv_name!r}")
    wait_for_device(path, timeout=device_timeout)
    return path
