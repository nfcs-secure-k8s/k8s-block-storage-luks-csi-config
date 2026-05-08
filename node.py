"""
CSI Node service — LUKS setup, staging, and mounting.

Flow:
  NodeStageVolume   → fetch key from Vault (auto-rotate if version changed)
                    → luksFormat (idempotent) → luksOpen → mount at staging path
  NodePublishVolume → bind-mount staging path → pod target path
  NodeUnpublishVolume → umount target path
  NodeUnstageVolume   → umount (with lazy fallback) → luks_close_robust
"""

import logging
import os
import socket
import subprocess

import grpc

from generated import csi_pb2, csi_pb2_grpc
import device
import luks
import vault as vault_mod

LOG = logging.getLogger(__name__)


def _mapper_name(volume_id: str) -> str:
    """Deterministic dm-crypt mapper name derived from volume ID."""
    return ("luks-" + volume_id.replace("/", "-"))[:63]


def _mount(source: str, target: str, fs_type: str = "", flags: list[str] | None = None) -> None:
    cmd = ["mount"]
    if flags:
        cmd += flags
    if fs_type:
        cmd += ["-t", fs_type]
    cmd += [source, target]
    result = subprocess.run(cmd, capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"mount failed: {result.stderr.decode().strip()}")


def _is_mounted(path: str) -> bool:
    result = subprocess.run(["mountpoint", "-q", path], capture_output=True)
    return result.returncode == 0


def _umount(path: str) -> None:
    result = subprocess.run(["umount", path], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"umount failed: {result.stderr.decode().strip()}")


def _umount_lazy(path: str) -> None:
    """Force lazy unmount — mirrors the janitor job's `umount -fl` fallback."""
    LOG.warning("Falling back to lazy unmount for %s", path)
    result = subprocess.run(["umount", "-fl", path], capture_output=True)
    if result.returncode != 0:
        raise RuntimeError(f"umount -fl failed: {result.stderr.decode().strip()}")



def _open_with_rotation(device: str, mapper: str, institution: str, volume_name: str) -> None:
    """
    Open the LUKS device, performing key rotation if the Vault version has advanced.

    Rotation sequence (mirrors the upstream rekey Job):
      1. Fetch current key (latest Vault version).
      2. Try to open — succeeds if the device already uses the current key.
      3. On wrong-key failure, fetch the previous Vault version and perform
         luksAddKey (add new) + luksRemoveKey (evict old), then open.
    """
    ver = vault_mod.current_version(institution, volume_name)
    current_key = vault_mod.read_secret(institution, volume_name).encode()

    if not luks.mapper_exists(mapper):
        try:
            luks.luks_open(device, mapper, current_key)
            return
        except RuntimeError as exc:
            # "No key available" means the device was formatted with an older key.
            if "No key available" not in str(exc) or ver < 2:
                raise
            LOG.info(
                "Current Vault key (v%d) rejected; attempting rotation for %s",
                ver, device,
            )
            prev_key = vault_mod.read_secret(institution, volume_name, version=ver - 1).encode()
            luks.luks_add_key(device, current_key, prev_key)
            luks.luks_remove_key(device, prev_key)
            LOG.info("Key rotation to Vault v%d complete for %s", ver, device)
            luks.luks_open(device, mapper, current_key)
    else:
        LOG.info("Mapper %s already open, skipping open", mapper)


class NodeServicer(csi_pb2_grpc.NodeServicer):

    def NodeStageVolume(self, request, context):
        """
        Format + open the LUKS device and mount it at the staging path.

        Key is fetched directly from Vault using volume_context fields:
          institution  — tenant name (default: "default")
          vaultPath    — full Vault path (used to derive volume_name for logging)

        Automatic key rotation occurs if the Vault version has advanced since
        the device was last formatted/opened.

        Other volume_context keys:
          backingPvName  — backing PV name; used to trigger attachment (via a
                           VolumeAttachment for CSI drivers) and resolve the block
                           device path through the device.RESOLVERS registry
          luksType       — luks1 or luks2 (default: luks2)
          filesystem     — ext4 or xfs   (default: ext4)
        """
        volume_id = request.volume_id
        staging_path = request.staging_target_path
        ctx = request.volume_context

        if not volume_id:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("volume_id is required")
            return csi_pb2.NodeStageVolumeResponse()

        institution = ctx.get("institution", "default")
        vault_path = ctx.get("vaultPath", "")
        volume_name = vault_path.rsplit("/", 1)[-1] if vault_path else volume_id.replace("/", "-")

        luks_type = ctx.get("luksType", "luks2")
        filesystem = ctx.get("filesystem", "ext4")
        mapper = _mapper_name(volume_id)

        try:
            pv_name = ctx.get("backingPvName")
            if not pv_name:
                raise ValueError("volume_context.backingPvName is required")

            block_device = device.attach_and_resolve(
                volume_id, pv_name, socket.gethostname()
            )

            LOG.info(
                "NodeStageVolume: volume=%s device=%s mapper=%s staging=%s vault=%s",
                volume_id, block_device, mapper, staging_path, vault_path,
            )

            current_key = vault_mod.read_secret(institution, volume_name).encode()

            if not luks.is_luks(block_device):
                LOG.info("Device %s is not LUKS-formatted; formatting now", block_device)
                luks.luks_format(block_device, current_key, luks_type)
                luks.luks_open(block_device, mapper, current_key)
                luks.make_filesystem(mapper, filesystem)
            else:
                LOG.info("Device %s already LUKS-formatted; opening with rotation check", block_device)
                _open_with_rotation(block_device, mapper, institution, volume_name)

            os.makedirs(staging_path, exist_ok=True)
            if _is_mounted(staging_path):
                LOG.info("Staging path %s already mounted", staging_path)
            else:
                _mount(f"/dev/mapper/{mapper}", staging_path, fs_type=filesystem)
                LOG.info("Mounted /dev/mapper/%s at %s", mapper, staging_path)

        except Exception as e:
            LOG.exception("NodeStageVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.NodeStageVolumeResponse()

        return csi_pb2.NodeStageVolumeResponse()

    def NodeUnstageVolume(self, request, context):
        """Unmount staging path (with lazy fallback) and close the LUKS mapper robustly."""
        volume_id = request.volume_id
        staging_path = request.staging_target_path
        mapper = _mapper_name(volume_id)

        LOG.info("NodeUnstageVolume: volume=%s staging=%s mapper=%s", volume_id, staging_path, mapper)

        try:
            if _is_mounted(staging_path):
                try:
                    _umount(staging_path)
                except RuntimeError:
                    _umount_lazy(staging_path)
            luks.luks_close_robust(mapper)
            # Release the VolumeAttachment so the backing CSI driver can detach
            # the raw block device.  No-op for local/hostPath volumes.
            device.release_attachment(volume_id, socket.gethostname())
        except Exception as e:
            LOG.exception("NodeUnstageVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.NodeUnstageVolumeResponse()

        return csi_pb2.NodeUnstageVolumeResponse()

    def NodePublishVolume(self, request, context):
        """Bind-mount the staged path to the pod-specific target path."""
        volume_id = request.volume_id
        staging_path = request.staging_target_path
        target_path = request.target_path
        readonly = request.readonly

        LOG.info(
            "NodePublishVolume: volume=%s staging=%s target=%s readonly=%s",
            volume_id, staging_path, target_path, readonly,
        )

        try:
            os.makedirs(target_path, exist_ok=True)
            if _is_mounted(target_path):
                LOG.info("Target path %s already mounted", target_path)
            else:
                _mount(staging_path, target_path, flags=["--bind"])
                if readonly:
                    _mount(target_path, target_path, flags=["-o", "remount,ro,bind"])
                    LOG.info("Bind-mounted %s → %s (read-only)", staging_path, target_path)
                else:
                    LOG.info("Bind-mounted %s → %s", staging_path, target_path)
        except Exception as e:
            LOG.exception("NodePublishVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.NodePublishVolumeResponse()

        return csi_pb2.NodePublishVolumeResponse()

    def NodeUnpublishVolume(self, request, context):
        """Unmount the pod-specific target path."""
        target_path = request.target_path
        LOG.info("NodeUnpublishVolume: target=%s", target_path)

        try:
            if _is_mounted(target_path):
                _umount(target_path)
        except Exception as e:
            LOG.exception("NodeUnpublishVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.NodeUnpublishVolumeResponse()

        return csi_pb2.NodeUnpublishVolumeResponse()

    def NodeGetCapabilities(self, request, context):
        return csi_pb2.NodeGetCapabilitiesResponse(
            capabilities=[
                csi_pb2.NodeServiceCapability(
                    rpc=csi_pb2.NodeServiceCapability.RPC(
                        type=csi_pb2.NodeServiceCapability.RPC.STAGE_UNSTAGE_VOLUME,
                    )
                )
            ]
        )

    def NodeGetInfo(self, request, context):
        return csi_pb2.NodeGetInfoResponse(node_id=socket.gethostname())
