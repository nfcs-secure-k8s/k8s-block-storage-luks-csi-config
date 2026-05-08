"""
CSI Controller service — volume provisioning via a backing StorageClass.

CreateVolume:
  1. Derive a backing PVC name from the requested volume name.
  2. Create the backing PVC using the 'backingStorageClass' StorageClass parameter.
  3. Wait for the PVC to reach Bound.
  4. Read the backing PV to determine the raw block device path.
  5. Auto-generate a LUKS key in Vault (idempotent) for this volume.
  6. Return a Volume whose volume_context carries backingPvName, luksType,
     filesystem, backingPvcName, backingPvcNamespace, institution, vaultPath,
     and deletionPolicy so NodeStageVolume can resolve the device and key.

DeleteVolume:
  Parse namespace and PVC name from volume ID, then:
  - If deletionPolicy == "Delete": destroy the Vault key (all versions).
  - Delete the backing PVC.
"""

import logging

import grpc

from generated import csi_pb2, csi_pb2_grpc
import k8s
import vault as vault_mod

LOG = logging.getLogger(__name__)

# StorageClass parameter keys
PARAM_BACKING_SC      = "backingStorageClass"
PARAM_LUKS_TYPE       = "luksType"
PARAM_FS              = "filesystem"
PARAM_BACKING_NS      = "backingNamespace"
PARAM_INSTITUTION     = "institution"
PARAM_DELETION_POLICY = "deletionPolicy"

GiB = 1 << 30

_PVC_PREFIX = "luks-backing-"
_PVC_NAME_MAX = 63


def _backing_pvc_name(volume_name: str) -> str:
    raw = _PVC_PREFIX + volume_name.lower().replace("_", "-")
    return raw[:_PVC_NAME_MAX].rstrip("-")



class ControllerServicer(csi_pb2_grpc.ControllerServicer):

    def CreateVolume(self, request, context):
        name = request.name
        params = request.parameters
        capacity = request.capacity_range.required_bytes if request.capacity_range else 0

        backing_sc = params.get(PARAM_BACKING_SC)
        if not backing_sc:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(f"StorageClass parameter '{PARAM_BACKING_SC}' is required")
            return csi_pb2.CreateVolumeResponse()

        luks_type = params.get(PARAM_LUKS_TYPE, "luks2")
        filesystem = params.get(PARAM_FS, "ext4")
        institution = params.get(PARAM_INSTITUTION, "default")
        deletion_policy = params.get(PARAM_DELETION_POLICY, "Delete")

        namespace = (
            params.get(PARAM_BACKING_NS)
            or params.get("csi.storage.k8s.io/pvc-namespace")
            or k8s.get_operator_namespace()
        )

        gib = max(1, (capacity + GiB - 1) // GiB)
        size_str = f"{gib}Gi"

        pvc_name = _backing_pvc_name(name)
        volume_id = f"{namespace}/{pvc_name}"

        LOG.info(
            "CreateVolume: name=%s pvc=%s/%s sc=%s size=%s institution=%s",
            name, namespace, pvc_name, backing_sc, size_str, institution,
        )

        try:
            k8s.create_pvc(pvc_name, namespace, backing_sc, size_str)
            pv_name = k8s.wait_for_pvc_bound(pvc_name, namespace)

            # Auto-generate LUKS key in Vault (no-op if already exists).
            # We use the CSI volume name (not the PVC name) as the Vault key
            # identifier so it stays stable across renames.
            vault_ver = vault_mod.ensure_secret(institution, name)
            vault_path = vault_mod.vault_path_str(institution, name)
            LOG.info(
                "Vault key ready for %s at %s (v%d)", name, vault_path, vault_ver
            )

            volume_context = {
                "backingPvcName": pvc_name,
                "backingPvcNamespace": namespace,
                "backingPvName": pv_name,
                "luksType": luks_type,
                "filesystem": filesystem,
                "institution": institution,
                "vaultPath": vault_path,
                "deletionPolicy": deletion_policy,
            }

            user_pvc_name = params.get("csi.storage.k8s.io/pvc-name")
            if user_pvc_name:
                k8s.emit_event(
                    name=user_pvc_name,
                    namespace=namespace,
                    reason="LuksKeyProvisioned",
                    message=(
                        f"Volume provisioned. LUKS key auto-generated in Vault at "
                        f'"{vault_path}" (v{vault_ver}). '
                        f"Deletion policy: {deletion_policy}."
                    ),
                )

            return csi_pb2.CreateVolumeResponse(
                volume=csi_pb2.Volume(
                    volume_id=volume_id,
                    capacity_bytes=gib * GiB,
                    volume_context=volume_context,
                )
            )

        except Exception as e:
            LOG.exception("CreateVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.CreateVolumeResponse()

    def DeleteVolume(self, request, context):
        volume_id = request.volume_id
        LOG.info("DeleteVolume: volume_id=%s", volume_id)

        if "/" in volume_id:
            namespace, pvc_name = volume_id.split("/", 1)
        else:
            namespace = k8s.get_operator_namespace()
            pvc_name = volume_id

        try:
            # Retrieve volume_context from the PV before deleting the PVC so we
            # know institution, vaultPath, and deletionPolicy.
            attrs = k8s.get_pv_volume_attributes_by_pvc(pvc_name, namespace)
            deletion_policy = attrs.get("deletionPolicy", "Delete")
            institution = attrs.get("institution", "default")
            vault_path = attrs.get("vaultPath", "")

            if deletion_policy == "Delete" and vault_path:
                volume_name = vault_path.rsplit("/", 1)[-1]
                LOG.info(
                    "deletionPolicy=Delete: destroying Vault key at %s", vault_path
                )
                vault_mod.delete_secret(institution, volume_name)

            k8s.delete_pvc(pvc_name, namespace)

        except Exception as e:
            LOG.exception("DeleteVolume failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details(str(e))
            return csi_pb2.DeleteVolumeResponse()

        return csi_pb2.DeleteVolumeResponse()

    def ControllerGetCapabilities(self, request, context):
        return csi_pb2.ControllerGetCapabilitiesResponse(
            capabilities=[
                csi_pb2.ControllerServiceCapability(
                    rpc=csi_pb2.ControllerServiceCapability.RPC(
                        type=csi_pb2.ControllerServiceCapability.RPC.CREATE_DELETE_VOLUME,
                    )
                )
            ]
        )

    def ValidateVolumeCapabilities(self, request, context):
        for cap in request.volume_capabilities:
            access_mode = getattr(cap, "access_mode", None)
            if not access_mode or access_mode.mode != csi_pb2.VolumeCapability.AccessMode.SINGLE_NODE_WRITER:
                return csi_pb2.ValidateVolumeCapabilitiesResponse(
                    message="only SINGLE_NODE_WRITER (ReadWriteOnce) is supported"
                )
        return csi_pb2.ValidateVolumeCapabilitiesResponse(
            confirmed=csi_pb2.ValidateVolumeCapabilitiesResponse.Confirmed(
                volume_capabilities=request.volume_capabilities,
            )
        )
