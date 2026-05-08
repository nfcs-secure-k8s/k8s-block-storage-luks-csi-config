"""
Block device resolution and attachment for the LUKS CSI node plugin.

Architecture
------------
Two concerns are handled here:

1. Attachment  (ensure_attached / release_attachment)
   For CSI-backed PVs, kubelet only asks the backing driver to attach a volume
   when a pod that consumes it is scheduled.  Our backing PVC has no such pod,
   so we create a VolumeAttachment object directly.  This triggers the backing
   driver's ControllerPublishVolume and places the block device on the node.
   The attachment is held open until NodeUnstageVolume calls release_attachment.

2. Device path resolution  (resolve_device_path / wait_for_device)
   Different storage backends expose the block device at different paths.
   Resolvers are tried in order; the first non-None result wins.
   ByIdResolver is the generic fallback for cloud-provider block storage and
   runs after attachment is complete so the device is guaranteed to be present.

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

If the VolumeAttachment approach does not work for a particular driver (e.g. it
manages attachment out-of-band), override attach_and_resolve for that driver
instead of patching the generic flow.
"""

import hashlib
import logging
import os
import time
from abc import ABC, abstractmethod

from kubernetes import client

import k8s

LOG = logging.getLogger(__name__)

POLL_INTERVAL  = 3    # seconds between existence / status checks
DEVICE_TIMEOUT = 120  # seconds to wait for device node to appear
ATTACH_TIMEOUT = 120  # seconds to wait for VolumeAttachment.status.attached


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
    Both kernel RBD and rbd-nbd create /dev/rbd/<pool>/<imageName>.
    """

    def resolve(self, pv) -> str | None:
        csi = _csi_spec(pv)
        if not csi or "rbd" not in (csi.driver or ""):
            return None
        attrs = csi.volume_attributes or {}
        pool  = attrs.get("pool", "")
        image = attrs.get("imageName") or attrs.get("image", "")
        if pool and image:
            return f"/dev/rbd/{pool}/{image}"
        return None


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


def _attachment_name(volume_id: str, node_name: str) -> str:
    """Stable, DNS-safe VolumeAttachment name derived from volume_id and node."""
    raw = f"luks-attach-{volume_id.replace('/', '-')}-{node_name}"
    if len(raw) <= 253:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"luks-attach-{digest}"


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


def ensure_attached(volume_id: str, pv, node_name: str, timeout: int = ATTACH_TIMEOUT) -> None:
    """
    Ensure the backing PV is attached to node_name.

    Creates a VolumeAttachment targeting the backing CSI driver and waits for
    status.attached = True.  The attachment is held open until release_attachment
    is called during NodeUnstageVolume.

    No-op for non-CSI PVs (local, hostPath) where the device is always present.
    """
    csi = _csi_spec(pv)
    if not csi:
        return

    pv_name     = pv.metadata.name
    attach_name = _attachment_name(volume_id, node_name)
    api         = k8s.storage()

    try:
        api.read_volume_attachment(attach_name)
        LOG.info("VolumeAttachment %s already exists", attach_name)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        va = client.V1VolumeAttachment(
            metadata=client.V1ObjectMeta(name=attach_name),
            spec=client.V1VolumeAttachmentSpec(
                attacher=csi.driver,
                node_name=node_name,
                source=client.V1VolumeAttachmentSource(
                    persistent_volume_name=pv_name,
                ),
            ),
        )
        api.create_volume_attachment(va)
        LOG.info(
            "Created VolumeAttachment %s (driver=%s node=%s pv=%s)",
            attach_name, csi.driver, node_name, pv_name,
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        va = api.read_volume_attachment(attach_name)
        if va.status and va.status.attached:
            LOG.info("VolumeAttachment %s reports attached", attach_name)
            return
        if va.status and va.status.attach_error:
            raise RuntimeError(
                f"VolumeAttachment {attach_name} failed: "
                f"{va.status.attach_error.message}"
            )
        LOG.info(
            "Waiting for VolumeAttachment %s (%.0fs remaining)...",
            attach_name, deadline - time.monotonic(),
        )
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(
        f"VolumeAttachment {attach_name} did not report attached within {timeout}s"
    )


def release_attachment(volume_id: str, node_name: str) -> None:
    """
    Delete the VolumeAttachment created by ensure_attached, triggering
    ControllerUnpublishVolume on the backing CSI driver.

    Safe to call when no attachment exists (e.g. local/hostPath volumes where
    ensure_attached was a no-op).
    """
    attach_name = _attachment_name(volume_id, node_name)
    try:
        k8s.storage().delete_volume_attachment(attach_name)
        LOG.info("Deleted VolumeAttachment %s", attach_name)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        LOG.debug("VolumeAttachment %s already gone", attach_name)


def attach_and_resolve(
    volume_id: str,
    pv_name: str,
    node_name: str,
    attach_timeout: int = ATTACH_TIMEOUT,
    device_timeout: int = DEVICE_TIMEOUT,
) -> str:
    """
    High-level helper used by NodeStageVolume.

    1. Fetch the backing PV.
    2. Trigger and await VolumeAttachment (CSI drivers only).
    3. Resolve the device path via the RESOLVERS registry.
    4. Wait for the device file to appear on the node.

    Returns the block device path ready for cryptsetup.
    """
    pv = k8s.core().read_persistent_volume(pv_name)
    ensure_attached(volume_id, pv, node_name, timeout=attach_timeout)
    path = resolve_device_path(pv)
    if not path:
        raise ValueError(f"Cannot determine block device path from PV {pv_name!r}")
    wait_for_device(path, timeout=device_timeout)
    return path
