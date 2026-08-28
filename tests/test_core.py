from __future__ import annotations

import os
import sys
import tempfile
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
        self.assertEqual(status["decision"]["state"], "armed_waiting_for_task")
        self.assertEqual(status["decision"]["summary"], "等待新任务")
        self.assertEqual(len(status["decision"]["gates"]), 7)

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

    def test_other_desktop_profile_busy_state_blocks_countdown(self):
        self.arm_ready({"require_user_idle": False, "quiescence_seconds": 30})
        self.terminal_task()
        core.record_ui_heartbeat(busy_count=2)
        core._evaluate()
        status = core.get_status()
        self.assertEqual(status["runtime"]["state"], "running")
        self.assertEqual(status["runtime"]["desktop_busy_count"], 2)

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

    def test_cancel_never_clears_execution_state_from_api_thread(self):
        self.arm_ready({"require_user_idle": False})
        with mock.patch.object(core, "_set_execution_state") as execution_state:
            core.cancel("thread-safety")
        execution_state.assert_not_called()

    def test_final_claim_token_check_observes_cancellation(self):
        self.arm_ready({"require_user_idle": False, "dry_run": False})
        self.terminal_task()
        self.enter_countdown()
        self.make_due()
        with mock.patch.dict(os.environ, {"HERMES_DESKTOP": "1"}, clear=False):
            claim = core._evaluate()
        self.assertTrue(core._claim_token_is_current(claim))
        core.record_external_activity("late-cancel")
        self.assertFalse(core._claim_token_is_current(claim))


if __name__ == "__main__":
    unittest.main(verbosity=2)
