"""
stethoscope tests — the probe layer's contracts, stdlib unittest only.

    python3 -m unittest discover tests

These run against the live machine (no mocks): the struct layout, the
timebase conversion, and rank_io's diff math are exactly the things that
fail silently on the wrong hardware, so they are tested on real syscalls.
"""

import ctypes
import os
import re
import subprocess
import time
import unittest

from core import rusage
from core.validate import (parse_header_struct, parse_header_v4,
                           sdk_header_path, signed64)
from scopes import cpu as cpu_scope
from scopes import disk


class TestStruct(unittest.TestCase):
    def test_sizeof(self):
        # 16-byte uuid + 35 u64s, exactly.
        self.assertEqual(ctypes.sizeof(rusage.RUsageInfoV4), 296)

    def test_fields_match_sdk_header(self):
        header = sdk_header_path()
        if not header:
            self.skipTest("no SDK header (Command Line Tools absent)")
        theirs = parse_header_v4(header)
        ours = [name for name, _ in rusage.RUsageInfoV4._fields_]
        self.assertEqual(ours, theirs)

    def test_trailing_field_is_last(self):
        # The whole point of the full declaration: the kernel writes through
        # to ri_runnable_time at flavor 4.
        self.assertEqual(rusage.RUsageInfoV4._fields_[-1][0], "ri_runnable_time")


class TestStructV6(unittest.TestCase):
    def test_sizeof(self):
        # 16-byte uuid + 47 u64s + 9 reserved u64s, exactly.
        self.assertEqual(ctypes.sizeof(rusage.RUsageInfoV6), 464)

    def test_is_superset_of_v4(self):
        v4 = [name for name, _ in rusage.RUsageInfoV4._fields_]
        v6 = [name for name, _ in rusage.RUsageInfoV6._fields_]
        self.assertEqual(v6[:len(v4)], v4)

    def test_fields_match_sdk_header(self):
        header = sdk_header_path()
        if not header:
            self.skipTest("no SDK header (Command Line Tools absent)")
        parsed = parse_header_struct(header, 6)
        if parsed is None:
            self.skipTest("SDK header has no rusage_info_v6")
        self.assertEqual([name for name, _ in rusage.RUsageInfoV6._fields_],
                         [name for name, _ in parsed])
        self.assertEqual(ctypes.sizeof(rusage.RUsageInfoV6),
                         sum(nbytes for _, nbytes in parsed))

    def test_trailing_field_is_reserved(self):
        # The kernel writes the reserved tail too at flavor 6 (S9).
        self.assertEqual(rusage.RUsageInfoV6._fields_[-1][0], "ri_reserved")


class TestTimebase(unittest.TestCase):
    def test_ticks_to_ns_matches_timebase(self):
        n, d = rusage.TIMEBASE_NUMER, rusage.TIMEBASE_DENOM
        self.assertGreater(n, 0)
        self.assertGreater(d, 0)
        for t in (0, 1, 3, 1000, 10**12):
            self.assertEqual(rusage.ticks_to_ns(t), t * n // d)

    def test_cpu_burn_converts_to_wall_seconds(self):
        # slow (~0.3 s): the S2 regression — converted CPU delta must track
        # wall clock; raw ticks would be 41.7x off on Apple Silicon.
        pid = os.getpid()
        before = rusage.rusage(pid)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 0.3:
            pass
        wall = time.perf_counter() - t0
        after = rusage.rusage(pid)
        delta = (after["cpu_user_s"] + after["cpu_system_s"]
                 - before["cpu_user_s"] - before["cpu_system_s"])
        self.assertLess(abs(delta - wall) / wall, 0.30)


class TestRusage(unittest.TestCase):
    KEYS = {"cpu_user_s", "cpu_system_s", "pkg_idle_wakeups", "interrupt_wakeups",
            "diskio_bytes_read", "diskio_bytes_written", "phys_footprint_bytes",
            "billed_energy_nj", "energy_nj", "qos_time_s", "billed_system_s",
            "serviced_system_s", "runnable_s", "start_time_epoch"}

    def test_own_pid_has_all_keys(self):
        r = rusage.rusage(os.getpid())
        self.assertIsNotNone(r)
        self.assertEqual(set(r), self.KEYS)

    def test_sane_ranges(self):
        r = rusage.rusage(os.getpid())
        self.assertGreater(r["cpu_user_s"], 0)
        self.assertLess(r["cpu_user_s"], 3600)
        self.assertGreater(r["phys_footprint_bytes"], 1 << 20)
        self.assertEqual(set(r["qos_time_s"]),
                         {"default", "maintenance", "background", "utility",
                          "legacy", "user_initiated", "user_interactive"})
        # start_time_epoch: this process started recently, and in the past.
        self.assertLess(r["start_time_epoch"], time.time())
        self.assertGreater(r["start_time_epoch"], time.time() - 3600)

    def test_proc_diskio_and_identity(self):
        pid = os.getpid()
        io = rusage.proc_diskio(pid)
        self.assertIsInstance(io, tuple)
        self.assertEqual(len(io), 2)
        ident = rusage.proc_identity(pid)
        self.assertEqual(ident[0], pid)
        self.assertGreater(ident[1], 0)
        # Identity is stable across calls — that is its whole job.
        self.assertEqual(ident, rusage.proc_identity(pid))

    def test_inaccessible_pid_returns_none(self):
        self.assertIsNone(rusage.rusage(2 ** 30))
        self.assertIsNone(rusage.proc_diskio(2 ** 30))


class TestRankIO(unittest.TestCase):
    def test_rates_and_exclusions(self):
        prev = {(10, 111): (1000, 2000), (20, 222): (500, 500), (30, 333): (9, 9)}
        cur = {(10, 111): (3000, 2000), (20, 222): (500, 500), (30, 333): (9, 9)}
        rows, sys_dr, sys_dw = disk.rank_io(prev, cur, 2.0)
        # Only pid 10 did I/O; idle processes are excluded from rows.
        self.assertEqual(len(rows), 1)
        total, dr, dw, r, w, pid, _name = rows[0]
        self.assertEqual((pid, dr, dw, r, w), (10, 1000.0, 0.0, 3000, 2000))
        self.assertEqual((sys_dr, sys_dw), (1000.0, 0.0))

    def test_negative_delta_clamped(self):
        # Counters are cumulative; a lower current value (shouldn't happen
        # within one identity, but belt-and-braces) clamps to zero.
        prev = {(10, 111): (5000, 5000)}
        cur = {(10, 111): (4000, 6000)}
        rows, sys_dr, sys_dw = disk.rank_io(prev, cur, 1.0)
        self.assertEqual(rows[0][1], 0.0)     # read rate clamped
        self.assertEqual(rows[0][2], 1000.0)  # write rate honest
        self.assertEqual(sys_dr, 0.0)

    def test_new_process_baselined_to_zero(self):
        # A key absent from prev contributes no rate its first interval.
        rows, sys_dr, sys_dw = disk.rank_io({}, {(10, 111): (1234, 5678)}, 1.0)
        self.assertEqual(rows, [])
        self.assertEqual((sys_dr, sys_dw), (0.0, 0.0))

    def test_pid_reuse_is_a_new_identity(self):
        # Same pid, new start_abstime: the huge counter of the dead process
        # must not be inherited as a rate spike (S10).
        prev = {(10, 111): (10 ** 12, 0)}
        cur = {(10, 999): (4096, 0)}
        rows, sys_dr, sys_dw = disk.rank_io(prev, cur, 1.0)
        # The new identity baselines to its own counters: no rows, no rate.
        self.assertEqual(rows, [])
        self.assertEqual((sys_dr, sys_dw), (0.0, 0.0))

    def test_zero_dt_defaults_to_one_second(self):
        prev = {(10, 111): (0, 0)}
        cur = {(10, 111): (100, 0)}
        rows, _, _ = disk.rank_io(prev, cur, 0)
        self.assertEqual(rows[0][1], 100.0)

    def test_snapshot_matches_live(self):
        snap = disk.snapshot_diskio()
        self.assertGreater(len(snap), 0)
        key = rusage.proc_identity(os.getpid())
        self.assertIn(key, snap)


class TestCpuSample(unittest.TestCase):
    def test_identity_matches_proc_identity(self):
        pid = os.getpid()
        identity, user_ns, system_ns, energy_nj = rusage.proc_cpu_sample(pid)
        self.assertEqual(identity, rusage.proc_identity(pid))
        self.assertGreater(user_ns, 0)
        self.assertGreaterEqual(system_ns, 0)
        if rusage.HAS_V6:
            self.assertIsInstance(energy_nj, int)
        else:
            self.assertIsNone(energy_nj)

    def test_burn_tracks_wall_clock(self):
        # slow (~0.3 s): the S2 regression on the cpu-scope path — the
        # sample's converted delta must track wall clock, not raw ticks.
        pid = os.getpid()
        _, u0, s0, _ = rusage.proc_cpu_sample(pid)
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 0.3:
            pass
        wall = time.perf_counter() - t0
        _, u1, s1, _ = rusage.proc_cpu_sample(pid)
        delta_s = (u1 + s1 - u0 - s0) / 1e9
        self.assertLess(abs(delta_s - wall) / wall, 0.30)

    def test_inaccessible_pid_returns_none(self):
        self.assertIsNone(rusage.proc_cpu_sample(2 ** 30))


class TestRankCPU(unittest.TestCase):
    def test_rates_and_exclusions(self):
        # pid 10 burned 1 s of CPU (0.6 user / 0.4 sys) and 2 J over 2 s.
        prev = {(10, 111): (1_000_000_000, 500_000_000, 1_000_000_000),
                (20, 222): (7_000_000_000, 0, 5_000_000_000)}
        cur = {(10, 111): (1_600_000_000, 900_000_000, 3_000_000_000),
               (20, 222): (7_000_000_000, 0, 5_000_000_000)}
        rows, sys_cpu, sys_watts = cpu_scope.rank_cpu(prev, cur, 2.0)
        # Only pid 10 was active; idle processes are excluded from rows.
        self.assertEqual(len(rows), 1)
        cpu_pct, u_pct, s_pct, watts, total_ns, start, pid, _name = rows[0]
        self.assertEqual(pid, 10)
        self.assertEqual(start, 111)
        self.assertAlmostEqual(cpu_pct, 50.0)
        self.assertAlmostEqual(u_pct, 30.0)
        self.assertAlmostEqual(s_pct, 20.0)
        self.assertAlmostEqual(watts, 1.0)
        self.assertEqual(total_ns, 2_500_000_000)
        self.assertAlmostEqual(sys_cpu, 50.0)
        self.assertAlmostEqual(sys_watts, 1.0)

    def test_no_energy_ledger_yields_none_watts(self):
        prev = {(10, 111): (0, 0, None)}
        cur = {(10, 111): (1_000_000_000, 0, None)}
        rows, sys_cpu, sys_watts = cpu_scope.rank_cpu(prev, cur, 1.0)
        self.assertIsNone(rows[0][3])
        self.assertIsNone(sys_watts)
        self.assertAlmostEqual(sys_cpu, 100.0)

    def test_negative_delta_clamped(self):
        prev = {(10, 111): (5_000_000_000, 0, 100)}
        cur = {(10, 111): (4_000_000_000, 1_000_000_000, 50)}
        rows, sys_cpu, sys_watts = cpu_scope.rank_cpu(prev, cur, 1.0)
        self.assertAlmostEqual(rows[0][1], 0.0)     # user rate clamped
        self.assertAlmostEqual(rows[0][2], 100.0)   # system rate honest
        self.assertAlmostEqual(sys_watts, 0.0)      # energy clamped too

    def test_new_process_baselined_to_zero(self):
        rows, sys_cpu, sys_watts = cpu_scope.rank_cpu(
            {}, {(10, 111): (1_000_000_000, 0, 500)}, 1.0)
        self.assertEqual(rows, [])
        self.assertAlmostEqual(sys_cpu, 0.0)

    def test_pid_reuse_is_a_new_identity(self):
        # Same pid, new start_abstime: the huge counter of the dead process
        # must not be inherited as a rate spike (S10).
        prev = {(10, 111): (10 ** 15, 0, 0)}
        cur = {(10, 999): (4096, 0, 0)}
        rows, sys_cpu, _ = cpu_scope.rank_cpu(prev, cur, 1.0)
        self.assertEqual(rows, [])
        self.assertAlmostEqual(sys_cpu, 0.0)

    def test_zero_dt_defaults_to_one_second(self):
        prev = {(10, 111): (0, 0, None)}
        cur = {(10, 111): (500_000_000, 0, None)}
        rows, _, _ = cpu_scope.rank_cpu(prev, cur, 0)
        self.assertAlmostEqual(rows[0][0], 50.0)

    def test_lifetime_duty(self):
        # 1 s of CPU over 4 s awake (in tick units the converter maps 1:1
        # only when numer == denom, so derive ticks from the real timebase).
        n, d = rusage.TIMEBASE_NUMER, rusage.TIMEBASE_DENOM
        awake_ticks = 4 * 10 ** 9 * d // n
        duty = cpu_scope.lifetime_duty_pct(1_000_000_000, 1000, 1000 + awake_ticks)
        self.assertAlmostEqual(duty, 25.0, places=1)

    def test_snapshot_matches_live(self):
        snap = cpu_scope.snapshot_cpu()
        self.assertGreater(len(snap), 0)
        key = rusage.proc_identity(os.getpid())
        self.assertIn(key, snap)


class TestSignedDecode(unittest.TestCase):
    def test_ioreg_negative_amperage(self):
        # The exact value Appendix A verified on this machine's ioreg output.
        self.assertEqual(signed64(18446744073709540666), -10950)

    def test_positive_passthrough(self):
        self.assertEqual(signed64(3210), 3210)
        self.assertEqual(signed64(0), 0)

    def test_boundaries(self):
        self.assertEqual(signed64((1 << 63) - 1), (1 << 63) - 1)
        self.assertEqual(signed64(1 << 63), -(1 << 63))
        self.assertEqual(signed64((1 << 64) - 1), -1)


if __name__ == "__main__":
    unittest.main()
