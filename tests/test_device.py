"""Unit tests for luks_csi_driver.device. Run from the repo root: uv run python -m unittest discover tests"""

import os
import sys
import tempfile
import unittest
from unittest import mock

from kubernetes import client

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from luks_csi_driver import device  # noqa: E402

PV_NAME = "pvc-f23939df-b6bf-4821-aeac-593cb7128613"
POD_UID = "99e8e8f9-60ac-4876-a874-aba77ae63dfd"


def csi_pv(name=PV_NAME, driver="rbd.csi.ceph.com", attrs=None):
    return client.V1PersistentVolume(
        metadata=client.V1ObjectMeta(name=name),
        spec=client.V1PersistentVolumeSpec(
            csi=client.V1CSIPersistentVolumeSource(
                driver=driver,
                volume_handle="handle-1",
                volume_attributes=attrs or {},
            ),
        ),
    )


def local_pv():
    return client.V1PersistentVolume(
        metadata=client.V1ObjectMeta(name="local-pv"),
        spec=client.V1PersistentVolumeSpec(local=client.V1LocalVolumeSource(path="/dev/sdz")),
    )


class KubeletPublishResolverTest(unittest.TestCase):
    """The resolver maps kubelet's per-pod published device to its /dev node."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = self._tmp.name
        self.kubelet_root = os.path.join(root, "kubelet")
        self.sys_root = os.path.join(root, "sys")
        self.dev_root = os.path.join(root, "dev")
        self.publish = os.path.join(
            self.kubelet_root, "plugins/kubernetes.io/csi/volumeDevices/publish", PV_NAME, POD_UID
        )
        # path -> (major, minor); paths absent from the map are "not a block device".
        self.rdevs = {self.publish: (251, 0)}
        self.set_devname("rbd0")

    def tearDown(self):
        self._tmp.cleanup()

    def set_devname(self, name):
        uevent_dir = os.path.join(self.sys_root, "dev", "block", "251:0")
        os.makedirs(uevent_dir, exist_ok=True)
        with open(os.path.join(uevent_dir, "uevent"), "w") as f:
            f.write(f"MAJOR=251\nMINOR=0\nDEVNAME={name}\nDEVTYPE=disk\n")
        self.rdevs[os.path.join(self.dev_root, name)] = (251, 0)

    def rdev(self, path):
        return self.rdevs.get(path)

    def resolver(self):
        return device.KubeletPublishResolver(
            kubelet_root=self.kubelet_root,
            sys_root=self.sys_root,
            dev_root=self.dev_root,
            rdev=self.rdev,
        )

    def test_resolves_published_device_to_dev_node(self):
        path = self.resolver().resolve(csi_pv(), staging_pod_uid=POD_UID)
        self.assertEqual(path, os.path.join(self.dev_root, "rbd0"))

    def test_not_applicable_without_staging_pod_uid(self):
        self.assertIsNone(self.resolver().resolve(csi_pv()))

    def test_not_applicable_for_non_csi_pv(self):
        self.assertIsNone(self.resolver().resolve(local_pv(), staging_pod_uid=POD_UID))

    def test_not_applicable_when_publish_path_is_not_block_device(self):
        del self.rdevs[self.publish]
        self.assertIsNone(self.resolver().resolve(csi_pv(), staging_pod_uid=POD_UID))

    def test_rejects_invalid_pod_uid(self):
        for uid in ("../../etc", "not-a-uid", POD_UID + "/x"):
            with self.subTest(uid=uid), self.assertRaises(ValueError):
                self.resolver().resolve(csi_pv(), staging_pod_uid=uid)

    def test_rejects_invalid_pv_name(self):
        with self.assertRaises(ValueError):
            self.resolver().resolve(csi_pv(name="../../etc/passwd"), staging_pod_uid=POD_UID)

    def test_rejects_unsafe_devname(self):
        for name in ("../etc/passwd", "a/../../b", ""):
            with self.subTest(devname=name), self.assertRaises(ValueError):
                self.set_devname(name)
                self.resolver().resolve(csi_pv(), staging_pod_uid=POD_UID)

    def test_rejects_dev_node_rdev_mismatch(self):
        self.rdevs[os.path.join(self.dev_root, "rbd0")] = (8, 48)
        with self.assertRaisesRegex(ValueError, "mismatch"):
            self.resolver().resolve(csi_pv(), staging_pod_uid=POD_UID)


class ResolveDevicePathTest(unittest.TestCase):

    def test_prefers_kubelet_resolver_when_staging_pod_uid_given(self):
        with mock.patch.object(device._kubelet_resolver, "resolve", return_value="/dev/rbd0") as kr, \
             mock.patch.object(device, "_scan_rbd_sysfs", return_value="/dev/rbd9"):
            path = device.resolve_device_path(csi_pv(attrs={"pool": "p", "imageName": "i"}), POD_UID)
        self.assertEqual(path, "/dev/rbd0")
        kr.assert_called_once()

    def test_falls_back_to_backend_resolver_when_kubelet_not_applicable(self):
        with mock.patch.object(device._kubelet_resolver, "resolve", return_value=None), \
             mock.patch.object(device, "_scan_rbd_sysfs", return_value="/dev/rbd9"):
            path = device.resolve_device_path(csi_pv(attrs={"pool": "p", "imageName": "i"}), POD_UID)
        self.assertEqual(path, "/dev/rbd9")


def running_pod(uid=POD_UID):
    return client.V1Pod(
        metadata=client.V1ObjectMeta(name="luks-stage-x", uid=uid),
        status=client.V1PodStatus(phase="Running"),
    )


class EnsureStagedTest(unittest.TestCase):

    def test_returns_running_staging_pod_uid(self):
        core = mock.Mock()
        core.read_namespaced_pod.return_value = running_pod()
        with mock.patch.object(device.k8s, "core", return_value=core):
            uid = device.ensure_staged("ns/vol", csi_pv(), "backing", "ns", "node-1")
        self.assertEqual(uid, POD_UID)

    def test_returns_none_for_non_csi_pv(self):
        self.assertIsNone(device.ensure_staged("ns/vol", local_pv(), "backing", "ns", "node-1"))


class AttachAndResolveTest(unittest.TestCase):

    def test_passes_staging_pod_uid_to_resolution(self):
        core = mock.Mock()
        core.read_persistent_volume.return_value = csi_pv()
        with mock.patch.object(device.k8s, "core", return_value=core), \
             mock.patch.object(device, "ensure_staged", return_value=POD_UID), \
             mock.patch.object(device, "resolve_device_path", return_value="/dev/rbd0") as rdp, \
             mock.patch.object(device, "wait_for_device"):
            path = device.attach_and_resolve("ns/vol", PV_NAME, "backing", "ns", "node-1")
        self.assertEqual(path, "/dev/rbd0")
        self.assertEqual(rdp.call_args.args[1], POD_UID)


if __name__ == "__main__":
    unittest.main()
