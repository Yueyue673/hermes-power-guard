from __future__ import annotations

import asyncio
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD = PLUGIN_ROOT / "dashboard" / "plugin_api.py"
os.environ["POWER_GUARD_DISABLE_MONITOR"] = "1"
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import power_guard_core as core


class PowerGuardApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        spec = importlib.util.spec_from_file_location("power_guard_plugin_api_test", DASHBOARD)
        cls.api = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(cls.api)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="power-guard-api-")
        self.old = (core.STATE_DIR, core.DB_PATH, core.LOG_PATH, core._MONITOR_STARTED)
        core.STATE_DIR = Path(self.temp.name)
        core.DB_PATH = core.STATE_DIR / "state.db"
        core.LOG_PATH = core.STATE_DIR / "power-guard.log"
        core._MONITOR_STARTED = True
        core.reset_for_tests()
        self.assertIs(self.api.core, core)

    def tearDown(self):
        core.STATE_DIR, core.DB_PATH, core.LOG_PATH, core._MONITOR_STARTED = self.old
        self.temp.cleanup()

    def run_async(self, awaitable):
        return asyncio.run(awaitable)

    def test_status_and_settings_routes(self):
        status = self.run_async(self.api.status())
        self.assertTrue(status["ok"])
        heartbeat = self.run_async(self.api.ui_heartbeat({"busy_count": 2}))
        self.assertEqual(heartbeat["runtime"]["desktop_busy_count"], 2)
        updated = self.run_async(self.api.settings({"action": "hibernate", "dry_run": True}))
        self.assertEqual(updated["settings"]["action"], "hibernate")
        self.assertTrue(updated["settings"]["dry_run"])

    def test_arm_cancel_and_simulated_test_routes(self):
        armed = self.run_async(self.api.arm({"require_user_idle": False}))
        self.assertTrue(armed["settings"]["armed"])
        snoozed = self.run_async(self.api.snooze({"minutes": 15}))
        self.assertEqual(snoozed["runtime"]["state"], "snoozed")
        resumed = self.run_async(self.api.resume())
        self.assertEqual(resumed["runtime"]["state"], "armed_waiting_for_quiet")
        cancelled = self.run_async(self.api.cancel({"reason": "api-test"}))
        self.assertFalse(cancelled["settings"]["armed"])
        test = self.run_async(self.api.test_countdown({"seconds": 3}))
        self.assertTrue(test["runtime"]["test_only"])
        self.assertEqual(test["runtime"]["state"], "countdown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
