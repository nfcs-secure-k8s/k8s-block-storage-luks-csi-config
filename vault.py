"""
HashiCorp Vault helpers for LUKS key management.

Keys are stored in Vault KV v2 at:
  {VAULT_MOUNT}/tenants/{institution}/luks-keys/{volume_name}

The node and controller both authenticate via the Kubernetes auth method
using the pod's service account JWT.
"""

import os
import secrets

import hvac

VAULT_ADDR = os.environ.get("VAULT_ADDR", "http://vault.default:8200")
VAULT_ROLE = os.environ.get("VAULT_ROLE", "luks-operator-role")
VAULT_MOUNT = os.environ.get("VAULT_MOUNT", "secret")

_SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"


def _split_path(institution: str, volume_name: str) -> tuple[str, str]:
    """Return (mount_point, secret_path) for a given institution + volume."""
    return VAULT_MOUNT, f"tenants/{institution}/luks-keys/{volume_name}"


def get_client() -> hvac.Client:
    """Return an authenticated Vault client using Kubernetes service account auth."""
    with open(_SA_TOKEN_PATH) as f:
        jwt = f.read()
    client = hvac.Client(url=VAULT_ADDR)
    client.auth.kubernetes.login(role=VAULT_ROLE, jwt=jwt)
    return client


def ensure_secret(institution: str, volume_name: str) -> int:
    """
    Ensure a LUKS key exists in Vault for this volume.

    If absent, generates a cryptographically secure 64-char hex key and stores it.
    Returns the current Vault version number.
    """
    client = get_client()
    mount, path = _split_path(institution, volume_name)
    try:
        resp = client.secrets.kv.v2.read_secret_version(mount_point=mount, path=path)
        return resp["data"]["metadata"]["version"]
    except hvac.exceptions.InvalidPath:
        new_key = secrets.token_hex(32)
        resp = client.secrets.kv.v2.create_or_update_secret(
            mount_point=mount,
            path=path,
            secret={"key": new_key},
        )
        return resp["data"]["version"]


def read_secret(institution: str, volume_name: str, version: int | None = None) -> str:
    """
    Read the LUKS key from Vault.

    Pass version=None to get the latest; pass an int for a specific version
    (used by key rotation to retrieve the previous key).
    Returns the key as a plain string.
    """
    client = get_client()
    mount, path = _split_path(institution, volume_name)
    kwargs: dict = {"mount_point": mount, "path": path}
    if version is not None:
        kwargs["version"] = version
    resp = client.secrets.kv.v2.read_secret_version(**kwargs)
    return resp["data"]["data"]["key"]


def current_version(institution: str, volume_name: str) -> int:
    """Return the current Vault version number for a volume's key."""
    client = get_client()
    mount, path = _split_path(institution, volume_name)
    resp = client.secrets.kv.v2.read_secret_version(mount_point=mount, path=path)
    return resp["data"]["metadata"]["version"]


def delete_secret(institution: str, volume_name: str) -> None:
    """
    Permanently delete a LUKS key and all its versions from Vault.

    Safe to call when the secret is already gone.
    """
    client = get_client()
    mount, path = _split_path(institution, volume_name)
    try:
        client.secrets.kv.v2.delete_metadata_and_all_versions(
            mount_point=mount,
            path=path,
        )
    except hvac.exceptions.InvalidPath:
        pass


def vault_path_str(institution: str, volume_name: str) -> str:
    """Return the human-readable Vault path string (for logging and volume_context)."""
    _, path = _split_path(institution, volume_name)
    return f"{VAULT_MOUNT}/{path}"
