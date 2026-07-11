"""Hermetic disk/core regression tests."""

import unittest
from unittest import mock

from core import rusage
from scopes import disk


class TestResolveVolume(unittest.TestCase):
    def test_whole_disk_match_stops_at_slice_boundary(self):
        mounts = [
            ("/dev/disk1", "/Volumes/Whole"),
            ("/dev/disk1s1", "/Volumes/One"),
            ("/dev/disk10s1", "/Volumes/Ten"),
        ]
        with mock.patch.object(disk, "_mount_table", return_value=mounts):
            self.assertEqual(
                disk.resolve_volume("disk1"),
                mounts[:2],
            )


class TestProcNameCache(unittest.TestCase):
    def setUp(self):
        rusage._name_cache.clear()

    def tearDown(self):
        rusage._name_cache.clear()

    def test_pid_reuse_refreshes_cached_name(self):
        libc = mock.Mock()
        paths = iter((b"/usr/bin/old-process", b"/usr/bin/new-process"))

        def proc_pidpath(_pid, buf, _size):
            buf.value = next(paths)
            return len(buf.value)

        libc.proc_pidpath.side_effect = proc_pidpath
        identities = [(42, 100), (42, 100), (42, 200)]
        with mock.patch.object(rusage, "_libc", libc), \
                mock.patch.object(rusage, "proc_identity", side_effect=identities):
            self.assertEqual(rusage.proc_name(42), "old-process")
            self.assertEqual(rusage.proc_name(42), "old-process")
            self.assertEqual(rusage.proc_name(42), "new-process")

        self.assertEqual(libc.proc_pidpath.call_count, 2)
        self.assertEqual(rusage._name_cache, {(42, 200): "new-process"})


if __name__ == "__main__":
    unittest.main()
