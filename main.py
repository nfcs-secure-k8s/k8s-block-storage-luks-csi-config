"""
Entry point for the LUKS CSI driver.

Starts a gRPC server on a Unix socket and registers the appropriate services
depending on CSI_MODE:
  controller  – Identity + Controller (runs in the operator Deployment)
  node        – Identity + Node       (runs in the DaemonSet)
  all         – Identity + Controller + Node (useful for local testing)
"""

import logging
import os
import signal
import sys
import threading
import time
from concurrent import futures

import grpc

from generated import csi_pb2_grpc
from driver import IdentityServicer
from controller import ControllerServicer
from node import NodeServicer
import k8s
import vault as vault_mod

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger(__name__)

DEFAULT_SOCKET = "/csi/csi.sock"


def _socket_address(path: str) -> str:
    return f"unix://{path}"


def _cleanup_socket(path: str) -> None:
    try:
        os.unlink(path)
        LOG.debug("Removed stale socket %s", path)
    except FileNotFoundError:
        pass


_VAULT_SYNC_INTERVAL = int(os.environ.get("VAULT_SYNC_INTERVAL", "30"))
_DRIVER_NAME = "luks.csi.example.com"
_VAULT_VERSION_ANNOTATION = "luks.csi.example.com/vault-version"


def _sync_vault_versions() -> None:
    """
    Poll Vault for the current key version of every PV managed by this driver
    and annotate the PV with luks.csi.example.com/vault-version.

    Mirrors the kopf timer (sync_vault_version) from the upstream operator:
    visible in 'kubectl describe pv' and useful for alerting on stale keys.
    """
    api = k8s.core()
    try:
        pvs = api.list_persistent_volume(
            label_selector=f"csi-driver={_DRIVER_NAME}"
        )
    except Exception as exc:
        LOG.debug("Vault sync: could not list PVs: %s", exc)
        return

    for pv in pvs.items:
        csi_spec = getattr(pv.spec, "csi", None)
        if not csi_spec or not csi_spec.volume_attributes:
            continue
        attrs = csi_spec.volume_attributes
        institution = attrs.get("institution", "default")
        vault_path = attrs.get("vaultPath", "")
        if not vault_path:
            continue
        volume_name = vault_path.rsplit("/", 1)[-1]
        try:
            ver = vault_mod.current_version(institution, volume_name)
            api.patch_persistent_volume(
                pv.metadata.name,
                {"metadata": {"annotations": {_VAULT_VERSION_ANNOTATION: str(ver)}}},
            )
            LOG.debug(
                "Vault sync: PV %s annotated with version %d", pv.metadata.name, ver
            )
        except Exception as exc:
            LOG.debug(
                "Vault sync: skipping PV %s: %s", pv.metadata.name, exc
            )


def _vault_sync_loop() -> None:
    """Background daemon thread: run _sync_vault_versions every VAULT_SYNC_INTERVAL seconds."""
    LOG.info("Vault sync thread started (interval=%ds)", _VAULT_SYNC_INTERVAL)
    while True:
        try:
            _sync_vault_versions()
        except Exception as exc:
            LOG.warning("Vault sync error: %s", exc)
        time.sleep(_VAULT_SYNC_INTERVAL)


def serve(socket_path: str, mode: str) -> None:
    _cleanup_socket(socket_path)
    os.makedirs(os.path.dirname(socket_path), exist_ok=True)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))

    # Identity is always registered
    csi_pb2_grpc.add_IdentityServicer_to_server(IdentityServicer(), server)

    if mode in ("controller", "all"):
        csi_pb2_grpc.add_ControllerServicer_to_server(ControllerServicer(), server)
        LOG.info("Registered Controller service")
        threading.Thread(target=_vault_sync_loop, daemon=True, name="vault-sync").start()

    if mode in ("node", "all"):
        csi_pb2_grpc.add_NodeServicer_to_server(NodeServicer(), server)
        LOG.info("Registered Node service")

    addr = _socket_address(socket_path)
    server.add_insecure_port(addr)
    server.start()
    LOG.info("gRPC server listening on %s (mode=%s)", addr, mode)

    def _shutdown(signum, frame):
        LOG.info("Shutting down (signal %d)", signum)
        server.stop(grace=5)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    server.wait_for_termination()


if __name__ == "__main__":
    socket_path = os.environ.get("CSI_ENDPOINT", DEFAULT_SOCKET)
    mode = os.environ.get("CSI_MODE", "all").lower()

    if mode not in ("controller", "node", "all"):
        LOG.error("Invalid CSI_MODE=%r — must be controller, node, or all", mode)
        sys.exit(1)

    LOG.info("Starting LUKS CSI driver (mode=%s, socket=%s)", mode, socket_path)
    serve(socket_path, mode)
