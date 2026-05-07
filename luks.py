"""
Thin subprocess wrappers around cryptsetup and mkfs.
All idempotency checks live here so callers stay simple.
"""

import logging
import os
import subprocess

LOG = logging.getLogger(__name__)


def _run(cmd: list[str], input_data: bytes | None = None) -> subprocess.CompletedProcess:
    LOG.debug("Running: %s", " ".join(cmd))
    result = subprocess.run(
        cmd,
        input=input_data,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"Command {cmd[0]} failed (exit {result.returncode}): "
            f"{result.stderr.decode().strip()}"
        )
    return result


def is_luks(device: str) -> bool:
    """Return True if device already has a LUKS header.

    Bypasses _run() intentionally: a non-zero exit here means "not LUKS",
    not a fatal error, so we inspect the return code directly.
    """
    result = subprocess.run(
        ["cryptsetup", "isLuks", device],
        capture_output=True,
    )
    return result.returncode == 0


def mapper_exists(mapper_name: str) -> bool:
    """Return True if /dev/mapper/<mapper_name> exists and is active."""
    return os.path.exists(f"/dev/mapper/{mapper_name}")


def luks_format(device: str, key: bytes, luks_type: str = "luks2") -> None:
    """Format device with LUKS. key is the raw passphrase bytes."""
    LOG.info("Formatting %s with LUKS (%s)", device, luks_type)
    _run(
        [
            "cryptsetup", "luksFormat",
            "--batch-mode",
            "--type", luks_type,
            "--key-file", "-",
            device,
        ],
        input_data=key,
    )


def luks_open(device: str, mapper_name: str, key: bytes) -> None:
    """Open an existing LUKS device, creating /dev/mapper/<mapper_name>."""
    if mapper_exists(mapper_name):
        LOG.info("Mapper %s already open, skipping luksOpen", mapper_name)
        return
    LOG.info("Opening LUKS device %s as %s", device, mapper_name)
    _run(
        [
            "cryptsetup", "luksOpen",
            "--key-file", "-",
            device, mapper_name,
        ],
        input_data=key,
    )


def luks_close(mapper_name: str) -> None:
    """Close an open LUKS mapper. Safe to call even if already closed."""
    if not mapper_exists(mapper_name):
        LOG.debug("Mapper %s not open, nothing to close", mapper_name)
        return
    LOG.info("Closing LUKS mapper %s", mapper_name)
    _run(["cryptsetup", "luksClose", mapper_name])


def make_filesystem(mapper_name: str, filesystem: str = "ext4") -> None:
    """Create a filesystem on /dev/mapper/<mapper_name>."""
    device = f"/dev/mapper/{mapper_name}"
    LOG.info("Creating %s filesystem on %s", filesystem, device)
    if filesystem == "ext4":
        _run(["mkfs.ext4", "-F", device])
    elif filesystem == "xfs":
        _run(["mkfs.xfs", "-f", device])
    else:
        raise ValueError(f"Unsupported filesystem: {filesystem}")


def luks_add_key(device: str, new_key: bytes, old_key: bytes) -> None:
    """Add a new key slot to a LUKS device, authenticated with old_key.

    Used for key rotation: the new key is added before the old one is removed,
    so the device is never left with zero valid keys.
    """
    import tempfile
    LOG.info("Adding new key slot to %s", device)
    # cryptsetup luksAddKey reads the existing key from --key-file and the new
    # key from stdin.  We write old_key to a temp file so it never appears on
    # the command line.
    with tempfile.NamedTemporaryFile(delete=True) as kf:
        kf.write(old_key)
        kf.flush()
        _run(
            [
                "cryptsetup", "luksAddKey",
                "--batch-mode",
                "--key-file", kf.name,
                device,
            ],
            input_data=new_key,
        )


def luks_remove_key(device: str, key: bytes) -> None:
    """Remove the key slot matching key from a LUKS device.

    Called after luks_add_key during rotation to evict the old passphrase.
    """
    LOG.info("Removing old key slot from %s", device)
    _run(
        [
            "cryptsetup", "luksRemoveKey",
            "--batch-mode",
            device,
        ],
        input_data=key,
    )


def luks_close_robust(mapper_name: str) -> None:
    """Close a LUKS mapper, falling back to dmsetup deferred removal if busy.

    Mirrors the janitor job logic from the upstream kopf operator so that
    NodeUnstageVolume can handle stuck devices without needing a separate Job.
    """
    if not mapper_exists(mapper_name):
        LOG.debug("Mapper %s not open, nothing to close", mapper_name)
        return

    LOG.info("Closing LUKS mapper %s", mapper_name)
    result = subprocess.run(
        ["cryptsetup", "luksClose", mapper_name],
        capture_output=True,
    )
    if result.returncode == 0:
        return

    LOG.warning(
        "cryptsetup luksClose failed for %s (%s); falling back to dmsetup",
        mapper_name,
        result.stderr.decode().strip(),
    )
    # --force: remove immediately even if open references exist
    subprocess.run(["dmsetup", "remove", "--force", mapper_name], capture_output=True)
    # --deferred: queued removal once all references drop (belt-and-suspenders)
    subprocess.run(["dmsetup", "remove", "--deferred", mapper_name], capture_output=True)
    LOG.info("dmsetup deferred removal issued for %s", mapper_name)
