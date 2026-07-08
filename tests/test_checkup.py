"""Unit tests for the checkup scope (scopes/checkup.py)."""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scopes"))
import checkup  # noqa: E402


class TestRunCheckup(unittest.TestCase):
    def _patches(self, cpu_v=None, mem=None, bat=None, drives=None):
        cpu_v = cpu_v or ({"system_cpu_pct": 10.0, "ncpu": 8, "top": None}, [])
        mem = mem or ({"pressure": "normal", "used": 1, "total": 2, "used_pct": 50.0}, [])
        bat = bat or ({"present": False}, [])
        drives = drives or ({"drives": []}, [])
        return (
            mock.patch.object(checkup, "_check_cpu", return_value=cpu_v),
            mock.patch.object(checkup, "_check_memory", return_value=mem),
            mock.patch.object(checkup, "_check_battery", return_value=bat),
            mock.patch.object(checkup, "_check_smart", return_value=drives),
        )

    def test_all_clear(self):
        ps = self._patches()
        for p in ps:
            p.start()
        try:
            r = checkup.run_checkup()
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r["overall"], "ok")
        self.assertEqual(r["findings"], [])

    def test_critical_dominates_and_sorts_first(self):
        smart_f = ({"drives": []}, [checkup._finding("critical", "smart", "disk0: FAILING")])
        mem_f = ({"pressure": "warn", "used": 1, "total": 2}, [
            checkup._finding("warn", "memory", "pressure elevated")])
        ps = self._patches(mem=mem_f, drives=smart_f)
        for p in ps:
            p.start()
        try:
            r = checkup.run_checkup()
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r["overall"], "critical")
        self.assertEqual(r["findings"][0]["severity"], "critical")   # sorted worst-first

    def test_info_findings_keep_overall_ok(self):
        cpu_f = ({"system_cpu_pct": 95.0, "ncpu": 8,
                  "top": {"pid": 1, "name": "x", "cpu_pct": 95.0}},
                 [checkup._finding("info", "cpu", "x is hot")])
        ps = self._patches(cpu_v=cpu_f)
        for p in ps:
            p.start()
        try:
            r = checkup.run_checkup()
        finally:
            for p in ps:
                p.stop()
        self.assertEqual(r["overall"], "ok")            # info doesn't make it "unwell"
        self.assertEqual(len(r["findings"]), 1)

    def test_broken_probe_is_contained(self):
        boom = mock.patch.object(checkup, "_check_smart", side_effect=RuntimeError("nope"))
        others = self._patches()[:3]
        for p in others:
            p.start()
        boom.start()
        try:
            r = checkup.run_checkup()
        finally:
            boom.stop()
            for p in others:
                p.stop()
        self.assertTrue(any(f["area"] == "smart" for f in r["findings"]))
        self.assertIn("error", r["vitals"]["smart"])


class TestCheckMemory(unittest.TestCase):
    def test_critical_pressure_finding(self):
        with mock.patch.object(checkup.memory, "system_memory",
                               return_value={"pressure": "critical", "used": 8, "total": 8}):
            v, f = checkup._check_memory()
        self.assertEqual(v["pressure"], "critical")
        self.assertEqual(f[0]["severity"], "critical")


if __name__ == "__main__":
    unittest.main()
