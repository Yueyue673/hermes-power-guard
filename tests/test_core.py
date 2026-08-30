from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

PLUGIN_ROOT = Path(__file__).resolve().parents[1]
if str(PLUGIN_ROOT) not in sys.path:
    sys.path.insert(0, str(PLUGIN_ROOT))

import power_guard_core as core


class PowerGuardCoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="power-guard-test-")
        self.state_dir = Path(self.temp.name)
        self.old_state_dir = core.STATE_DIR
        self.old_db_path = core.DB_PATH
        self.old_log_path = core.LOG_PATH
        self.old_monitor_started = core._MONITOR_STARTED
        core.STATE_DIR = self.state_dir
        core.DB_PATH = self.state_dir / "state.db"
        core.LOG_PATH = self.state_dir / "power-guard.log"
        # Unit tests drive ticks deterministically; never start daemon monitors.
        # Keep countdown-input tests deterministic on non-Windows CI; tests that
        # exercise active/missing input sensors override this mock explicitly.
        self.idle_patcher = mock.patch.object(core, "_user_idle_seconds", return_value=600)
        self.idle_patcher.start()
        core._MONITOR_STARTED = True
        core.reset_for_tests()

    def tearDown(self) -> None:
        self.idle_patcher.stop()
        core._MONITOR_STARTED = self.old_monitor_started
        core.STATE_DIR = self.old_state_dir
        core.DB_PATH = self.old_db_path
        core.LOG_PATH = self.old_log_path
        self.temp.cleanup()

    def update(self, **values):
        return core.update_settings(values)

    def arm_ready(self, overrides=None):
        core.record_ui_heartbeat()
        return core.arm(overrides or {})

    def terminal_task(self, *, response: str = "done", completed: bool = True, failed: bool = False, interrupted: bool = False, reason: str = "text_response(stop)"):
        core.record_turn_start(session_id="session-a", task_id="task-a", turn_id="turn-a", platform_name="desktop")
        core.record_response(session_id="session-a", task_id="task-a", turn_id="turn-a", response=response)
        core.finish_turn(
            session_id="session-a",
            task_id="task-a",
            turn_id="turn-a",
            completed=completed,
            failed=failed,
            interrupted=interrupted,
            turn_exit_reason=reason,
        )

    def make_due(self):
        with core._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            runtime = core._meta_get(conn, "runtime")
            runtime["countdown_due"] = time.time() - 1
            core._meta_set(conn, "runtime", runtime)
            conn.commit()

    def enter_countdown(self):
        self.assertIsNone(core._evaluate())
        with core._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            runtime = core._meta_get(conn, "runtime")
            settings = core.validate_settings(core._meta_get(conn, "settings") or {})
            runtime["quiet_since"] = time.time() - int(settings["quiescence_seconds"]) - 1
            core._meta_set(conn, "runtime", runtime)
            conn.commit()
        self.assertIsNone(core._evaluate())
        self.assertEqual(core.get_status()["runtime"]["state"], "countdown")

    def test_defaults_are_disarmed_and_safe(self):
        status = core.get_status()
        self.assertFalse(status["settings"]["armed"])
        self.assertTrue(status["settings"]["require_user_idle"])
        self.assertTrue(status["settings"]["cancel_on_new_activity"])
        self.assertFalse(status["settings"]["include_interrupted"])
        self.assertFalse(status["settings"]["detect_blocker_text"])
        self.assertGreaterEqual(status["settings"]["quiescence_seconds"], 30)
        self.assertGreaterEqual(status["settings"]["countdown_seconds"], 60)
        self.assertGreaterEqual(status["settings"]["arm_expiry_minutes"], 30)
        self.assertEqual(status["runtime"]["state"], "disarmed")

    def test_validation_clamps_and_filters(self):
        settings = self.update(
            action="not-real",
            countdown_seconds=1,
            stale_task_seconds=2,
            protected_processes=" Blender.EXE ; obs64.exe ",
            blocker_keywords="需要登录\ncannot proceed",
        )
        self.assertEqual(settings["action"], "sleep")
        self.assertEqual(settings["countdown_seconds"], 60)
        self.assertEqual(settings["stale_task_seconds"], 300)
        self.assertEqual(settings["protected_processes"], ["blender.exe", "obs64.exe"])
        self.assertEqual(settings["blocker_keywords"], ["需要登录", "cannot proceed"])

    def test_arm_requires_post_arm_activity(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0})
        claim = core._evaluate()
        status = core.get_status()
        self.assertIsNone(claim)
        self.assertEqual(status["runtime"]["state"], "armed_waiting_for_task")
        self.assertFalse(status["runtime"]["activity_seen"])

    def test_arm_captures_current_running_session(self):
        core.record_turn_start(
            session_id="current-session",
            task_id="current-task",
            turn_id="current-turn",
            platform_name="desktop",
            profile="default",
        )
        core.record_ui_heartbeat()
        armed = core.arm(
            {"require_user_idle": False},
            current_session_id="current-session",
            current_profile="default",
        )
        self.assertEqual(armed["runtime"]["state"], "running")
        self.assertTrue(armed["runtime"]["activity_seen"])
        self.assertEqual(armed["runtime"]["scope_mode"], "current")
        self.assertEqual(armed["runtime"]["scope_session_id"], "current-session")
        self.assertEqual(armed["runtime"]["captured_task_count"], 1)
        with core._connect() as conn:
            row = conn.execute(
                "SELECT generation FROM tasks WHERE session_id='current-session'"
            ).fetchone()
        self.assertEqual(int(row[0]), armed["runtime"]["generation"])
        core.record_response(
            session_id="current-session",
            task_id="current-task",
            turn_id="current-turn",
            profile="default",
            response="done",
        )
        core.finish_turn(
            session_id="current-session",
            task_id="current-task",
            turn_id="current-turn",
            profile="default",
            completed=True,
            turn_exit_reason="text_response(stop)",
        )
        core._evaluate()
        self.assertEqual(core.get_status()["runtime"]["state"], "quiescence")

    def test_arm_does_not_capture_other_running_session(self):
        core.record_turn_start(
            session_id="other-session",
            task_id="other-task",
            turn_id="other-turn",
            platform_name="desktop",
            profile="default",
        )
        core.record_ui_heartbeat()
        armed = core.arm(
            {"require_user_idle": False},
            current_session_id="current-session",
            current_profile="default",
        )
        self.assertEqual(armed["runtime"]["state"], "armed_waiting_for_task")
        self.assertFalse(armed["runtime"]["activity_seen"])
        self.assertEqual(armed["runtime"]["scope_mode"], "next")
        self.assertEqual(armed["runtime"]["captured_task_count"], 0)

    def test_current_project_process_is_not_hidden_in_old_daemon_baseline(self):
        core.record_turn_start(
            session_id="current-session",
            task_id="current-task",
            turn_id="current-turn",
            platform_name="desktop",
            profile="default",
        )
        core.record_ui_heartbeat()
        detail = {
            "source": "process",
            "id": "project-worker",
            "command": "project worker",
            "started_at": time.time() - 300,
            "units": 1,
        }
        with mock.patch.object(
            core, "_background_snapshot", return_value=(1, 0, [detail])
        ), mock.patch.object(
            core, "_scope_process_keys", return_value={"process:project-worker"}
        ):
            armed = core.arm(
                {"require_user_idle": False},
                current_session_id="current-session",
                current_profile="default",
            )
        self.assertNotIn("process:project-worker", armed["runtime"]["background_baseline"])

    def test_missing_desktop_ui_heartbeat_blocks_terminal_campaign(self):
        core.arm({"require_user_idle": False, "quiescence_seconds": 0})
        self.terminal_task()
        core._evaluate()
        self.assertEqual(core.get_status()["runtime"]["state"], "waiting_for_desktop_ui")

    def test_completed_task_enters_countdown_then_single_simulated_claim(self):
        self.arm_ready({
            "require_user_idle": False,
            "quiescence_seconds": 0,
            "countdown_seconds": 10,
            "dry_run": True,
        })
        self.terminal_task()
        self.enter_countdown()
        status = core.get_status()
        self.assertEqual(status["runtime"]["state"], "countdown")
        self.make_due()

        with ThreadPoolExecutor(max_workers=8) as pool:
            claims = list(pool.map(lambda _: core._evaluate(), range(20)))
        claims = [claim for claim in claims if claim]
        self.assertEqual(len(claims), 1)
        self.assertTrue(claims[0]["simulate"])
        ok, message = core._execute_power_action(claims[0]["action"], claims[0]["simulate"])
        self.assertTrue(ok)
        self.assertIn("SIMULATED", message)
        core._finalize_action(claims[0], ok, message)
        status = core.get_status()
        self.assertEqual(status["runtime"]["state"], "action_complete")
        self.assertTrue(status["runtime"]["last_action"]["simulated"])

    def test_new_turn_cancels_countdown(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0, "dry_run": True})
        self.terminal_task()
        self.enter_countdown()
        self.assertEqual(core.get_status()["runtime"]["state"], "countdown")
        core.record_turn_start(session_id="session-b", task_id="task-b", turn_id="turn-b")
        runtime = core.get_status()["runtime"]
        self.assertEqual(runtime["state"], "running")
        self.assertIsNone(runtime["countdown_due"])

    def test_settings_change_invalidates_countdown_snapshot(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0, "dry_run": True})
        self.terminal_task()
        self.enter_countdown()
        self.assertEqual(core.get_status()["runtime"]["state"], "countdown")
        core.update_settings({"action": "lock"})
        status = core.get_status()
        self.assertFalse(status["settings"]["armed"])
        self.assertEqual(status["runtime"]["state"], "disarmed")
        self.assertIsNone(status["runtime"]["countdown_due"])

    def test_gateway_activity_cancels_countdown_before_turn_start(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0, "dry_run": True})
        self.terminal_task()
        self.enter_countdown()
        self.assertEqual(core.get_status()["runtime"]["state"], "countdown")
        core.record_external_activity("test_gateway_message")
        runtime = core.get_status()["runtime"]
        self.assertEqual(runtime["state"], "armed_waiting_for_quiet")
        self.assertIsNone(runtime["countdown_due"])

    def test_snooze_and_resume_recompute_safely(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 30})
        self.terminal_task()
        snoozed = core.snooze(15)
        self.assertEqual(snoozed["runtime"]["state"], "snoozed")
        self.assertGreater(snoozed["runtime"]["snooze_remaining_seconds"], 800)
        core._evaluate()
        self.assertEqual(core.get_status()["runtime"]["state"], "snoozed")
        resumed = core.resume_snooze()
        self.assertEqual(resumed["runtime"]["state"], "armed_waiting_for_quiet")
        self.assertIsNone(resumed["runtime"]["snooze_until"])

    def test_armed_campaign_expires_fail_closed(self):
        self.arm_ready({"require_user_idle": False, "arm_expiry_minutes": 30})
        with core._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            runtime = core._meta_get(conn, "runtime")
            runtime["armed_expires_at"] = time.time() - 1
            core._meta_set(conn, "runtime", runtime)
            conn.commit()
        core._evaluate()
        status = core.get_status()
        self.assertFalse(status["settings"]["armed"])
        self.assertEqual(status["runtime"]["state"], "expired")

    def test_decision_snapshot_explains_current_gate(self):
        status = self.arm_ready({"require_user_idle": False})
        self.assertEqual(status["capabilities"]["ui_contract_version"], 2)
        self.assertEqual(status["decision"]["state"], "armed_waiting_for_task")
        self.assertEqual(status["decision"]["summary"], "等待下一个 Hermes 任务")
        self.assertEqual(status["decision"]["next"], "无需操作；开始下一个任务后自动跟踪。")
        self.assertEqual(len(status["decision"]["gates"]), 7)
        visible_copy = json.dumps(status["decision"], ensure_ascii=False)
        self.assertNotIn("武装", visible_copy)
        self.assertNotIn("安全门", visible_copy)
        self.assertNotIn("终态", visible_copy)

    def test_new_turn_can_abort_a_claim_before_os_call(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 30, "dry_run": False})
        self.terminal_task()
        self.enter_countdown()
        self.make_due()
        with mock.patch.dict(os.environ, {"HERMES_DESKTOP": "1"}, clear=False):
            claim = core._evaluate()
        self.assertIsNotNone(claim)
        self.assertFalse(claim["simulate"])
        core.record_turn_start(session_id="new-s", task_id="new-t", turn_id="new-u")
        self.assertFalse(core._claim_still_safe(claim))
        runtime = core.get_status()["runtime"]
        self.assertEqual(runtime["state"], "disarmed")
        self.assertIsNone(runtime["countdown_token"] or None)

    def test_blocker_text_is_terminal_in_done_or_blocked(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0, "detect_blocker_text": True})
        self.terminal_task(response="请先登录账号，我才能继续。")
        task = core.get_status()["tasks"][0]
        self.assertEqual(task["outcome"], "blocked")
        self.assertIn("请先登录", task["reason"])
        self.enter_countdown()
        self.assertEqual(core.get_status()["runtime"]["state"], "countdown")

    def test_done_only_refuses_blocked_terminal(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0, "trigger_mode": "done_only"})
        self.terminal_task(completed=False, failed=False, reason="max_iterations_reached(100)")
        core._evaluate()
        self.assertEqual(core.get_status()["runtime"]["state"], "terminal_not_eligible")

    def test_interruption_is_ineligible_by_default(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0})
        self.terminal_task(completed=False, interrupted=True, reason="interrupted_during_api_call")
        core._evaluate()
        self.assertEqual(core.get_status()["runtime"]["state"], "terminal_not_eligible")

    def test_unknown_exit_reason_is_hard_veto(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0, "include_failures": True})
        self.terminal_task(completed=True, reason="future_reason_not_in_contract")
        core._evaluate()
        status = core.get_status()
        self.assertEqual(status["tasks"][0]["outcome"], "unknown")
        self.assertEqual(status["runtime"]["state"], "terminal_not_eligible")

    def test_late_tool_event_cannot_reopen_terminal_turn(self):
        self.arm_ready({"require_user_idle": False})
        self.terminal_task()
        core.record_tool_state(
            session_id="session-a",
            task_id="task-a",
            turn_id="turn-a",
            tool_name="late_tool",
            waiting=False,
        )
        task = core.get_status()["tasks"][0]
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["outcome"], "completed")

    def test_user_activity_gate(self):
        self.arm_ready({"require_user_idle": True, "user_idle_seconds": 300, "quiescence_seconds": 0})
        self.terminal_task()
        with mock.patch.object(core, "_user_idle_seconds", return_value=10):
            core._evaluate()
            self.assertEqual(core.get_status()["runtime"]["state"], "waiting_for_user_idle")
        with mock.patch.object(core, "_user_idle_seconds", return_value=600):
            core._evaluate()
            self.assertEqual(core.get_status()["runtime"]["state"], "quiescence")

    def test_background_heartbeat_blocks_countdown(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0})
        self.terminal_task()
        with core._connect() as conn:
            conn.execute(
                "INSERT INTO heartbeats(owner_instance,owner_pid,updated_at,active_processes,active_delegations,process_details) "
                "VALUES('worker',999,?,1,2,'[]')",
                (time.time(),),
            )
        core._evaluate()
        status = core.get_status()
        self.assertEqual(status["runtime"]["state"], "running")
        self.assertEqual(status["runtime"]["active_background_processes"], 1)
        self.assertEqual(status["runtime"]["active_delegations"], 2)

    def test_duplicate_background_heartbeats_count_one_process_once(self):
        self.arm_ready({
            "background_process_policy": "wait_all",
            "require_user_idle": False,
            "quiescence_seconds": 0,
        })
        self.terminal_task()
        details = json.dumps([
            {"source": "process", "id": "preview", "command": "python server.py", "units": 1}
        ])
        with core._connect() as conn:
            for owner in ("worker-a", "worker-b"):
                conn.execute(
                    "INSERT INTO heartbeats(owner_instance,owner_pid,updated_at,active_processes,active_delegations,process_details) "
                    "VALUES(?,?,?,1,0,?)",
                    (owner, 999, time.time(), details),
                )
        core._evaluate()
        status = core.get_status()
        self.assertEqual(status["runtime"]["state"], "running")
        self.assertEqual(status["runtime"]["active_background_processes"], 1)
        self.assertEqual(len(status["runtime"]["background_details"]), 1)

    def test_preexisting_background_process_is_baselined_by_default(self):
        before_arm = time.time() - 120
        details = json.dumps([
            {
                "source": "process",
                "id": "preview",
                "command": "python server.py",
                "started_at": before_arm,
                "units": 1,
            }
        ])
        with core._connect() as conn:
            conn.execute(
                "INSERT INTO heartbeats(owner_instance,owner_pid,updated_at,active_processes,active_delegations,process_details) "
                "VALUES('worker',999,?,1,0,?)",
                (time.time(), details),
            )
        armed = self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0})
        self.assertEqual(armed["settings"]["background_process_policy"], "wait_new")
        self.assertEqual(armed["runtime"]["background_baseline"]["process:preview"], 1)
        self.terminal_task()
        core._evaluate()
        status = core.get_status()
        self.assertEqual(status["runtime"]["active_background_processes"], 0)
        self.assertNotEqual(status["runtime"]["state"], "running")

        with core._connect() as conn:
            conn.execute(
                "UPDATE heartbeats SET updated_at=?,active_processes=0,process_details='[]' "
                "WHERE owner_instance='worker'",
                (time.time(),),
            )
        core._evaluate()
        self.assertEqual(core.get_status()["runtime"]["background_baseline"]["process:preview"], 1)

        restarted = json.dumps([
            {
                "source": "process",
                "id": "preview",
                "command": "python server.py",
                "started_at": float(armed["runtime"]["armed_at"]) + 10,
                "units": 1,
            }
        ])
        with core._connect() as conn:
            conn.execute(
                "UPDATE heartbeats SET updated_at=?,active_processes=1,process_details=? "
                "WHERE owner_instance='worker'",
                (time.time(), restarted),
            )
        core._evaluate()
        restarted_status = core.get_status()
        self.assertEqual(restarted_status["runtime"]["state"], "running")
        self.assertEqual(restarted_status["runtime"]["active_background_processes"], 1)

    def test_040_explicit_wait_all_is_preserved(self):
        with core._connect() as conn:
            settings = core._meta_get(conn, "settings")
            settings["background_process_policy"] = "wait_all"
            core._meta_set(conn, "settings", settings)
            conn.execute("DELETE FROM meta WHERE key='migration_background_policy_041'")
        self.assertEqual(core.get_settings()["background_process_policy"], "wait_all")
        with core._connect() as conn:
            self.assertTrue(core._meta_get(conn, "migration_background_policy_041"))

    def test_background_process_started_after_arm_still_blocks(self):
        armed = self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0})
        self.terminal_task()
        details = json.dumps([
            {
                "source": "process",
                "id": "new-preview",
                "command": "python server.py",
                "started_at": float(armed["runtime"]["armed_at"]) + 1,
                "units": 1,
            }
        ])
        with core._connect() as conn:
            conn.execute(
                "INSERT INTO heartbeats(owner_instance,owner_pid,updated_at,active_processes,active_delegations,process_details) "
                "VALUES('worker',999,?,1,0,?)",
                (time.time(), details),
            )
        core._evaluate()
        status = core.get_status()
        self.assertEqual(status["runtime"]["state"], "running")
        self.assertEqual(status["runtime"]["active_background_processes"], 1)

    def test_preexisting_cron_and_completion_delivery_still_block(self):
        details = json.dumps([
            {"source": "cron", "id": "daily", "command": "cron job", "units": 1},
            {
                "source": "completion_queue",
                "id": "pending",
                "command": "pending completion delivery",
                "units": 1,
            },
        ])
        with core._connect() as conn:
            conn.execute(
                "INSERT INTO heartbeats(owner_instance,owner_pid,updated_at,active_processes,active_delegations,process_details) "
                "VALUES('worker',999,?,1,1,?)",
                (time.time(), details),
            )
        armed = self.arm_ready({"require_user_idle": False, "quiescence_seconds": 0})
        self.assertEqual(armed["runtime"]["background_baseline"], {})
        self.terminal_task()
        core._evaluate()
        status = core.get_status()
        self.assertEqual(status["runtime"]["state"], "running")
        self.assertEqual(status["runtime"]["active_background_processes"], 1)
        self.assertEqual(status["runtime"]["active_delegations"], 1)

    def test_added_units_under_a_baseline_identity_still_block(self):
        runtime = core._fresh_runtime()
        runtime["armed_at"] = 100.0
        runtime["background_baseline"] = {"process:shared": 1}
        scoped = core._scope_background_details(
            {**core.DEFAULT_SETTINGS, "background_process_policy": "wait_new"},
            runtime,
            [
                {
                    "source": "process",
                    "id": "shared",
                    "command": "worker group",
                    "started_at": 99.0,
                    "units": 2,
                }
            ],
        )
        self.assertEqual(len(scoped), 1)
        self.assertEqual(scoped[0]["units"], 1)

    def test_unobserved_process_is_not_baselined_by_coarse_timestamp(self):
        runtime = core._fresh_runtime()
        runtime["armed_at"] = 100.9
        runtime["background_baseline"] = {}
        scoped = core._scope_background_details(
            {**core.DEFAULT_SETTINGS, "background_process_policy": "wait_new"},
            runtime,
            [
                {
                    "source": "process",
                    "id": "unobserved",
                    "started_at": 100.0,
                    "units": 1,
                }
            ],
        )
        self.assertEqual(len(scoped), 1)

    def test_process_start_identity_is_stable_across_samples(self):
        row = {
            "started_at": "2026-08-28T23:59:58",
            "uptime_seconds": 1,
        }
        first = core._process_started_at(row, 1000.1)
        second = core._process_started_at(row, 1000.9)
        self.assertEqual(first, second)

    def test_arm_samples_live_registries_outside_write_transaction(self):
        future_start = time.time() + 60

        def sensor(settings, **kwargs):
            with core._connect() as conn:
                core._meta_set(conn, "arm_sensor_probe", {"ok": True})
            return 1, 0, [
                {
                    "source": "process",
                    "id": "raced-process",
                    "command": "python worker.py",
                    "started_at": future_start,
                    "units": 1,
                }
            ]

        with mock.patch.object(core, "_background_snapshot", side_effect=sensor):
            armed = self.arm_ready({"require_user_idle": False})
        self.assertNotIn("process:raced-process", armed["runtime"]["background_baseline"])
        with core._connect() as conn:
            self.assertEqual(core._meta_get(conn, "arm_sensor_probe"), {"ok": True})

    def test_cancel_wins_against_inflight_arm(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_sensor(settings, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return 0, 0, []

        core.record_ui_heartbeat()
        errors = []

        def arm_worker():
            try:
                core.arm({"require_user_idle": False})
            except Exception as exc:  # expected cancellation result
                errors.append(exc)

        with mock.patch.object(core, "_background_snapshot", side_effect=slow_sensor):
            worker = threading.Thread(target=arm_worker)
            worker.start()
            self.assertTrue(entered.wait(5))
            cancelled = core.cancel("concurrent-user-cancel")
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertFalse(cancelled["settings"]["armed"])
        self.assertEqual(cancelled["runtime"]["state"], "disarmed")
        self.assertTrue(any(isinstance(exc, RuntimeError) for exc in errors))
        final = core.get_status()
        self.assertFalse(final["settings"]["armed"])
        self.assertEqual(final["runtime"]["state"], "disarmed")

    def test_settings_change_wins_against_inflight_arm(self):
        entered = threading.Event()
        release = threading.Event()

        def slow_sensor(settings, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return 0, 0, []

        core.record_ui_heartbeat()
        errors = []

        def arm_worker():
            try:
                core.arm({"require_user_idle": False})
            except Exception as exc:
                errors.append(exc)

        with mock.patch.object(core, "_background_snapshot", side_effect=slow_sensor):
            worker = threading.Thread(target=arm_worker)
            worker.start()
            self.assertTrue(entered.wait(5))
            updated = core.update_settings({"user_idle_seconds": 777})
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(updated["user_idle_seconds"], 777)
        self.assertTrue(any(isinstance(exc, RuntimeError) for exc in errors))
        final = core.get_status()
        self.assertFalse(final["settings"]["armed"])
        self.assertEqual(final["settings"]["user_idle_seconds"], 777)

    def test_turn_started_during_arm_sampling_joins_new_generation(self):
        entered = threading.Event()
        release = threading.Event()

        # Reuse the same turn identity so started_at remains historical under
        # the UPSERT; arm must use the boundary update, not only started_at.
        core.record_turn_start(
            session_id="boundary-session",
            task_id="boundary-task",
            turn_id="boundary-turn",
            platform_name="desktop",
        )
        core.finish_turn(
            session_id="boundary-session",
            task_id="boundary-task",
            turn_id="boundary-turn",
            completed=True,
            turn_exit_reason="text_response(stop)",
        )

        def slow_sensor(settings, **kwargs):
            entered.set()
            self.assertTrue(release.wait(5))
            return 0, 0, []

        core.record_ui_heartbeat()
        result = []
        with mock.patch.object(core, "_background_snapshot", side_effect=slow_sensor):
            worker = threading.Thread(
                target=lambda: result.append(core.arm({"require_user_idle": False}))
            )
            worker.start()
            self.assertTrue(entered.wait(5))
            core.record_turn_start(
                session_id="boundary-session",
                task_id="boundary-task",
                turn_id="boundary-turn",
                platform_name="desktop",
            )
            release.set()
            worker.join(5)
        self.assertFalse(worker.is_alive())
        status = result[0]
        self.assertTrue(status["settings"]["armed"])
        self.assertTrue(status["runtime"]["activity_seen"])
        self.assertEqual(status["runtime"]["state"], "running")
        with core._connect() as conn:
            task_generation = int(conn.execute("SELECT generation FROM tasks").fetchone()[0])
        self.assertEqual(task_generation, status["runtime"]["generation"])

    def test_other_desktop_profile_busy_state_blocks_countdown(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 30})
        self.terminal_task()
        core.record_ui_heartbeat(busy_count=2)
        core._evaluate()
        status = core.get_status()
        self.assertEqual(status["runtime"]["state"], "running")
        self.assertEqual(status["runtime"]["desktop_busy_count"], 2)

    def test_protected_process_scan_runs_outside_write_transaction(self):
        self.arm_ready({
            "require_user_idle": False,
            "protected_processes": ["obs64.exe"],
        })
        self.terminal_task()

        def scanner(names):
            with core._connect() as conn:
                core._meta_set(conn, "protected_scan_probe", {"ok": True})
            return ["obs64.exe"], True

        with mock.patch.object(core, "_protected_processes_running", side_effect=scanner):
            core._evaluate()
        with core._connect() as conn:
            self.assertEqual(core._meta_get(conn, "protected_scan_probe"), {"ok": True})

    def test_waiting_input_times_out_to_blocked(self):
        self.arm_ready({"require_user_idle": False, "waiting_input_timeout_seconds": 60, "quiescence_seconds": 0})
        core.record_turn_start(session_id="s", task_id="t", turn_id="u")
        core.record_tool_state(session_id="s", task_id="t", turn_id="u", tool_name="clarify", waiting=True)
        with core._connect() as conn:
            conn.execute("UPDATE tasks SET updated_at=?", (time.time() - 61,))
        core._evaluate()
        task = core.get_status()["tasks"][0]
        self.assertEqual(task["outcome"], "blocked")
        self.assertEqual(task["reason"], "waiting_for_user_timeout")

    def test_kanban_lifecycle(self):
        self.arm_ready({"require_user_idle": False})
        core.record_kanban("kb-1", "running", board="board-a", profile="worker")
        task = core.get_status()["tasks"][0]
        self.assertEqual(task["status"], "running")
        core.record_kanban("kb-1", "completed", reason="ok", board="board-a", profile="worker")
        task = core.get_status()["tasks"][0]
        self.assertEqual(task["outcome"], "completed")

    def test_test_countdown_never_requests_real_power(self):
        status = core.start_test_countdown(3)
        self.assertTrue(status["runtime"]["test_only"])
        self.make_due()
        claim = core._evaluate()
        self.assertIsNotNone(claim)
        self.assertTrue(claim["simulate"])
        with mock.patch.object(subprocess := __import__('subprocess'), "run", side_effect=AssertionError("must not run")):
            ok, message = core._execute_power_action("shutdown", simulate=True)
        self.assertTrue(ok)
        self.assertEqual(message, "SIMULATED shutdown")

    def test_simulation_test_is_rejected_while_real_campaign_is_armed(self):
        self.arm_ready({"require_user_idle": False})
        with self.assertRaises(RuntimeError):
            core.start_test_countdown(3)

    def test_actual_power_action_requires_desktop_managed_backend(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            ok, message = core._execute_power_action("shutdown", simulate=False)
        self.assertFalse(ok)
        self.assertIn("Desktop-managed", message)

    def test_desktop_backend_restart_always_disarms(self):
        self.arm_ready({"require_user_idle": False})
        with mock.patch.dict(os.environ, {"HERMES_DESKTOP": "1"}, clear=False):
            core.initialize_desktop_backend()
        status = core.get_status()
        self.assertFalse(status["settings"]["armed"])
        self.assertEqual(status["runtime"]["state"], "disarmed")
        self.assertIsNone(status["runtime"]["countdown_due"])

    def test_same_turn_id_isolated_by_session_and_profile(self):
        self.arm_ready({"require_user_idle": False})
        core.record_turn_start(session_id="s1", task_id="t", turn_id="same", profile="p1")
        core.record_turn_start(session_id="s2", task_id="t", turn_id="same", profile="p2")
        self.assertEqual(len(core.get_status()["tasks"]), 2)
        core.finish_turn(
            session_id="s1", task_id="t", turn_id="same", profile="p1",
            completed=True, turn_exit_reason="text_response(stop)",
        )
        states = {task["session_id"]: task["status"] for task in core.get_status()["tasks"]}
        self.assertEqual(states, {"s1": "completed", "s2": "running"})

    def test_late_approval_reopens_timed_out_wait(self):
        self.arm_ready({"require_user_idle": False, "waiting_input_timeout_seconds": 60})
        core.record_turn_start(session_id="s", task_id="t", turn_id="u")
        core.record_tool_state(session_id="s", task_id="t", turn_id="u", tool_name="clarify", waiting=True)
        with core._connect() as conn:
            conn.execute("UPDATE tasks SET updated_at=?", (time.time() - 61,))
        core._evaluate()
        self.assertEqual(core.get_status()["tasks"][0]["status"], "blocked")
        core.record_latest_owner_resumed("allow")
        task = core.get_status()["tasks"][0]
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["outcome"], "")
        self.assertIsNone(task["ended_at"])

    def test_reused_kanban_id_moves_to_current_generation(self):
        self.arm_ready({"require_user_idle": False})
        core.record_kanban("same", "completed", board="b", profile="p")
        first_generation = core.get_status()["runtime"]["generation"]
        core.cancel("next-generation")
        self.arm_ready({"require_user_idle": False})
        core.record_kanban("same", "running", board="b", profile="p")
        status = core.get_status()
        self.assertGreater(status["runtime"]["generation"], first_generation)
        self.assertEqual(len(status["tasks"]), 1)
        self.assertEqual(status["tasks"][0]["status"], "running")

    def test_background_sensor_failures_are_not_zero(self):
        real_import = __import__
        blocked = {"tools.process_registry", "tools.async_delegation", "cron.scheduler"}
        def guarded_import(name, *args, **kwargs):
            if name in blocked:
                raise ImportError(name)
            return real_import(name, *args, **kwargs)
        with mock.patch("builtins.__import__", side_effect=guarded_import):
            processes, delegations, details = core._background_snapshot(core.get_settings())
        self.assertGreaterEqual(processes, 2)
        self.assertGreaterEqual(delegations, 1)
        self.assertTrue(any(str(item.get("id", "")).startswith("sensor:") for item in details))

    def test_clock_or_resume_gap_restarts_full_confirmation(self):
        self.arm_ready({"require_user_idle": False, "dry_run": True})
        self.terminal_task()
        self.enter_countdown()
        core._reset_timing_after_clock_discontinuity("test-gap")
        runtime = core.get_status()["runtime"]
        self.assertEqual(runtime["state"], "armed_waiting_for_quiet")
        self.assertIsNone(runtime["countdown_due"])

    def test_multiple_ui_heartbeats_preserve_busy_peer(self):
        core.record_ui_heartbeat(busy_count=2, instance_id="window-a")
        core.record_ui_heartbeat(busy_count=0, instance_id="window-b")
        self.assertEqual(core.get_status()["runtime"]["desktop_busy_count"], 2)
        with core._connect() as conn:
            conn.execute(
                "UPDATE ui_heartbeats SET updated_at=? WHERE instance_id='window-a'",
                (time.time() - core.UI_HEARTBEAT_TIMEOUT_SECONDS - 1,),
            )
        core.record_ui_heartbeat(busy_count=0, instance_id="window-b")
        self.assertEqual(core.get_status()["runtime"]["desktop_busy_count"], 0)

    def test_user_input_cancels_countdown_even_without_idle_gate(self):
        self.arm_ready({"require_user_idle": False, "dry_run": True})
        self.terminal_task()
        with mock.patch.object(core, "_user_idle_seconds", return_value=100):
            self.enter_countdown()
        with mock.patch.object(core, "_user_idle_seconds", return_value=0):
            core._evaluate()
        runtime = core.get_status()["runtime"]
        self.assertEqual(runtime["state"], "armed_waiting_for_quiet")
        self.assertIsNone(runtime["countdown_due"])

    def test_exit_reason_requires_contract_boundary(self):
        self.assertFalse(core._reason_is_blocked("max_iterations_reached_but_recovered"))
        self.assertTrue(core._reason_is_blocked("max_iterations_reached(100/100)"))
        self.arm_ready({"require_user_idle": False})
        self.terminal_task(completed=True, reason="text_response_corrupt")
        self.assertEqual(core.get_status()["tasks"][0]["outcome"], "unknown")

    def test_non_desktop_process_cannot_claim_real_action(self):
        self.arm_ready({"require_user_idle": False, "dry_run": False})
        self.terminal_task()
        self.enter_countdown()
        self.make_due()
        with mock.patch.dict(
            os.environ,
            {"HERMES_DESKTOP": "0", "POWER_GUARD_TEST_MODE": "0"},
            clear=False,
        ):
            claim = core._evaluate()
        self.assertIsNone(claim)
        self.assertTrue(core.get_status()["settings"]["armed"])
        self.assertEqual(core.get_status()["runtime"]["state"], "waiting_for_desktop_backend")

    def test_pending_power_plan_recovery_restores_only_expected_plan(self):
        previous = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"
        desired = core.POWER_PLAN_GUIDS["balanced"]
        with core._connect() as conn:
            runtime = core._meta_get(conn, "runtime")
            runtime["power_plan_previous"] = previous
            runtime["power_plan_applied"] = "pending"
            core._meta_set(conn, "runtime", runtime)
        with mock.patch.object(core, "_current_power_plan", return_value=desired), mock.patch.object(
            core, "_set_power_plan", return_value=(True, previous)
        ) as setter:
            core._recover_pending_power_plan(previous, "balanced")
        setter.assert_called_once_with(previous)
        runtime = core.get_status()["runtime"]
        self.assertEqual(runtime["power_plan_applied"], "")
        self.assertEqual(runtime["power_plan_previous"], "")

    def test_power_plan_probe_runs_outside_write_transaction(self):
        settings = {**core.get_settings(), "power_plan": "balanced"}
        previous = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"

        def current_plan():
            with core._connect() as conn:
                core._meta_set(conn, "power_plan_probe", {"ok": True})
            return previous

        with mock.patch.object(core, "_current_power_plan", side_effect=current_plan), mock.patch.object(
            core, "_set_power_plan", return_value=(True, core.POWER_PLAN_GUIDS["balanced"])
        ):
            core._manage_global_power_plan_locked(settings, True)
        with core._connect() as conn:
            self.assertEqual(core._meta_get(conn, "power_plan_probe"), {"ok": True})

    def test_cancel_never_clears_execution_state_from_api_thread(self):
        self.arm_ready({"require_user_idle": False})
        with mock.patch.object(core, "_set_execution_state") as execution_state:
            core.cancel("thread-safety")
        execution_state.assert_not_called()

    def test_final_safety_check_uses_campaign_scoped_background_work(self):
        details = json.dumps([
            {
                "source": "process",
                "id": "preview",
                "command": "python server.py",
                "started_at": time.time() - 120,
                "units": 1,
            }
        ])
        with core._connect() as conn:
            conn.execute(
                "INSERT INTO heartbeats(owner_instance,owner_pid,updated_at,active_processes,active_delegations,process_details) "
                "VALUES('worker',999,?,1,0,?)",
                (time.time(), details),
            )
        self.arm_ready({"require_user_idle": False, "dry_run": False})
        self.terminal_task()
        token = "final-check-token"
        with core._connect() as conn:
            runtime = core._meta_get(conn, "runtime")
            runtime.update({
                "state": "executing",
                "countdown_token": token,
                "action_claimed_by": core.PROCESS_INSTANCE,
                "countdown_last_input_at": time.time() - 60,
                "ui_heartbeat_at": time.time(),
            })
            core._meta_set(conn, "runtime", runtime)
        claim = {"token": token, "action": "sleep", "simulate": False}
        with mock.patch.object(core.time, "sleep"), mock.patch.object(
            core, "_user_idle_seconds", return_value=600
        ), mock.patch.object(
            core, "_protected_processes_running", return_value=([], True)
        ), mock.patch.object(
            core, "_background_snapshot", return_value=(1, 0, json.loads(details))
        ):
            self.assertTrue(core._claim_still_safe(claim))

    def test_final_safety_check_resamples_live_background_work(self):
        armed = self.arm_ready({"require_user_idle": False, "dry_run": False})
        self.terminal_task()
        token = "late-background-token"
        with core._connect() as conn:
            runtime = core._meta_get(conn, "runtime")
            runtime.update({
                "state": "executing",
                "countdown_token": token,
                "action_claimed_by": core.PROCESS_INSTANCE,
                "countdown_last_input_at": time.time() - 60,
                "ui_heartbeat_at": time.time(),
            })
            core._meta_set(conn, "runtime", runtime)
        late_details = [
            {
                "source": "process",
                "id": "late-worker",
                "command": "python worker.py",
                "started_at": float(armed["runtime"]["armed_at"]) + 1,
                "units": 1,
            }
        ]
        claim = {"token": token, "action": "sleep", "simulate": False}
        with mock.patch.object(core.time, "sleep"), mock.patch.object(
            core, "_user_idle_seconds", return_value=600
        ), mock.patch.object(
            core, "_protected_processes_running", return_value=([], True)
        ), mock.patch.object(
            core, "_background_snapshot", return_value=(1, 0, late_details)
        ):
            self.assertFalse(core._claim_still_safe(claim))
        self.assertEqual(core.get_status()["runtime"]["state"], "disarmed")

    def test_preexisting_daemon_reaches_real_sleep_request_boundary(self):
        details = json.dumps([
            {
                "source": "process",
                "id": "preview",
                "command": "python server.py",
                "started_at": time.time() - 120,
                "units": 1,
            }
        ])
        with core._connect() as conn:
            conn.execute(
                "INSERT INTO heartbeats(owner_instance,owner_pid,updated_at,active_processes,active_delegations,process_details) "
                "VALUES('worker',999,?,1,0,?)",
                (time.time(), details),
            )
        self.arm_ready({"require_user_idle": False, "dry_run": False})
        self.terminal_task()
        self.assertIsNone(core._evaluate())
        with core._connect() as conn:
            runtime = core._meta_get(conn, "runtime")
            runtime["quiet_since"] = time.time() - 31
            core._meta_set(conn, "runtime", runtime)
        self.assertIsNone(core._evaluate())
        self.make_due()
        with mock.patch.dict(
            os.environ,
            {"HERMES_DESKTOP": "1", "POWER_GUARD_TEST_MODE": "0"},
            clear=False,
        ):
            claim = core._evaluate()
        self.assertIsNotNone(claim)
        self.assertFalse(claim["simulate"])
        with mock.patch.object(core.time, "sleep"), mock.patch.object(
            core, "_user_idle_seconds", return_value=600
        ), mock.patch.object(
            core, "_protected_processes_running", return_value=([], True)
        ), mock.patch.object(
            core, "_background_snapshot", return_value=(1, 0, json.loads(details))
        ):
            self.assertTrue(core._claim_still_safe(claim))
        with mock.patch.dict(os.environ, {"POWER_GUARD_TEST_MODE": "0"}, clear=False), mock.patch.object(
            core, "_background_snapshot", return_value=(1, 0, json.loads(details))
        ), mock.patch.object(
            core, "_protected_processes_running", return_value=([], True)
        ):
            self.assertTrue(core._claim_token_is_current(claim))
        with mock.patch.object(
            core, "_execute_power_action", return_value=(True, "Sleep requested")
        ) as execute:
            success, message = core._execute_power_action("sleep", False)
        execute.assert_called_once_with("sleep", False)
        core._finalize_action(claim, success, message)
        status = core.get_status()
        self.assertEqual(status["runtime"]["state"], "action_requested")
        self.assertEqual(status["runtime"]["last_action"]["message"], "Sleep requested")

    def test_final_claim_token_check_observes_cancellation(self):
        self.arm_ready({"require_user_idle": False, "dry_run": False})
        self.terminal_task()
        self.enter_countdown()
        self.make_due()
        with mock.patch.dict(os.environ, {"HERMES_DESKTOP": "1"}, clear=False):
            claim = core._evaluate()
        with mock.patch.object(
            core, "_background_snapshot", return_value=(0, 0, [])
        ), mock.patch.object(
            core, "_protected_processes_running", return_value=([], True)
        ):
            self.assertTrue(core._claim_token_is_current(claim))
            core.record_external_activity("late-cancel")
            self.assertFalse(core._claim_token_is_current(claim))

    def test_settings_change_after_final_safety_check_invalidates_claim(self):
        self.arm_ready({"require_user_idle": False, "dry_run": False})
        self.terminal_task()
        self.enter_countdown()
        self.make_due()
        with mock.patch.dict(os.environ, {"HERMES_DESKTOP": "1"}, clear=False):
            claim = core._evaluate()
        core.update_settings({"action": "lock", "dry_run": True})
        with mock.patch.object(
            core, "_background_snapshot", return_value=(0, 0, [])
        ), mock.patch.object(
            core, "_protected_processes_running", return_value=([], True)
        ):
            self.assertFalse(core._claim_token_is_current(claim))
        status = core.get_status()
        self.assertEqual(status["runtime"]["state"], "disarmed")
        self.assertEqual(status["settings"]["action"], "lock")
        self.assertTrue(status["settings"]["dry_run"])

    def test_background_start_after_final_safety_check_invalidates_claim(self):
        armed = self.arm_ready({"require_user_idle": False, "dry_run": False})
        self.terminal_task()
        self.enter_countdown()
        self.make_due()
        with mock.patch.dict(os.environ, {"HERMES_DESKTOP": "1"}, clear=False):
            claim = core._evaluate()
        late_details = [
            {
                "source": "process",
                "id": "micro-race-worker",
                "started_at": float(armed["runtime"]["armed_at"]) + 1,
                "units": 1,
            }
        ]
        with mock.patch.object(
            core, "_background_snapshot", return_value=(1, 0, late_details)
        ), mock.patch.object(
            core, "_protected_processes_running", return_value=([], True)
        ):
            self.assertFalse(core._claim_token_is_current(claim))
        self.assertEqual(core.get_status()["runtime"]["state"], "disarmed")

    def test_desktop_busy_after_final_safety_check_invalidates_claim(self):
        self.arm_ready({"require_user_idle": False, "dry_run": False})
        self.terminal_task()
        self.enter_countdown()
        self.make_due()
        with mock.patch.dict(os.environ, {"HERMES_DESKTOP": "1"}, clear=False):
            claim = core._evaluate()
        core.record_ui_heartbeat(busy_count=1, instance_id="late-busy-window")
        with mock.patch.object(
            core, "_background_snapshot", return_value=(0, 0, [])
        ), mock.patch.object(
            core, "_protected_processes_running", return_value=([], True)
        ):
            self.assertFalse(core._claim_token_is_current(claim))
        self.assertEqual(core.get_status()["runtime"]["state"], "disarmed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
