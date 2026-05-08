"""
Kubernetes API helpers for the CSI driver.
Handles PVC lifecycle and Secret reads.
"""

import base64
import datetime
import functools
import logging
import time

from kubernetes import client, config

LOG = logging.getLogger(__name__)

_SA_NAMESPACE_FILE = "/var/run/secrets/kubernetes.io/serviceaccount/namespace"


def get_operator_namespace() -> str:
    """Return the namespace this pod is running in (falls back to 'default')."""
    try:
        return open(_SA_NAMESPACE_FILE).read().strip()
    except OSError:
        return "default"


@functools.lru_cache(maxsize=None)
def _api_client() -> client.ApiClient:
    """Load Kubernetes config once and return a cached ApiClient."""
    try:
        config.load_incluster_config()
    except config.ConfigException:
        config.load_kube_config()
    return client.ApiClient()


def core() -> client.CoreV1Api:
    return client.CoreV1Api(api_client=_api_client())


def storage() -> client.StorageV1Api:
    return client.StorageV1Api(api_client=_api_client())


# ---------------------------------------------------------------------------
# PVC helpers
# ---------------------------------------------------------------------------

def create_pvc(
    name: str,
    namespace: str,
    storage_class: str,
    size: str,
) -> None:
    """Create a Block-mode PVC backed by storage_class. No-op if it already exists."""
    api = core()
    try:
        api.read_namespaced_persistent_volume_claim(name, namespace)
        LOG.info("Backing PVC %s/%s already exists", namespace, name)
        return
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise

    pvc = client.V1PersistentVolumeClaim(
        metadata=client.V1ObjectMeta(name=name, namespace=namespace),
        spec=client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            volume_mode="Block",
            storage_class_name=storage_class,
            resources=client.V1VolumeResourceRequirements(
                requests={"storage": size}
            ),
        ),
    )
    api.create_namespaced_persistent_volume_claim(namespace, pvc)
    LOG.info("Created backing PVC %s/%s (%s, %s)", namespace, name, storage_class, size)


def wait_for_pvc_bound(name: str, namespace: str, timeout: int = 120) -> str:
    """Poll until PVC is Bound. Returns the PV name."""
    api = core()
    deadline = time.time() + timeout
    while time.time() < deadline:
        pvc = api.read_namespaced_persistent_volume_claim(name, namespace)
        if pvc.status.phase == "Bound":
            LOG.info("PVC %s/%s is Bound to PV %s", namespace, name, pvc.spec.volume_name)
            return pvc.spec.volume_name
        LOG.debug("PVC %s/%s phase: %s — waiting", namespace, name, pvc.status.phase)
        time.sleep(3)
    raise TimeoutError(f"PVC {namespace}/{name} did not bind within {timeout}s")


def delete_pvc(name: str, namespace: str) -> None:
    """Delete a PVC. Safe to call if already deleted."""
    api = core()
    try:
        api.delete_namespaced_persistent_volume_claim(name, namespace)
        LOG.info("Deleted backing PVC %s/%s", namespace, name)
    except client.exceptions.ApiException as e:
        if e.status != 404:
            raise
        LOG.debug("PVC %s/%s already gone", namespace, name)


# ---------------------------------------------------------------------------
# Event helpers
# ---------------------------------------------------------------------------

def emit_event(
    name: str,
    namespace: str,
    reason: str,
    message: str,
    event_type: str = "Normal",
) -> None:
    """Post a Kubernetes Event against a PersistentVolumeClaim.

    Uses a deterministic event name so repeated CreateVolume calls (retries)
    don't create duplicate events — a 409 AlreadyExists is silently ignored.
    """
    api = core()
    event_name = f"{name}.{reason.lower()}"[:253]
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    event = client.CoreV1Event(
        metadata=client.V1ObjectMeta(name=event_name, namespace=namespace),
        involved_object=client.V1ObjectReference(
            kind="PersistentVolumeClaim",
            name=name,
            namespace=namespace,
            api_version="v1",
        ),
        reason=reason,
        message=message,
        type=event_type,
        first_timestamp=now,
        last_timestamp=now,
        count=1,
        reporting_component="luks-csi-driver",
        reporting_instance="luks-csi-driver",
    )
    try:
        api.create_namespaced_event(namespace, event)
    except client.exceptions.ApiException as e:
        if e.status != 409:  # ignore AlreadyExists on retries
            LOG.warning("Failed to emit event on PVC %s/%s: %s", namespace, name, e)


# ---------------------------------------------------------------------------
# PV helpers
# ---------------------------------------------------------------------------

def get_pv_volume_attributes_by_pvc(pvc_name: str, namespace: str) -> dict:
    """Return the CSI volumeAttributes stored on the PV bound to pvc_name.

    Used by DeleteVolume to read institution, vaultPath, and deletionPolicy
    that were written into volume_context at CreateVolume time.
    Returns an empty dict if the PVC or PV is not found or has no CSI attributes.
    """
    api = core()
    try:
        pvc = api.read_namespaced_persistent_volume_claim(pvc_name, namespace)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            LOG.debug("PVC %s/%s not found when reading PV attributes", namespace, pvc_name)
            return {}
        raise

    pv_name = pvc.spec.volume_name
    if not pv_name:
        return {}

    try:
        pv = api.read_persistent_volume(pv_name)
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return {}
        raise

    csi_spec = getattr(pv.spec, "csi", None)
    if csi_spec and csi_spec.volume_attributes:
        return dict(csi_spec.volume_attributes)
    return {}


# ---------------------------------------------------------------------------
# Secret helpers
# ---------------------------------------------------------------------------

def secret_exists(name: str, namespace: str) -> bool:
    """Return True if the named Secret exists in namespace."""
    try:
        core().read_namespaced_secret(name, namespace)
        return True
    except client.exceptions.ApiException as e:
        if e.status == 404:
            return False
        raise


def read_secret_key(name: str, namespace: str, key: str = "luksKey") -> bytes:
    """Read a single key from a Kubernetes Secret and return its raw bytes."""
    api = core()
    secret = api.read_namespaced_secret(name, namespace)
    if secret.data and key in secret.data:
        return base64.b64decode(secret.data[key])
    if secret.string_data and key in secret.string_data:
        return secret.string_data[key].encode()
    raise KeyError(f"Key '{key}' not found in secret {namespace}/{name}")
