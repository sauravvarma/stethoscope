"""
stethoscope tests: the gpu scope and its two probes, stdlib unittest only.

    python3 -m unittest discover tests

Pure-function tests pin the diff math and the degrade-to-None contract with
synthetic inputs. Live tests run against this machine's IORegistry and
IOReport and skip, never fail, where the hardware lacks a node.
"""

import ctypes
import os
import re
import subprocess
import time
import unittest

from core import ioaccel, ioreport, rusage
from core.validate import parse_proc_bsdinfo_header, sdk_header_path
from scopes import gpu


# ---------------------------------------------------------------------------
# core.rusage: proc_bsdinfo + proc_state
# ---------------------------------------------------------------------------

class TestProcBSDInfo(unittest.TestCase):
    def test_sizeof(self):
        # 12 u32 + 16 + 32 chars + 5 u32 + i32 + 2 u64 = 136, exactly.
        self.assertEqual(ctypes.sizeof(rusage.ProcBSDInfo), 136)

    def test_fields_match_sdk_header(self):
        header = sdk_header_path("sys/proc_info.h")
        if not header:
            self.skipTest("no SDK header (Command Line Tools absent)")
        parsed = parse_proc_bsdinfo_header(header)
        self.assertEqual([n for n, _ in rusage.ProcBSDInfo._fields_],
                         [n for n, _ in parsed])
        self.assertEqual(ctypes.sizeof(rusage.ProcBSDInfo), sum(sz for _, sz in parsed))

    def test_own_pid_is_alive(self):
        self.assertEqual(rusage.proc_state(os.getpid()), rusage.ALIVE)
        status, err = rusage.proc_status(os.getpid())
        self.assertIn(status, (rusage.SRUN, rusage.SSLEEP))
        self.assertEqual(err, 0)

    def test_nonexistent_pid_is_gone(self):
        self.assertEqual(rusage.proc_state(2 ** 30), rusage.GONE)

    def test_unreaped_child_is_exited_then_gone(self):
        # A real zombie: fork, let the child exit, do not wait. The kernel
        # refuses bsdinfo (ESRCH) but still answers rusage (casebook 0008.11).
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        deadline = time.time() + 2.0
        while rusage.proc_state(pid) == rusage.ALIVE and time.time() < deadline:
            time.sleep(0.01)
        self.assertEqual(rusage.proc_state(pid), rusage.EXITED)
        os.waitpid(pid, 0)
        self.assertEqual(rusage.proc_state(pid), rusage.GONE)

    def test_root_owned_pid_is_unknown_not_gone(self):
        if os.geteuid() == 0:
            self.skipTest("running as root: every pid is readable")
        # launchd is always pid 1, always alive, never ours.
        self.assertEqual(rusage.proc_state(1), rusage.UNKNOWN)


# ---------------------------------------------------------------------------
# core.ioaccel: parsing
# ---------------------------------------------------------------------------

class TestIOAccelParsing(unittest.TestCase):
    def test_parse_creator(self):
        self.assertEqual(ioaccel.parse_creator("pid 5671, GTA5_Enhanced.ex"),
                         (5671, "GTA5_Enhanced.ex"))
        self.assertEqual(ioaccel.parse_creator("pid 592, WindowServer"), (592, "WindowServer"))
        self.assertIsNone(ioaccel.parse_creator("garbage"))
        self.assertIsNone(ioaccel.parse_creator(None))

    def test_unpack_voltage_states(self):
        import struct
        blob = struct.pack("<6I", 0, 0, 338_000_000, 500, 1_620_000_000, 900)
        self.assertEqual(ioaccel.unpack_voltage_states(blob), [0, 338_000_000, 1_620_000_000])
        self.assertEqual(ioaccel.unpack_voltage_states(b"\x00" * 7), [])   # not u32 pairs
        self.assertEqual(ioaccel.unpack_voltage_states(None), [])

    def test_merge_clients_sums_per_pid(self):
        children = [
            {"IOUserClientCreator": "pid 10, app", "CommandQueueCount": 2,
             "AppUsage": [{"accumulatedGPUTime": 100, "lastSubmittedTime": 5},
                          {"accumulatedGPUTime": 50, "lastSubmittedTime": 9}]},
            {"IOUserClientCreator": "pid 10, app", "CommandQueueCount": 1,
             "AppUsage": [{"accumulatedGPUTime": 25, "lastSubmittedTime": 7}]},
            {"IOUserClientCreator": "pid 20, quiet", "AppUsage": []},
            {"IOObjectClass": "NotAClient"},
        ]
        out = {}
        ioaccel._merge_clients(children, out)
        self.assertEqual(out[10], {"name": "app", "clients": 2, "queues": 3,
                                   "gpu_ns": 175, "last_submit_ns": 9})
        self.assertEqual(out[20], {"name": "quiet", "clients": 1, "queues": 0,
                                   "gpu_ns": 0, "last_submit_ns": 0})
        self.assertEqual(set(out), {10, 20})


class TestIOAccelLive(unittest.TestCase):
    def test_snapshot_shape(self):
        snap = ioaccel.gpu_snapshot()
        if snap is None:
            self.skipTest("no IOAccelerator node on this machine")
        dev = snap["devices"][0]
        self.assertEqual(set(dev), {"class", "model", "cores", "util_pct", "renderer_pct",
                                    "tiler_pct", "mem_inuse", "mem_alloc", "restarts"})
        if dev["util_pct"] is not None:
            self.assertTrue(0 <= dev["util_pct"] <= 100)
        # The window server always holds a device client on a desktop Mac.
        self.assertTrue(any(c["name"] == "WindowServer" for c in snap["clients"].values()))

    def test_pstate_table_is_monotonic_and_off_first(self):
        table = ioaccel.gpu_pstate_mhz()
        if not table:
            self.skipTest("pmgr voltage-states absent")
        self.assertEqual(table[0], 0.0)
        self.assertEqual(table[1:], sorted(table[1:]))
        self.assertGreater(table[-1], 300)


# ---------------------------------------------------------------------------
# core.ioreport: derivations (pure) and the live subscription
# ---------------------------------------------------------------------------

def _delta(states, energy_nj=None):
    return {"states": states, "ticks": sum(r for _, r in states), "energy_nj": energy_nj}


class TestIOReportDerivations(unittest.TestCase):
    def test_active_fraction(self):
        d = _delta([("OFF", 25), ("P1", 25), ("P2", 50)])
        self.assertAlmostEqual(ioreport.active_fraction(d), 0.75)
        self.assertIsNone(ioreport.active_fraction(_delta([("P1", 10)])))   # no OFF state
        self.assertIsNone(ioreport.active_fraction(None))
        self.assertIsNone(ioreport.active_fraction(_delta([("OFF", 0)])))    # zero ticks

    def test_active_frequency_weights_by_residency(self):
        d = _delta([("OFF", 1000), ("P1", 25), ("P2", 75)])
        mhz = ioreport.active_frequency_mhz(d, [0, 400, 800])
        self.assertAlmostEqual(mhz, 700.0)          # 0.25*400 + 0.75*800; OFF ignored
        # States past the table (unused upper P-states) are skipped, not zeroed.
        d = _delta([("OFF", 0), ("P1", 50), ("P2", 50), ("P3", 50)])
        self.assertAlmostEqual(ioreport.active_frequency_mhz(d, [0, 400, 800]), 600.0)
        self.assertIsNone(ioreport.active_frequency_mhz(d, []))
        self.assertIsNone(ioreport.active_frequency_mhz(_delta([("OFF", 5)]), [0, 400]))

    def test_state_mhz_keys_on_state_number_not_position(self):
        table = [0, 338, 486, 636]
        self.assertEqual(ioreport.state_mhz("P3", table), 636)
        self.assertIsNone(ioreport.state_mhz("OFF", table))
        self.assertIsNone(ioreport.state_mhz("P9", table))       # past the table
        self.assertIsNone(ioreport.state_mhz("weird", table))
        # An omitted state must not shift the others by one bin.
        d = _delta([("OFF", 0), ("P3", 100)])
        self.assertAlmostEqual(ioreport.active_frequency_mhz(d, table), 636.0)

    def test_watts(self):
        self.assertAlmostEqual(ioreport.watts(_delta([("OFF", 1)], 40_000_000_000), 2.0), 20.0)
        self.assertIsNone(ioreport.watts(_delta([("OFF", 1)], None), 1.0))     # channel absent
        self.assertIsNone(ioreport.watts(None, 1.0))
        self.assertEqual(ioreport.watts(_delta([("OFF", 1)], -5), 1.0), 0.0)  # clamped, not negative


class TestIOReportLive(unittest.TestCase):
    def setUp(self):
        if not ioreport.AVAILABLE:
            self.skipTest("IOReport unavailable: %s" % ioreport.why_unavailable())
        self.rep = ioreport.GPUReporter()
        if not self.rep.available:
            self.skipTest("no GPU channels: %s" % self.rep.reason)

    def test_residency_clock_tracks_wall_time(self):
        # The load-bearing invariant: state residencies sum to 24e6 ticks/s.
        a = self.rep.sample()
        t0 = time.perf_counter()
        time.sleep(0.5)
        b = self.rep.sample()
        wall = time.perf_counter() - t0
        d = self.rep.delta(a, b)
        self.rep.release(a)
        self.rep.release(b)
        self.assertIsNotNone(d)
        self.assertLess(abs(d["ticks"] / ioreport.TICKS_PER_S - wall) / wall, 0.05)
        names = [n for n, _ in d["states"]]
        self.assertEqual(names[0], "OFF")
        self.assertTrue(all(re.match(r"^P\d+$", n) for n in names[1:]))

    def test_energy_channel_present_in_nj(self):
        a = self.rep.sample()
        time.sleep(0.3)
        b = self.rep.sample()
        d = self.rep.delta(a, b)
        self.rep.release(a)
        self.rep.release(b)
        # Present (int), never None, on a machine that publishes GPU Energy.
        self.assertIsInstance(d["energy_nj"], int)
        self.assertGreaterEqual(d["energy_nj"], 0)


# ---------------------------------------------------------------------------
# scopes.gpu: the data layer
# ---------------------------------------------------------------------------

def _client(name, gpu_ns, queues=1, last=0):
    return {"name": name, "clients": 1, "queues": queues, "gpu_ns": gpu_ns, "last_submit_ns": last}


class TestRankClients(unittest.TestCase):
    def test_rates_baseline_and_attribution(self):
        me = os.getpid()
        prev = {me: _client("me", 1_000_000_000, last=10)}
        cur = {me: _client("me", 1_500_000_000, last=10),
               2 ** 30: _client("newcomer", 9_000_000_000, queues=1)}
        rows, attributed = gpu.rank_clients(prev, cur, 2.0, mach_ns=1_000_000_010)
        by_pid = {r[4]: r for r in rows}
        # 0.5 s of GPU over 2 s = 25%; newcomer baselines to zero its first interval.
        self.assertAlmostEqual(by_pid[me][0], 25.0)
        self.assertAlmostEqual(by_pid[2 ** 30][0], 0.0)
        self.assertAlmostEqual(attributed, 25.0)
        # idle_ns from awake-time clock; state from the kernel.
        self.assertEqual(by_pid[me][3], 1_000_000_000)
        self.assertEqual(by_pid[me][6], "")               # alive renders blank
        self.assertEqual(by_pid[2 ** 30][6], "gone")

    def test_pid_reuse_with_new_name_baselines_fresh(self):
        prev = {10: _client("old", 10 ** 15)}
        cur = {10: _client("new", 4096)}
        rows, attributed = gpu.rank_clients(prev, cur, 1.0, 0)
        self.assertAlmostEqual(attributed, 0.0)
        self.assertAlmostEqual(rows[0][0], 0.0)

    def test_negative_delta_and_idle_clamped(self):
        prev = {10: _client("x", 5000, last=0)}
        cur = {10: _client("x", 4000, last=999)}
        rows, _ = gpu.rank_clients(prev, cur, 1.0, mach_ns=500)   # clock behind last_submit
        self.assertAlmostEqual(rows[0][0], 0.0)
        self.assertEqual(rows[0][3], 0)

    def test_idle_clients_without_queues_are_dropped(self):
        me = os.getpid()
        cur = {me: _client("me", 0, queues=0)}
        rows, _ = gpu.rank_clients(cur, cur, 1.0, 0)
        self.assertEqual(rows, [])

    def test_exited_holder_is_kept_even_at_zero(self):
        # The row that diagnoses a leaked context: 0% now, queues held, gone.
        cur = {2 ** 30: _client("leaker", 4_722_950_445_870, queues=40)}
        rows, _ = gpu.rank_clients(cur, cur, 1.0, 0)
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0][2], rows[0][6]), (40, "gone"))

    def test_zero_dt_defaults_to_one_second(self):
        me = os.getpid()
        prev = {me: _client("me", 0)}
        cur = {me: _client("me", 500_000_000)}
        rows, _ = gpu.rank_clients(prev, cur, 0, 0)
        self.assertAlmostEqual(rows[0][0], 50.0)


class _FakeReporter:
    """Stands in for GPUReporter so gpu_vitals can be tested without IOReport."""
    def __init__(self, delta):
        self._d = delta
        self.available = delta is not None
        self.reason = None if self.available else "fake: unavailable"

    def sample(self):
        return object() if self.available else None

    def release(self, ref):
        pass

    def delta(self, prev, cur):
        return self._d


def _snap(t, clients, util=100, ior=object()):
    return {"t": t, "mach_ns": 10 ** 12,
            "acc": {"devices": [{"class": "X", "model": "Fake GPU", "cores": 8,
                                 "util_pct": util, "renderer_pct": util, "tiler_pct": util,
                                 "mem_inuse": 8 * 2 ** 30, "mem_alloc": 10 * 2 ** 30,
                                 "restarts": 0}],
                    "clients": clients},
            "ior": ior}


class TestGpuVitals(unittest.TestCase):
    def test_unattributed_is_active_minus_live_shares(self):
        me = os.getpid()
        prev = _snap(0.0, {me: _client("me", 0)})
        cur = _snap(1.0, {me: _client("me", 140_000_000)})               # 14%
        rep = _FakeReporter(_delta([("OFF", 0), ("P11", 1_200_000), ("P12", 1_200_000)],
                                   energy_nj=36_500_000_000))
        v = gpu.gpu_vitals(prev, cur, rep, [0] + [100 * i for i in range(1, 14)])
        self.assertAlmostEqual(v["active_pct"], 100.0)
        self.assertAlmostEqual(v["attributed_pct"], 14.0)
        self.assertAlmostEqual(v["unattributed_pct"], 86.0)
        self.assertAlmostEqual(v["watts"], 36.5)
        self.assertAlmostEqual(v["freq_mhz"], 1150.0)                     # mean of P11/P12
        self.assertEqual([lbl for lbl, _ in v["pstates"]], ["1100MHz", "1200MHz"])

    def test_unattributed_falls_back_to_device_util_without_ioreport(self):
        me = os.getpid()
        prev = _snap(0.0, {me: _client("me", 0)}, util=60, ior=None)
        cur = _snap(1.0, {me: _client("me", 100_000_000)}, util=60, ior=None)
        v = gpu.gpu_vitals(prev, cur, _FakeReporter(None), [])
        self.assertIsNone(v["active_pct"])
        self.assertIsNone(v["freq_mhz"])
        self.assertIsNone(v["watts"])                                      # never a fake zero
        self.assertEqual(v["pstates"], [])
        self.assertAlmostEqual(v["unattributed_pct"], 50.0)                # 60 - 10

    def test_unattributed_clamps_at_zero(self):
        me = os.getpid()
        prev = _snap(0.0, {me: _client("me", 0)}, util=5, ior=None)
        cur = _snap(1.0, {me: _client("me", 200_000_000)}, util=5, ior=None)   # 20% > 5%
        v = gpu.gpu_vitals(prev, cur, _FakeReporter(None), [])
        self.assertEqual(v["unattributed_pct"], 0.0)

    def test_no_gpu_node(self):
        prev = {"t": 0.0, "mach_ns": 0, "acc": None, "ior": None}
        cur = {"t": 1.0, "mach_ns": 0, "acc": None, "ior": None}
        v = gpu.gpu_vitals(prev, cur, _FakeReporter(None), [])
        self.assertIsNone(v["device"])
        self.assertIsNone(v["unattributed_pct"])
        self.assertEqual(v["rows"], [])
        lines = gpu.render_top(v, 1.0, 20)
        self.assertTrue(any("no IOAccelerator node" in ln for ln in lines))

    def test_render_marks_leak_and_dashes(self):
        me = os.getpid()
        prev = _snap(0.0, {me: _client("me", 0)}, ior=None)
        cur = _snap(1.0, {me: _client("me", 0), 2 ** 30: _client("leaker", 10 ** 12, queues=40)}, ior=None)
        v = gpu.gpu_vitals(prev, cur, _FakeReporter(None), [])
        text = "\n".join(gpu.render_top(v, 1.0, 20, reporter_reason="fake: unavailable"))
        self.assertIn("unattributed 100.0%", text)
        self.assertIn("leaked context?", text)
        self.assertIn("active - @ -   -   memory", text)                   # dashes, not zeros
        self.assertIn("IOReport unavailable (fake: unavailable)", text)
        self.assertIn("leaker", text)
        self.assertIn("gone", text)


class TestGpuFormatting(unittest.TestCase):
    def test_dur_str(self):
        self.assertEqual(gpu.dur_str(None), "-")
        self.assertEqual(gpu.dur_str(3.2e9), "3.2s")
        self.assertEqual(gpu.dur_str(725e9), "12m05s")
        self.assertEqual(gpu.dur_str(4_722_950_445_870), "1h18m")

    def test_bytes_str(self):
        self.assertEqual(gpu.bytes_str(None), "-")
        self.assertEqual(gpu.bytes_str(512), "512B")
        self.assertEqual(gpu.bytes_str(8_791_375_872), "8.2G")

    def test_pct_and_opt(self):
        self.assertEqual(gpu.pct_str(None), "-")
        self.assertEqual(gpu.pct_str(86.04, 1), "86.0%")
        self.assertEqual(gpu.opt_str("%.1f W", None), "-")
        self.assertEqual(gpu.opt_str("%.0f MHz", 1534.4), "1534 MHz")


class TestGpuCLI(unittest.TestCase):
    def test_dispatcher_help_lists_gpu(self):
        out = subprocess.run(["./stethoscope", "--help"], capture_output=True, text=True).stdout
        self.assertIn("gpu     GPU visibility", out)

    def test_scope_help(self):
        out = subprocess.run(["./stethoscope", "gpu", "--help"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 0)
        self.assertIn("unattributed", out.stdout)

    def test_unknown_mode_exits_2(self):
        out = subprocess.run(["./stethoscope", "gpu", "bogus"], capture_output=True, text=True)
        self.assertEqual(out.returncode, 2)


if __name__ == "__main__":
    unittest.main()
