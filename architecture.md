```mermaid
flowchart TB
    subgraph cluster["Kubernetes Cluster"]
        subgraph userNS["User Namespace"]
            pod(["User Pod"])
            upvc["User PVC"]
        end

        subgraph ctrlPod["LUKS Controller Pod  ·  kube-system"]
            ep["external-provisioner"]
            ctrl["controller.py"]
            ctrlK8s["k8s.py"]
            ctrlVault["vault.py"]
            ep --> ctrl --> ctrlK8s & ctrlVault
        end

        subgraph nodePod["LUKS Node DaemonSet  ·  per node"]
            registrar["node-driver-registrar"]
            nodeSvc["node.py"]

            subgraph devPy["device.py  ·  RESOLVERS registry"]
                direction TB
                r1["1 · LocalResolver  ·  /dev/path from spec"]
                r2["2 · LonghornResolver  ·  /dev/longhorn/handle"]
                r3["3 · CephRBDResolver  ·  /dev/rbd/pool/image"]
                r4["4 · ByIdResolver  ·  scan /dev/disk/by-id/  fallback"]
                r1 --- r2 --- r3 --- r4
            end

            luksPy["luks.py"]
            nodeVault["vault.py"]
            nodeSvc --> devPy & luksPy & nodeVault
        end

        kapi[("Kubernetes API")]
        kubelet(["kubelet"])

        subgraph backDrv["Backing CSI Driver"]
            backCtrl["Controller + external-attacher"]
            backNode["Node Plugin"]
            backCtrl --> backNode
        end

        storage[("Backing Storage  ·  Longhorn · Ceph · Cinder · EBS · local-path")]
    end

    vault[("HashiCorp Vault  ·  KV v2")]

    pod --> upvc
    upvc -- "unbound PVC" --> ep
    ctrlK8s <-- "PVC · PV · Events" --> kapi
    ctrlVault <-- "ensure / read / delete key" --> vault

    registrar -. "register CSI socket" .-> kubelet
    kubelet -- "NodeStageVolume etc." --> nodeSvc
    devPy -- "create / delete VolumeAttachment" --> kapi
    kapi -- "external-attacher watches" --> backCtrl
    backNode --> storage
    nodeVault <-- "read key · rotate" --> vault

    classDef luks fill:#fff3e0,stroke:#e65100,color:#212121
    classDef vaultStyle fill:#f3e5f5,stroke:#6a1b9a,color:#212121
    classDef k8sStyle fill:#e3f2fd,stroke:#0d47a1,color:#212121
    classDef backStyle fill:#e8f5e9,stroke:#1b5e20,color:#212121
    classDef resolverStyle fill:#fafafa,stroke:#bdbdbd,color:#212121

    class ep,ctrl,ctrlK8s,ctrlVault,registrar,nodeSvc,luksPy,nodeVault luks
    class r1,r2,r3,r4 resolverStyle
    class vault vaultStyle
    class kapi,kubelet k8sStyle
    class backCtrl,backNode,storage backStyle
