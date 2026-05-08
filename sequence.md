```mermaid
sequenceDiagram
    autonumber
    actor User
    participant K8s as Kubernetes API
    participant EP as external-provisioner
    participant Ctrl as controller.py
    participant Vault as HashiCorp Vault
    participant kbl as kubelet
    participant Node as node.py
    participant Dev as device.py
    participant BDrv as Backing CSI Driver
    participant LUKS as luks.py

    rect rgb(225, 240, 255)
        Note over User,Vault: Phase 1 — Volume Provisioning

        User->>K8s: create PVC with luks-encrypted StorageClass
        K8s->>EP: unbound PVC event
        EP->>Ctrl: CreateVolume(name, params)
        Ctrl->>K8s: create backing PVC using backingStorageClass
        K8s-->>Ctrl: PVC Bound, pv_name returned
        Ctrl->>Vault: ensure_secret(institution, volume_name)
        Note right of Vault: Idempotent — skips if key already exists
        Vault-->>Ctrl: key version
        Ctrl-->>EP: Volume with volume_context backingPvName, vaultPath, luksType
        EP->>K8s: create PV, bind to User PVC
        K8s-->>User: PVC Bound
    end

    rect rgb(255, 243, 224)
        Note over kbl,LUKS: Phase 2 — Volume Staging

        kbl->>Node: NodeStageVolume(volume_id, staging_path, volume_context)
        Node->>Dev: attach_and_resolve(volume_id, pv_name, node_name)

        Dev->>K8s: create VolumeAttachment(attacher, nodeName, pvName)
        K8s->>BDrv: external-attacher sees new VolumeAttachment
        BDrv->>BDrv: ControllerPublishVolume
        BDrv-->>K8s: VolumeAttachment.status.attached = true
        K8s-->>Dev: attached

        Note over Dev: Try RESOLVERS in order until a path is returned
        Dev->>Dev: resolve_device_path(pv) via RESOLVERS registry

        loop wait_for_device — polls every 3s, up to 120s
            Dev->>Dev: os.path.exists(device_path)?
        end

        Dev-->>Node: block device path

        Node->>Vault: read_secret(institution, volume_name)
        Vault-->>Node: LUKS key + current version

        alt First use — device not yet LUKS-formatted
            Node->>LUKS: luks_format(device, key, luks_type)
            Node->>LUKS: luks_open(device, mapper, key)
            Node->>LUKS: make_filesystem(mapper, filesystem)
        else Device already LUKS-formatted
            Node->>LUKS: luks_open(device, mapper, current_key)
            opt Vault version advanced — key rotation required
                Node->>Vault: read_secret(institution, volume_name, version=prev)
                Vault-->>Node: previous key
                Node->>LUKS: luks_add_key(device, new_key, old_key)
                Node->>LUKS: luks_remove_key(device, old_key)
                Node->>LUKS: luks_open(device, mapper, new_key)
            end
        end

        Node->>Node: mount /dev/mapper/luks-name to staging_path
        Node-->>kbl: NodeStageVolumeResponse
    end

    rect rgb(232, 245, 233)
        Note over kbl,Node: Phase 3 — Volume Publish

        kbl->>Node: NodePublishVolume(staging_path, target_path, readonly)
        Node->>Node: bind-mount staging_path to pod target_path
        Node-->>kbl: NodePublishVolumeResponse
        kbl-->>User: Pod running, volume mounted
    end

    rect rgb(252, 228, 236)
        Note over kbl,BDrv: Phase 4 — Teardown

        kbl->>Node: NodeUnpublishVolume(target_path)
        Node->>Node: umount target_path
        Node-->>kbl: NodeUnpublishVolumeResponse

        kbl->>Node: NodeUnstageVolume(volume_id, staging_path)
        Node->>Node: umount staging_path, lazy fallback if busy
        Node->>LUKS: luks_close_robust(mapper)
        Node->>Dev: release_attachment(volume_id, node_name)
        Dev->>K8s: delete VolumeAttachment
        K8s->>BDrv: external-attacher sees deletion
        BDrv->>BDrv: ControllerUnpublishVolume, block device detached
        Node-->>kbl: NodeUnstageVolumeResponse
    end
